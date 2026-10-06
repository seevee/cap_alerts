"""Australian state-feed conventions: the Australian Warning System tier.

One source's interpretive rules, registered into ``conventions`` at import.
"""

from __future__ import annotations

from collections.abc import Mapping

from ..conventions import SourceConventions, register
from ..model import CAPAlert

# --- icons ------------------------------------------------------------------

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


def au_icon(alert: CAPAlert, event: str) -> str | None:
    """Needles over the event text and ``IncidentType``; None lets the shared sweep run."""
    if not event:
        return None
    incident_type = (alert.parameters or {}).get("IncidentType", "")
    text = f"{event} {incident_type}".lower()
    for needle, icon in _AU_EVENT_SUBSTRINGS:
        if needle in text:
            return icon
    return None


# --- severity ---------------------------------------------------------------

# Australian Warning System tier → CAP canonical tier (issue #127). The three
# national tiers map onto the top three CAP tiers; the informational tiers
# below the ladder — QLD ``Information``, NSW ``Not Applicable`` (an incident
# with no warning attached, a grass fire under control) and ``Planned Burn``
# (a hazard reduction) — are ``minor`` rather than ``unknown``: they are the
# bottom of a ladder the agency publishes, not an absence of information.
# Keys are casefolded.
_AU_ALERT_LEVEL_SEVERITY = {
    "emergency warning": "extreme",
    "watch and act": "severe",
    "advice": "moderate",
    "information": "minor",
    "not applicable": "minor",
    "planned burn": "minor",
}

# The tier names as written, for headline recognition and the floor option.
# Longest first so "Emergency Warning" cannot be read as a bare "Warning".
AU_ALERT_LEVEL_LABELS: tuple[str, ...] = (
    "Emergency Warning",
    "Watch and Act",
    "Advice",
)

# The CAP parameter three of the four state feeds carry the tier in.
AU_ALERT_LEVEL_PARAMETER = "AlertLevel"


def au_alert_level(parameters: Mapping[str, str] | None, headline: str = "") -> str:
    """The Australian Warning System tier an alert publishes, or ``""``.

    Read from the ``AlertLevel`` CAP parameter where the feed writes one (NSW,
    QLD, TAS), otherwise recognised in the headline: WA publishes no such
    parameter and writes the tier as the headline prefix ("Bushfire Advice
    MONITOR CONDITIONS - LAKE ARGYLE"). The parameter value is returned as
    published, so the agencies' own sub-ladder tiers ("Planned Burn",
    "Information") come through verbatim; a headline match returns the
    canonical spelling. Public because the provider uses the same reading for
    the minimum-level option and for filling in WA's missing parameter.
    """
    raw = (parameters or {}).get(AU_ALERT_LEVEL_PARAMETER, "")
    if raw and str(raw).strip():
        return str(raw).strip()
    text = headline.casefold()
    for label in AU_ALERT_LEVEL_LABELS:
        if label.casefold() in text:
            return label
    return ""


def au_alert_level_severity(alert: CAPAlert) -> str | None:
    """Map the Australian Warning System tier to a canonical severity, or None.

    CAP ``<severity>`` is uniform or near-uniform on every state feed (QLD:
    68 ``Minor`` and 5 ``Moderate`` across two tiers of warning; NSW's
    planned burns say ``Unknown``), so the tier is where severity lives.
    Returns ``None`` for an alert with no recognisable tier — WA's "Facility
    Closure", for one — so the caller falls back to CAP ``severity``.
    """
    level = au_alert_level(alert.parameters, alert.headline)
    if not level:
        return None
    return _AU_ALERT_LEVEL_SEVERITY.get(level.casefold())


# Severity is the Australian Warning System tier, not CAP ``<severity>``. Each
# feed publishes one constant ISO 3166-2 geocode (``AU-NSW``), so prefix
# narrowing could only match everything or nothing and the field is withheld.
# Default absence policy on purpose, as for GDACS: the provider carries no
# ``expires`` (every feed's value is a regeneration TTL, see
# ``providers/au.py``), the feeds publish no terminal vocabulary and nothing
# fetches terminations, so an alert ends the moment its feed withdraws it —
# which is what withdrawal from a "current incidents" feed means (issue #127).
AU_CONVENTIONS = register(
    "au",
    SourceConventions(
        severity=au_alert_level_severity,
        icon=au_icon,
        publishes_geocodes=False,
    ),
)
