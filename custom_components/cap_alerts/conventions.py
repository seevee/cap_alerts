"""Per-source convention table — declarative interpretive rules (issue #82).

A *convention* is an encoding that is true for one alert source and meaningless
everywhere else: which area-code prefixes denote water, which lifecycle token
means "this is over", how a source spells severity. They are not CAP 1.2; they
are local habits layered on top of it.

The governing split is **parse faithfully, interpret separately**. Providers and
``providers/cap.py`` reproduce what a feed actually published, losing nothing;
this module holds the interpretation applied on top. That keeps the shared CAP
parser spec-pure — safe for every provider to reuse — and keeps ``normalize.py``
source-agnostic, since it consults the table rather than hard-coding one
source's vocabulary.

The pattern mirrors ``model.GEOCODE_SCHEME_ALIASES``: conventions are *data*, so
adding a source is a table entry rather than a new branch in shared code, and a
source that publishes no such signal simply omits the field instead of being
silently absent from an ``if`` somewhere.

Entries are keyed by source, **not** by provider. A single provider can carry
several dialects — MeteoAlarm relays every EUMETNET member, and MeteoFrance
alone needs its own identity, green-marker, and episode handling — so
``conventions_for()`` resolves ``provider/sender`` before falling back to
``provider``. No sender-scoped entry exists yet; the MeteoFrance rules still
live in ``providers/meteoalarm.py`` and migrating them is deliberate future
work (see issue #82's non-goals).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType

from .model import CAPAlert

# ---------------------------------------------------------------------------
# Source-specific vocabularies
# ---------------------------------------------------------------------------

# UGC area prefixes (first two chars of a zone code) that denote marine/water
# zones — coastal/offshore waters, Great Lakes, and high-seas areas. These are
# disjoint from the US state/territory postal codes used for land zones, so a
# prefix test never misclassifies a land alert. A newly minted marine-area code
# would need to be added here; until then such an alert classifies as land
# (fail-open — a marine alert is shown, never a non-marine alert hidden).
NWS_MARINE_UGC_PREFIXES: frozenset[str] = frozenset(
    {
        "AM",  # Western North Atlantic / Caribbean / Gulf offshore
        "AN",  # Atlantic coastal/offshore
        "GM",  # Gulf of Mexico
        "LC",  # Lake St. Clair
        "LE",  # Lake Erie
        "LH",  # Lake Huron
        "LM",  # Lake Michigan
        "LO",  # Lake Ontario
        "LS",  # Lake Superior
        "PH",  # Hawaiian coastal/offshore
        "PK",  # Alaskan coastal
        "PM",  # Western Pacific (Marianas)
        "PS",  # American Samoa
        "PZ",  # Pacific coastal/offshore
        "SL",  # St. Lawrence River
    }
)

# ECCC Canadian Location Codes are province-numbered for land zones; marine and
# water zones are the "00…" block.
ECCC_MARINE_CLC_PREFIX = "00"

# ECCC ``Alert_Location_Status`` tokens that mean the alert has reached
# end-of-life for the area it was selected for, whatever its ``msgType`` and
# ``expires`` still say. Unknown values are deliberately absent so an
# unfamiliar token degrades to msg_type handling rather than silently retiring
# a live alert.
ECCC_TERMINAL_LIFECYCLE_STATUSES: frozenset[str] = frozenset(
    {"ended", "transitioned_out"}
)

# VTEC significance → severity tier (NWS).
_VTEC_SIG_SEVERITY = {
    "W": "severe",  # Warning
    "A": "moderate",  # Watch
    "Y": "minor",  # Advisory
    "S": "unknown",  # Statement
}

# Phenomena codes that escalate a Warning to "extreme".
_VTEC_EXTREME_PHENOMENA = {"TO", "EW"}  # Tornado, Extreme Wind

# MeteoAlarm awareness color → CAP canonical tier. Green has no analogue on
# the canonical axis (no "none" tier), so it lands on the neutral "unknown"
# rather than the misleading "minor".
_METEOALARM_AWARENESS_TO_SEVERITY = {
    "green": "unknown",
    "yellow": "moderate",
    "orange": "severe",
    "red": "extreme",
}


# ---------------------------------------------------------------------------
# Severity derivations
# ---------------------------------------------------------------------------
#
# Each returns ``None`` when the source-specific signal is absent or
# unrecognized, which tells the caller to fall back to CAP ``severity``.


def nws_vtec_severity(alert: CAPAlert) -> str | None:
    """Derive severity from VTEC codes (authoritative for NWS)."""
    sig = alert.vtec_significance
    if not sig:
        return None
    # Tornado/Extreme Wind warnings are "extreme", not just "severe"
    if sig == "W" and alert.vtec_phenomena in _VTEC_EXTREME_PHENOMENA:
        return "extreme"
    return _VTEC_SIG_SEVERITY.get(sig, "unknown")


def meteoalarm_awareness_severity(alert: CAPAlert) -> str | None:
    """Map MeteoAlarm ``awareness_level`` to a canonical severity, or None.

    The parameter format published by EUMETNET members is ``"N; color; Label"``
    (e.g. ``"3; orange; Severe"``). The color token is the contract; the
    numeric prefix and trailing label are ignored. Returns ``None`` for
    missing, malformed, or unrecognized values so the caller falls back to
    CAP ``severity``.
    """
    if alert.parameters is None:
        return None
    raw = alert.parameters.get("awareness_level")
    if not raw:
        return None
    parts = raw.split(";")
    if len(parts) < 2:
        return None
    color = parts[1].strip().lower()
    return _METEOALARM_AWARENESS_TO_SEVERITY.get(color)


# ---------------------------------------------------------------------------
# The table
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SourceConventions:
    """Interpretive rules for one alert source. Every field is optional.

    An omitted field means "this source publishes no such signal", which is a
    declaration rather than an absence: ``classifies_marine`` is what the
    options flow asks before offering the exclude-marine toggle, instead of
    re-listing the supporting providers by hand.
    """

    # Area-code prefixes denoting marine/water zones. Empty when the source
    # publishes no marine discriminator.
    marine_code_prefixes: frozenset[str] = frozenset()
    # Provider-native ``lifecycle_status`` tokens meaning end-of-life.
    terminal_lifecycle_statuses: frozenset[str] = frozenset()
    # Source-specific severity derivation, or None to use CAP ``severity``.
    severity: Callable[[CAPAlert], str | None] | None = None

    @property
    def classifies_marine(self) -> bool:
        """True when this source can tell marine zones from land zones."""
        return bool(self.marine_code_prefixes)


CONVENTIONS: Mapping[str, SourceConventions] = MappingProxyType(
    {
        "nws": SourceConventions(
            marine_code_prefixes=NWS_MARINE_UGC_PREFIXES,
            severity=nws_vtec_severity,
        ),
        "eccc": SourceConventions(
            marine_code_prefixes=frozenset({ECCC_MARINE_CLC_PREFIX}),
            terminal_lifecycle_statuses=ECCC_TERMINAL_LIFECYCLE_STATUSES,
        ),
        "meteoalarm": SourceConventions(
            severity=meteoalarm_awareness_severity,
        ),
        "wmo": SourceConventions(),
    }
)

# Every field empty: a source with no registered conventions gets pure CAP
# handling rather than an error, so an unknown provider degrades gracefully.
_NO_CONVENTIONS = SourceConventions()


def conventions_for(provider: str, sender: str = "") -> SourceConventions:
    """Resolve conventions for a source, most specific key first.

    Tries ``"{provider}/{sender}"`` before ``provider`` so a single provider
    can host per-sender dialects, and falls back to an all-empty entry for
    sources the table does not know.
    """
    if sender:
        scoped = CONVENTIONS.get(f"{provider}/{sender}")
        if scoped is not None:
            return scoped
    return CONVENTIONS.get(provider, _NO_CONVENTIONS)


def is_marine_code(codes: Iterable[str], conventions: SourceConventions) -> bool:
    """True when any area code carries one of the source's marine prefixes.

    Sources whose prefixes are fixed-width (NWS's two-char UGC block) and those
    testing a leading run (ECCC's ``"00…"``) are the same test, so both go
    through this one predicate.
    """
    prefixes = conventions.marine_code_prefixes
    if not prefixes:
        return False
    return any(code.startswith(prefix) for code in codes for prefix in prefixes)
