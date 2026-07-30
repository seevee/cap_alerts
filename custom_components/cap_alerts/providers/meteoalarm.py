"""MeteoAlarm (EUMETNET) per-country JSON warnings feed provider.

Uses the aggregate JSON endpoint
(``feeds.meteoalarm.org/api/v1/warnings/feeds-{country-slug}``) which ships
proper CAP-1.2 ``info`` blocks (multi-language) and per-area geocodes. Feeds
carry a mix of area-geocode schemes across countries (``EMMA_ID``, ``NUTS3``,
``NUTS2``, ``WARNCELLID``, ``CISORP``); some countries carry two at once and
some none (polygon-only). Geocodes are collected into the scheme-keyed
``CAPAlert.geocodes`` container rather than a single named field.

Three filter modes selectable via config-flow:

* country-wide — all warnings for the configured country.
* gps-polygon — parses ``area.polygon`` from each warning and keeps only
  warnings whose polygon contains the configured point. Fails loud when a
  non-empty warnings page contains zero polygons (the country does not
  publish per-warning geometry).
* region-picker — keeps warnings whose region codes intersect the configured
  region selection. Region codes are resolved from ``geocodes`` by scheme
  priority (``METEOALARM_REGION_SCHEMES``) so a country's coarsest
  administrative scheme (e.g. ``EMMA_ID`` for DE, ``NUTS3`` for FR) is what
  both the picker offers and the filter matches.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any

import aiohttp

from homeassistant.helpers.update_coordinator import UpdateFailed

from ..const import (
    CONF_COUNTRY,
    CONF_COUNTRY_ENTITY,
    CONF_GPS_LOC,
    CONF_LANGUAGE,
    CONF_REGIONS,
    METEOALARM_COUNTRY_SLUGS,
)
from ..model import CAPAlert, geocodes_from

_LOGGER = logging.getLogger(__name__)

METEOALARM_FEED_URL = "https://feeds.meteoalarm.org/api/v1/warnings/feeds-{country}"
METEOALARM_REGIONS_URL = "https://feeds.meteoalarm.org/api/v1/regions/feeds-{country}"

# Region-selectable geocode schemes in priority order: EUMETNET canonical
# region id first, then NUTS3 (department/county) preferred over NUTS2 (region)
# when both are present. The first scheme present on an area is what the region
# picker offers and the region filter matches. Sub-region cell schemes
# (WARNCELLID, CISORP) always co-occur with one of these and are stored in
# ``geocodes`` but never offered in the picker. ``areaDesc`` is a last resort
# when a feed names areas but carries no region-selectable scheme.
METEOALARM_REGION_SCHEMES: tuple[str, ...] = ("EMMA_ID", "NUTS3", "NUTS2")

# MeteoFrance publishes via MeteoAlarm with a per-message CAP identifier that
# embeds an issue timestamp, so every re-issue of the same logical warning mints
# a fresh identifier (issue #37). Identity for this sender alone is derived from
# a content key (see ``_meteofrance_id``); every other authority keeps the
# per-message identifier hash, whose collisions there are genuinely-distinct
# concurrent warnings, not re-issues.
_MF_SENDER = "vigilance@meteo.fr"


def _awareness_type_code(parameters: Mapping[str, str] | None) -> str:
    """Language-independent phenomenon key: the leading token of the
    ``awareness_type`` parameter (``"3; Thunderstorm"`` → ``"3"``).

    Returns ``""`` when the parameter (or the whole mapping) is absent.
    """
    if not parameters:
        return ""
    raw = parameters.get("awareness_type") or ""
    return raw.split(";", 1)[0].strip()


def _forecast_window_key(onset: str, effective: str, sent: str) -> str:
    """Forecast-day key: the ``YYYY-MM-DD`` prefix of the first non-empty of
    ``onset``/``effective``/``sent``.

    MeteoFrance re-issues a given day's warning several times but keeps the
    ``onset`` date stable, so the date (not the full timestamp) merges re-issues
    while keeping the J/J+1/J+2/J+3 outlook days distinct. Returns ``""`` when
    all three are empty.
    """
    for value in (onset, effective, sent):
        if value:
            return value[:10]
    return ""


def _default_id(identifier: str, uuid: str) -> str:
    """Hash a CAP identifier (or ``uuid`` fallback) to a 12-hex stable ID.

    This is the identity for every authority except MeteoFrance.
    """
    key = identifier or uuid
    return hashlib.sha256(key.encode()).hexdigest()[:12]


def _meteofrance_id(
    sender: str,
    event_key: str,
    region_codes: Sequence[str],
    window_key: str,
    *,
    fallback: str,
) -> str:
    """Content-key identity for MeteoFrance vigilance.

    Keys on sender + phenomenon + forecast-region set + forecast day so a
    re-issue (fresh per-message identifier, same logical warning) keeps one
    stable id, while distinct phenomena, regions, and forecast days stay
    distinct entities. Severity/color is intentionally excluded so an
    orange→red escalation updates the existing entity rather than spawning a
    new one. Falls back to hashing ``fallback`` when every key component is
    empty (degenerate warning).
    """
    region_key = ";".join(sorted(region_codes))
    if not (sender or event_key or region_key or window_key):
        return hashlib.sha256(fallback.encode()).hexdigest()[:12]
    key = f"{sender}|{event_key}|{region_key}|{window_key}"
    return hashlib.sha256(key.encode()).hexdigest()[:12]


def _compute_alert_id(
    sender: str,
    identifier: str,
    uuid: str,
    event_key: str,
    region_codes: Sequence[str],
    window_key: str,
) -> str:
    """Dispatch identity by sender.

    MeteoFrance gets the re-issue-stable content key; every other authority
    keeps the per-message identifier hash (byte-for-byte unchanged from before
    issue #37's fix).
    """
    if sender == _MF_SENDER:
        return _meteofrance_id(
            sender, event_key, region_codes, window_key, fallback=identifier or uuid
        )
    return _default_id(identifier, uuid)


def _lang_prefix(value: str) -> str:
    """Lowercase 2-letter prefix of a BCP-47 code (``de-DE`` → ``de``)."""
    if not value:
        return ""
    return value.split("-", 1)[0].lower()


def _pick_info_blocks(
    infos: list[dict[str, Any]], preferred_prefix: str
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Pick the primary info block by language and an alternate if any.

    Preference order:
    1. info with a ``language`` whose 2-letter prefix matches
       ``preferred_prefix``;
    2. info whose language prefix is ``en`` (generic fallback);
    3. first info block in document order.

    The alternate is the first remaining block, if any.
    """
    primary_idx: int | None = None
    en_idx: int | None = None
    for idx, info in enumerate(infos):
        prefix = _lang_prefix(info.get("language", ""))
        if preferred_prefix and prefix == preferred_prefix and primary_idx is None:
            primary_idx = idx
        if prefix == "en" and en_idx is None:
            en_idx = idx

    if primary_idx is None:
        primary_idx = en_idx if en_idx is not None else 0

    primary = infos[primary_idx]
    alt: dict[str, Any] | None = None
    for idx, info in enumerate(infos):
        if idx == primary_idx:
            continue
        alt = info
        break
    return primary, alt


def _flatten_parameters(info: Mapping[str, Any]) -> dict[str, str]:
    """Collect ``parameter`` valueName/value pairs into a flat dict.

    When the same ``valueName`` repeats, values are joined with ``"; "``.
    """
    params: dict[str, str] = {}
    for entry in info.get("parameter") or []:
        name = entry.get("valueName") or ""
        value = entry.get("value") or ""
        if not name:
            continue
        existing = params.get(name)
        params[name] = f"{existing}; {value}" if existing else value
    return params


def _join_areas(info: Mapping[str, Any]) -> str:
    """Concatenate ``areaDesc`` from every area block in document order."""
    descs: list[str] = []
    for area in info.get("area") or []:
        desc = area.get("areaDesc") or ""
        if desc and desc not in descs:
            descs.append(desc)
    return ", ".join(descs)


def _scheme_geocodes(info: Mapping[str, Any]) -> Mapping[str, tuple[str, ...]]:
    """All area geocodes keyed by ``valueName`` (scheme).

    Collects every ``geocode`` across the info's area blocks into a
    scheme→values mapping, e.g. ``{"EMMA_ID": (...), "WARNCELLID": (...)}``.
    ``geocodes_from`` de-duplicates per scheme, order-preserving, and drops
    empty schemes/values. Areas without any geocode contribute nothing.
    """
    collected: dict[str, list[str]] = {}
    for area in info.get("area") or []:
        for code in area.get("geocode") or []:
            scheme = code.get("valueName") or ""
            collected.setdefault(scheme, []).append(code.get("value") or "")
    return geocodes_from(collected)


def _region_pairs(info: Mapping[str, Any]) -> list[tuple[str, str]]:
    """``(code, label)`` region-picker pairs for the info's areas.

    Per area, pick the first scheme present in ``METEOALARM_REGION_SCHEMES``
    with a non-empty value → ``(value, areaDesc or value)``. If no
    region-selectable scheme is present but ``areaDesc`` is set, fall back to
    ``(areaDesc, areaDesc)`` so named-but-schemeless feeds still populate the
    picker. Document order; de-duplicated by code (first label wins).
    """
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for area in info.get("area") or []:
        desc = area.get("areaDesc") or ""
        by_scheme: dict[str, str] = {}
        for code in area.get("geocode") or []:
            scheme = code.get("valueName") or ""
            value = code.get("value") or ""
            if scheme and value and scheme not in by_scheme:
                by_scheme[scheme] = value
        code_value = ""
        for scheme in METEOALARM_REGION_SCHEMES:
            if by_scheme.get(scheme):
                code_value = by_scheme[scheme]
                break
        if not code_value and desc:
            code_value = desc
        if not code_value or code_value in seen:
            continue
        seen.add(code_value)
        out.append((code_value, desc or code_value))
    return out


def _region_codes(
    geocodes: Mapping[str, tuple[str, ...]],
    area_descs: tuple[str, ...] = (),
) -> tuple[str, ...]:
    """Region codes for an alert, matching ``_region_pairs`` selection.

    Returns the values of the first scheme present in
    ``METEOALARM_REGION_SCHEMES``; if none is present, falls back to the
    alert's area descriptions (mirroring ``_region_pairs``' ``areaDesc``
    fallback) so picker values and filter keys stay in the same namespace for
    the same feed.
    """
    for scheme in METEOALARM_REGION_SCHEMES:
        values = geocodes.get(scheme)
        if values:
            return tuple(values)
    return tuple(area_descs)


def _first(value: Any) -> str:
    """Return the first element of a list-or-string value as a string.

    The JSON feed wraps several CAP fields (``category``, ``responseType``)
    in single-element lists; this normalizes them back to a scalar.
    """
    if isinstance(value, list):
        return str(value[0]) if value else ""
    return str(value or "")


def _info_text(info: Mapping[str, Any] | None, key: str) -> str:
    if info is None:
        return ""
    return str(info.get(key) or "")


def _parse_cap_polygon(text: str) -> list[list[float]] | None:
    """Parse a CAP ``polygon`` string into ``[[lon, lat], ...]``.

    CAP-1.2 polygon syntax is whitespace-separated ``lat,lon`` pairs.
    Returns ``None`` for empty input, malformed pairs, or rings with
    fewer than 3 distinct points.
    """
    if not text:
        return None
    pairs = text.strip().split()
    if len(pairs) < 3:
        return None
    coords: list[list[float]] = []
    for pair in pairs:
        if "," not in pair:
            return None
        lat_s, _, lon_s = pair.partition(",")
        try:
            lat = float(lat_s)
            lon = float(lon_s)
        except ValueError:
            return None
        coords.append([lon, lat])
    distinct = {(round(c[0], 6), round(c[1], 6)) for c in coords}
    if len(distinct) < 3:
        return None
    return coords


def _point_in_polygon(lat: float, lon: float, polygon: list[list[float]]) -> bool:
    """Ray-casting point-in-polygon test. Polygon is ``[[lon, lat], ...]``."""
    n = len(polygon)
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = polygon[i][0], polygon[i][1]  # lon, lat
        xj, yj = polygon[j][0], polygon[j][1]
        if ((yi > lat) != (yj > lat)) and (
            lon < (xj - xi) * (lat - yi) / (yj - yi) + xi
        ):
            inside = not inside
        j = i
    return inside


def _extract_geometries(info: Mapping[str, Any]) -> list[list[list[float]]]:
    """Return polygon rings from a CAP info block.

    One ring per area that carries a usable polygon, in GeoJSON
    ``[[lon, lat], ...]`` order. ``area.polygon`` is accepted as a string or a
    list of strings; unparseable entries are skipped. Region-picker pairs are
    derived separately by ``_region_pairs``.
    """
    rings: list[list[list[float]]] = []
    for area in info.get("area") or []:
        polygon = area.get("polygon")
        candidates: list[str]
        if isinstance(polygon, list):
            candidates = [p for p in polygon if isinstance(p, str)]
        elif isinstance(polygon, str):
            candidates = [polygon]
        else:
            candidates = []
        for text in candidates:
            ring = _parse_cap_polygon(text)
            if ring is not None:
                rings.append(ring)
    return rings


def _geometry_from_rings(
    rings: list[list[list[float]]],
) -> dict[str, Any] | None:
    """Build a GeoJSON geometry from one or more polygon rings.

    Single ring → ``Polygon``; multiple rings → ``MultiPolygon``; empty → ``None``.
    """
    if not rings:
        return None
    if len(rings) == 1:
        return {"type": "Polygon", "coordinates": [rings[0]]}
    return {"type": "MultiPolygon", "coordinates": [[ring] for ring in rings]}


def _warning_to_alert(
    warning: Mapping[str, Any], preferred_prefix: str
) -> CAPAlert | None:
    """Convert one ``{"alert": ..., "uuid": ...}`` warning to a ``CAPAlert``.

    Returns ``None`` for warnings filtered out (non-Actual status, missing
    info blocks).
    """
    alert = warning.get("alert") or {}
    status = alert.get("status") or ""
    if status and status != "Actual":
        return None

    infos = alert.get("info") or []
    if not infos:
        return None

    primary, alt = _pick_info_blocks(infos, preferred_prefix)
    identifier = alert.get("identifier") or ""
    uuid = warning.get("uuid") or ""
    parameters = _flatten_parameters(primary)
    geocodes = _scheme_geocodes(primary)
    rings = _extract_geometries(primary)
    geometry = _geometry_from_rings(rings)

    sender = alert.get("sender") or ""
    event = _info_text(primary, "event")
    onset = _info_text(primary, "onset")
    sent = alert.get("sent") or ""
    area_descs = tuple(d.strip() for d in _join_areas(primary).split(",") if d.strip())
    event_key = _awareness_type_code(parameters) or event.casefold()
    window_key = _forecast_window_key(onset, "", sent)
    region_codes = _region_codes(geocodes, area_descs)

    return CAPAlert(
        id=_compute_alert_id(
            sender, identifier, uuid, event_key, region_codes, window_key
        ),
        url="",
        identifier=identifier,
        event=event,
        msg_type=alert.get("msgType") or "",
        status=status,
        scope=alert.get("scope") or "",
        category=_first(primary.get("category")),
        urgency=_info_text(primary, "urgency"),
        severity=_info_text(primary, "severity"),
        certainty=_info_text(primary, "certainty"),
        response_type=_first(primary.get("responseType")),
        sent=sent,
        effective="",
        onset=onset,
        expires=_info_text(primary, "expires"),
        headline=_info_text(primary, "headline"),
        description=_info_text(primary, "description"),
        instruction=_info_text(primary, "instruction") or None,
        web=_info_text(primary, "web"),
        area_desc=_join_areas(primary),
        geocodes=geocodes,
        geometry=geometry,
        sender=sender,
        sender_name=_info_text(primary, "senderName"),
        parameters=parameters or None,
        language=_info_text(primary, "language"),
        headline_alt=_info_text(alt, "headline"),
        description_alt=_info_text(alt, "description"),
        instruction_alt=_info_text(alt, "instruction") or None,
        language_alt=_info_text(alt, "language"),
        provider="meteoalarm",
    )


def _parse_gps(value: str) -> tuple[float, float] | None:
    """Extract ``(lat, lon)`` from a ``"lat,lon"`` config string."""
    if not value:
        return None
    try:
        parts = value.split(",")
        return float(parts[0].strip()), float(parts[1].strip())
    except (ValueError, IndexError):
        return None


def _alert_polygons(alert: CAPAlert) -> list[list[list[float]]]:
    """Extract the polygon rings already stored on a CAPAlert geometry."""
    geom = alert.geometry
    if not geom:
        return []
    gtype = geom.get("type")
    coords = geom.get("coordinates")
    if not coords:
        return []
    if gtype == "Polygon":
        return [coords[0]] if coords else []
    if gtype == "MultiPolygon":
        return [poly[0] for poly in coords if poly]
    return []


async def fetch_regions_for_country(
    session: aiohttp.ClientSession, country_iso: str
) -> list[tuple[str, str]]:
    """Return ``[(region_code, label), ...]`` for the given country.

    ``region_code`` is in the country's region-selectable scheme (EMMA_ID for
    most, NUTS3 for FR/BG/RO/MK, NUTS2 for HU) — the same namespace the
    per-alert region filter matches against.

    Tries the regions endpoint first; on any failure (HTTP error, JSON
    error, empty response, unexpected shape) falls back to deriving the
    region list from the warnings feed. Raises ``UpdateFailed`` only when
    both paths fail.
    """
    country = (country_iso or "").upper()
    slug = METEOALARM_COUNTRY_SLUGS.get(country)
    if slug is None:
        raise UpdateFailed(f"MeteoAlarm: unsupported country {country}")

    regions = await _fetch_regions_endpoint(session, slug)
    if not regions:
        regions = await _fetch_regions_from_warnings(session, slug, country)
    if not regions:
        raise UpdateFailed(f"MeteoAlarm: failed to load regions for {country}")
    seen: dict[str, str] = {}
    for code, label in regions:
        if code and code not in seen:
            seen[code] = label or code
    return sorted(seen.items(), key=lambda item: item[1].lower())


async def _fetch_regions_endpoint(
    session: aiohttp.ClientSession, slug: str
) -> list[tuple[str, str]]:
    """Probe the regions endpoint. Returns ``[]`` on any failure."""
    url = METEOALARM_REGIONS_URL.format(country=slug)
    try:
        async with session.get(url) as resp:
            if resp.status != 200:
                return []
            try:
                payload = await resp.json(content_type=None)
            except (aiohttp.ContentTypeError, ValueError):
                return []
    except aiohttp.ClientError:
        return []

    entries: list[Any] = []
    if isinstance(payload, list):
        entries = payload
    elif isinstance(payload, dict):
        candidate = payload.get("regions")
        if isinstance(candidate, list):
            entries = candidate

    out: list[tuple[str, str]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        code = str(entry.get("code") or entry.get("EMMA_ID") or "").strip()
        label = str(entry.get("name") or entry.get("areaDesc") or "").strip()
        if code:
            out.append((code, label or code))
    return out


async def _fetch_regions_from_warnings(
    session: aiohttp.ClientSession, slug: str, country: str
) -> list[tuple[str, str]]:
    """Derive the region list from the warnings feed."""
    url = METEOALARM_FEED_URL.format(country=slug)
    try:
        async with session.get(url) as resp:
            if resp.status != 200:
                return []
            try:
                payload = await resp.json(content_type=None)
            except (aiohttp.ContentTypeError, ValueError):
                return []
    except aiohttp.ClientError:
        return []

    warnings = payload.get("warnings") if isinstance(payload, dict) else None
    if not isinstance(warnings, list):
        return []

    out: list[tuple[str, str]] = []
    for warning in warnings:
        if not isinstance(warning, dict):
            continue
        alert = warning.get("alert") or {}
        for info in alert.get("info") or []:
            out.extend(_region_pairs(info))
    return out


class MeteoAlarmProvider:
    """Per-country MeteoAlarm JSON warnings provider."""

    @property
    def name(self) -> str:
        return "meteoalarm"

    async def async_fetch(
        self,
        session: aiohttp.ClientSession,
        config: Mapping[str, Any],
        options: Mapping[str, Any],
        *,
        cap_content_cache=None,
        user_agent=None,
    ) -> list[CAPAlert]:
        """Fetch the country feed and return a ``CAPAlert`` per warning."""
        country = (config.get(CONF_COUNTRY, "") or "").upper()
        if not country:
            raise UpdateFailed("MeteoAlarm: country not configured")
        slug = METEOALARM_COUNTRY_SLUGS.get(country)
        if slug is None:
            raise UpdateFailed(f"MeteoAlarm: unsupported country {country}")

        url = METEOALARM_FEED_URL.format(country=slug)
        async with session.get(url) as resp:
            if resp.status != 200:
                raise UpdateFailed(f"MeteoAlarm {country}: HTTP {resp.status}")
            try:
                payload = await resp.json(content_type=None)
            except (aiohttp.ContentTypeError, ValueError) as err:
                raise UpdateFailed(f"MeteoAlarm: invalid JSON: {err}") from err

        warnings = payload.get("warnings") if isinstance(payload, dict) else None
        if not isinstance(warnings, list):
            raise UpdateFailed("MeteoAlarm: feed missing 'warnings' array")

        preferred_prefix = _lang_prefix(options.get(CONF_LANGUAGE, "")) or "en"

        alerts: list[CAPAlert] = []
        for warning in warnings:
            if not isinstance(warning, dict):
                continue
            alert = _warning_to_alert(warning, preferred_prefix)
            if alert is not None:
                alerts.append(alert)

        gps_loc = config.get(CONF_GPS_LOC)
        regions = config.get(CONF_REGIONS)

        if gps_loc:
            # Fully-mobile mode (country resolved from a source entity) can
            # roam into countries that publish partial or no per-warning
            # geometry; there, warnings without geometry are kept rather
            # than dropped. Explicit fixed-country GPS/tracker modes still
            # filter strictly and fail loud on zero polygons.
            mobile = CONF_COUNTRY_ENTITY in config
            return self._filter_by_polygon(
                alerts, gps_loc, country, keep_polygonless=mobile
            )
        if regions:
            return self._filter_by_regions(alerts, regions)
        return alerts

    @staticmethod
    def _filter_by_polygon(
        alerts: list[CAPAlert],
        gps_loc: str,
        country: str,
        *,
        keep_polygonless: bool = False,
    ) -> list[CAPAlert]:
        """Keep alerts whose geometry contains the configured GPS point.

        When the page has alerts but none carry polygons, the country does
        not publish per-warning geometry. By default this fails loud — the
        user explicitly chose GPS filtering for a known country — and
        warnings without geometry are dropped when others carry it. In
        fully-mobile mode (``keep_polygonless``) warnings without usable
        geometry are kept instead: roaming into a country with partial or
        absent geometry degrades to broader coverage rather than silently
        dropped warnings or an unavailable entry.
        """
        if not alerts:
            return []
        with_polygons = [a for a in alerts if _alert_polygons(a)]
        if not with_polygons:
            if keep_polygonless:
                _LOGGER.info(
                    "MeteoAlarm %s: no per-warning geometry; keeping all %d warnings",
                    country,
                    len(alerts),
                )
                return alerts
            raise UpdateFailed(
                f"MeteoAlarm {country}: GPS filter requested but "
                f"{len(alerts)} warnings carry no polygons; this country "
                "does not publish per-warning geometry — use region-picker "
                "mode instead"
            )
        gps = _parse_gps(gps_loc)
        if gps is None:
            raise UpdateFailed(
                f"MeteoAlarm {country}: invalid GPS coordinates {gps_loc!r}"
            )
        lat, lon = gps
        kept: list[CAPAlert] = []
        for alert in alerts:
            rings = _alert_polygons(alert)
            if not rings:
                if keep_polygonless:
                    kept.append(alert)
                continue
            if any(_point_in_polygon(lat, lon, ring) for ring in rings):
                kept.append(alert)
        return kept

    @staticmethod
    def _filter_by_regions(alerts: list[CAPAlert], regions: Any) -> list[CAPAlert]:
        """Keep alerts whose resolved region codes intersect ``regions``.

        Region codes are resolved from each alert's ``geocodes`` container via
        the shared scheme-priority resolver, so the values compared here are
        the same scheme the region picker offered (see ``_region_pairs``).

        For MeteoFrance (issue #37), the kept alert's id is recomputed against
        the *configured-region intersection* rather than the bulletin's full
        department set, so a fixed department keeps a stable id even when other
        departments enter or leave the bulletin between polls. Non-MeteoFrance
        alerts are kept unchanged.
        """
        wanted = {str(r) for r in regions if r}
        if not wanted:
            return []
        kept: list[CAPAlert] = []
        for a in alerts:
            descs = tuple(d.strip() for d in a.area_desc.split(",") if d.strip())
            resolved = _region_codes(a.geocodes, descs)
            if not wanted.intersection(resolved):
                continue
            if a.sender == _MF_SENDER:
                matched = sorted(wanted & set(resolved))
                event_key = _awareness_type_code(a.parameters) or a.event.casefold()
                window_key = _forecast_window_key(a.onset, a.effective, a.sent)
                a = replace(
                    a,
                    id=_meteofrance_id(
                        a.sender,
                        event_key,
                        matched,
                        window_key,
                        fallback=a.identifier or a.id,
                    ),
                )
            kept.append(a)
        return kept
