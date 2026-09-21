"""Event-type → Material Design Icon dispatch for alerts.

RFC §2.6: the integration populates `icon`. Taxonomy seeded from NWS
phenomena/event names and ECCC event-name strings. Unknown events fall
back to ``mdi:alert``.
"""

from __future__ import annotations

from .conventions import meteoalarm_awareness_type_code
from .model import CAPAlert

FALLBACK_ICON = "mdi:alert"

# MeteoAlarm ``awareness_type`` code → mdi (issue #97). The code is the
# EUMETNET hazard key: language-independent, REQUIRED on every MeteoAlarm
# alert, and therefore the only classifier that reaches the 30-odd non-English
# member services. Event text can't — a Finnish reader on the FMI feed gets
# ``Tuulivaroitus maa-alueille``, and the English-alternate path (#91) doesn't
# save it because FMI's alternate block is Swedish and some of its alerts carry
# no English block at all.
#
# Pinned to MeteoAlarm CAP Profile v2.0 §2.2.17 (September 2025), which is also
# where the gap at 11 comes from — the profile skips it. 14/15 arrived with the
# profile's marine and drought hazards; the rest were cross-checked against a
# live sweep of 33 member feeds.
_METEOALARM_AWARENESS_ICONS: dict[str, str] = {
    "1": "mdi:weather-windy",  # Wind
    "2": "mdi:snowflake",  # Snow or Ice
    "3": "mdi:weather-lightning",  # Thunderstorm
    "4": "mdi:weather-fog",  # Fog
    "5": "mdi:weather-sunny-alert",  # High Temperature
    "6": "mdi:snowflake-thermometer",  # Low Temperature
    "7": "mdi:waves",  # Coastal Event
    "8": "mdi:fire",  # Forest Fire
    "9": "mdi:snowflake-alert",  # Avalanche
    "10": "mdi:weather-pouring",  # Rain
    "12": "mdi:home-flood",  # Flood
    "13": "mdi:home-flood",  # Rain Flood
    "14": "mdi:waves",  # Marine Hazard
    "15": "mdi:water-off",  # Drought
}

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

# Australian state feeds (issue #127): needles over the event text plus the
# ``IncidentType`` parameter. The event is a short fixed vocabulary per agency
# ("Bushfire", "Grass Fire", "Fire", "Storm", "Facility Closure", "Other
# Non-Urgent Alerts"), and where it is generic — NSW's "Other" — the incident
# type says what it is ("Structure Fire", "Hazard Reduction", "Assist Other
# Agency"). The govshare ``eventCode`` would do the same job on three feeds,
# but WA's is malformed and the parser drops it, so text is the one classifier
# that reaches all four. Order matters where one needle sits inside another.
_AU_EVENT_SUBSTRINGS: tuple[tuple[str, str], ...] = (
    ("thunderstorm", "mdi:weather-lightning"),
    ("cyclone", "mdi:weather-hurricane"),
    ("tsunami", "mdi:tsunami"),
    ("flood", "mdi:home-flood"),
    ("storm", "mdi:weather-lightning"),
    ("heat", "mdi:weather-sunny-alert"),
    ("smoke", "mdi:smoke"),
    ("hazardous material", "mdi:biohazard"),
    ("hazmat", "mdi:biohazard"),
    ("chemical", "mdi:biohazard"),
    ("fire", "mdi:fire"),
    ("burn", "mdi:fire"),
    ("hazard reduction", "mdi:fire"),
    ("closure", "mdi:cancel"),
    ("rescue", "mdi:lifebuoy"),
    # NSW's road-crash incidents ("MVA/Transport", eventCode ``roadCrash``).
    ("crash", "mdi:car-emergency"),
    ("mva/", "mdi:car-emergency"),
    ("transport", "mdi:car-emergency"),
)

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

# MeteoAlarm event vocabulary is open across ~35 national services. Match on
# CAP-event substrings; the MeteoAlarm canonical set documents the keywords
# below as the EUMETNET hazard taxonomy.
_METEOALARM_EVENT_SUBSTRINGS: tuple[tuple[str, str], ...] = (
    ("avalanche", "mdi:snowflake-alert"),
    ("fire", "mdi:fire"),
    ("thunderstorm", "mdi:weather-lightning"),
    ("snow/ice", "mdi:snowflake"),
    # Must precede ``snow``: WMO consults this table before the ECCC list, so
    # the broad needle would otherwise shadow ECCC's specific "snow squall"
    # mapping below and a squall warning would draw the plain snow icon.
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


def _is_english(tag: str) -> bool:
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
        and not _is_english(alert.language)
        and _is_english(alert.language_alt)
    ):
        return alert.event_alt
    return alert.event


def _bbk_english_headline(alert: CAPAlert) -> str:
    """The English headline, from whichever block is English, or ``""``."""
    if _is_english(alert.language):
        return alert.headline
    if _is_english(alert.language_alt):
        return alert.headline_alt
    return ""


def _bbk_icon(alert: CAPAlert, event: str) -> str | None:
    """BBK classification: DWD hazard group first, then civil-protection needles.

    ``event`` is the already-lowercased classification event. The needle
    sweep also reads the English headline, because the civil-protection
    channels put the hazard there and leave ``event`` generic. Returns
    ``None`` to let the caller fall through to the international tables,
    which is where a DWD group this table does not know still classifies on
    the English event text.
    """
    group = (alert.parameters or {}).get("GROUP", "")
    if (icon := _BBK_DWD_GROUP_ICONS.get(str(group).strip().upper())) is not None:
        return icon
    text = f"{event} {_bbk_english_headline(alert)}".lower()
    for needle, icon in _BBK_EVENT_SUBSTRINGS:
        if needle in text:
            return icon
    return None


def _au_icon(alert: CAPAlert, event: str) -> str | None:
    """Australian classification over the event text and ``IncidentType``.

    ``event`` is the already-lowercased classification event. Returns
    ``None`` to let the caller fall through to the international tables.
    """
    incident_type = (alert.parameters or {}).get("IncidentType", "")
    text = f"{event} {incident_type}".lower()
    for needle, icon in _AU_EVENT_SUBSTRINGS:
        if needle in text:
            return icon
    return None


def icon_for(alert: CAPAlert) -> str:
    """Return an ``mdi:*`` icon for ``alert`` based on provider + event."""
    # MeteoAlarm carries a coded hazard, so classify on that before touching
    # free text — it beats the event tables in every language, including
    # English. Deliberately not applied to the other providers: WMO and ECCC
    # publish no ``awareness_type``, so they keep the English-alternate path.
    # Codes outside the table (a profile revision we haven't seen) fall
    # through to the event tables rather than to the fallback icon.
    if alert.provider == "meteoalarm":
        code = meteoalarm_awareness_type_code(alert.parameters)
        if (icon := _METEOALARM_AWARENESS_ICONS.get(code)) is not None:
            return icon

    event = (classification_event(alert) or "").strip().lower()
    if not event:
        return FALLBACK_ICON

    if alert.provider == "nws" and (icon := _NWS_EVENT_ICONS.get(event)) is not None:
        return icon

    if (
        alert.provider == "gdacs"
        and (icon := _GDACS_EVENT_ICONS.get(event)) is not None
    ):
        return icon

    if alert.provider == "bbk" and (icon := _bbk_icon(alert, event)) is not None:
        return icon

    if alert.provider == "au" and (icon := _au_icon(alert, event)) is not None:
        return icon

    # MeteoAlarm services emit hyphenated/underscored compound terms (e.g.
    # ``high-temperature``, ``snow_ice``); fold separators to spaces so
    # substring needles match across naming styles. Harmless for the other
    # providers, whose vocabularies carry no separators.
    normalized = event.replace("-", " ").replace("_", " ")

    # WMO relays ~140 national services, so its vocabulary is international
    # rather than Canadian — "high temperature", "heavy rain" and "gale" are
    # in the EUMETNET taxonomy and absent from ECCC's. Both consult it first,
    # then fall through rather than stopping: the early return here used to
    # hide ``tsunami``, ``tornado`` and ``smog`` from MeteoAlarm even though
    # ECCC's list below carries all three.
    if alert.provider in ("meteoalarm", "wmo", "bbk"):
        for needle, icon in _METEOALARM_EVENT_SUBSTRINGS:
            if needle in normalized:
                return icon

    for needle, icon in _ECCC_EVENT_SUBSTRINGS:
        if needle in normalized:
            return icon

    return FALLBACK_ICON
