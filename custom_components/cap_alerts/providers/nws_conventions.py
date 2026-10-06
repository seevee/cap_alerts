"""NWS conventions: marine UGC prefixes, VTEC severity, the re-issue collapse.

One source's interpretive rules, exposed to the provider as ``CONVENTIONS``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from types import MappingProxyType

from ..conventions import (
    PipelineStage,
    SourceConventions,
    StageContext,
    episode_id,
    parse_instant,
)
from ..model import CAPAlert

# --- icons ------------------------------------------------------------------

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


def nws_icon(alert: CAPAlert, event: str) -> str | None:
    """Exact match on the NWS event name; None lets the shared sweep run."""
    return _NWS_EVENT_ICONS.get(event)


# --- severity ---------------------------------------------------------------

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

# VTEC significance → severity tier (NWS).
_VTEC_SIG_SEVERITY = {
    "W": "severe",  # Warning
    "A": "moderate",  # Watch
    "Y": "minor",  # Advisory
    "S": "unknown",  # Statement
}

# Phenomena codes that escalate a Warning to "extreme".
_VTEC_EXTREME_PHENOMENA = {"TO", "EW"}  # Tornado, Extreme Wind


def nws_vtec_severity(alert: CAPAlert) -> str | None:
    """Derive severity from VTEC codes (authoritative for NWS)."""
    sig = alert.vtec_significance
    if not sig:
        return None
    # Tornado/Extreme Wind warnings are "extreme", not just "severe"
    if sig == "W" and alert.vtec_phenomena in _VTEC_EXTREME_PHENOMENA:
        return "extreme"
    return _VTEC_SIG_SEVERITY.get(sig, "unknown")


# ---------------------------------------------------------------------------
# NWS re-issue collapse
# ---------------------------------------------------------------------------
#
# A VTEC string is a supersession protocol: it carries a stable event identity
# across every revision, which is what ``_compute_alert_id`` keys on. NWS
# products published *without* one have no such protocol — each re-transmission
# is a fresh ``messageType: Alert`` with an empty ``<references>`` and a new
# ``urn:oid:`` identifier, and the message it replaces stays active until its
# own ``expires``. Hashing that identifier mints an entity per transmission, so
# one running advisory reads as a pile of duplicates.
#
# Measured on the national feed 2026-08-06: 23 of 65 active non-VTEC alerts
# were surplus re-issues (35%), the deepest cluster six messages of one Air
# Quality Alert, and ``<references>`` was populated on none of the 65 — so the
# store's reference-based supersession path cannot see them either.


def _nws_parameter(alert: CAPAlert, name: str) -> str:
    """First value of an NWS ``parameters`` entry, whose values are lists.

    ``CAPAlert.parameters`` is an untyped dict carrying each provider's native
    shape; NWS publishes ``{"AWIPSidentifier": ["AQABOU"]}`` where MeteoAlarm
    publishes a bare string, so both are accepted here.
    """
    raw = (alert.parameters or {}).get(name)
    if isinstance(raw, str):
        return raw
    if isinstance(raw, (list, tuple)) and raw:
        return str(raw[0])
    return ""


def _nws_reissue_key(alert: CAPAlert) -> tuple[str, str, tuple[str, ...]] | None:
    """Content key for a re-issuable NWS product, or ``None`` to leave it alone.

    ``AWIPSidentifier`` names the product *and* the issuing office (``AQABOU``
    = Air Quality Alert out of Boulder), which is precisely the slot a
    re-transmission supersedes. ``event`` guards one office publishing two
    hazards under a single product, and the UGC set keeps genuinely concurrent
    advisories apart — the sampled feed carried two live ``AQABOU`` groups over
    different county sets, which must stay two entities.

    Returns ``None`` — meaning "keep the per-message identity" — for anything
    VTEC-bearing, and for a degenerate key naming neither product nor area.
    Refusing to collapse on an unknown is the fail-open direction: a duplicate
    entity is a nuisance, a silently dropped alert is not.
    """
    if alert.vtec:
        return None
    awips = _nws_parameter(alert, "AWIPSidentifier")
    ugc = tuple(sorted(alert.geocodes.get("UGC", ())))
    if not awips and not ugc:
        return None
    return (awips, alert.event, ugc)


def _reissue_recency(alert: CAPAlert) -> tuple[int, float, str]:
    """Recency ordering for ``max``: a parseable ``sent`` beats an unparseable
    one, ties broken on identifier so the winner is deterministic.

    Deliberately not ``ts_sort_key``, which sorts unparseable values *last* for
    ascending callers — under ``max`` that would hand the group to the one
    message whose timestamp could not be read.
    """
    parsed = parse_instant(alert.sent)
    if parsed is None:
        return (0, 0.0, alert.identifier)
    return (1, parsed.timestamp(), alert.identifier)


def collapse_nws_reissues(alerts: list[CAPAlert], ctx: StageContext) -> list[CAPAlert]:
    """Keep the newest transmission of each non-VTEC NWS product, re-minting its
    id from the content key.

    Both halves are load-bearing. Dropping the older messages alone would still
    churn the entity id on every re-transmission — the failure issue #37
    documents for MeteoFrance, where an id that rolls over breaks any automation
    or card referencing it. Re-minting alone would collapse the group onto one
    id and leave the alert store, which keys incoming alerts by id, to pick the
    winner by list order; NWS returns newest-first, so the *oldest* message
    would win.

    The newest by ``sent`` supplies the record wholesale rather than blending
    fields, for the reason ``_merge_run`` gives: a blended record can contradict
    itself. It is the right choice operationally too — a re-transmission
    restates the currently-running advisory, so its window is the live one.
    Verified across every multi-message cluster in the national sample: the
    newest member was already in effect in all of them, never pending.

    No window component enters the key, which is what retires a finished-but-
    unexpired advisory. NWS stamps these with an ``expires`` well past the
    window they describe (the sampled Denver cluster carried a Wed→Thu advisory
    expiring Friday 09:00), so keying on the window would keep it alongside the
    live one as a second entity.
    """
    keyed: dict[tuple[str, str, tuple[str, ...]], list[CAPAlert]] = {}
    passthrough: list[CAPAlert] = []
    for alert in alerts:
        key = _nws_reissue_key(alert)
        if key is None:
            passthrough.append(alert)
        else:
            keyed.setdefault(key, []).append(alert)

    collapsed: list[CAPAlert] = []
    for (awips, event, ugc), members in keyed.items():
        newest = max(members, key=_reissue_recency)
        collapsed.append(
            replace(
                newest,
                id=episode_id(
                    newest.sender,
                    f"{awips}|{event}",
                    ugc,
                    "",
                    fallback=newest.identifier or newest.id,
                ),
            )
        )
    return passthrough + collapsed


NWS_REISSUE_STAGES: tuple[PipelineStage, ...] = (
    PipelineStage("merge", collapse_nws_reissues),
)


# The collapse is a stage rather than an ``identity`` hook because a
# per-alert rewrite cannot also discard the messages it superseded, and
# leaving that to the store's id-keyed last-write-wins would pick the
# oldest of them off a newest-first feed.
NWS_CONVENTIONS = SourceConventions(
    marine_code_prefixes=NWS_MARINE_UGC_PREFIXES,
    severity=nws_vtec_severity,
    icon=nws_icon,
    stages=NWS_REISSUE_STAGES,
    # NWSProvider._fetch_cancellations goes and gets the VTEC CAN
    # products the active endpoint never carries.
    discovers_terminations=True,
)


# What this provider declares, keyed as ``conventions_for`` resolves it.
CONVENTIONS: Mapping[str, SourceConventions] = MappingProxyType(
    {
        "nws": NWS_CONVENTIONS,
    }
)
