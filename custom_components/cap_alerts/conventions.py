"""Per-source convention mechanism — declarative interpretive rules (issue #82).

A *convention* is an encoding that is true for one alert source and meaningless
everywhere else: which area-code prefixes denote water, which lifecycle token
means "this is over", how a source spells severity. They are not CAP 1.2; they
are local habits layered on top of it.

The governing split is **parse faithfully, interpret separately**. Providers and
``providers/cap.py`` reproduce what a feed actually published, losing nothing;
the convention rows hold the interpretation applied on top. That keeps the
shared CAP parser spec-pure — safe for every provider to reuse — and keeps
``normalize.py``, ``store.py`` and ``icons.py`` source-agnostic, since they
consult a row rather than hard-coding one source's vocabulary.

This module is the **mechanism** only: what a row may declare
(``SourceConventions``), the list-shaped stage contract, the shared identity
and timestamp helpers, and the registry rows resolve through. The rows
themselves, and every helper that reads one source's vocabulary, live beside
that source in ``providers/<name>_conventions.py`` and call ``register`` at
import. Importing the ``providers`` package registers every shipped source;
nothing here names one. That is what lets this half be hosted away from the
providers (issue #216): a new source is a module that registers a row, and
adding it touches nothing in the shared modules.

Rows are keyed by source, **not** by provider. A single provider can carry
several dialects — MeteoAlarm relays every EUMETNET member, and MeteoFrance
alone needs its own identity, green-marker, and episode handling — so
``conventions_for()`` resolves ``provider/sender`` before falling back to
``provider``. A sender-scoped row *replaces* the provider's rather than
layering on top of it, so it restates every rule it still wants.

Two hook shapes cover what a dialect can do. Most rules are per-alert
callables — ``severity``, ``identity``, ``keep``, ``icon`` — and stay pure
functions of one alert. The two that cannot be (splitting one message into
several, collapsing several into one) are list-shaped ``PipelineStage`` entries
bound to a named slot in the fetch. The provider owns the order and decides
where the slots sit; conventions declare stages, they never reorder the fetch:

    construct → [identity] → [explode] → [keep] → mode filters → [merge] → return

Every constraint in that order is load-bearing:

* **identity** first — it feeds entity ids, and everything downstream keys on
  them.
* **explode** before **keep** — markers are dropped per exploded region.
* **keep** before the provider's mode filters, so all three MeteoAlarm modes
  are equally protected.
* **merge** last, immediately before the fetch returns. It must precede the
  alert store, which keys incoming alerts by id and would silently drop one of
  any pair sharing the window-free id the merge produces.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Literal

from .model import CAPAlert

# ---------------------------------------------------------------------------
# List-shaped hooks
# ---------------------------------------------------------------------------


# ``(scheme, code, label)`` for one region an alert covers.
RegionEntry = tuple[str, str, str]


def _no_regions(alert: CAPAlert) -> tuple[RegionEntry, ...]:
    """Default ``regions_for``: the provider supplies no region entries."""
    return ()


@dataclass(frozen=True, slots=True)
class StageContext:
    """Everything a stage may read beyond the alerts themselves."""

    now: datetime
    # Empty outside the provider's region-picker mode.
    wanted_regions: frozenset[str] = frozenset()
    # The regions an alert covers, one ``(scheme, code, label)`` each, when the
    # provider can still supply them. A deliberate seam: ``CAPAlert`` flattens
    # every ``<area>`` into one comma-joined ``area_desc`` and one merged
    # geocode container, which destroys the name ↔ code pairing a region
    # explode depends on. The provider already resolves these entries for its
    # region picker, so routing them through here makes an exploded entity's
    # name the same string the user picked *by construction*, instead of
    # re-deriving the pairing rules a second time in this module.
    regions_for: Callable[[CAPAlert], tuple[RegionEntry, ...]] = _no_regions


@dataclass(frozen=True, slots=True)
class PipelineStage:
    """A list-shaped dialect stage, bound to a named point in the fetch."""

    slot: Literal["explode", "merge"]
    run: Callable[[list[CAPAlert], StageContext], list[CAPAlert]]


# ---------------------------------------------------------------------------
# Shared timestamp helpers
# ---------------------------------------------------------------------------
#
# Every row that orders or compares CAP timestamps goes through these rather
# than comparing raw strings: window edges within one episode can carry
# different UTC offsets across a DST boundary, and a feed may drop the offset
# entirely.


def parse_timestamp(value: str) -> datetime | None:
    """Parse an ISO-8601 timestamp, or ``None`` when absent or unparseable."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def parse_instant(value: str) -> datetime | None:
    """Parse a timestamp to a comparable aware instant, or ``None``.

    Window edges within one episode can carry different UTC offsets across a
    DST boundary, and a feed may drop the offset entirely, so every comparison
    in this module goes through here rather than comparing raw values. Naive
    timestamps are read as UTC.
    """
    parsed = parse_timestamp(value)
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def ts_sort_key(value: str) -> tuple[int, float, str]:
    """Total ordering over ISO timestamps: instant when parseable, else text.

    Unparseable values sort last but stay deterministic.
    """
    parsed = parse_instant(value)
    if parsed is None:
        return (1, 0.0, value)
    return (0, parsed.timestamp(), value)


# --- identity -------------------------------------------------------------


def episode_id(
    sender: str,
    event_key: str,
    region_codes: Sequence[str],
    window_key: str,
    *,
    fallback: str,
) -> str:
    """Content-key identity for an episode dialect's alerts.

    Keys on sender + phenomenon + region set + window so a re-issue (fresh
    per-message identifier, same logical warning) keeps one stable id, while
    distinct phenomena and regions stay distinct entities. Shipped ids are
    minted by the merge stage with an *empty* ``window_key`` so they survive
    the message split; the window component survives only as the collision
    tie-breaker for a second live run of one episode key. Severity/color is
    intentionally excluded so an orange→red escalation updates the existing
    entity rather than spawning a new one. Falls back to hashing ``fallback``
    when every key component is empty (degenerate warning).
    """
    region_key = ";".join(sorted(region_codes))
    if not (sender or event_key or region_key or window_key):
        return hashlib.sha256(fallback.encode()).hexdigest()[:12]
    key = f"{sender}|{event_key}|{region_key}|{window_key}"
    return hashlib.sha256(key.encode()).hexdigest()[:12]


# ---------------------------------------------------------------------------
# The table
# ---------------------------------------------------------------------------


# Shared empty mapping for sources that declare no lifecycle vocabulary. A
# frozen/slots dataclass rejects a mutable default, so the field uses a
# ``default_factory`` returning this singleton (as ``model.CAPAlert`` does for
# ``geocodes``).
_NO_REMOVAL_REASONS: Mapping[str, str] = MappingProxyType({})

# Values for ``SourceConventions.absence_policy``. See the field for why
# retaining is the default.
ABSENCE_RETAIN = "retain"
ABSENCE_ENDS = "ends"


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
    # Provider-native ``lifecycle_status`` tokens meaning end-of-life, mapped to
    # the neutral reason published as ``removal_reason`` on ``incident_removed``
    # (``const.REMOVAL_REASON_*``). The keys are the terminal set normalization
    # tests against, so a token cannot end an alert without saying why.
    lifecycle_removal_reasons: Mapping[str, str] = field(
        default_factory=lambda: _NO_REMOVAL_REASONS
    )
    # Successor CAP identifier for an ending this source's own vocabulary maps
    # to ``REMOVAL_REASON_SUPERSEDED``, or None for a source that supplies no
    # such extraction. Consulted only after ``removal_reason`` has already
    # resolved to "superseded" (issue #190); the hook's own ``None`` return
    # means "the parameter is absent or unparseable this time", not "this
    # source never has one" — a source can be wired here and still omit the
    # key on many individual endings.
    superseded_by: Callable[[CAPAlert], str | None] | None = None
    # Source-specific severity derivation, or None to use CAP ``severity``.
    severity: Callable[[CAPAlert], str | None] | None = None
    # Replacement entity id for a finished alert, or None to keep the
    # provider's default. Runs after construction, so it reads the alert.
    identity: Callable[[CAPAlert], str | None] | None = None
    # False for a record this source publishes that is not a warning at all.
    keep: Callable[[CAPAlert], bool] | None = None
    # List-shaped stages, each bound to a named slot in the provider's fetch.
    stages: tuple[PipelineStage, ...] = ()
    # Icon classifier for this source, given the alert and its lowercased
    # classification event (``icons.classification_event``), which may be
    # empty. Returns an ``mdi:`` icon, or None to fall through to the shared
    # hazard vocabulary in ``icons.py``. Consulted *before* the empty-event
    # check there, so a source that classifies on a coded parameter can
    # answer without any event text; a source whose classifier reads text
    # returns None for an empty event itself.
    icon: Callable[[CAPAlert, str], str | None] | None = None
    # What ``language: auto`` means for this source: Home Assistant's UI
    # language in, the tag the provider selects ``<info>`` blocks on out.
    # Read on the provider row only — resolution runs before any sender is
    # known — and None for a source that takes no language, which leaves
    # ``auto`` in the options untouched.
    resolve_language: Callable[[str], str] | None = None
    # Maps a country-source entity's value — a code or a name, however a
    # geocoder spells it — to the country code this source is queried by, or
    # None for an unrecognised value. Provider row only, as above; None for a
    # source with no country scope, which is every shipped one but MeteoAlarm.
    country_code: Callable[[Any], str | None] | None = None
    # What an alert's absence from a reconciliation means for this source.
    # ``ABSENCE_RETAIN`` (the default) says absence is an observation failure
    # until proven otherwise: the store keeps the alert, marks it stale, and
    # waits for its ``expires`` or an explicit terminal signal. ``ABSENCE_ENDS``
    # says the feed publishes only live records and withdrawing one is how this
    # source announces the end, so absence terminates immediately — with or
    # without an ``expires`` on the record. The policy is the *only* thing that
    # makes absence authoritative: an alert that merely omits ``expires`` under
    # ``ABSENCE_RETAIN`` is retained indefinitely (visibly stale) until an
    # explicit terminal signal, because a missing field on one message is not a
    # statement about what withdrawal means for the source.
    #
    # Retaining is the safe default because a feed gap is indistinguishable
    # from a cancellation at the moment of observation, and the two errors are
    # not symmetric: retaining a finished alert shows a stale warning until its
    # published expiry, while dropping a live one silently clears a hazard from
    # the user's dashboard and then re-creates it as a *new* incident when the
    # feed recovers, fragmenting its history (RFC §1.2, §1.4 item 8). Declare
    # ``ABSENCE_ENDS`` only on positive evidence of the source's contract —
    # a feed documented (or observed) to withdraw records as its end-of-life
    # announcement. No shipped source meets that bar today: NWS, ECCC, and
    # MeteoAlarm publish expiries and terminal signals, and WMO's RSS sources
    # are exactly the lossy feeds retention exists to protect, so declaring it
    # there on speculation would forfeit the protection. The cost of that
    # caution is that a WMO alert with no ``expires`` in its CAP body can stay
    # stale until its source re-publishes or a human removes the entry — an
    # accepted trade against silently clearing a live hazard.
    absence_policy: str = ABSENCE_RETAIN
    # False for a source that never publishes CAP ``<geocode>`` at all, which
    # is what the options flow asks before offering the area-code narrowing
    # field — on such a source the coordinator's geocode filter can only ever
    # trip its fail-loud path and leave the entry permanently unavailable.
    # Unlike the other fields the default here is positive, because geocodes
    # are the CAP norm rather than a per-source signal: every shipped source
    # except GDACS publishes them, so the flag marks the exception.
    publishes_geocodes: bool = True
    # True when the provider actively fetches terminations the active feed
    # omits, rather than waiting for one to arrive. NWS is the case: it
    # publishes cancellations as VTEC ``CAN`` products but never on
    # ``/alerts/active``, so the provider queries them separately.
    #
    # Read by ``store._retain_on_absence`` as one of the three ways an alert
    # can eventually be ended. It matters only for alerts with no ``expires``,
    # where time cannot end them: retaining one is safe if a termination will
    # be fetched, and unsafe if nothing will ever arrive. A source with no
    # expiry, no terminal vocabulary and no lookup has no exit at all, and
    # retaining its alerts would leave entities that outlive the hazard
    # indefinitely — measured on WMO, where two authorities publish no
    # ``<expires>`` on any alert.
    discovers_terminations: bool = False

    @property
    def classifies_marine(self) -> bool:
        """True when this source can tell marine zones from land zones."""
        return bool(self.marine_code_prefixes)

    def stages_at(
        self, slot: str
    ) -> tuple[Callable[[list[CAPAlert], StageContext], list[CAPAlert]], ...]:
        """The stage callables registered at ``slot``, in declaration order."""
        return tuple(stage.run for stage in self.stages if stage.slot == slot)


# Every field empty: a source with no registered conventions gets pure CAP
# handling rather than an error, so an unknown provider degrades gracefully.
_NO_CONVENTIONS = SourceConventions()

# The rows, keyed ``provider`` or ``provider/sender``. Filled by ``register``
# from each source's own module; read-only to everyone else.
_REGISTRY: dict[str, SourceConventions] = {}
CONVENTIONS: Mapping[str, SourceConventions] = MappingProxyType(_REGISTRY)


def register(key: str, conventions: SourceConventions) -> SourceConventions:
    """Register one source's row under ``provider`` or ``provider/sender``.

    Called at import by ``providers/<name>_conventions.py``, so importing the
    ``providers`` package registers every shipped source. Returns the row so
    the module can bind it to a name. Two modules claiming one key is a wiring
    bug and raises; re-registering the same object is harmless.
    """
    existing = _REGISTRY.get(key)
    if existing is not None and existing is not conventions:
        raise ValueError(f"conventions for {key!r} are already registered")
    _REGISTRY[key] = conventions
    return conventions


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
