"""MeteoAlarm conventions: awareness vocabulary, region schemes, episode dialects.

One provider, many dialects: MeteoAlarm relays every EUMETNET member, so the
provider row carries what every member shares (the ``awareness_level`` severity
derivation) and two senders declare rows of their own. The provider
exposes them all as ``CONVENTIONS``.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime
from types import MappingProxyType

from ..const import (
    METEOALARM_COUNTRIES,
    METEOALARM_COUNTRY_CODE_ALIASES,
    METEOALARM_COUNTRY_NAME_ALIASES,
    METEOALARM_COUNTRY_NAMES,
)
from ..conventions import (
    PipelineStage,
    RegionEntry,
    SourceConventions,
    StageContext,
    episode_id,
    parse_instant,
    parse_timestamp,
    ts_sort_key,
)
from ..icons import INTERNATIONAL_EVENT_SUBSTRINGS, match_event_substrings
from ..model import CAPAlert, geocodes_from
from ..normalize import SEVERITY_RANK

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
# a content key (see ``episode_id``); every other authority keeps the
# per-message identifier hash, whose collisions there are genuinely-distinct
# concurrent warnings, not re-issues.
METEOFRANCE_SENDER = "vigilance@meteo.fr"

# The Finnish Meteorological Institute, the second sender to split a continuous
# warning across messages (issue #98). Its split is at the window edge rather
# than the calendar day, which is the whole reason the run predicate is
# declared per dialect — see ``EpisodeDialect``.
FMI_SENDER = "cap@fmi.fi"

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
# Episode dialects (issues #37, #88, #98)
# ---------------------------------------------------------------------------
#
# Two senders publish one continuous warning as a chain of messages, and both
# put a component of that chain into the entity id, so a single episode becomes
# one entity per message and the id rolls over mid-episode — breaking any
# automation or dashboard card that referenced it.
#
# MeteoFrance publishes one warning per calendar *day*, each running roughly
# 00:00 → 00:00 local, with the next day's bulletin live alongside the current
# day's for most of the day. FMI instead re-issues at the window edge: a
# nine-day wildfire warning arrived as nine messages, most of them ending
# exactly at the midnight the next one starts on.
#
# The merge below collapses a run of such messages back into one episode, keyed
# without the per-message component so it survives the split. In region-picker
# mode the message is first exploded into one alert per configured region,
# because the *set* of regions covered moves message to message (measured:
# a France thunderstorm bulletin went from 83 departments to 54 overnight; the
# FMI wildfire chain grew from one region to five), so any set-derived key would
# split the episode anyway.
#
# What the two senders do *not* share is what makes consecutive messages one
# episode, so that predicate is declared per sender (``EpisodeDialect.split``)
# and everything else here is one implementation.


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


def _canonical_severity(alert: CAPAlert) -> str:
    """Canonical severity for ranking, using normalization's own mapping.

    MeteoAlarm severity lives in the ``awareness_level`` parameter rather than
    CAP ``<severity>``, so the ranking reads that first and falls back to the
    CAP field.
    """
    severity = meteoalarm_awareness_severity(alert) or alert.severity.lower()
    return severity if severity in SEVERITY_RANK else "unknown"


def _severity_rank(alert: CAPAlert) -> int:
    return SEVERITY_RANK[_canonical_severity(alert)]


def _parse_day(value: str) -> date | None:
    """Parse a ``YYYY-MM-DD`` forecast-day key, or ``None``."""
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _is_finished(alert: CAPAlert, now: datetime) -> bool:
    """True once the warning's window has closed.

    Finished messages must leave the episode before the id drops its
    per-message component, or a finished run and an upcoming run for the same
    key would collide on one id — the alert store keys by id, so one would
    silently overwrite the other.
    """
    expires = parse_instant(alert.expires)
    return expires is not None and expires <= now


def meteofrance_identity(alert: CAPAlert) -> str | None:
    """Re-mint a MeteoFrance id from the finished alert's own fields.

    Every component of the content key is recoverable after construction, so
    identity is a rewrite rather than a hook threaded through the parser. The
    id minted here is provisional — the merge stage recomputes every live
    MeteoFrance id before the fetch returns — which is what makes computing it
    one step later than the parser safe.
    """
    event_key = (
        meteoalarm_awareness_type_code(alert.parameters) or alert.event.casefold()
    )
    descs = tuple(d.strip() for d in alert.area_desc.split(",") if d.strip())
    return episode_id(
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
    department-day — and ``episode_id`` deliberately excludes severity, so
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

    Known interplay with absence retention: a green marker is also how
    MeteoFrance announces a warning lifted early, and dropping it here means
    that announcement never reaches the alert store — the lifted warning goes
    absent and is retained ``stale`` until its published expiry (end of the
    forecast day) instead of clearing on the next poll. Accepted deliberately:
    the marker cannot be forwarded as a terminal record, because the
    zero-length shape routinely coexists with a *live* bulletin of the same
    episode (a green day alongside a warned day), so treating any marker as
    termination would end running warnings — the displacement bug of issue #37
    in terminal form. Distinguishing "green for a day that currently has a
    live warning" needs run-aware state this per-alert hook does not have.
    """
    onset = parse_timestamp(alert.onset)
    expires = parse_timestamp(alert.expires)
    if onset is None or expires is None:
        return True
    try:
        return expires > onset
    except TypeError:
        # Mixed offset-aware/naive timestamps — not comparable, fail open.
        return True


# --- region explode -------------------------------------------------------


def _explode_alert(
    alert: CAPAlert, entries: tuple[RegionEntry, ...], wanted: frozenset[str]
) -> list[CAPAlert]:
    """One alert per configured region the message covers.

    Splitting on the *region entry* rather than on the ``<area>`` block is what
    makes this sender-neutral: France publishes one area block per department
    (one name, one NUTS3 code), while FMI packs every warned region into a
    single block holding N ``EMMA_ID`` codes and an ``areaDesc`` naming all N.
    A per-block split would leave an FMI alert still scoped to the whole set.

    Each resulting alert is scoped to one region: the label the region picker
    offered, and that code alone. That makes the episode key stable (the covered
    set churns from message to message; a single configured region does not) and
    replaces an ``area_desc`` listing up to 83 departments with the one the user
    actually selected. Sub-region schemes on the same area (``WARNCELLID``,
    ``CISORP``) are dropped with the rest of the set — they belong to the
    message's full footprint, not to the region being scoped to.

    Ids are left alone here — the merge recomputes them.
    """
    out: list[CAPAlert] = []
    seen: set[str] = set()
    for scheme, code, label in entries:
        if code not in wanted or code in seen:
            continue
        seen.add(code)
        out.append(
            replace(
                alert,
                area_desc=label or alert.area_desc,
                # A schemeless entry is the ``areaDesc`` fallback, where the
                # description *is* the region key, so an empty container leaves
                # ``meteoalarm_region_codes`` resolving it off ``area_desc``.
                geocodes=geocodes_from({scheme: [code]}),
            )
        )
    return out


def _explode_by_region(
    alerts: list[CAPAlert], ctx: StageContext, dialect: EpisodeDialect
) -> list[CAPAlert]:
    """Explode each of this dialect's messages into the regions it covers.

    A no-op outside region-picker mode (no configured regions) and for any
    message whose region entries the provider could not supply. A message
    covering none of the configured regions contributes nothing, which the mode
    filter would have done anyway.
    """
    if not ctx.wanted_regions:
        return alerts
    out: list[CAPAlert] = []
    for alert in alerts:
        entries = ctx.regions_for(alert) if alert.sender == dialect.sender else ()
        if not entries:
            out.append(alert)
            continue
        out.extend(_explode_alert(alert, entries, ctx.wanted_regions))
    return out


# --- episode merge --------------------------------------------------------


def _episode_group_key(alert: CAPAlert) -> tuple[str, str, str]:
    """``(sender, phenomenon, region scope)`` — everything but the window.

    The region component is whatever scope the alert already carries: a single
    region after ``_explode_by_region`` in region-picker mode, the message's
    full resolved set otherwise. Country-wide mode therefore still splits an
    episode when the footprint moves between messages; that is a known
    limitation, kept because per-region explosion there would turn France into
    roughly 150 entities.
    """
    event_key = (
        meteoalarm_awareness_type_code(alert.parameters) or alert.event.casefold()
    )
    descs = tuple(d.strip() for d in alert.area_desc.split(",") if d.strip())
    region_key = ";".join(sorted(meteoalarm_region_codes(alert.geocodes, descs)))
    return (alert.sender, event_key, region_key)


def _calendar_day_runs(alerts: list[CAPAlert]) -> list[list[CAPAlert]]:
    """Runs of consecutive forecast days — the MeteoFrance run rule.

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
        if current is None or (_severity_rank(alert), ts_sort_key(alert.sent)) > (
            _severity_rank(current),
            ts_sort_key(current.sent),
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


def _contiguous_window_runs(alerts: list[CAPAlert]) -> list[list[CAPAlert]]:
    """Runs of touching windows — the FMI run rule.

    Sorted by onset, a message joins the current run when it starts at or
    before the run's furthest reach, and starts a new one otherwise. That
    merges the midnight split the reporter saw (``… → 08-05T00:00`` followed by
    ``08-05T00:00 → …``, nine messages deep in the sampled wildfire chain) while
    keeping genuinely separate advisories apart: two live ``FI809`` wind
    warnings on 2026-08-06 sat an hour apart (``09:00–21:00`` and
    ``22:00–00:00``) and must stay two entities.

    Calendar-day collapse is wrong here in both directions — it would drop one
    of those two same-day advisories, and its ``(severity, sent)`` tie-break
    could not even choose, because FMI stamps a whole batch with one ``sent``
    (12 of 23 sampled warnings shared a timestamp to the second).

    A message whose onset cannot be placed on the timeline is contiguous with
    nothing and gets its own run, degrading to one entity per message rather
    than merging on an unknown.
    """
    ordered = sorted(
        alerts, key=lambda a: (ts_sort_key(a.onset), ts_sort_key(a.expires))
    )
    runs: list[list[CAPAlert]] = []
    run: list[CAPAlert] = []
    reach: datetime | None = None
    for alert in ordered:
        onset = parse_instant(alert.onset)
        contiguous = (
            bool(run) and onset is not None and reach is not None and onset <= reach
        )
        if run and not contiguous:
            runs.append(run)
            run = []
            reach = None
        run.append(alert)
        expires = parse_instant(alert.expires)
        if expires is not None and (reach is None or expires > reach):
            reach = expires
    if run:
        runs.append(run)
    return runs


def _forecast_day_key(alert: CAPAlert) -> str:
    """The MeteoFrance tie-breaker window: the run's first forecast day.

    Day-truncated on purpose. MeteoFrance re-issues a day's bulletin with the
    onset *time* clipped to the issue time, so any finer key would churn a
    pending run's id on every re-issue of its first day.
    """
    return _forecast_window_key(alert.onset, alert.effective, alert.sent)


def _window_edge_key(alert: CAPAlert) -> str:
    """The FMI tie-breaker window: the run's opening window, verbatim.

    Day truncation is not enough here: the contiguity rule splits sub-day, so
    a second and a third disjoint same-day run would collide on the day key —
    and the alert store, keying by id, would silently drop one of them. Two
    distinct runs always differ in their opening window, because a run
    boundary *is* a gap between one window and the next. Verbatim rather than
    parsed: the strings repeat identically on every poll of the same message,
    and unparseable edges still yield distinct keys.
    """
    return f"{alert.onset}/{alert.expires}"


def _episode_day(alert: CAPAlert) -> dict[str, str]:
    """One ``episode_days`` entry: what this message actually said.

    ``date`` is the message's own window key. It is a forecast day for
    MeteoFrance, one message per day; for a dialect that splits at the window
    edge instead, two entries of one run can share a date (FMI publishes two
    same-day wind advisories an hour apart), so the entry is keyed by nothing —
    it is a profile, and ``onset``/``expires`` carry the exact window.
    """
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
    """Collapse one run of messages into a single episode alert.

    The most severe message supplies the content wholesale, tie-broken to the
    earliest onset. Blending fields instead would let the record contradict
    itself — ``severity_normalized`` comes from ``awareness_level`` and the
    icon from ``event``, so a mixed record could read "Vigilance **jaune**
    canicule" while carrying an **orange** level. Per-message truth goes to
    ``episode_days``; the window is widened to span the whole run.

    A single-message run leaves ``episode_days`` empty: the profile would only
    restate the alert's own fields, and the attribute stays sparse.
    """
    sender, event_key, region_key = key
    dominant = min(run, key=lambda a: (-_severity_rank(a), ts_sort_key(a.onset)))
    onsets = [a.onset for a in run if a.onset]
    expiries = [a.expires for a in run if a.expires]
    region_codes = tuple(region_key.split(";")) if region_key else ()
    return replace(
        dominant,
        id=episode_id(
            sender,
            event_key,
            region_codes,
            window_key,
            fallback=dominant.identifier or dominant.id,
        ),
        onset=min(onsets, key=ts_sort_key) if onsets else dominant.onset,
        expires=max(expiries, key=ts_sort_key) if expiries else dominant.expires,
        episode_days=tuple(_episode_day(a) for a in run) if len(run) > 1 else (),
    )


def _merge_episodes(
    alerts: list[CAPAlert], ctx: StageContext, dialect: EpisodeDialect
) -> list[CAPAlert]:
    """Collapse this dialect's messages into episodes; pass everything else.

    Bound to the ``merge`` slot, which the provider runs last: it must precede
    the alert store, which keys incoming alerts by id and would silently drop
    one of any pair sharing the window-free id this produces.
    """
    if not any(a.sender == dialect.sender for a in alerts):
        return alerts

    passthrough = [a for a in alerts if a.sender != dialect.sender]
    groups: dict[tuple[str, str, str], list[CAPAlert]] = {}
    for alert in alerts:
        if alert.sender != dialect.sender or _is_finished(alert, ctx.now):
            continue
        groups.setdefault(_episode_group_key(alert), []).append(alert)

    merged: list[CAPAlert] = []
    for key, members in groups.items():
        runs = dialect.split(members)
        for index, run in enumerate(runs):
            # The earliest run keeps the window-free id — surviving the message
            # split is the entire point. A *second* live run for one phenomenon
            # and region is normal for FMI (two wind advisories an hour apart)
            # and needs MeteoFrance to skip a forecast day mid-episode, which
            # 227 live samples never showed. Either way the runs must not
            # collide on a single id, because the alert store keys by id and
            # would silently drop one. Later runs therefore re-add their first
            # message's window — at the dialect's own granularity
            # (``EpisodeDialect.window_key``) — which churns only the pending
            # entity and never the one currently in effect.
            first = run[0]
            window_key = "" if index == 0 else dialect.window_key(first)
            merged.append(_merge_run(run, key, window_key))
    merged.sort(key=lambda a: (ts_sort_key(a.onset), a.event, a.id))
    return passthrough + merged


# --- dialect registration -------------------------------------------------


@dataclass(frozen=True, slots=True)
class EpisodeDialect:
    """One sender's episode conventions: whose messages, and what makes a run.

    ``split`` is the only thing two dialects disagree on, and it is the one
    thing that cannot be shared — each sender's rule is wrong for the other.
    MeteoFrance re-issues a forecast day with the onset clipped to the issue
    time, so two re-issues of one day *overlap*, and contiguity would merge
    them into a bogus two-day episode with ``onset`` widened back to the
    superseded issue time. In the other direction, day-collapse would silently
    drop one of FMI's two same-day advisories, with its ``(severity, sent)``
    tie-break unable to even choose because FMI stamps a whole batch with one
    ``sent``.

    ``window_key`` is the run rule's granularity applied to identity: the
    window a second-or-later live run re-adds to its id so two runs of one
    episode key can never collide. It must be exactly as fine as ``split`` can
    cut. MeteoFrance's day key would collapse a second and a third same-day
    FMI run onto one id (and the alert store would drop one); FMI's verbatim
    key would churn a pending MeteoFrance run's id on every re-issue, whose
    onset time moves with the issue time.

    Everything downstream of the split — the finished-drop, dominant selection,
    window widening, ``episode_days``, id minting — is one implementation.
    """

    sender: str
    split: Callable[[list[CAPAlert]], list[list[CAPAlert]]]
    window_key: Callable[[CAPAlert], str]


METEOFRANCE_EPISODES = EpisodeDialect(
    METEOFRANCE_SENDER, _calendar_day_runs, _forecast_day_key
)
FMI_EPISODES = EpisodeDialect(FMI_SENDER, _contiguous_window_runs, _window_edge_key)


def episode_stages(dialect: EpisodeDialect) -> tuple[PipelineStage, ...]:
    """The explode + merge stage pair for one episode dialect.

    Closures over the dialect rather than sender literals in the stage bodies,
    so registering a sender is a table entry (the module's whole thesis) and
    the pipeline is implemented once however many senders declare it.
    """

    def explode(alerts: list[CAPAlert], ctx: StageContext) -> list[CAPAlert]:
        return _explode_by_region(alerts, ctx, dialect)

    def merge(alerts: list[CAPAlert], ctx: StageContext) -> list[CAPAlert]:
        return _merge_episodes(alerts, ctx, dialect)

    return (PipelineStage("explode", explode), PipelineStage("merge", merge))


# ---------------------------------------------------------------------------
# Icons
# ---------------------------------------------------------------------------

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


def meteoalarm_icon(alert: CAPAlert, event: str) -> str | None:
    """The coded hazard first, then the international vocabulary.

    The ``awareness_type`` code beats the event tables in every language,
    English included, and needs no event text at all — which is why the hook
    runs before the empty-event check. Codes outside the table (a profile
    revision we have not seen) fall through to the text, and from there to
    the shared sweep.
    """
    code = meteoalarm_awareness_type_code(alert.parameters)
    if (icon := _METEOALARM_AWARENESS_ICONS.get(code)) is not None:
        return icon
    if not event:
        return None
    return match_event_substrings(event, INTERNATIONAL_EVENT_SUBSTRINGS)


# ---------------------------------------------------------------------------
# Country-source resolution (fully-mobile mode)
# ---------------------------------------------------------------------------


def resolve_country_code(value: object) -> str | None:
    """Map a country-source value to a MeteoAlarm ISO-2 code, or ``None``.

    Accepts a two-letter code — MeteoAlarm's own (``"UK"``), ISO 3166-1
    (``"GB"``), or the EU institutional variant (``"EL"``) — or a country
    name, case-insensitively. Names match ``METEOALARM_COUNTRY_NAMES``
    display names and ``METEOALARM_COUNTRY_NAME_ALIASES``, after stripping
    parenthetical suffixes so reverse-geocoder output like
    ``"Moldova (the Republic of)"`` resolves. Non-string or unrecognized
    values return ``None``.
    """
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    upper = cleaned.upper()
    if upper in METEOALARM_COUNTRIES:
        return upper
    if upper in METEOALARM_COUNTRY_CODE_ALIASES:
        return METEOALARM_COUNTRY_CODE_ALIASES[upper]
    folded = re.sub(r"\s*\([^)]*\)", "", cleaned).strip().casefold()
    alias = METEOALARM_COUNTRY_NAME_ALIASES.get(folded)
    if alias is not None:
        return alias
    for iso, name in METEOALARM_COUNTRY_NAMES.items():
        if name.casefold() == folded:
            return iso
    return None


def resolve_language(language: str) -> str:
    """``auto`` on MeteoAlarm: the 2-letter prefix of the UI language.

    MeteoAlarm spans ~35 locales but one region set per country, so the
    prefix is enough for the provider's language-prefix matcher to find the
    closest ``<cap:info>`` block.
    """
    return language.split("-", 1)[0].lower() or "en"


# ---------------------------------------------------------------------------
# The rows
# ---------------------------------------------------------------------------

METEOALARM_CONVENTIONS = SourceConventions(
    severity=meteoalarm_awareness_severity,
    icon=meteoalarm_icon,
    resolve_language=resolve_language,
    country_code=resolve_country_code,
)


# A sender-scoped row replaces the provider's, so the MeteoAlarm severity
# derivation and icon classifier are restated here rather than inherited.
METEOFRANCE_CONVENTIONS = SourceConventions(
    severity=meteoalarm_awareness_severity,
    icon=meteoalarm_icon,
    identity=meteofrance_identity,
    keep=meteofrance_is_live_warning,
    stages=episode_stages(METEOFRANCE_EPISODES),
)


# FMI splits a continuous warning at the window edge (issue #98), so it
# declares the episode stages with its own run rule — and nothing else. No
# ``keep``: Finland publishes no green/no-warning markers (all 23 sampled
# warnings were ``2; yellow``, none with a degenerate window). No ``identity``
# either: the merge re-mints every shipped id, and MeteoFrance's identity hook
# is load-bearing there only because of the green-marker collision FMI does
# not have.
FMI_CONVENTIONS = SourceConventions(
    severity=meteoalarm_awareness_severity,
    icon=meteoalarm_icon,
    stages=episode_stages(FMI_EPISODES),
)


# What this provider declares, keyed as ``conventions_for`` resolves it.
CONVENTIONS: Mapping[str, SourceConventions] = MappingProxyType(
    {
        "meteoalarm": METEOALARM_CONVENTIONS,
        f"meteoalarm/{METEOFRANCE_SENDER}": METEOFRANCE_CONVENTIONS,
        f"meteoalarm/{FMI_SENDER}": FMI_CONVENTIONS,
    }
)
