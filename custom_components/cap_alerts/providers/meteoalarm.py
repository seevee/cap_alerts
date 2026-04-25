"""MeteoAlarm (EUMETNET) per-country CAP Atom feed provider."""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Mapping
from typing import Any
from xml.etree.ElementTree import Element

import aiohttp
from defusedxml import ElementTree as ET

from homeassistant.helpers.update_coordinator import UpdateFailed

from ..const import CONF_COUNTRY, CONF_GPS_LOC, CONF_LANGUAGE
from ..model import CAPAlert

_LOGGER = logging.getLogger(__name__)

METEOALARM_FEED_URL = (
    "https://feeds.meteoalarm.org/feeds/meteoalarm-legacy-atom-{country}"
)

NS_ATOM = "http://www.w3.org/2005/Atom"
NS_CAP = "urn:oasis:names:tc:emergency:cap:1.2"


def _compute_id(identifier: str, fallback_url: str) -> str:
    """Hash a CAP identifier (or fallback URL) to a 12-hex stable ID."""
    key = identifier or fallback_url
    return hashlib.sha256(key.encode()).hexdigest()[:12]


def _parse_polygon_text(text: str) -> list[list[float]] | None:
    """Parse a CAP/GeoRSS-style ``lat,lon lat,lon …`` polygon string.

    Returns a list of ``[lon, lat]`` coordinate pairs (GeoJSON order) or
    ``None`` if the polygon is malformed.
    """
    if not text:
        return None
    pairs = text.strip().split()
    if len(pairs) < 4:
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
    return coords


def _point_in_polygon(lat: float, lon: float, polygon: list[list[float]]) -> bool:
    """Ray-casting point-in-polygon test. Polygon is ``[[lon, lat], …]``."""
    n = len(polygon)
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = polygon[i][0], polygon[i][1]
        xj, yj = polygon[j][0], polygon[j][1]
        if ((yi > lat) != (yj > lat)) and (
            lon < (xj - xi) * (lat - yi) / (yj - yi) + xi
        ):
            inside = not inside
        j = i
    return inside


def _lang_prefix(value: str) -> str:
    """Lowercase 2-letter prefix of a BCP-47 code (``de-DE`` → ``de``)."""
    if not value:
        return ""
    return value.split("-", 1)[0].lower()


def _pick_info_blocks(
    infos: list[Element], preferred_prefix: str
) -> tuple[Element, Element | None]:
    """Pick the primary info block by language and an alternate if any.

    Preference order:
    1. info with a ``<cap:language>`` whose 2-letter prefix matches
       ``preferred_prefix``;
    2. info whose language prefix is ``en`` (generic fallback);
    3. first info block in document order.

    The alternate is the first remaining block, if any.
    """
    primary_idx: int | None = None
    en_idx: int | None = None
    for idx, info in enumerate(infos):
        lang = info.findtext(f"{{{NS_CAP}}}language", "")
        prefix = _lang_prefix(lang)
        if preferred_prefix and prefix == preferred_prefix and primary_idx is None:
            primary_idx = idx
        if prefix == "en" and en_idx is None:
            en_idx = idx

    if primary_idx is None:
        primary_idx = en_idx if en_idx is not None else 0

    primary = infos[primary_idx]
    alt: Element | None = None
    for idx, info in enumerate(infos):
        if idx == primary_idx:
            continue
        alt = info
        break
    return primary, alt


def _parse_parameters(info: Element) -> dict[str, str]:
    """Collect ``<cap:parameter>`` valueName/value pairs into a flat dict."""
    params: dict[str, str] = {}
    for param in info.findall(f"{{{NS_CAP}}}parameter"):
        name = param.findtext(f"{{{NS_CAP}}}valueName", "")
        value = param.findtext(f"{{{NS_CAP}}}value", "")
        if name:
            params[name] = value
    return params


def _info_text(info: Element, tag: str) -> str:
    return info.findtext(f"{{{NS_CAP}}}{tag}", "") or ""


def _first_area_polygon(info: Element) -> list[list[float]] | None:
    """Return the first ``<cap:polygon>`` text under any ``<cap:area>``."""
    for area in info.findall(f"{{{NS_CAP}}}area"):
        poly_text = area.findtext(f"{{{NS_CAP}}}polygon", "") or ""
        coords = _parse_polygon_text(poly_text)
        if coords:
            return coords
    return None


def _first_area_desc(info: Element) -> str:
    for area in info.findall(f"{{{NS_CAP}}}area"):
        desc = area.findtext(f"{{{NS_CAP}}}areaDesc", "") or ""
        if desc:
            return desc
    return ""


def _entry_to_alert(
    entry: Element,
    primary: Element,
    alt: Element | None,
) -> CAPAlert:
    """Build a CAPAlert from an entry's primary (and optional alt) info block."""
    identifier = entry.findtext(f"{{{NS_CAP}}}identifier", "") or ""
    sender = entry.findtext(f"{{{NS_CAP}}}sender", "") or ""
    sent = entry.findtext(f"{{{NS_CAP}}}sent", "") or ""
    status = entry.findtext(f"{{{NS_CAP}}}status", "") or ""
    msg_type = entry.findtext(f"{{{NS_CAP}}}msgType", "") or ""
    scope = entry.findtext(f"{{{NS_CAP}}}scope", "") or ""
    references = entry.findtext(f"{{{NS_CAP}}}references", "") or ""

    entry_url = entry.findtext(f"{{{NS_ATOM}}}id", "") or ""
    link_el = entry.find(f"{{{NS_ATOM}}}link")
    link = link_el.get("href", "") if link_el is not None else ""

    coords = _first_area_polygon(primary)
    geometry: dict | None = None
    if coords:
        # Close the ring if the feed didn't.
        if coords[0] != coords[-1]:
            coords = [*coords, coords[0]]
        geometry = {"type": "Polygon", "coordinates": [coords]}

    parameters = _parse_parameters(primary)

    references_tuple = tuple(r for r in references.split() if r) if references else ()

    return CAPAlert(
        id=_compute_id(identifier, entry_url),
        url=entry_url,
        identifier=identifier,
        event=_info_text(primary, "event"),
        msg_type=msg_type,
        status=status,
        scope=scope,
        category=_info_text(primary, "category"),
        urgency=_info_text(primary, "urgency"),
        severity=_info_text(primary, "severity"),
        certainty=_info_text(primary, "certainty"),
        response_type=_info_text(primary, "responseType"),
        sent=sent,
        effective=_info_text(primary, "effective"),
        onset=_info_text(primary, "onset"),
        expires=_info_text(primary, "expires"),
        headline=_info_text(primary, "headline"),
        description=_info_text(primary, "description"),
        instruction=_info_text(primary, "instruction") or None,
        web=_info_text(primary, "web") or link,
        area_desc=_first_area_desc(primary),
        geometry=geometry,
        sender=sender,
        sender_name=_info_text(primary, "senderName"),
        references=references_tuple,
        parameters=parameters or None,
        language=_info_text(primary, "language"),
        headline_alt=_info_text(alt, "headline") if alt is not None else "",
        description_alt=_info_text(alt, "description") if alt is not None else "",
        instruction_alt=(
            _info_text(alt, "instruction") or None if alt is not None else None
        ),
        language_alt=_info_text(alt, "language") if alt is not None else "",
        provider="meteoalarm",
    )


class MeteoAlarmProvider:
    """Per-country MeteoAlarm CAP Atom feed provider."""

    @property
    def name(self) -> str:
        return "meteoalarm"

    async def async_fetch(
        self,
        session: aiohttp.ClientSession,
        config: Mapping[str, Any],
        options: Mapping[str, Any],
    ) -> list[CAPAlert]:
        """Fetch the country feed and return ``CAPAlert`` per entry.

        Country mode: returns every active entry. GPS mode: filters by
        point-in-polygon against the entry's primary ``<cap:polygon>``;
        entries without polygons are excluded.
        """
        country = (config.get(CONF_COUNTRY, "") or "").lower()
        if not country:
            raise UpdateFailed("MeteoAlarm: country not configured")

        url = METEOALARM_FEED_URL.format(country=country)

        async with session.get(url) as resp:
            if resp.status != 200:
                raise UpdateFailed(f"MeteoAlarm {country}: HTTP {resp.status}")
            text = await resp.text()

        try:
            root = ET.fromstring(text)
        except ET.ParseError as err:
            raise UpdateFailed(f"MeteoAlarm: failed to parse feed: {err}") from err

        preferred_prefix = _lang_prefix(options.get(CONF_LANGUAGE, "")) or "en"
        gps_lat, gps_lon = self._parse_gps(config)

        alerts: list[CAPAlert] = []
        for entry in root.findall(f"{{{NS_ATOM}}}entry"):
            status = entry.findtext(f"{{{NS_CAP}}}status", "") or ""
            if status and status != "Actual":
                continue

            infos = entry.findall(f"{{{NS_CAP}}}info")
            if not infos:
                continue

            primary, alt = _pick_info_blocks(infos, preferred_prefix)
            alert = _entry_to_alert(entry, primary, alt)

            if gps_lat is not None and gps_lon is not None:
                coords = _first_area_polygon(primary)
                if coords is None or not _point_in_polygon(gps_lat, gps_lon, coords):
                    continue

            alerts.append(alert)

        return alerts

    @staticmethod
    def _parse_gps(
        config: Mapping[str, Any],
    ) -> tuple[float | None, float | None]:
        """Extract GPS coordinates from config; ``(None, None)`` in country mode."""
        gps_loc = config.get(CONF_GPS_LOC, "")
        if not gps_loc:
            return None, None
        try:
            parts = gps_loc.split(",")
            return float(parts[0].strip()), float(parts[1].strip())
        except (ValueError, IndexError):
            return None, None
