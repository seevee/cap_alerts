"""GeoSphere Austria conventions: a coded hazard type, in either language.

One source's interpretive rules, exposed to the provider as ``CONVENTIONS``.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from ..conventions import SourceConventions
from ..model import CAPAlert

# ``warntypid`` → mdi, the same icons MeteoAlarm draws for the matching
# awareness types. Keyed on the code rather than the event name, so a German
# entry classifies exactly like an English one; no event table needed.
_GEOSPHERE_TYPE_ICONS: dict[str, str] = {
    "1": "mdi:weather-windy",  # Wind
    "2": "mdi:weather-pouring",  # Rain
    "3": "mdi:snowflake",  # Snow
    "4": "mdi:snowflake-melt",  # Black ice
    "5": "mdi:weather-lightning",  # Thunderstorm
    "6": "mdi:weather-sunny-alert",  # Heat
    "7": "mdi:snowflake-thermometer",  # Cold
}


def geosphere_icon(alert: CAPAlert, event: str) -> str | None:
    """The coded hazard; None (a type outside the enum) lets the shared sweep run."""
    code = str((alert.parameters or {}).get("warntypid", ""))
    return _GEOSPHERE_TYPE_ICONS.get(code)


# Everything else is default. Geocodes are published (the municipality's
# Gemeindekennziffer, whose leading digit is the Bundesland, so prefix
# narrowing means something for a tracker entry). Absence keeps the default
# retain policy: every warning carries an end time, and whether GeoSphere
# withdraws a cancelled warning or re-issues it with a shorter end is not yet
# observed (#300), which is the evidence ``ABSENCE_ENDS`` asks for.
GEOSPHERE_CONVENTIONS = SourceConventions(icon=geosphere_icon)


# What this provider declares, keyed as ``conventions_for`` resolves it.
CONVENTIONS: Mapping[str, SourceConventions] = MappingProxyType(
    {
        "geosphere": GEOSPHERE_CONVENTIONS,
    }
)
