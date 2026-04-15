"""Event-type → Material Design Icon dispatch for alerts.

RFC §2.6: the integration populates `icon`. Taxonomy seeded from NWS
phenomena/event names and ECCC event-name strings. Unknown events fall
back to ``mdi:alert``.
"""

from __future__ import annotations

from .model import CAPAlert

FALLBACK_ICON = "mdi:alert"

# NWS event-name (CAP ``event``) → mdi. Keys are case-insensitive matched.
_NWS_EVENT_ICONS: dict[str, str] = {
    "tornado warning": "mdi:weather-tornado",
    "tornado watch": "mdi:weather-tornado",
    "severe thunderstorm warning": "mdi:weather-lightning",
    "severe thunderstorm watch": "mdi:weather-lightning",
    "flood warning": "mdi:home-flood",
    "flood watch": "mdi:home-flood",
    "flash flood warning": "mdi:water",
    "flash flood watch": "mdi:water",
    "coastal flood warning": "mdi:waves",
    "coastal flood watch": "mdi:waves",
    "winter storm warning": "mdi:snowflake-alert",
    "winter storm watch": "mdi:snowflake-alert",
    "winter weather advisory": "mdi:snowflake",
    "blizzard warning": "mdi:snowflake-alert",
    "ice storm warning": "mdi:snowflake-melt",
    "excessive heat warning": "mdi:weather-sunny-alert",
    "excessive heat watch": "mdi:weather-sunny-alert",
    "heat advisory": "mdi:weather-sunny-alert",
    "red flag warning": "mdi:fire",
    "fire weather watch": "mdi:fire",
    "high wind warning": "mdi:weather-windy",
    "high wind watch": "mdi:weather-windy",
    "wind advisory": "mdi:weather-windy",
    "dense fog advisory": "mdi:weather-fog",
    "air quality alert": "mdi:smog",
    "special weather statement": "mdi:alert-circle",
    "hurricane warning": "mdi:weather-hurricane",
    "hurricane watch": "mdi:weather-hurricane",
    "tropical storm warning": "mdi:weather-hurricane",
    "tropical storm watch": "mdi:weather-hurricane",
    "tsunami warning": "mdi:tsunami",
    "tsunami watch": "mdi:tsunami",
}

# ECCC event-name substrings → mdi. Matched after lowercasing ``event``.
# Substring match handles ECCC's variable naming (e.g. "severe thunderstorm
# warning", "tornado warning issued").
_ECCC_EVENT_SUBSTRINGS: tuple[tuple[str, str], ...] = (
    ("tornado", "mdi:weather-tornado"),
    ("thunderstorm", "mdi:weather-lightning"),
    ("blizzard", "mdi:snowflake-alert"),
    ("snowfall", "mdi:snowflake"),
    ("snow squall", "mdi:snowflake-alert"),
    ("winter storm", "mdi:snowflake-alert"),
    ("freezing rain", "mdi:snowflake-melt"),
    ("freezing drizzle", "mdi:snowflake-melt"),
    ("rainfall", "mdi:weather-pouring"),
    ("wind", "mdi:weather-windy"),
    ("heat", "mdi:weather-sunny-alert"),
    ("extreme cold", "mdi:snowflake-thermometer"),
    ("frost", "mdi:snowflake-thermometer"),
    ("fog", "mdi:weather-fog"),
    ("smog", "mdi:smog"),
    ("air quality", "mdi:smog"),
    ("hurricane", "mdi:weather-hurricane"),
    ("tropical storm", "mdi:weather-hurricane"),
    ("tsunami", "mdi:tsunami"),
    ("flood", "mdi:home-flood"),
)


def icon_for(alert: CAPAlert) -> str:
    """Return an ``mdi:*`` icon for ``alert`` based on provider + event."""
    event = (alert.event or "").strip().lower()
    if not event:
        return FALLBACK_ICON

    if alert.provider == "nws":
        if (icon := _NWS_EVENT_ICONS.get(event)) is not None:
            return icon

    for needle, icon in _ECCC_EVENT_SUBSTRINGS:
        if needle in event:
            return icon

    return FALLBACK_ICON
