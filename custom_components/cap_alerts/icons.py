"""Event-type → Material Design Icon dispatch for alerts.

RFC §2.6: the integration populates ``icon``. A source classifies first, through
the ``icon`` hook on its convention row (``providers/<name>_conventions.py``),
and whatever it declines falls through to the shared hazard vocabulary below.
Unknown events fall back to ``mdi:alert``.
"""

from __future__ import annotations

from .conventions import conventions_for
from .model import CAPAlert

FALLBACK_ICON = "mdi:alert"

# The shared hazard vocabulary, every source's last resort before the fallback
# icon. Substrings rather than whole names, so variable naming ("severe
# thunderstorm warning", "tornado warning issued") still classifies.
COMMON_EVENT_SUBSTRINGS: tuple[tuple[str, str], ...] = (
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

# The EUMETNET hazard taxonomy, as substrings. A source whose vocabulary is
# international rather than North American — "high temperature", "heavy rain"
# and "gale" are here and absent from the common list — consults it from its
# own classifier, ahead of the common sweep. Needles that also sit in the
# common list must keep their specific-before-broad order here, because a
# source reads this list first.
INTERNATIONAL_EVENT_SUBSTRINGS: tuple[tuple[str, str], ...] = (
    ("avalanche", "mdi:snowflake-alert"),
    ("fire", "mdi:fire"),
    ("thunderstorm", "mdi:weather-lightning"),
    ("snow/ice", "mdi:snowflake"),
    # Must precede ``snow``: this list is read before the common one, so the
    # broad needle would otherwise shadow the common list's specific "snow
    # squall" mapping and a squall warning would draw the plain snow icon.
    ("snow squall", "mdi:snowflake-alert"),
    ("snow", "mdi:snowflake"),
    ("ice", "mdi:snowflake-melt"),
    ("frost", "mdi:snowflake-thermometer"),
    # Must precede ``rain``, for the same reason ``snow squall`` precedes
    # ``snow``: a WMO "Freezing Rain Warning" is ice, not a downpour.
    ("freezing rain", "mdi:snowflake-melt"),
    ("rain flood", "mdi:home-flood"),
    ("flood", "mdi:home-flood"),
    ("rain", "mdi:weather-pouring"),
    ("wind", "mdi:weather-windy"),
    ("gale", "mdi:weather-windy"),
    ("fog", "mdi:weather-fog"),
    ("extreme high temp", "mdi:weather-sunny-alert"),
    ("extreme low temp", "mdi:snowflake-thermometer"),
    ("high temperature", "mdi:weather-sunny-alert"),
    ("low temperature", "mdi:snowflake-thermometer"),
    # Must precede ``wave``: several services spell the hazard "Heat wave",
    # which otherwise matches the coastal needle and yields ``mdi:waves`` for
    # a temperature alert (observed live on a MeteoAlarm CH entry).
    ("heat", "mdi:weather-sunny-alert"),
    ("coastal event", "mdi:waves"),
    ("coastal", "mdi:waves"),
    ("wave", "mdi:waves"),
)


def is_english(tag: str) -> bool:
    """Whether a BCP 47 tag's primary subtag is English."""
    return tag.strip().lower().split("-", 1)[0] == "en"


def classification_event(alert: CAPAlert) -> str:
    """Return the event text to classify on, which may not be the displayed one.

    CAP 1.2 §3.2.1 makes ``<event>`` human-readable free text, so a feed
    presenting a localized block carries an event no keyword table can match —
    ``高温`` and ``Hitzewelle`` are the same hazard as ``high temperature`` and
    match nothing. Multilingual sources publish a second ``<info>`` block, and
    when it is English its event is classifiable while the user goes on reading
    their own language (issue #91).

    Guarded on the alternate *being* English rather than merely existing.
    Since #154 the providers prefer an English block as the alternate, so the
    guard is the backstop for documents with no English at all — a ``zh``/``pt``
    body yields a Portuguese alternate, no more matchable than the Chinese it
    would replace.

    This is deliberately provider-neutral. WMO surfaced it, but MeteoAlarm
    relays 38 mostly non-English services and publishes the same alternate
    block, so a German user hits the identical defect.
    """
    if (
        alert.event_alt
        and not is_english(alert.language)
        and is_english(alert.language_alt)
    ):
        return alert.event_alt
    return alert.event


def _normalize_event(event: str) -> str:
    """Fold the separators compound hazard names come with.

    Services emit hyphenated and underscored compounds (``high-temperature``,
    ``snow_ice``); folding both to spaces lets one substring needle match
    across naming styles. Harmless for vocabularies that carry no separators.
    """
    return event.replace("-", " ").replace("_", " ")


def match_event_substrings(text: str, table: tuple[tuple[str, str], ...]) -> str | None:
    """The first needle of ``table`` found in the separator-folded ``text``."""
    normalized = _normalize_event(text)
    for needle, icon in table:
        if needle in normalized:
            return icon
    return None


def icon_for(alert: CAPAlert) -> str:
    """Return an ``mdi:*`` icon for ``alert``: its source's classifier, then the shared sweep."""
    event = (classification_event(alert) or "").strip().lower()
    # The source gets first refusal, and gets it before the empty-event check:
    # a classifier that reads a coded parameter can answer with no event text
    # at all, while one that reads text declines an empty event itself.
    conventions = conventions_for(alert.provider, alert.sender)
    if conventions.icon is not None:
        icon = conventions.icon(alert, event)
        if icon is not None:
            return icon
    if not event:
        return FALLBACK_ICON
    return match_event_substrings(event, COMMON_EVENT_SUBSTRINGS) or FALLBACK_ICON
