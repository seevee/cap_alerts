"""Environment Canada NAAD Atom feed provider."""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Mapping
from typing import Any

import aiohttp
from defusedxml import ElementTree as ET

from homeassistant.helpers.update_coordinator import UpdateFailed

from ..const import CONF_GPS_LOC, CONF_LANGUAGE, CONF_PROVINCE
from ..model import CAPAlert

_LOGGER = logging.getLogger(__name__)

NAAD_FEED_URL = "https://rss.naad-adna.pelmorex.com/"

# Atom namespace
NS_ATOM = "http://www.w3.org/2005/Atom"
NS_GEORSS = "http://www.georss.org/georss"


def _parse_categories(entry: ET.Element) -> dict[str, str]:
    """Extract category term key=value pairs from an Atom entry."""
    cats: dict[str, str] = {}
    for cat in entry.findall(f"{{{NS_ATOM}}}category"):
        term = cat.get("term", "")
        if "=" in term:
            key, _, val = term.partition("=")
            cats[key.strip()] = val.strip()
    return cats


def _parse_georss_polygon(entry: ET.Element) -> list[list[float]] | None:
    """Parse <georss:polygon> into a list of [lon, lat] coordinate pairs."""
    poly_el = entry.find(f"{{{NS_GEORSS}}}polygon")
    if poly_el is None or not poly_el.text:
        return None
    parts = poly_el.text.strip().split()
    if len(parts) < 6 or len(parts) % 2 != 0:
        return None
    coords = []
    for i in range(0, len(parts), 2):
        try:
            lat = float(parts[i])
            lon = float(parts[i + 1])
            coords.append([lon, lat])
        except ValueError:
            return None
    return coords


def _point_in_polygon(
    lat: float, lon: float, polygon: list[list[float]]
) -> bool:
    """Ray-casting point-in-polygon test. Polygon is [[lon, lat], ...]."""
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


def _compute_eccc_id(event: str, area_desc: str, sent: str) -> str:
    """Compute lifecycle-aware ID for ECCC alerts.

    Hash event + areaDesc + issued_date (date portion of sent).
    """
    # Extract date portion (YYYY-MM-DD) from ISO timestamp
    issued_date = sent[:10] if len(sent) >= 10 else sent
    key = f"{event}{area_desc}{issued_date}"
    return hashlib.sha256(key.encode()).hexdigest()[:12]


def _matches_province(area_desc: str, geocode: str, province: str) -> bool:
    """Check if an alert matches the configured province."""
    province_upper = province.upper()
    # Check geocode prefix (e.g. "ONxxx")
    if geocode and geocode[:2].upper() == province_upper:
        return True
    # Check area_desc contains province
    if province_upper in area_desc.upper():
        return True
    return False


def _entry_to_alert(
    entry: ET.Element, cats: dict[str, str]
) -> CAPAlert:
    """Convert an Atom entry + parsed categories to CAPAlert."""
    entry_id = entry.findtext(f"{{{NS_ATOM}}}id", "")
    updated = entry.findtext(f"{{{NS_ATOM}}}updated", "")
    summary = entry.findtext(f"{{{NS_ATOM}}}summary", "")
    link_el = entry.find(f"{{{NS_ATOM}}}link")
    link = link_el.get("href", "") if link_el is not None else ""

    event = cats.get("event", "")
    area_desc = cats.get("areaDesc", "")
    if not area_desc and summary:
        # Extract area from summary "Area: ..."
        if summary.startswith("Area:"):
            area_desc = summary[5:].strip()

    # Geometry
    geometry: dict | None = None
    coords = _parse_georss_polygon(entry)
    if coords:
        geometry = {"type": "Polygon", "coordinates": [coords]}

    alert_id = _compute_eccc_id(event, area_desc, updated)

    return CAPAlert(
        id=alert_id,
        url=entry_id,
        event=event,
        msg_type=cats.get("msgType", ""),
        status=cats.get("status", ""),
        severity=cats.get("severity", ""),
        urgency=cats.get("urgency", ""),
        certainty=cats.get("certainty", ""),
        sent=updated,
        expires=cats.get("expires", ""),
        area_desc=area_desc,
        geometry=geometry,
        web=link,
        provider="eccc",
    )


class ECCCProvider:
    """Environment Canada NAAD Atom feed provider."""

    @property
    def name(self) -> str:
        return "eccc"

    async def async_fetch(
        self,
        session: aiohttp.ClientSession,
        config: Mapping[str, Any],
        options: Mapping[str, Any],
    ) -> list[CAPAlert]:
        """Fetch active alerts from ECCC NAAD feed."""
        language = options.get(CONF_LANGUAGE, "en-CA")

        async with session.get(NAAD_FEED_URL) as resp:
            if resp.status != 200:
                raise UpdateFailed(
                    f"ECCC NAAD feed returned {resp.status}"
                )
            text = await resp.text()

        try:
            root = ET.fromstring(text)
        except ET.ParseError as err:
            raise UpdateFailed(f"ECCC: failed to parse Atom feed: {err}") from err

        alerts: list[CAPAlert] = []
        province = config.get(CONF_PROVINCE, "")
        gps_loc = config.get(CONF_GPS_LOC, "")
        gps_lat: float | None = None
        gps_lon: float | None = None
        if gps_loc:
            try:
                parts = gps_loc.split(",")
                gps_lat = float(parts[0].strip())
                gps_lon = float(parts[1].strip())
            except (ValueError, IndexError):
                pass

        for entry in root.findall(f"{{{NS_ATOM}}}entry"):
            cats = _parse_categories(entry)

            # Filter by language
            if cats.get("language", "") != language:
                continue

            # Filter by status
            if cats.get("status", "") != "Actual":
                continue

            # Filter out cancels
            if cats.get("msgType", "") == "Cancel":
                continue

            alert = _entry_to_alert(entry, cats)

            # Location filter
            if province:
                geocode = cats.get("geocode", "")
                if not _matches_province(alert.area_desc, geocode, province):
                    continue
            elif gps_lat is not None and gps_lon is not None:
                # Point-in-polygon test
                coords = _parse_georss_polygon(entry)
                if coords:
                    if not _point_in_polygon(gps_lat, gps_lon, coords):
                        continue
                else:
                    # No polygon — skip (can't determine location match)
                    continue
            else:
                # No location filter configured — return empty
                return []

            alerts.append(alert)

        return alerts
