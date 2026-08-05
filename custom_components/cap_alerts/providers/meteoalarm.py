"""MeteoAlarm (EUMETNET) per-country JSON warnings feed provider.

Uses the aggregate JSON endpoint
(``feeds.meteoalarm.org/api/v1/warnings/feeds-{country-slug}``) which ships
proper CAP-1.2 ``info`` blocks (multi-language) and per-area geocodes. Feeds
carry a mix of area-geocode schemes across countries (``EMMA_ID``, ``NUTS3``,
``NUTS2``, ``WARNCELLID``, ``CISORP``); some countries carry two at once and
some none (polygon-only). Geocodes are collected into the scheme-keyed
``CAPAlert.geocodes`` container rather than a single named field.

Three filter modes selectable via config-flow:

* country-wide — all warnings for the configured country.
* gps-polygon — parses ``area.polygon`` from each warning and keeps only
  warnings whose polygon contains the configured point. Fails loud when a
  non-empty warnings page contains zero polygons (the country does not
  publish per-warning geometry).
* region-picker — keeps warnings whose region codes intersect the configured
  region selection. Region codes are resolved from ``geocodes`` by scheme
  priority (``METEOALARM_REGION_SCHEMES``) so a country's coarsest
  administrative scheme (e.g. ``EMMA_ID`` for DE, ``NUTS3`` for FR) is what
  both the picker offers and the filter matches.

The picker list itself is derived from the warnings feed. No usable regions
endpoint exists: ``feeds.meteoalarm.org/api/v1/regions/feeds-{slug}`` is 404
for all 38 countries, the official successor ``api.meteoalarm.org/metadata/v1``
needs a re-user API key, and the public endpoint behind meteoalarm.org's own
map keys areas by internal UUID rather than by any CAP geocode. Deriving from
warnings is not the fallback it reads as — members publish green/no-warning
entries for every area, so a live feed enumerates the country's full
administrative tree (measured 2026-08-04: DE 408 regions, PL 383, ES 233).

An area may publish several region codes under a single ``areaDesc`` — FMI
names four sea areas in one string — so ``_region_pairs`` offers every code of
the area's scheme and labels each from the most specific honest source
available: per-code names when the description zips 1:1 with the codes, the
block name qualified by the code when it carries a single name, the bare code
otherwise. Harvesting reads one ``<info>`` block per warning, chosen by
language, because a feed whose areas carry no region-selectable scheme falls
back to ``areaDesc`` — there the code *is* the label, so reading every block
would offer each region once per published language.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import replace
from datetime import date, datetime, timezone
from typing import Any

import aiohttp

from homeassistant.helpers.update_coordinator import UpdateFailed

from ..const import (
    CONF_COUNTRY,
    CONF_COUNTRY_ENTITY,
    CONF_GPS_LOC,
    CONF_LANGUAGE,
    CONF_REGIONS,
    METEOALARM_COUNTRY_SLUGS,
)
from ..conventions import meteoalarm_awareness_severity
from ..model import CAPAlert, geocodes_from
from .cap import parse_cap_polygon_text
from .geometry import geometry_from_polygons
from ..normalize import SEVERITY_RANK

_LOGGER = logging.getLogger(__name__)

METEOALARM_FEED_URL = "https://feeds.meteoalarm.org/api/v1/warnings/feeds-{country}"

# Region-selectable geocode schemes in priority order: EUMETNET canonical
# region id first, then NUTS3 (department/county) preferred over NUTS2 (region)
# when both are present. The first scheme present on an area is what the region
# picker offers and the region filter matches. Sub-region cell schemes
# (WARNCELLID, CISORP) always co-occur with one of these and are stored in
# ``geocodes`` but never offered in the picker. ``areaDesc`` is a last resort
# when a feed names areas but carries no region-selectable scheme.
METEOALARM_REGION_SCHEMES: tuple[str, ...] = ("EMMA_ID", "NUTS3", "NUTS2")

# ``Region (District, District, …)`` — the Czech areaDesc shape, where the
# parenthesized list names the area's individual region codes and the prefix
# names the block they belong to. Requires balanced, non-nested parentheses so
# a name that merely contains a bracket never reaches the split.
_PARENTHETICAL = re.compile(r"^[^()]+\(([^()]+)\)$")

# MeteoFrance publishes via MeteoAlarm with a per-message CAP identifier that
# embeds an issue timestamp, so every re-issue of the same logical warning mints
# a fresh identifier (issue #37). Identity for this sender alone is derived from
# a content key (see ``_meteofrance_id``); every other authority keeps the
# per-message identifier hash, whose collisions there are genuinely-distinct
# concurrent warnings, not re-issues.
_MF_SENDER = "vigilance@meteo.fr"


def _awareness_type_code(parameters: Mapping[str, str] | None) -> str:
    """Language-independent phenomenon key: the leading token of the
    ``awareness_type`` parameter (``"3; Thunderstorm"`` → ``"3"``).

    Returns ``""`` when the parameter (or the whole mapping) is absent.
    """
    if not parameters:
        return ""
    raw = parameters.get("awareness_type") or ""
    return raw.split(";", 1)[0].strip()


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


def _parse_ts(value: str) -> datetime | None:
    """Parse an ISO-8601 timestamp, or ``None`` when absent or unparseable."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _is_live_mf_warning(alert: CAPAlert) -> bool:
    """False for a MeteoFrance "no warning" marker, True for a real bulletin.

    MeteoFrance encodes green/no-warning as an ``Actual``/``Update`` with a
    degenerate window, in two shapes: ``expires < onset`` (supersede marker,
    ``expires`` is the replacement's issue time) and ``expires == onset``
    (zero-length). Both are non-warnings, but they carry the same ``event``
    text, ``awareness_type``, and areas as the real bulletin for that
    department-day — and ``_meteofrance_id`` deliberately excludes severity, so
    a marker and the bulletin it refers to hash to the *same* id. The alert
    store keys incoming alerts by id, so whichever arrives last wins and the
    real warning can be silently displaced by a green one (issue #37).

    The ``>`` is load-bearing and must not be "tidied" to ``>=``: the
    zero-length shape is a third of a live France feed, its ``expires`` is a
    future day boundary (so a plain ``expires <= now`` check never catches it),
    and it is sent seconds apart from the genuine bulletin.

    Fails open — an absent or unparseable window keeps the warning, so a feed
    format change can never silently drop real alerts. Callers gate on
    ``sender == _MF_SENDER``; the shape is unverified for other authorities.
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


def _drop_mf_non_warnings(alerts: list[CAPAlert]) -> list[CAPAlert]:
    """Drop MeteoFrance green markers, leaving every other sender untouched."""
    kept = [a for a in alerts if a.sender != _MF_SENDER or _is_live_mf_warning(a)]
    dropped = len(alerts) - len(kept)
    if dropped:
        _LOGGER.debug(
            "MeteoAlarm: dropped %d MeteoFrance no-warning marker(s) of %d",
            dropped,
            len(alerts),
        )
    return kept


def _default_id(identifier: str, uuid: str) -> str:
    """Hash a CAP identifier (or ``uuid`` fallback) to a 12-hex stable ID.

    This is the identity for every authority except MeteoFrance.
    """
    key = identifier or uuid
    return hashlib.sha256(key.encode()).hexdigest()[:12]


def _meteofrance_id(
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
    Shipped ids are minted by ``_merge_meteofrance_episodes`` with an *empty*
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


def _compute_alert_id(
    sender: str,
    identifier: str,
    uuid: str,
    event_key: str,
    region_codes: Sequence[str],
    window_key: str,
) -> str:
    """Dispatch identity by sender.

    MeteoFrance gets the re-issue-stable content key; every other authority
    keeps the per-message identifier hash (byte-for-byte unchanged from before
    issue #37's fix). The MeteoFrance id minted here is provisional —
    ``_merge_meteofrance_episodes`` recomputes every live MeteoFrance id
    before ``async_fetch`` returns.
    """
    if sender == _MF_SENDER:
        return _meteofrance_id(
            sender, event_key, region_codes, window_key, fallback=identifier or uuid
        )
    return _default_id(identifier, uuid)


# --- MeteoFrance episode merge (issue #37) --------------------------------
#
# MeteoFrance publishes one warning per calendar *day*, each running roughly
# 00:00 → 00:00 local, and the next day's bulletin goes live alongside the
# current day's for most of the day. With a forecast-day component in the id
# (see ``_meteofrance_id``) a single multi-day heat or storm episode therefore
# becomes one entity per day, and the id rolls over at midnight — breaking any
# automation or dashboard card that referenced it.
#
# The merge below collapses a run of consecutive forecast days back into one
# episode, keyed without the day component so it survives midnight. In
# region-picker mode the bulletin is first exploded into one alert per
# configured region, because the *set* of departments a bulletin covers moves
# day to day (measured: a thunderstorm bulletin went from 83 departments to 54
# overnight), so any set-derived key would split the episode anyway.


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


def _canonical_severity(alert: CAPAlert) -> str:
    """Canonical severity for ranking, using normalization's own mapping.

    MeteoAlarm severity lives in the ``awareness_level`` parameter rather than
    CAP ``<severity>``, and the merge must rank days on the same ladder the
    normalizer will later apply, or the entity's dominant day and its
    ``severity_normalized`` could disagree.
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


def _area_geocodes(area: Mapping[str, Any]) -> Mapping[str, tuple[str, ...]]:
    """Geocode container for a single ``<area>`` block, all schemes."""
    collected: dict[str, list[str]] = {}
    for code in area.get("geocode") or []:
        scheme = code.get("valueName") or ""
        collected.setdefault(scheme, []).append(code.get("value") or "")
    return geocodes_from(collected)


def _explode_mf_by_region(
    alert: CAPAlert, info: Mapping[str, Any], wanted: set[str]
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
        codes = _region_codes(geocodes, (desc,) if desc else ())
        matched = tuple(c for c in codes if c in wanted)
        if not matched or matched in seen:
            continue
        seen.add(matched)
        out.append(replace(alert, area_desc=desc or alert.area_desc, geocodes=geocodes))
    return out


def _episode_group_key(alert: CAPAlert) -> tuple[str, str, str]:
    """``(sender, phenomenon, region scope)`` — everything but the day.

    The region component is whatever scope the alert already carries: a single
    department after ``_explode_mf_by_region`` in region-picker mode, the
    bulletin's full resolved set otherwise. Country-wide mode therefore still
    splits an episode when the bulletin's footprint moves overnight; that is a
    known limitation, kept because per-department explosion there would turn
    France into roughly 150 entities.
    """
    event_key = _awareness_type_code(alert.parameters) or alert.event.casefold()
    descs = tuple(d.strip() for d in alert.area_desc.split(",") if d.strip())
    region_key = ";".join(sorted(_region_codes(alert.geocodes, descs)))
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
        id=_meteofrance_id(
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


def _merge_meteofrance_episodes(
    alerts: list[CAPAlert], *, now: datetime
) -> list[CAPAlert]:
    """Collapse MeteoFrance forecast days into episodes; pass everything else.

    Runs last in ``async_fetch`` because it must precede the alert store, which
    keys incoming alerts by id and would silently drop one of any pair sharing
    the day-free id this produces.
    """
    if not any(a.sender == _MF_SENDER for a in alerts):
        return alerts

    passthrough = [a for a in alerts if a.sender != _MF_SENDER]
    groups: dict[tuple[str, str, str], list[CAPAlert]] = {}
    for alert in alerts:
        if alert.sender != _MF_SENDER or _is_finished(alert, now):
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


def _lang_prefix(value: str) -> str:
    """Lowercase 2-letter prefix of a BCP-47 code (``de-DE`` → ``de``)."""
    if not value:
        return ""
    return value.split("-", 1)[0].lower()


def _pick_info_blocks(
    infos: list[dict[str, Any]], preferred_prefix: str
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Pick the primary info block by language and an alternate if any.

    Preference order:
    1. info with a ``language`` whose 2-letter prefix matches
       ``preferred_prefix``;
    2. info whose language prefix is ``en`` (generic fallback);
    3. first info block in document order.

    The alternate is the first remaining block, if any.
    """
    primary_idx: int | None = None
    en_idx: int | None = None
    for idx, info in enumerate(infos):
        prefix = _lang_prefix(info.get("language", ""))
        if preferred_prefix and prefix == preferred_prefix and primary_idx is None:
            primary_idx = idx
        if prefix == "en" and en_idx is None:
            en_idx = idx

    if primary_idx is None:
        primary_idx = en_idx if en_idx is not None else 0

    primary = infos[primary_idx]
    alt: dict[str, Any] | None = None
    for idx, info in enumerate(infos):
        if idx == primary_idx:
            continue
        alt = info
        break
    return primary, alt


def _flatten_parameters(info: Mapping[str, Any]) -> dict[str, str]:
    """Collect ``parameter`` valueName/value pairs into a flat dict.

    When the same ``valueName`` repeats, values are joined with ``"; "``.
    """
    params: dict[str, str] = {}
    for entry in info.get("parameter") or []:
        name = entry.get("valueName") or ""
        value = entry.get("value") or ""
        if not name:
            continue
        existing = params.get(name)
        params[name] = f"{existing}; {value}" if existing else value
    return params


def _join_areas(info: Mapping[str, Any]) -> str:
    """Concatenate ``areaDesc`` from every area block in document order."""
    descs: list[str] = []
    for area in info.get("area") or []:
        desc = area.get("areaDesc") or ""
        if desc and desc not in descs:
            descs.append(desc)
    return ", ".join(descs)


def _scheme_geocodes(info: Mapping[str, Any]) -> Mapping[str, tuple[str, ...]]:
    """All area geocodes keyed by ``valueName`` (scheme).

    Collects every ``geocode`` across the info's area blocks into a
    scheme→values mapping, e.g. ``{"EMMA_ID": (...), "WARNCELLID": (...)}``.
    ``geocodes_from`` de-duplicates per scheme, order-preserving, and drops
    empty schemes/values. Areas without any geocode contribute nothing.
    """
    collected: dict[str, list[str]] = {}
    for area in info.get("area") or []:
        for code in area.get("geocode") or []:
            scheme = code.get("valueName") or ""
            collected.setdefault(scheme, []).append(code.get("value") or "")
    return geocodes_from(collected)


def _split_area_names(desc: str, count: int) -> tuple[str, ...]:
    """Per-code region names derived from an ``areaDesc``, or ``()``.

    An area may carry several region codes under a single ``areaDesc`` that
    names each of them, e.g. FMI's ``"Pohjois-Itämeren itäosa, Pohjois-Itämeren
    länsiosa, Ahvenanmeri, Saaristomeri"`` over four ``EMMA_ID`` values. The
    CAP profile lists those names in geocode order separated by ``", "``, so a
    split that yields exactly ``count`` names zips 1:1 with the codes.

    A label must never claim to name a code it does not name, so the split is
    only trusted when it is unambiguous:

    * ``count <= 1`` — no derivation at all. The single code keeps the whole
      ``areaDesc`` byte for byte, which protects names that legitimately
      contain a comma or parentheses (``"Ibiza y Formentera (Illes
      Balears)"``).
    * ``Region (District, District, …)`` — the parenthesized list is used when
      it yields exactly ``count`` names.
    * otherwise the whole ``desc`` is split, and accepted only when it yields
      exactly ``count`` names **and** no part contains a bracket. A stray
      bracket means the split cut through a structural name, not between two
      region names.

    Anything else (notably the elided ``"Etelä-, Keski- ja
    Pohjois-Pohjanmaa"``, 2 parts over 3 codes) returns ``()`` so the caller
    can fall back to a label that stays true.
    """
    if count <= 1:
        return (desc,) if desc else ()

    match = _PARENTHETICAL.match(desc)
    if match:
        inner = _split_names(match.group(1))
        if len(inner) == count:
            return inner

    parts = _split_names(desc)
    if len(parts) == count and not any("(" in p or ")" in p for p in parts):
        return parts
    return ()


def _split_names(desc: str) -> tuple[str, ...]:
    """Split a comma-separated name list, stripped, empties dropped."""
    return tuple(part.strip() for part in desc.split(",") if part.strip())


def _qualified_labels(desc: str, codes: Sequence[str]) -> tuple[str, ...]:
    """``"{name} ({code})"`` per code when ``desc`` carries exactly one name.

    An area whose ``areaDesc`` holds a single name over several codes names
    the *block* the codes were published under (Denmark's ``"All areas"``,
    Czechia's bare ``"Karlovarský kraj"``, Germany's shared ``"Kreis
    Göttingen"``), not any individual code. The code is appended because the
    name is true of the block rather than of the code alone — and because a
    dropdown holding 28 options that all read ``"All areas"`` is unusable.

    Returns ``()`` when ``desc`` is empty or splits to more than one name;
    those shapes have no honest per-code mapping and fall through to the bare
    code.
    """
    names = _split_names(desc)
    if len(names) != 1:
        return ()
    return tuple(f"{names[0]} ({code})" for code in codes)


def _label_tier(code: str, label: str) -> int:
    """Rank a label's specificity: 1 per-code, 2 block-qualified, 3 bare code.

    Inferred from the label's shape rather than tracked alongside it, which
    keeps ``_region_pairs``' ``(code, label)`` contract unchanged. The shapes
    read here are the ones ``_qualified_labels`` produces one function
    earlier; a feed name that happens to end in its own parenthesized code
    would only shift a preference between two labels, never invent one.
    """
    if label == code:
        return 3
    if label.endswith(f" ({code})"):
        return 2
    return 1


def _merge_region_pairs(pairs: Iterable[tuple[str, str]]) -> list[tuple[str, str]]:
    """De-duplicate ``(code, label)`` pairs by code, keeping the best label.

    The same code can be labeled differently by two warnings — named on its
    own in one area and block-qualified in another — so the most specific
    label wins (``_label_tier``), ties going to the first seen. Empty codes are
    dropped and an empty label falls back to the code. First-appearance order
    is preserved; sorting is the caller's business.
    """
    best: dict[str, str] = {}
    for code, label in pairs:
        if not code:
            continue
        resolved = label or code
        current = best.get(code)
        if current is None or _label_tier(code, resolved) < _label_tier(code, current):
            best[code] = resolved
    return list(best.items())


def _region_pairs(info: Mapping[str, Any]) -> list[tuple[str, str]]:
    """``(code, label)`` region-picker pairs for the info's areas.

    Per area, take **every** value of the first scheme present in
    ``METEOALARM_REGION_SCHEMES`` — an area may carry several region codes
    under one ``areaDesc`` (issue #48), and the region filter matches on all of
    them, so the picker has to offer all of them. Labels come from the first
    tier that applies:

    1. per-code names, when ``areaDesc`` splits 1:1 with the codes
       (``_split_area_names``);
    2. the block name qualified by the code, when ``areaDesc`` carries a single
       name (``_qualified_labels``);
    3. the bare code, when neither mapping is honest.

    If no region-selectable scheme is present but ``areaDesc`` is set, fall
    back to ``(areaDesc, areaDesc)`` so named-but-schemeless feeds still
    populate the picker. Document order; de-duplicated by code.
    """
    out: list[tuple[str, str]] = []
    for area in info.get("area") or []:
        desc = area.get("areaDesc") or ""
        by_scheme: dict[str, list[str]] = {}
        for code in area.get("geocode") or []:
            scheme = code.get("valueName") or ""
            value = code.get("value") or ""
            if scheme and value:
                values = by_scheme.setdefault(scheme, [])
                if value not in values:
                    values.append(value)
        codes: tuple[str, ...] = ()
        for scheme in METEOALARM_REGION_SCHEMES:
            if by_scheme.get(scheme):
                codes = tuple(by_scheme[scheme])
                break
        if not codes:
            if desc:
                out.append((desc, desc))
            continue
        labels = (
            _split_area_names(desc, len(codes))
            or _qualified_labels(desc, codes)
            or codes
        )
        out.extend(zip(codes, labels))
    return _merge_region_pairs(out)


def _region_codes(
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


def _first(value: Any) -> str:
    """Return the first element of a list-or-string value as a string.

    The JSON feed wraps several CAP fields (``category``, ``responseType``)
    in single-element lists; this normalizes them back to a scalar.
    """
    if isinstance(value, list):
        return str(value[0]) if value else ""
    return str(value or "")


def _info_text(info: Mapping[str, Any] | None, key: str) -> str:
    if info is None:
        return ""
    return str(info.get(key) or "")


def _point_in_polygon(lat: float, lon: float, polygon: list[list[float]]) -> bool:
    """Ray-casting point-in-polygon test. Polygon is ``[[lon, lat], ...]``."""
    n = len(polygon)
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = polygon[i][0], polygon[i][1]  # lon, lat
        xj, yj = polygon[j][0], polygon[j][1]
        if ((yi > lat) != (yj > lat)) and (
            lon < (xj - xi) * (lat - yi) / (yj - yi) + xi
        ):
            inside = not inside
        j = i
    return inside


def _extract_geometries(info: Mapping[str, Any]) -> list[list[list[float]]]:
    """Return polygon rings from a CAP info block.

    One ring per area that carries a usable polygon, in GeoJSON
    ``[[lon, lat], ...]`` order. ``area.polygon`` is accepted as a string or a
    list of strings; unparseable entries are skipped. Region-picker pairs are
    derived separately by ``_region_pairs``.
    """
    rings: list[list[list[float]]] = []
    for area in info.get("area") or []:
        polygon = area.get("polygon")
        candidates: list[str]
        if isinstance(polygon, list):
            candidates = [p for p in polygon if isinstance(p, str)]
        elif isinstance(polygon, str):
            candidates = [polygon]
        else:
            candidates = []
        for text in candidates:
            ring = parse_cap_polygon_text(text)
            if ring is not None:
                rings.append(ring)
    return rings


def _primary_info(
    warning: Mapping[str, Any], preferred_prefix: str
) -> Mapping[str, Any] | None:
    """The info block ``_warning_to_alert`` selected, for callers needing areas.

    ``_warning_to_alert`` flattens the area blocks into ``area_desc`` and a
    de-duplicated ``geocodes`` container, which loses the pairing between a
    department's code and its name. The region explosion needs that pairing, so
    it re-selects the same block rather than trying to re-zip two independently
    de-duplicated sequences.
    """
    infos = (warning.get("alert") or {}).get("info") or []
    if not infos:
        return None
    primary, _alt = _pick_info_blocks(infos, preferred_prefix)
    return primary


def _warning_to_alert(
    warning: Mapping[str, Any], preferred_prefix: str
) -> CAPAlert | None:
    """Convert one ``{"alert": ..., "uuid": ...}`` warning to a ``CAPAlert``.

    Returns ``None`` for warnings filtered out (non-Actual status, missing
    info blocks).
    """
    alert = warning.get("alert") or {}
    status = alert.get("status") or ""
    if status and status != "Actual":
        return None

    infos = alert.get("info") or []
    if not infos:
        return None

    primary, alt = _pick_info_blocks(infos, preferred_prefix)
    identifier = alert.get("identifier") or ""
    uuid = warning.get("uuid") or ""
    parameters = _flatten_parameters(primary)
    geocodes = _scheme_geocodes(primary)
    rings = _extract_geometries(primary)
    geometry = geometry_from_polygons(rings)

    sender = alert.get("sender") or ""
    event = _info_text(primary, "event")
    onset = _info_text(primary, "onset")
    sent = alert.get("sent") or ""
    area_descs = tuple(d.strip() for d in _join_areas(primary).split(",") if d.strip())
    event_key = _awareness_type_code(parameters) or event.casefold()
    window_key = _forecast_window_key(onset, "", sent)
    region_codes = _region_codes(geocodes, area_descs)

    return CAPAlert(
        id=_compute_alert_id(
            sender, identifier, uuid, event_key, region_codes, window_key
        ),
        url="",
        identifier=identifier,
        event=event,
        msg_type=alert.get("msgType") or "",
        status=status,
        scope=alert.get("scope") or "",
        category=_first(primary.get("category")),
        urgency=_info_text(primary, "urgency"),
        severity=_info_text(primary, "severity"),
        certainty=_info_text(primary, "certainty"),
        response_type=_first(primary.get("responseType")),
        sent=sent,
        effective="",
        onset=onset,
        expires=_info_text(primary, "expires"),
        headline=_info_text(primary, "headline"),
        description=_info_text(primary, "description"),
        instruction=_info_text(primary, "instruction") or None,
        web=_info_text(primary, "web"),
        area_desc=_join_areas(primary),
        geocodes=geocodes,
        geometry=geometry,
        sender=sender,
        sender_name=_info_text(primary, "senderName"),
        parameters=parameters or None,
        language=_info_text(primary, "language"),
        headline_alt=_info_text(alt, "headline"),
        description_alt=_info_text(alt, "description"),
        instruction_alt=_info_text(alt, "instruction") or None,
        language_alt=_info_text(alt, "language"),
        provider="meteoalarm",
    )


def _parse_gps(value: str) -> tuple[float, float] | None:
    """Extract ``(lat, lon)`` from a ``"lat,lon"`` config string."""
    if not value:
        return None
    try:
        parts = value.split(",")
        return float(parts[0].strip()), float(parts[1].strip())
    except (ValueError, IndexError):
        return None


def _alert_polygons(alert: CAPAlert) -> list[list[list[float]]]:
    """Extract the polygon rings already stored on a CAPAlert geometry."""
    geom = alert.geometry
    if not geom:
        return []
    gtype = geom.get("type")
    coords = geom.get("coordinates")
    if not coords:
        return []
    if gtype == "Polygon":
        return [coords[0]] if coords else []
    if gtype == "MultiPolygon":
        return [poly[0] for poly in coords if poly]
    return []


async def fetch_regions_for_country(
    session: aiohttp.ClientSession, country_iso: str, *, language: str = ""
) -> list[tuple[str, str]]:
    """Return ``[(region_code, label), ...]`` for the given country.

    ``region_code`` is in the country's region-selectable scheme (EMMA_ID for
    most, NUTS3 for FR/BG/RO/MK, NUTS2 for HU/BE) — the same namespace the
    per-alert region filter matches against. Countries whose feeds carry no
    region-selectable scheme at all (CH, EE, IE, IL, LU, NO, SE, SI, UA, UK,
    LV) fall back to ``areaDesc`` strings in both places.

    ``language`` picks the ``<info>`` block labels are read from; defaults to
    English, matching ``async_fetch``. It is load-bearing for the ``areaDesc``
    countries, where the label *is* the code.

    Returns ``[]`` when the feed is reachable but names no regions — a real
    state for a single-zone or currently-quiet country, and the caller's
    business to present. Raises ``UpdateFailed`` for an unsupported country or
    a feed that could not be read.
    """
    country = (country_iso or "").upper()
    slug = METEOALARM_COUNTRY_SLUGS.get(country)
    if slug is None:
        raise UpdateFailed(f"MeteoAlarm: unsupported country {country}")

    preferred_prefix = _lang_prefix(language) or "en"
    regions = await _fetch_regions_from_warnings(session, slug, preferred_prefix)
    return sorted(_merge_region_pairs(regions), key=lambda item: item[1].lower())


async def _fetch_regions_from_warnings(
    session: aiohttp.ClientSession, slug: str, preferred_prefix: str
) -> list[tuple[str, str]]:
    """Derive the region list from the warnings feed.

    Reads one info block per warning — the one ``_pick_info_blocks`` selects
    for ``preferred_prefix`` — rather than all of them. A multi-language feed
    repeats its areas per language, and for the ``areaDesc`` fallback those
    repeats are distinct codes that no de-duplication can merge (Norway
    published 26 entries for 13 regions on 2026-08-04).

    Raises ``UpdateFailed`` on any failure to read the feed, so the caller can
    tell a broken fetch from a country that genuinely names no regions.
    """
    url = METEOALARM_FEED_URL.format(country=slug)
    try:
        async with session.get(url) as resp:
            if resp.status != 200:
                raise UpdateFailed(f"MeteoAlarm {slug}: HTTP {resp.status}")
            try:
                payload = await resp.json(content_type=None)
            except (aiohttp.ContentTypeError, ValueError) as err:
                raise UpdateFailed(f"MeteoAlarm {slug}: invalid JSON: {err}") from err
    except aiohttp.ClientError as err:
        raise UpdateFailed(f"MeteoAlarm {slug}: {err}") from err

    warnings = payload.get("warnings") if isinstance(payload, dict) else None
    if not isinstance(warnings, list):
        raise UpdateFailed(f"MeteoAlarm {slug}: feed missing 'warnings' array")

    out: list[tuple[str, str]] = []
    for warning in warnings:
        if not isinstance(warning, dict):
            continue
        infos = (warning.get("alert") or {}).get("info") or []
        if not infos:
            continue
        primary, _alt = _pick_info_blocks(infos, preferred_prefix)
        out.extend(_region_pairs(primary))
    return out


class MeteoAlarmProvider:
    """Per-country MeteoAlarm JSON warnings provider."""

    @property
    def name(self) -> str:
        return "meteoalarm"

    async def async_fetch(
        self,
        session: aiohttp.ClientSession,
        config: Mapping[str, Any],
        options: Mapping[str, Any],
        *,
        cap_content_cache=None,
        user_agent=None,
        now: datetime | None = None,
    ) -> list[CAPAlert]:
        """Fetch the country feed and return a ``CAPAlert`` per warning.

        ``now`` is the clock the MeteoFrance episode merge uses to decide which
        forecast days have finished; injected so tests never race the wall
        clock. Extra keyword with a default, so the ``AlertProvider`` protocol
        is still satisfied.
        """
        country = (config.get(CONF_COUNTRY, "") or "").upper()
        if not country:
            raise UpdateFailed("MeteoAlarm: country not configured")
        slug = METEOALARM_COUNTRY_SLUGS.get(country)
        if slug is None:
            raise UpdateFailed(f"MeteoAlarm: unsupported country {country}")

        url = METEOALARM_FEED_URL.format(country=slug)
        async with session.get(url) as resp:
            if resp.status != 200:
                raise UpdateFailed(f"MeteoAlarm {country}: HTTP {resp.status}")
            try:
                payload = await resp.json(content_type=None)
            except (aiohttp.ContentTypeError, ValueError) as err:
                raise UpdateFailed(f"MeteoAlarm: invalid JSON: {err}") from err

        warnings = payload.get("warnings") if isinstance(payload, dict) else None
        if not isinstance(warnings, list):
            raise UpdateFailed("MeteoAlarm: feed missing 'warnings' array")

        preferred_prefix = _lang_prefix(options.get(CONF_LANGUAGE, "")) or "en"

        gps_loc = config.get(CONF_GPS_LOC)
        regions = config.get(CONF_REGIONS)
        # Region-picker mode only: the configured scope MeteoFrance bulletins
        # are exploded against (see ``_explode_mf_by_region``).
        wanted = (
            {str(r) for r in regions if r} if regions and not gps_loc else set[str]()
        )

        alerts: list[CAPAlert] = []
        for warning in warnings:
            if not isinstance(warning, dict):
                continue
            alert = _warning_to_alert(warning, preferred_prefix)
            if alert is None:
                continue
            info = (
                _primary_info(warning, preferred_prefix)
                if wanted and alert.sender == _MF_SENDER
                else None
            )
            if info is not None:
                alerts.extend(_explode_mf_by_region(alert, info, wanted))
            else:
                alerts.append(alert)

        # Before any mode filter, so all three modes are equally protected.
        alerts = _drop_mf_non_warnings(alerts)

        if gps_loc:
            # Fully-mobile mode (country resolved from a source entity) can
            # roam into countries that publish partial or no per-warning
            # geometry; there, warnings without geometry are kept rather
            # than dropped. Explicit fixed-country GPS/tracker modes still
            # filter strictly and fail loud on zero polygons.
            mobile = CONF_COUNTRY_ENTITY in config
            alerts = self._filter_by_polygon(
                alerts, gps_loc, country, keep_polygonless=mobile
            )
        elif regions:
            alerts = self._filter_by_regions(alerts, regions)

        return _merge_meteofrance_episodes(
            alerts, now=now or datetime.now(timezone.utc)
        )

    @staticmethod
    def _filter_by_polygon(
        alerts: list[CAPAlert],
        gps_loc: str,
        country: str,
        *,
        keep_polygonless: bool = False,
    ) -> list[CAPAlert]:
        """Keep alerts whose geometry contains the configured GPS point.

        When the page has alerts but none carry polygons, the country does
        not publish per-warning geometry. By default this fails loud — the
        user explicitly chose GPS filtering for a known country — and
        warnings without geometry are dropped when others carry it. In
        fully-mobile mode (``keep_polygonless``) warnings without usable
        geometry are kept instead: roaming into a country with partial or
        absent geometry degrades to broader coverage rather than silently
        dropped warnings or an unavailable entry.
        """
        if not alerts:
            return []
        with_polygons = [a for a in alerts if _alert_polygons(a)]
        if not with_polygons:
            if keep_polygonless:
                _LOGGER.info(
                    "MeteoAlarm %s: no per-warning geometry; keeping all %d warnings",
                    country,
                    len(alerts),
                )
                return alerts
            raise UpdateFailed(
                f"MeteoAlarm {country}: GPS filter requested but "
                f"{len(alerts)} warnings carry no polygons; this country "
                "does not publish per-warning geometry — use region-picker "
                "mode instead"
            )
        gps = _parse_gps(gps_loc)
        if gps is None:
            raise UpdateFailed(
                f"MeteoAlarm {country}: invalid GPS coordinates {gps_loc!r}"
            )
        lat, lon = gps
        kept: list[CAPAlert] = []
        for alert in alerts:
            rings = _alert_polygons(alert)
            if not rings:
                if keep_polygonless:
                    kept.append(alert)
                continue
            if any(_point_in_polygon(lat, lon, ring) for ring in rings):
                kept.append(alert)
        return kept

    @staticmethod
    def _filter_by_regions(alerts: list[CAPAlert], regions: Any) -> list[CAPAlert]:
        """Keep alerts whose resolved region codes intersect ``regions``.

        Region codes are resolved from each alert's ``geocodes`` container via
        the shared scheme-priority resolver, so the values compared here are
        the same scheme the region picker offered (see ``_region_pairs``).

        Pure filtering for every sender: MeteoFrance identity is owned by
        ``_merge_meteofrance_episodes``, which runs after this and recomputes
        the id from the exploded single-department scope.
        """
        wanted = {str(r) for r in regions if r}
        if not wanted:
            return []
        kept: list[CAPAlert] = []
        for a in alerts:
            descs = tuple(d.strip() for d in a.area_desc.split(",") if d.strip())
            resolved = _region_codes(a.geocodes, descs)
            if wanted.intersection(resolved):
                kept.append(a)
        return kept
