"""BBK / NINA conventions: no geocodes, a verbatim language tag.

One source's interpretive rules, exposed to the provider as ``CONVENTIONS``.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from ..conventions import SourceConventions
from ..icons import INTERNATIONAL_EVENT_SUBSTRINGS, is_english, match_event_substrings
from ..model import CAPAlert

# DWD ``GROUP`` eventCode → mdi, for the BBK provider's DWD channel (issue
# #66). The code is the DWD CAP profile's hazard group — language-independent
# and on every DWD document, where ``event`` is German prose ("SCHWERE
# STURMBÖEN") and even the English block says "storm-force gusts", which no
# weather table carries a needle for. Groups not listed fall through to the
# text tables rather than to the fallback icon.
_BBK_DWD_GROUP_ICONS: dict[str, str] = {
    "WIND": "mdi:weather-windy",
    "TORNADO": "mdi:weather-tornado",
    "THUNDERSTORM": "mdi:weather-lightning",
    "RAIN": "mdi:weather-pouring",
    "HAIL": "mdi:weather-hail",
    "SNOWFALL": "mdi:snowflake",
    "ICE": "mdi:snowflake-melt",
    "GLAZE": "mdi:snowflake-melt",
    "FROST": "mdi:snowflake-thermometer",
    "THAW": "mdi:snowflake-melt",
    "FOG": "mdi:weather-fog",
    "HEAT": "mdi:weather-sunny-alert",
    "UV": "mdi:weather-sunny-alert",
}

# BBK civil-protection needles → mdi, matched against the English event and
# headline. MoWaS / KATWARN / BIWAPP documents carry a generic ``event``
# ("Gefahreninformation" in every language) and put the hazard in the
# headline, which the API translates from a fixed catalogue ("Contaminated
# drinking water", "Fumes"), so the headline is the classifier here. None of
# these hazards is weather, so no weather table can carry them. Order matters
# where one needle is inside another (``gas leak`` before ``gas``).
_BBK_EVENT_SUBSTRINGS: tuple[tuple[str, str], ...] = (
    ("drinking water", "mdi:water-alert"),
    ("water supply", "mdi:water-off"),
    ("gas leak", "mdi:gas-cylinder"),
    ("gas ", "mdi:gas-cylinder"),
    ("fumes", "mdi:smoke"),
    ("smoke", "mdi:smoke"),
    ("fire", "mdi:fire"),
    ("conflagration", "mdi:fire"),
    ("explosive", "mdi:bomb"),
    ("ordnance", "mdi:bomb"),
    ("bomb", "mdi:bomb"),
    ("evacuat", "mdi:exit-run"),
    ("power outage", "mdi:flash-off"),
    ("power failure", "mdi:flash-off"),
    ("electricity", "mdi:flash-off"),
    ("hazardous", "mdi:biohazard"),
    ("chemical", "mdi:biohazard"),
    ("radioactiv", "mdi:radioactive"),
    ("radiation", "mdi:radioactive"),
    ("disease", "mdi:virus"),
    ("infection", "mdi:virus"),
    ("water level", "mdi:home-flood"),
    ("high water", "mdi:home-flood"),
    ("flood", "mdi:home-flood"),
    ("air pollution", "mdi:smog"),
    ("odour", "mdi:smog"),
    ("odor", "mdi:smog"),
    ("test warning", "mdi:bullhorn"),
    ("test alert", "mdi:bullhorn"),
    ("siren", "mdi:bullhorn"),
    ("all-clear", "mdi:check-circle"),
    ("all clear", "mdi:check-circle"),
)


def _english_headline(alert: CAPAlert) -> str:
    """The English headline, from whichever block is English, or ``""``."""
    if is_english(alert.language):
        return alert.headline
    if is_english(alert.language_alt):
        return alert.headline_alt
    return ""


def bbk_icon(alert: CAPAlert, event: str) -> str | None:
    """DWD hazard group first, then civil-protection needles, then the
    international vocabulary.

    ``event`` is the already-lowercased classification event; an empty one
    classifies nothing here, as for every text-reading source. The needle
    sweep also reads the English headline, because the civil-protection
    channels put the hazard there and leave ``event`` generic. Returns
    ``None`` to let the shared sweep run, which is where a DWD group this
    table does not know still classifies on the English event text.
    """
    if not event:
        return None
    group = (alert.parameters or {}).get("GROUP", "")
    if (icon := _BBK_DWD_GROUP_ICONS.get(str(group).strip().upper())) is not None:
        return icon
    text = f"{event} {_english_headline(alert)}".lower()
    for needle, icon in _BBK_EVENT_SUBSTRINGS:
        if needle in text:
            return icon
    return match_event_substrings(event, INTERNATIONAL_EVENT_SUBSTRINGS)


def resolve_language(language: str) -> str:
    """``auto`` passes the full tag through.

    Blocks are tagged ``de-DE`` / ``de`` / ``de-LS`` / ``en`` …; the shared
    matcher degrades to the primary subtag and then to English on its own.
    """
    return language.strip() or "en"


# ``area[]`` carries ``areaDesc`` only (both channels verified 2026-09-19; the
# occasional ``AreaId: 0`` is noise), so the area-code narrowing option is
# withheld as for GDACS. Default absence policy on purpose: DWD documents
# publish ``expires`` and are retained until it, while MoWaS documents publish
# none and have no terminal vocabulary, so they end the moment the index
# withdraws them — which is what withdrawal means on warnung.bund.de
# (issue #66).
BBK_CONVENTIONS = SourceConventions(
    publishes_geocodes=False, resolve_language=resolve_language, icon=bbk_icon
)


# What this provider declares, keyed as ``conventions_for`` resolves it.
CONVENTIONS: Mapping[str, SourceConventions] = MappingProxyType(
    {
        "bbk": BBK_CONVENTIONS,
    }
)
