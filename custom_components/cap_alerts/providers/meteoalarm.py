"""MeteoAlarm (EUMETNET) per-country JSON warnings feed provider.

Uses the aggregate JSON endpoint
(``feeds.meteoalarm.org/api/v1/warnings/feeds-{country-slug}``) which ships
proper CAP-1.2 ``info`` blocks (multi-language) and per-area geocodes
(EMMA_ID/WARNCELLID). The legacy Atom feed under the same domain is a flat
per-region summary without info blocks and is unsuitable.

GPS filtering is unavailable: MeteoAlarm warnings carry geocodes only, no
``polygon`` coordinates. When ``gps_loc`` is configured the provider logs
a warning and falls back to country-wide behavior.
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
    METEOALARM_COUNTRY_SLUGS,
)
from ..model import CAPAlert

_LOGGER = logging.getLogger(__name__)

METEOALARM_FEED_URL = "https://feeds.meteoalarm.org/api/v1/warnings/feeds-{country}"


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
        geometry=None,
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

        if config.get(CONF_GPS_LOC):
            _LOGGER.warning(
                "MeteoAlarm GPS filter is unavailable (warnings carry no "
                "polygons); falling back to country-wide for %s",
                country,
            )

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
        return alerts
