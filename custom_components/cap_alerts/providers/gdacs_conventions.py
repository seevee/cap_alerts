"""GDACS conventions: an event-report source with no area geocodes.

One source's interpretive rules, registered into ``conventions`` at import.
"""

from __future__ import annotations

from ..conventions import SourceConventions, register
from ..model import CAPAlert

# GDACS event-name (CAP ``event``) → mdi. Keys are case-insensitive matched.
# GDACS emits one fixed English name per hazard type, so this is an exact
# lookup rather than a substring sweep — and the hazards themselves are why
# it can't lean on the tables below: an earthquake or a volcano is not
# weather, and no weather vocabulary carries a needle for either. Every name
# here was checked against the shipped MDI set.
_GDACS_EVENT_ICONS: dict[str, str] = {
    "earthquake": "mdi:pulse",
    "volcano": "mdi:volcano",
    "tropical cyclone": "mdi:weather-hurricane",
    "flood": "mdi:home-flood",
    "tsunami": "mdi:tsunami",
    "drought": "mdi:water-off",
    "wildfire": "mdi:fire",
}


def gdacs_icon(alert: CAPAlert, event: str) -> str | None:
    """Exact match on the fixed GDACS hazard name; None lets the shared sweep run."""
    return _GDACS_EVENT_ICONS.get(event)


# GDACS publishes no area geocodes at all (its identity travels in the RSS
# envelope instead), so the area-code narrowing option is withheld. Everything
# else is deliberately default — in particular no absence policy: GDACS alerts
# have no <expires> and no terminal vocabulary, so ``_retain_on_absence``
# already ends them the moment they leave the feed, which is the only
# end-of-life signal this source has. ``iscurrent`` is not that signal: items
# go false while the feed still lists them. Droughts did in the 2026-08-08
# sample, and on 2026-10-03 so did 27 wildfires and 8 floods, each about 100 h
# past its ``todate``. Every earthquake, cyclone and volcano observed stayed
# true right up to the poll it vanished on.
GDACS_CONVENTIONS = register(
    "gdacs", SourceConventions(publishes_geocodes=False, icon=gdacs_icon)
)
