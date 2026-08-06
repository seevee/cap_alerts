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
``provider``. A sender-scoped entry *replaces* the provider's rather than
layering on top of it, so it restates every rule it still wants: the
MeteoFrance entry repeats the MeteoAlarm severity derivation for that reason.

Two hook shapes cover what a dialect can do. Most rules are per-alert
callables — ``severity``, ``identity``, ``keep`` — and stay pure functions of
one alert. The two that cannot be (splitting one message into several,
collapsing several into one) are list-shaped ``PipelineStage`` entries bound to
a named slot in the fetch. The provider owns the order and decides where the
slots sit; conventions declare stages, they never reorder the fetch:

    construct → [identity] → [explode] → [keep] → mode filters → [merge] → return

Every constraint in that order is load-bearing:

* **identity** first — it feeds entity ids, and everything downstream keys on
  them.
* **explode** before **keep** — markers are dropped per exploded region.
* **keep** before the provider's mode filters, so all three MeteoAlarm modes
  are equally protected.
* **merge** last, immediately before the fetch returns. It must precede the
  alert store, which keys incoming alerts by id and would silently drop one of
  any pair sharing the day-free id the merge produces.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from types import MappingProxyType
from typing import Any, Literal

from .model import CAPAlert, geocodes_from

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

# Region-selectable geocode schemes in priority order: EUMETNET canonical
# region id first, then NUTS3 (department/county) preferred over NUTS2 (region)
# when both are present. The first scheme present on an area is what the region
# picker offers and the region filter matches. Sub-region cell schemes
# (WARNCELLID, CISORP) always co-occur with one of these and are stored in
# ``geocodes`` but never offered in the picker. ``areaDesc`` is a last resort
# when a feed names areas but carries no region-selectable scheme.
METEOALARM_REGION_SCHEMES: tuple[str, ...] = ("EMMA_ID", "NUTS3", "NUTS2")

# MeteoFrance publishes via MeteoAlarm with a per-message CAP identifier that
# embeds an issue timestamp, so every re-issue of the same logical warning mints
# a fresh identifier (issue #37). Identity for this sender alone is derived from
# a content key (see ``meteofrance_id``); every other authority keeps the
# per-message identifier hash, whose collisions there are genuinely-distinct
# concurrent warnings, not re-issues.
METEOFRANCE_SENDER = "vigilance@meteo.fr"


# ---------------------------------------------------------------------------
# Parameter accessors
# ---------------------------------------------------------------------------


def meteoalarm_awareness_type_code(parameters: Mapping[str, str] | None) -> str:
    """Language-independent phenomenon key: the leading token of the
    ``awareness_type`` parameter (``"3; Thunderstorm"`` → ``"3"``).

    MeteoAlarm CAP Profile v2.0 §2.2.17 defines the value as ``code + "; " +
    label``, and the label is the same hazard spelled however the member
    service prefers — live feeds carry both ``"1; Wind"`` and ``"1; wind"`` —
    so the code is the only stable key. Returns ``""`` when the parameter (or
    the whole mapping) is absent.
    """
    if not parameters:
        return ""
    raw = parameters.get("awareness_type") or ""
    return raw.split(";", 1)[0].strip()


def meteoalarm_region_codes(
    geocodes: Mapping[str, tuple[str, ...]],
    area_descs: tuple[str, ...] = (),
) -> tuple[str, ...]:
    """Region codes for an alert, matching ``_region_pairs`` selection.

    Returns the values of the first scheme present in
    ``METEOALARM_REGION_SCHEMES``; if none is present, falls back to the
    alert's area descriptions (mirroring ``_region_pairs``' ``areaDesc``
    fallback) so picker values and filter keys stay in the same namespace for
    the same feed.
    """
    for scheme in METEOALARM_REGION_SCHEMES:
        values = geocodes.get(scheme)
        if values:
            return tuple(values)
    return tuple(area_descs)


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
# List-shaped hooks
# ---------------------------------------------------------------------------


def _no_info(alert: CAPAlert) -> Mapping[str, Any] | None:
    """Default ``info_for``: no raw ``<info>`` block is available."""
    return None


@dataclass(frozen=True, slots=True)
class StageContext:
    """Everything a stage may read beyond the alerts themselves."""

    now: datetime
    # Empty outside the provider's region-picker mode.
    wanted_regions: frozenset[str] = frozenset()
    # The raw ``<info>`` block an alert was built from, when the provider can
    # still supply it. A deliberate seam: ``CAPAlert`` flattens every ``<area>``
    # into one comma-joined ``area_desc`` and one merged geocode container,
    # which destroys the ``areaDesc`` ↔ code pairing a region explode depends
    # on. Naming the dependency beats passing the whole feed page around.
    info_for: Callable[[CAPAlert], Mapping[str, Any] | None] = _no_info


@dataclass(frozen=True, slots=True)
class PipelineStage:
    """A list-shaped dialect stage, bound to a named point in the fetch."""

    slot: Literal["explode", "merge"]
    run: Callable[[list[CAPAlert], StageContext], list[CAPAlert]]


# ---------------------------------------------------------------------------
# MeteoFrance dialect (issues #37, #88)
# ---------------------------------------------------------------------------
#
# MeteoFrance publishes one warning per calendar *day*, each running roughly
# 00:00 → 00:00 local, and the next day's bulletin goes live alongside the
# current day's for most of the day. With a forecast-day component in the id
# (see ``meteofrance_id``) a single multi-day heat or storm episode therefore
# becomes one entity per day, and the id rolls over at midnight — breaking any
# automation or dashboard card that referenced it.
#
# The merge below collapses a run of consecutive forecast days back into one
# episode, keyed without the day component so it survives midnight. In
# region-picker mode the bulletin is first exploded into one alert per
# configured region, because the *set* of departments a bulletin covers moves
# day to day (measured: a thunderstorm bulletin went from 83 departments to 54
# overnight), so any set-derived key would split the episode anyway.


def _parse_ts(value: str) -> datetime | None:
    """Parse an ISO-8601 timestamp, or ``None`` when absent or unparseable."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _forecast_window_key(onset: str, effective: str, sent: str) -> str:
    """Forecast-day key: the ``YYYY-MM-DD`` prefix of the first non-empty of
    ``onset``/``effective``/``sent``.

    MeteoFrance re-issues a given day's warning several times but keeps the
    ``onset`` date stable, so the date (not the full timestamp) merges re-issues
    while keeping forecast days distinct. The episode merge groups days on this
    key; it reaches a shipped id only as the collision tie-breaker for a second
    live run of one episode key. Returns ``""`` when all three are empty.
    """
    for value in (onset, effective, sent):
        if value:
            return value[:10]
    return ""


def _severity_ranks() -> Mapping[str, int]:
    """The canonical severity ladder, fetched lazily.

    ``normalize`` consults this table, so importing it at module scope would
    close an import cycle. The merge has to rank days on the same ladder the
    normalizer will later apply, or the entity's dominant day and its
    ``severity_normalized`` could disagree.
    """
    from .normalize import SEVERITY_RANK

    return SEVERITY_RANK


def _canonical_severity(alert: CAPAlert) -> str:
    """Canonical severity for ranking, using normalization's own mapping.

    MeteoAlarm severity lives in the ``awareness_level`` parameter rather than
    CAP ``<severity>``, so the ranking reads that first and falls back to the
    CAP field.
    """
    severity = meteoalarm_awareness_severity(alert) or alert.severity.lower()
    return severity if severity in _severity_ranks() else "unknown"


def _severity_rank(alert: CAPAlert) -> int:
    return _severity_ranks()[_canonical_severity(alert)]


def _ts_sort_key(value: str) -> tuple[int, float, str]:
    """Total ordering over ISO timestamps: instant when parseable, else text.

    Window edges within a run can carry different UTC offsets across a DST
    boundary, so comparing the strings directly would mis-order them. Naive
    values are read as UTC; unparseable ones sort last but stay deterministic.
    """
    parsed = _parse_ts(value)
    if parsed is None:
        return (1, 0.0, value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return (0, parsed.timestamp(), value)


def _parse_day(value: str) -> date | None:
    """Parse a ``YYYY-MM-DD`` forecast-day key, or ``None``."""
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _is_finished(alert: CAPAlert, now: datetime) -> bool:
    """True once the warning's window has closed.

    Finished days must leave the episode before the id drops its day
    component, or a finished run and an upcoming run for the same key would
    collide on one id — the alert store keys by id, so one would silently
    overwrite the other.
    """
    expires = _parse_ts(alert.expires)
    if expires is None:
        return False
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    return expires <= now


# --- identity -------------------------------------------------------------


def meteofrance_id(
    sender: str,
    event_key: str,
    region_codes: Sequence[str],
    window_key: str,
    *,
    fallback: str,
) -> str:
    """Content-key identity for MeteoFrance vigilance.

    Keys on sender + phenomenon + forecast-region set + forecast day so a
    re-issue (fresh per-message identifier, same logical warning) keeps one
    stable id, while distinct phenomena and regions stay distinct entities.
    Shipped ids are minted by ``meteofrance_merge_episodes`` with an *empty*
    ``window_key`` so they survive midnight; the day component survives only
    as the collision tie-breaker for a second live run of one episode key.
    Severity/color is intentionally excluded so an orange→red escalation
    updates the existing entity rather than spawning a new one. Falls back to
    hashing ``fallback`` when every key component is empty (degenerate
    warning).
    """
    region_key = ";".join(sorted(region_codes))
    if not (sender or event_key or region_key or window_key):
        return hashlib.sha256(fallback.encode()).hexdigest()[:12]
    key = f"{sender}|{event_key}|{region_key}|{window_key}"
    return hashlib.sha256(key.encode()).hexdigest()[:12]


def meteofrance_identity(alert: CAPAlert) -> str | None:
    """Re-mint a MeteoFrance id from the finished alert's own fields.

    Every component of the content key is recoverable after construction, so
    identity is a rewrite rather than a hook threaded through the parser. The
    id minted here is provisional — ``meteofrance_merge_episodes`` recomputes
    every live MeteoFrance id before the fetch returns — which is what makes
    computing it one step later than the parser safe.
    """
    event_key = (
        meteoalarm_awareness_type_code(alert.parameters) or alert.event.casefold()
    )
    descs = tuple(d.strip() for d in alert.area_desc.split(",") if d.strip())
    return meteofrance_id(
        alert.sender,
        event_key,
        meteoalarm_region_codes(alert.geocodes, descs),
        _forecast_window_key(alert.onset, alert.effective, alert.sent),
        fallback=alert.identifier or alert.id,
    )


# --- green markers --------------------------------------------------------


def meteofrance_is_live_warning(alert: CAPAlert) -> bool:
    """False for a MeteoFrance "no warning" marker, True for a real bulletin.

    MeteoFrance encodes green/no-warning as an ``Actual``/``Update`` with a
    degenerate window, in two shapes: ``expires < onset`` (supersede marker,
    ``expires`` is the replacement's issue time) and ``expires == onset``
    (zero-length). Both are non-warnings, but they carry the same ``event``
    text, ``awareness_type``, and areas as the real bulletin for that
    department-day — and ``meteofrance_id`` deliberately excludes severity, so
    a marker and the bulletin it refers to hash to the *same* id. The alert
    store keys incoming alerts by id, so whichever arrives last wins and the
    real warning can be silently displaced by a green one (issue #37).

    The ``>`` is load-bearing and must not be "tidied" to ``>=``: the
    zero-length shape is a third of a live France feed, its ``expires`` is a
    future day boundary (so a plain ``expires <= now`` check never catches it),
    and it is sent seconds apart from the genuine bulletin.

    Fails open — an absent or unparseable window keeps the warning, so a feed
    format change can never silently drop real alerts. The table gates this on
    the sender; the shape is unverified for other authorities.
    """
    onset = _parse_ts(alert.onset)
    expires = _parse_ts(alert.expires)
    if onset is None or expires is None:
        return True
    try:
        return expires > onset
    except TypeError:
        # Mixed offset-aware/naive timestamps — not comparable, fail open.
        return True


# --- region explode -------------------------------------------------------


def _area_geocodes(area: Mapping[str, Any]) -> Mapping[str, tuple[str, ...]]:
    """Geocode container for a single ``<area>`` block, all schemes."""
    collected: dict[str, list[str]] = {}
    for code in area.get("geocode") or []:
        scheme = code.get("valueName") or ""
        collected.setdefault(scheme, []).append(code.get("value") or "")
    return geocodes_from(collected)


def _explode_alert(
    alert: CAPAlert, info: Mapping[str, Any], wanted: frozenset[str]
) -> list[CAPAlert]:
    """One MeteoFrance alert per configured region the bulletin covers.

    A France bulletin carries one ``<area>`` per department, each with its own
    ``areaDesc`` and NUTS3 code, so splitting on the area blocks gives each
    resulting alert a single-department scope for free. That makes the episode
    key stable (the bulletin's department set churns overnight; a single
    configured department does not) and replaces an ``area_desc`` listing up to
    83 departments with the one the user actually selected.

    Ids are left alone here — the merge recomputes them.
    """
    out: list[CAPAlert] = []
    seen: set[tuple[str, ...]] = set()
    for area in info.get("area") or []:
        desc = (area.get("areaDesc") or "").strip()
        geocodes = _area_geocodes(area)
        codes = meteoalarm_region_codes(geocodes, (desc,) if desc else ())
        matched = tuple(c for c in codes if c in wanted)
        if not matched or matched in seen:
            continue
        seen.add(matched)
        out.append(replace(alert, area_desc=desc or alert.area_desc, geocodes=geocodes))
    return out


def meteofrance_explode_by_region(
    alerts: list[CAPAlert], ctx: StageContext
) -> list[CAPAlert]:
    """Explode each MeteoFrance bulletin into the configured regions it covers.

    A no-op outside region-picker mode (no configured regions) and for any
    bulletin whose raw ``<info>`` block the provider could not supply. A
    bulletin covering none of the configured regions contributes nothing, which
    the mode filter would have done anyway.
    """
    if not ctx.wanted_regions:
        return alerts
    out: list[CAPAlert] = []
    for alert in alerts:
        info = ctx.info_for(alert) if alert.sender == METEOFRANCE_SENDER else None
        if info is None:
            out.append(alert)
            continue
        out.extend(_explode_alert(alert, info, ctx.wanted_regions))
    return out


# --- episode merge --------------------------------------------------------


def _episode_group_key(alert: CAPAlert) -> tuple[str, str, str]:
    """``(sender, phenomenon, region scope)`` — everything but the day.

    The region component is whatever scope the alert already carries: a single
    department after ``meteofrance_explode_by_region`` in region-picker mode,
    the bulletin's full resolved set otherwise. Country-wide mode therefore
    still splits an episode when the bulletin's footprint moves overnight; that
    is a known limitation, kept because per-department explosion there would
    turn France into roughly 150 entities.
    """
    event_key = (
        meteoalarm_awareness_type_code(alert.parameters) or alert.event.casefold()
    )
    descs = tuple(d.strip() for d in alert.area_desc.split(",") if d.strip())
    region_key = ";".join(sorted(meteoalarm_region_codes(alert.geocodes, descs)))
    return (alert.sender, event_key, region_key)


def _calendar_day_runs(alerts: list[CAPAlert]) -> list[list[CAPAlert]]:
    """Split one group's alerts into runs of consecutive forecast days.

    Two alerts on the same day are resolved by ``(severity, sent)`` — severity
    first, so a lower-severity message can never displace a higher one on send
    order alone. Live sampling says this should not happen (at most one live
    warning per department, phenomenon and day across 203 samples), so it is
    defensive; the ordering matters because the alternative silently picks by
    upstream timing.

    A gap of more than one calendar day starts a new run, on the reading that
    MeteoFrance skipping a day means a genuinely separate episode. That case
    has never been observed live, so a wrong reading here degrades to the
    previous behaviour (two entities) rather than losing anything.
    """
    by_day: dict[str, CAPAlert] = {}
    for alert in alerts:
        day = _forecast_window_key(alert.onset, alert.effective, alert.sent)
        current = by_day.get(day)
        if current is None or (_severity_rank(alert), _ts_sort_key(alert.sent)) > (
            _severity_rank(current),
            _ts_sort_key(current.sent),
        ):
            by_day[day] = alert

    runs: list[list[CAPAlert]] = []
    run: list[CAPAlert] = []
    previous: date | None = None
    for day in sorted(by_day):
        parsed = _parse_day(day)
        contiguous = (
            run
            and parsed is not None
            and previous is not None
            and (parsed - previous).days <= 1
        )
        if run and not contiguous:
            runs.append(run)
            run = []
        run.append(by_day[day])
        previous = parsed
    if run:
        runs.append(run)
    return runs


def _episode_day(alert: CAPAlert) -> dict[str, str]:
    """One ``episode_days`` entry: what this forecast day actually said."""
    return {
        "date": _forecast_window_key(alert.onset, alert.effective, alert.sent),
        "onset": alert.onset,
        "expires": alert.expires,
        "severity": _canonical_severity(alert),
        "awareness_level": (alert.parameters or {}).get("awareness_level", ""),
        "event": alert.event,
        "headline": alert.headline,
        "area_desc": alert.area_desc,
    }


def _merge_run(
    run: list[CAPAlert], key: tuple[str, str, str], window_key: str
) -> CAPAlert:
    """Collapse one run of forecast days into a single episode alert.

    The most severe day supplies the content wholesale, tie-broken to the
    earliest onset. Blending fields instead would let the record contradict
    itself — ``severity_normalized`` comes from ``awareness_level`` and the
    icon from ``event``, so a mixed record could read "Vigilance **jaune**
    canicule" while carrying an **orange** level. Per-day truth goes to
    ``episode_days``; the window is widened to span the whole run.

    A single-day run leaves ``episode_days`` empty: the profile would only
    restate the alert's own fields, and the attribute stays sparse.
    """
    sender, event_key, region_key = key
    dominant = min(run, key=lambda a: (-_severity_rank(a), _ts_sort_key(a.onset)))
    onsets = [a.onset for a in run if a.onset]
    expiries = [a.expires for a in run if a.expires]
    region_codes = tuple(region_key.split(";")) if region_key else ()
    return replace(
        dominant,
        id=meteofrance_id(
            sender,
            event_key,
            region_codes,
            window_key,
            fallback=dominant.identifier or dominant.id,
        ),
        onset=min(onsets, key=_ts_sort_key) if onsets else dominant.onset,
        expires=max(expiries, key=_ts_sort_key) if expiries else dominant.expires,
        episode_days=tuple(_episode_day(a) for a in run) if len(run) > 1 else (),
    )


def meteofrance_merge_episodes(
    alerts: list[CAPAlert], ctx: StageContext
) -> list[CAPAlert]:
    """Collapse MeteoFrance forecast days into episodes; pass everything else.

    Bound to the ``merge`` slot, which the provider runs last: it must precede
    the alert store, which keys incoming alerts by id and would silently drop
    one of any pair sharing the day-free id this produces.
    """
    if not any(a.sender == METEOFRANCE_SENDER for a in alerts):
        return alerts

    passthrough = [a for a in alerts if a.sender != METEOFRANCE_SENDER]
    groups: dict[tuple[str, str, str], list[CAPAlert]] = {}
    for alert in alerts:
        if alert.sender != METEOFRANCE_SENDER or _is_finished(alert, ctx.now):
            continue
        groups.setdefault(_episode_group_key(alert), []).append(alert)

    merged: list[CAPAlert] = []
    for key, members in groups.items():
        runs = _calendar_day_runs(members)
        for index, run in enumerate(runs):
            # The earliest run keeps the day-free id — surviving midnight is
            # the entire point. A *second* live run for one phenomenon and
            # region needs MeteoFrance to skip a forecast day mid-episode,
            # which 227 live samples never showed; but if it ever happens the
            # runs must not collide on a single id, because the alert store
            # keys by id and would silently drop one. Later runs therefore
            # re-add their first day, which churns only the pending entity and
            # never the one currently in effect.
            first = run[0]
            window_key = (
                ""
                if index == 0
                else _forecast_window_key(first.onset, first.effective, first.sent)
            )
            merged.append(_merge_run(run, key, window_key))
    merged.sort(key=lambda a: (_ts_sort_key(a.onset), a.event, a.id))
    return passthrough + merged


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
    # Replacement entity id for a finished alert, or None to keep the
    # provider's default. Runs after construction, so it reads the alert.
    identity: Callable[[CAPAlert], str | None] | None = None
    # False for a record this source publishes that is not a warning at all.
    keep: Callable[[CAPAlert], bool] | None = None
    # List-shaped stages, each bound to a named slot in the provider's fetch.
    stages: tuple[PipelineStage, ...] = ()

    @property
    def classifies_marine(self) -> bool:
        """True when this source can tell marine zones from land zones."""
        return bool(self.marine_code_prefixes)

    def stages_at(
        self, slot: str
    ) -> tuple[Callable[[list[CAPAlert], StageContext], list[CAPAlert]], ...]:
        """The stage callables registered at ``slot``, in declaration order."""
        return tuple(stage.run for stage in self.stages if stage.slot == slot)


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
        # A sender-scoped entry replaces the provider's, so the MeteoAlarm
        # severity derivation is restated here rather than inherited.
        f"meteoalarm/{METEOFRANCE_SENDER}": SourceConventions(
            severity=meteoalarm_awareness_severity,
            identity=meteofrance_identity,
            keep=meteofrance_is_live_warning,
            stages=(
                PipelineStage("explode", meteofrance_explode_by_region),
                PipelineStage("merge", meteofrance_merge_episodes),
            ),
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
