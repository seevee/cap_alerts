"""MeteoAlarm (EUMETNET) per-country JSON warnings feed provider.

Uses the aggregate JSON endpoint
(``feeds.meteoalarm.org/api/v1/warnings/feeds-{country-slug}``) which ships
proper CAP-1.2 ``info`` blocks (multi-language) and per-area geocodes
(EMMA_ID/WARNCELLID).

Three filter modes selectable via config-flow:

* country-wide — all warnings for the configured country.
* gps-polygon — parses ``area.polygon`` from each warning and keeps only
  warnings whose polygon contains the configured point. Fails loud when a
  non-empty warnings page contains zero polygons (the country does not
  publish per-warning geometry).
* region-picker — keeps warnings whose ``EMMA_ID`` set intersects the
  configured region selection.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Mapping
from typing import Any

import aiohttp

from homeassistant.helpers.update_coordinator import UpdateFailed

from ..const import (
    CONF_COUNTRY,
    CONF_GPS_LOC,
    CONF_LANGUAGE,
    CONF_REGIONS,
    METEOALARM_COUNTRY_SLUGS,
)
from ..model import CAPAlert

_LOGGER = logging.getLogger(__name__)

METEOALARM_FEED_URL = "https://feeds.meteoalarm.org/api/v1/warnings/feeds-{country}"
METEOALARM_REGIONS_URL = "https://feeds.meteoalarm.org/api/v1/regions/feeds-{country}"


def _compute_id(identifier: str, fallback: str) -> str:
    """Hash a CAP identifier (or fallback) to a 12-hex stable ID."""
    key = identifier or fallback
    return hashlib.sha256(key.encode()).hexdigest()[:12]


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


def _emma_geocodes(info: Mapping[str, Any]) -> tuple[str, ...]:
    """All ``EMMA_ID`` geocode values across the info's area blocks."""
    out: list[str] = []
    for area in info.get("area") or []:
        for code in area.get("geocode") or []:
            if code.get("valueName") == "EMMA_ID":
                value = code.get("value") or ""
                if value and value not in out:
                    out.append(value)
    return tuple(out)


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


def _extract_geometries(
    info: Mapping[str, Any],
) -> tuple[list[list[list[float]]], list[tuple[str, str]]]:
    """Return ``(polygon_rings, area_pairs)`` from a CAP info block.

    ``polygon_rings`` is one ring per area that carries a usable polygon,
    in GeoJSON ``[[lon, lat], ...]`` order. ``area_pairs`` is the
    ``(EMMA_ID, areaDesc)`` for every area block, used to populate the
    region-picker fallback. ``area.polygon`` is accepted as a string or
    a list of strings; unparseable entries are skipped.
    """
    rings: list[list[list[float]]] = []
    pairs: list[tuple[str, str]] = []
    for area in info.get("area") or []:
        desc = area.get("areaDesc") or ""
        for code in area.get("geocode") or []:
            if code.get("valueName") == "EMMA_ID":
                value = code.get("value") or ""
                if value:
                    pairs.append((value, desc))
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
    return rings, pairs


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
    geocodes = _emma_geocodes(primary)
    rings, _pairs = _extract_geometries(primary)
    geometry = _geometry_from_rings(rings)

    return CAPAlert(
        id=_compute_id(identifier, uuid),
        url="",
        identifier=identifier,
        event=_info_text(primary, "event"),
        msg_type=alert.get("msgType") or "",
        status=status,
        scope=alert.get("scope") or "",
        category=_first(primary.get("category")),
        urgency=_info_text(primary, "urgency"),
        severity=_info_text(primary, "severity"),
        certainty=_info_text(primary, "certainty"),
        response_type=_first(primary.get("responseType")),
        sent=alert.get("sent") or "",
        effective="",
        onset=_info_text(primary, "onset"),
        expires=_info_text(primary, "expires"),
        headline=_info_text(primary, "headline"),
        description=_info_text(primary, "description"),
        instruction=_info_text(primary, "instruction") or None,
        web=_info_text(primary, "web"),
        area_desc=_join_areas(primary),
        geocode_same=geocodes,
        geometry=geometry,
        sender=alert.get("sender") or "",
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
    """Return ``[(EMMA_ID, label), ...]`` for the given country.

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
            _rings, pairs = _extract_geometries(info)
            out.extend(pairs)
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
            return self._filter_by_polygon(alerts, gps_loc, country)
        if regions:
            return self._filter_by_regions(alerts, regions)
        return alerts

    @staticmethod
    def _filter_by_polygon(
        alerts: list[CAPAlert], gps_loc: str, country: str
    ) -> list[CAPAlert]:
        """Keep alerts whose geometry contains the configured GPS point.

        Fails loud when the page has alerts but none carry polygons — that
        signals the country does not publish per-warning geometry.
        """
        if not alerts:
            return []
        with_polygons = [a for a in alerts if a.geometry]
        if not with_polygons:
            raise UpdateFailed(
                f"MeteoAlarm {country}: GPS filter requested but "
                f"{len(alerts)} warnings carry no polygons; this country "
                "does not publish per-warning geometry"
            )
        gps = _parse_gps(gps_loc)
        if gps is None:
            raise UpdateFailed(
                f"MeteoAlarm {country}: invalid GPS coordinates {gps_loc!r}"
            )
        lat, lon = gps
        kept: list[CAPAlert] = []
        for alert in alerts:
            for ring in _alert_polygons(alert):
                if _point_in_polygon(lat, lon, ring):
                    kept.append(alert)
                    break
        return kept

    @staticmethod
    def _filter_by_regions(alerts: list[CAPAlert], regions: Any) -> list[CAPAlert]:
        """Keep alerts whose ``geocode_same`` intersects ``regions``."""
        wanted = {str(r) for r in regions if r}
        if not wanted:
            return []
        return [a for a in alerts if wanted.intersection(a.geocode_same)]
