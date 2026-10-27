"""GeoSphere Austria provider: municipality warnings from the Warn API (#300).

GeoSphere (the former ZAMG) publishes the meteorologists' warnings for every
Austrian municipality. MeteoAlarm relays the same warnings at district level
with the texts compressed; this reads them one level finer, with the impacts,
recommendations and synopsis intact. The two identifier sets are disjoint, so
an entry here and a MeteoAlarm Austria entry show every warning twice.

The feed is GeoJSON, not CAP, so this is the third provider (after GDACS and
NWS) that builds ``CAPAlert`` without a CAP parser. One request per poll:
``getWarningsForCoords`` resolves the point to its municipality and returns
the municipality's MultiPolygon with every warning on it. Coordinates arrive
in EPSG:31287 metres and are reprojected by ``austria_lambert``.

**Identity is the warning id alone.** ``warnid`` is national and stable across
revisions, while ``chgid`` counts them. The municipality would add nothing (an
entity's unique id is already namespaced by entry) and would re-mint the
entity when a tracker crosses a boundary under the same warning.
``verlaufid`` ("sequential number of the warning's manifestation") stays out
of identity too until a live sample says what a new manifestation is; erring
that way keeps one entity rather than fragments.

**Times come from the epoch.** ``rawinfo.start``/``end`` are true UTC epoch
seconds (the spec's example: ``1679896800`` is 06:00Z, its ``begin`` of 08:00
CEST), whatever the spec's prose says about "+02:00". ``begin``/``end`` are
Vienna local time, ``dd.mm.yyyy HH:MM`` in the example and ISO in the schema,
and are read only when the epoch is missing.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from datetime import datetime, timezone, tzinfo
from typing import Any
from zoneinfo import ZoneInfo

import aiohttp

from ..const import (
    CONF_GPS_LOC,
    CONF_LANGUAGE,
    GEOSPHERE_LANGUAGES,
    GEOSPHERE_LEVEL_SEVERITY,
    GEOSPHERE_MAP_URL,
    GEOSPHERE_WARN_TYPES,
    GEOSPHERE_WARNINGS_URL,
)
from ..conventions import SourceConventions
from ..model import CAPAlert, geocodes_from
from . import ProviderError
from .austria_lambert import reproject_coordinates
from .cap_content_cache import CAPContentCache
from .geometry import normalize_ring
from .geosphere_conventions import CONVENTIONS as _CONVENTIONS
from .gps import parse_gps

_SENDER = "GeoSphere Austria"
_SENDER_NAME = "GeoSphere Austria (Warn API)"
_VIENNA = ZoneInfo("Europe/Vienna")
_LOCAL_FORMAT = "%d.%m.%Y %H:%M"
# Statistik Austria's Gemeindekennziffer, the scheme ``gemeindenr`` is in.
_GEOCODE_SCHEME = "GKZ"


def _compute_geosphere_id(warn_id: Any) -> str:
    """Hash ``"geosphere:{warnid}"`` to a 12-hex stable ID; see the module doc."""
    return hashlib.sha256(f"geosphere:{warn_id}".encode()).hexdigest()[:12]


def _point(gps_loc: str) -> tuple[float, float]:
    """``(lat, lon)`` rounded to four places, about 11 m.

    The API resolves a municipality, so more precision buys nothing, and it is
    the precision every request carries.
    """
    gps = parse_gps(gps_loc)
    if gps is None:
        raise ProviderError("GeoSphere: invalid GPS coordinates")
    return round(gps[0], 4), round(gps[1], 4)


def _request_url(lat: float, lon: float, lang: str) -> str:
    return f"{GEOSPHERE_WARNINGS_URL}?lon={lon}&lat={lat}&lang={lang}"


def _language(options: Mapping[str, Any]) -> str:
    lang = str(options.get(CONF_LANGUAGE) or "").strip().lower()
    return lang if lang in GEOSPHERE_LANGUAGES else GEOSPHERE_LANGUAGES[0]


def _is_error(body: Any) -> bool:
    """The API's error shape: ``type`` is ``Error`` live, ``error`` in the spec."""
    return isinstance(body, dict) and str(body.get("type", "")).lower() == "error"


def _epoch(value: Any) -> str:
    try:
        return datetime.fromtimestamp(int(value), timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


def _iso(value: Any, naive_tz: tzinfo) -> str:
    """An ISO string as UTC ISO, read in ``naive_tz`` when it has no offset."""
    if not isinstance(value, str) or not value.strip():
        return ""
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return ""
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=naive_tz)
    return parsed.astimezone(timezone.utc).isoformat()


def _local(value: Any) -> str:
    """A Vienna-local ``begin``/``end``, dotted or ISO, as UTC ISO."""
    try:
        parsed = datetime.strptime(str(value).strip(), _LOCAL_FORMAT).replace(
            tzinfo=_VIENNA
        )
    except ValueError:
        return _iso(value, _VIENNA)
    return parsed.astimezone(timezone.utc).isoformat()


def _int(*values: Any) -> int | None:
    """The first of ``values`` that reads as an int."""
    for value in values:
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _geometry(geometry: Any) -> tuple[dict[str, Any] | None, str]:
    """The municipality shape in WGS84, and its native bbox for the map link.

    The published nesting is kept, so an enclave's hole stays a hole; each
    ring goes through ``normalize_ring``, and a polygon whose outer ring fails
    it is dropped. Returns ``(None, "")`` for anything unusable.
    """
    if not isinstance(geometry, dict):
        return None, ""
    kind = geometry.get("type")
    coordinates = geometry.get("coordinates")
    polygons: Any
    if kind == "Polygon":
        polygons = [coordinates]
    elif kind == "MultiPolygon":
        polygons = coordinates
    else:
        return None, ""
    if not isinstance(polygons, list):
        return None, ""
    xs: list[float] = []
    ys: list[float] = []
    out: list[list[list[list[float]]]] = []
    try:
        for polygon in polygons:
            rings: list[list[list[float]]] = []
            for index, ring in enumerate(polygon):
                normalized = normalize_ring(reproject_coordinates(ring))
                if normalized is None:
                    if index == 0:
                        break
                    continue
                rings.append(normalized)
                xs.extend(float(pos[0]) for pos in ring)
                ys.extend(float(pos[1]) for pos in ring)
            if rings:
                out.append(rings)
    except (TypeError, ValueError, IndexError):
        return None, ""
    if not out:
        return None, ""
    bbox = f"{round(min(xs))},{round(min(ys))},{round(max(xs))},{round(max(ys))}"
    if len(out) == 1:
        return {"type": "Polygon", "coordinates": out[0]}, bbox
    return {"type": "MultiPolygon", "coordinates": out}, bbox


# The envelope fields passed through under their own names, when non-empty.
_PARAMETER_KEYS = (
    "warnid",
    "chgid",
    "verlaufid",
    "warntypid",
    "warnstufeid",
    "updategrund",
)


def _parameters(props: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: props[key] for key in _PARAMETER_KEYS if props.get(key) not in (None, "")
    }


def _build_alert(
    props: Mapping[str, Any],
    municipality: Mapping[str, Any],
    geometry: dict[str, Any] | None,
    web: str,
    lang: str,
) -> CAPAlert | None:
    """One ``warnings[]`` entry → ``CAPAlert``; None when it has no ``warnid``."""
    warn_id = props.get("warnid")
    if warn_id in (None, ""):
        return None
    raw = props.get("rawinfo") or {}
    if not isinstance(raw, dict):
        raw = {}
    warn_type = _int(props.get("warntypid"), raw.get("wtype"))
    level = _int(props.get("warnstufeid"), raw.get("wlevel"))
    chg_id = _int(props.get("chgid")) or 0
    onset = _epoch(raw.get("start")) or _local(props.get("begin"))
    impacts = _text(props.get("auswirkungen"))
    synopsis = _text(props.get("meteotext"))
    description = "\n\n".join(part for part in (impacts, synopsis) if part)
    labels = GEOSPHERE_WARN_TYPES.get(warn_type or 0) or {}
    event = labels.get(lang) or ("Warnung" if lang == "de" else "Warning")
    code = municipality.get("gemeindenr")
    return CAPAlert(
        id=_compute_geosphere_id(warn_id),
        url=web,
        # Not identity, see _compute_geosphere_id; it moves with the revision,
        # so a re-issue is visible.
        identifier=f"geosphere:{warn_id}:{chg_id}",
        event=event,
        msg_type="Update" if chg_id > 1 else "Alert",
        status="Actual",
        scope="Public",
        category="Met",
        severity=GEOSPHERE_LEVEL_SEVERITY.get(level or 0, ""),
        sent=_iso(props.get("create"), timezone.utc),
        effective=onset,
        onset=onset,
        expires=_epoch(raw.get("end")) or _local(props.get("end")),
        headline=_text(props.get("text")),
        description=description,
        instruction=_text(props.get("empfehlungen")) or None,
        web=web,
        area_desc=_text(municipality.get("name")),
        geocodes=geocodes_from(
            {_GEOCODE_SCHEME: [str(code)]} if code not in (None, "") else {}
        ),
        geometry=geometry,
        sender=_SENDER,
        sender_name=_SENDER_NAME,
        parameters=_parameters(props) or None,
        language=lang,
        provider="geosphere",
    )


def parse_warnings(body: Any, lang: str) -> list[CAPAlert]:
    """A ``getWarningsForCoords`` body → alerts; ``ProviderError`` on bad shape."""
    if _is_error(body):
        raise ProviderError(f"GeoSphere: API error: {body.get('msg', '')}")
    if not isinstance(body, dict):
        raise ProviderError("GeoSphere: response is not a GeoJSON Feature")
    props = body.get("properties")
    if not isinstance(props, dict) or not isinstance(props.get("warnings"), list):
        raise ProviderError("GeoSphere: response carries no warnings list")
    location = props.get("location") or {}
    municipality = (
        location.get("properties") if isinstance(location, dict) else None
    ) or {}
    warnings = props["warnings"]
    if not warnings:
        return []
    geometry, bbox = _geometry(body.get("geometry"))
    web = GEOSPHERE_MAP_URL.format(lang=lang, bbox=bbox) if bbox else ""
    alerts: list[CAPAlert] = []
    for warning in warnings:
        wprops = warning.get("properties") if isinstance(warning, dict) else None
        if not isinstance(wprops, dict):
            continue
        alert = _build_alert(wprops, municipality, geometry, web, lang)
        if alert is not None:
            alerts.append(alert)
    return alerts


class GeoSphereProvider:
    """GeoSphere Austria Warn API → CAPAlert, one municipality per entry."""

    @property
    def name(self) -> str:
        return "geosphere"

    @property
    def conventions(self) -> Mapping[str, SourceConventions]:
        return _CONVENTIONS

    async def async_validate_config(
        self,
        session: aiohttp.ClientSession,
        config: Mapping[str, Any],
        *,
        user_agent: str | None = None,
    ) -> str | None:
        """Check the point is in Austria, with the same request a poll makes.

        Outside Austria the endpoint answers 404 "Could not find municipal for
        coords.", the one answer that speaks to the input. Tracker mode stays
        unvalidated: a tracker is supposed to move.
        """
        try:
            lat, lon = _point(str(config.get(CONF_GPS_LOC, "") or ""))
        except ProviderError:
            # Shape is ``_validate_gps``'s job; a tracker has no point to ask about.
            return None
        headers = {"User-Agent": user_agent} if user_agent else None
        async with session.get(
            _request_url(lat, lon, GEOSPHERE_LANGUAGES[0]), headers=headers
        ) as resp:
            if resp.status == 404:
                return "geosphere_outside_austria"
            if resp.status != 200:
                return "cannot_connect"
        return None

    async def async_fetch(
        self,
        session: aiohttp.ClientSession,
        config: Mapping[str, Any],
        options: Mapping[str, Any],
        *,
        cap_content_cache: CAPContentCache | None = None,
        user_agent: str | None = None,
    ) -> list[CAPAlert]:
        """Fetch the warnings on the entry's municipality.

        A 404 means the point is outside Austria. A fixed point was checked at
        setup, so that is a tracker that left the country, and it raises: an
        empty list would read as all clear for a place this source does not
        cover. No message here carries the coordinates.
        """
        lat, lon = _point(str(config.get(CONF_GPS_LOC, "") or ""))
        lang = _language(options)
        headers = {"User-Agent": user_agent} if user_agent else None
        async with session.get(_request_url(lat, lon, lang), headers=headers) as resp:
            if resp.status == 404:
                raise ProviderError("GeoSphere: location is outside Austria")
            if resp.status != 200:
                raise ProviderError(f"GeoSphere: HTTP {resp.status}")
            try:
                body = await resp.json(content_type=None)
            except ValueError as err:
                raise ProviderError(f"GeoSphere: malformed JSON: {err}") from err
        return parse_warnings(body, lang)
