"""NWS GeoJSON API provider — zone/GPS/tracker."""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Mapping
from typing import Any

import aiohttp

from homeassistant.exceptions import ConfigEntryError
from homeassistant.helpers.update_coordinator import UpdateFailed

from ..const import CONF_GPS_LOC, CONF_ZONE_ID
from ..model import CAPAlert

_LOGGER = logging.getLogger(__name__)

NWS_API_BASE = "https://api.weather.gov/alerts/active"
MAX_PAGINATION_FOLLOWS = 5

# VTEC regex: /P.ACTION.OFFICE.PP.S.NNNN.YYMMDDTHHMMZ-YYMMDDTHHMMZ/
_VTEC_RE = re.compile(
    r"/[A-Z]\.([A-Z]{3})\.([A-Z]{4})\.([A-Z]{2})\.([A-Z])\.(\d{4})"
    r"\.(\d{2})\d{4}T\d{4}Z-\d{6}T\d{4}Z/"
)


def _parse_vtec(vtec_str: str) -> dict[str, str]:
    """Parse a VTEC string into component fields."""
    m = _VTEC_RE.match(vtec_str)
    if not m:
        return {}
    return {
        "vtec_action": m.group(1),
        "vtec_office": m.group(2),
        "vtec_phenomena": m.group(3),
        "vtec_significance": m.group(4),
        "vtec_tracking": m.group(5),
        "_year": m.group(6),
    }


def _compute_alert_id(props: dict[str, Any]) -> str:
    """Compute lifecycle-aware alert ID.

    VTEC-bearing: hash the stable event identity tuple.
    Non-VTEC: hash the alert URL.
    """
    vtec_list = (props.get("parameters") or {}).get("VTEC", [])
    if vtec_list:
        parsed = _parse_vtec(vtec_list[0])
        if parsed:
            # Stable identity: office.phenomena.significance.tracking.year
            key = (
                f"{parsed['vtec_office']}.{parsed['vtec_phenomena']}."
                f"{parsed['vtec_significance']}.{parsed['vtec_tracking']}."
                f"20{parsed['_year']}"
            )
            return hashlib.sha256(key.encode()).hexdigest()[:12]

    # Fallback for non-VTEC alerts
    url = props.get("id", props.get("@id", ""))
    return hashlib.sha256(url.encode()).hexdigest()[:12]


def _extract_zone_codes(uris: list[str]) -> tuple[str, ...]:
    """Extract zone codes from NWS zone URIs."""
    codes = []
    for uri in uris:
        # URI like https://api.weather.gov/zones/county/OHC049
        code = uri.rsplit("/", 1)[-1].upper()
        if code:
            codes.append(code)
    return tuple(codes)


def _parse_feature(feature: dict[str, Any]) -> CAPAlert:
    """Parse a single GeoJSON feature into a CAPAlert."""
    props = feature.get("properties", {})

    # VTEC parsing
    vtec_list = (props.get("parameters") or {}).get("VTEC", [])
    vtec_fields: dict[str, str] = {}
    if vtec_list:
        vtec_fields = _parse_vtec(vtec_list[0])
        vtec_fields.pop("_year", None)

    # Zone URIs and codes
    zone_uris = props.get("affectedZones", [])
    zone_codes = _extract_zone_codes(zone_uris)

    # Geocodes
    geocode = props.get("geocode") or {}
    geocode_ugc = tuple(geocode.get("UGC", []))
    geocode_same = tuple(geocode.get("SAME", []))

    # Event codes
    event_codes = props.get("eventCode") or {}
    nws_codes = event_codes.get("NationalWeatherService", [])
    same_codes = event_codes.get("SAME", [])

    # Headline fallback
    headline = props.get("headline", "")
    if not headline:
        nws_headlines = (props.get("parameters") or {}).get("NWSheadline", [])
        if nws_headlines:
            headline = nws_headlines[0]

    # Geometry — from feature or parsed from properties
    geometry = feature.get("geometry")

    alert_id = _compute_alert_id(props)

    return CAPAlert(
        id=alert_id,
        url=props.get("id", ""),
        identifier=props.get("id", ""),
        event=props.get("event", ""),
        msg_type=props.get("messageType", ""),
        status=props.get("status", ""),
        scope=props.get("scope", ""),
        category=props.get("category", ""),
        urgency=props.get("urgency", ""),
        severity=props.get("severity", ""),
        certainty=props.get("certainty", ""),
        response_type=props.get("response", ""),
        sent=props.get("sent", ""),
        effective=props.get("effective", ""),
        onset=props.get("onset", ""),
        expires=props.get("expires", ""),
        ends=props.get("ends"),
        headline=headline,
        description=props.get("description", ""),
        instruction=props.get("instruction"),
        note=props.get("note", ""),
        web=props.get("web", ""),
        area_desc=props.get("areaDesc", ""),
        affected_zones=zone_codes,
        affected_zone_uris=tuple(zone_uris),
        geocode_ugc=geocode_ugc,
        geocode_same=geocode_same,
        geometry=geometry,
        event_code_nws=nws_codes[0] if nws_codes else "",
        event_code_same=same_codes[0] if same_codes else "",
        vtec=tuple(vtec_list),
        vtec_office=vtec_fields.get("vtec_office", ""),
        vtec_phenomena=vtec_fields.get("vtec_phenomena", ""),
        vtec_significance=vtec_fields.get("vtec_significance", ""),
        vtec_action=vtec_fields.get("vtec_action", ""),
        vtec_tracking=vtec_fields.get("vtec_tracking", ""),
        sender=props.get("sender", ""),
        sender_name=props.get("senderName", ""),
        references=tuple(props.get("references", []) or []),
        replaced_by=props.get("replacedBy", "") or "",
        replaced_at=props.get("replacedAt", "") or "",
        parameters=props.get("parameters"),
        provider="nws",
    )


class NWSProvider:
    """NWS GeoJSON API provider."""

    @property
    def name(self) -> str:
        return "nws"

    async def async_fetch(
        self,
        session: aiohttp.ClientSession,
        config: Mapping[str, Any],
        options: Mapping[str, Any],
    ) -> list[CAPAlert]:
        """Fetch active alerts from NWS."""
        url = self._build_url(config)
        if not url:
            return []

        alerts: list[CAPAlert] = []
        follows = 0

        while url and follows <= MAX_PAGINATION_FOLLOWS:
            data = await self._fetch_page(session, url)
            for feature in data.get("features", []):
                alerts.append(_parse_feature(feature))

            # Follow pagination
            pagination = data.get("pagination", {})
            url = pagination.get("next")
            follows += 1

        return alerts

    def _build_url(self, config: Mapping[str, Any]) -> str:
        """Build the NWS API URL from config."""
        if CONF_ZONE_ID in config and config[CONF_ZONE_ID]:
            zone_id = config[CONF_ZONE_ID]
            return f"{NWS_API_BASE}?zone={zone_id}"

        if CONF_GPS_LOC in config and config[CONF_GPS_LOC]:
            gps = config[CONF_GPS_LOC]
            # Round to 4 decimal places for CDN cache hits
            try:
                parts = gps.split(",")
                lat = round(float(parts[0].strip()), 4)
                lon = round(float(parts[1].strip()), 4)
                return f"{NWS_API_BASE}?point={lat},{lon}"
            except (ValueError, IndexError):
                return ""

        return ""

    async def _fetch_page(
        self, session: aiohttp.ClientSession, url: str
    ) -> dict[str, Any]:
        """Fetch a single page from NWS API."""
        headers = {"Accept": "application/geo+json"}
        async with session.get(url, headers=headers) as resp:
            if resp.status != 200:
                raise UpdateFailed(
                    f"NWS API returned {resp.status} for {url}"
                )
            data = await resp.json()

        # NWS sometimes returns error objects with 200 status
        if data.get("type") != "FeatureCollection":
            problem_type = data.get("type", "unknown")
            detail = data.get("detail", "")
            raise UpdateFailed(
                f"NWS API returned {problem_type}: {detail}"
            )

        return data
