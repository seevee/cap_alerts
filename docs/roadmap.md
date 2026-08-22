# Roadmap

Future work tracked for context — none of these are in scope until prioritized.

This file holds **design work that has no issue**: shape questions, deferred
refactors, and cleanups pinned to an external date. Anything with a concrete
reporter or a reproducible bug lives on the issue tracker instead, and anything
already shipped lives in [`architecture.md`](architecture.md).

---

## Declared content identity (issue #116)

Tracked in full on [#116](https://github.com/seevee/cap_alerts/issues/116);
kept here because it is the one open item that shapes the convention table.

The original entry here proposed merge-on-content-match as a fallback for
sources that replace a whole message without a stable identifier. That fallback
has now shipped twice, both times as a bespoke per-sender rule: `#37`
(MeteoFrance, `meteofrance_identity`) and `#115` (NWS non-VTEC, a re-issue
collapse stage). Two instances is enough to say the general shape is a declared
extractor triple — event key, region codes, optional window key — composed over
the provider-neutral `episode_id()`.

Two things block a straight table row, and both are worth remembering before
anyone reaches for this:

- The `identity` and `keep` hooks are consumed only by `providers/meteoalarm.py`.
  NWS, ECCC and WMO never call them, which is why #115 had to ship as a stage.
  Wiring them everywhere touches entity ids, so it renames entities in live
  dashboards.
- A per-alert hook can't discard. Collapsing N messages onto one id leaves the
  store's id-keyed last-write-wins to pick the survivor, and NWS returns
  newest-first, so the *oldest* message would win. Re-minting and discarding
  have to happen together.

**Deferred alternative:** a user-facing identity strategy (`stable_event` vs.
`message_strict`) was considered and isn't wanted. Every shipped source now
either has a supersession protocol we honor or a declared content key, so the
choice belongs in the table per source rather than in a toggle the user has to
reason about.

---

## Provider capability declarations — what the convention table left behind

The original `ProviderCapabilities` dataclass proposal is superseded:
`conventions.py::SourceConventions` is that mechanism, keyed by source rather
than by provider, and `classifies_marine` is literally a derived capability
flag (the options flow asks it instead of re-listing supporting providers).

What the table hasn't absorbed is the residue of `provider == "…"` branches:

- `icons.py` — MeteoAlarm classifies on `awareness_type`, NWS on a full-event
  table, everyone else on substrings. This is a *deliberate* branch documented
  in architecture.md → *Icon policy*, so the question is whether it reads better
  as a declared classifier per source.
- `coordinator.py::_resolve_config` — the three-way `language: auto`
  resolution (2-letter prefix for MeteoAlarm, full tag for WMO, EN/FR for
  ECCC). A `language_granularity` field would carry this.
- `flows/<provider>.py` — per-provider option schemas. Genuinely
  provider-shaped UI, probably not table material.

Worth doing only when a new provider makes one of these branches a three-way,
not as a standalone refactor.

---

## Parameter extraction policy

Providers copy `<parameter>` blocks into `CAPAlert.parameters` wholesale (ECCC
and WMO also merge `<eventCode>` in), and nothing bounds what a source can put
there. It no longer threatens the recorder: `AlertEntity` declares `parameters`
unrecorded (#150), so the ceiling is measured without it and the payload budget
holds regardless of how much a feed sends.

What's left is a display and websocket concern — the block still rides on the
state object, so a source that publishes 40 KB of parameters ships it to every
connected frontend on every poll. Needs either a provider-declared allowlist of
keys to retain, or a size cap that drops low-priority parameters when exceeded.
The allowlist is the better fit for the convention table; the size cap is the one
that protects against a source we haven't sampled.

---

## Language as a setup-time choice

`CONF_LANGUAGE` is an options-flow setting, resolved to a concrete tag by the
coordinator and consumed by each provider when it selects an `<info>` block.

The original argument for promoting it to the config flow was that language
decides *which* alerts you see on bilingual providers. That's no longer true:
every provider now resolves language at info-block selection, ECCC yields one
info per declared language and merges the siblings, and #80 stopped MeteoAlarm
offering each region once per feed language.

The residue is narrower but real: the **region picker labels** during initial
setup come from `hass.config.language`, because the language option doesn't
exist yet at that point. `_picker_language()` already threads the configured
option through on reconfigure, so only first-run setup is affected. Someone
running HA in English who wants French region labels has to set up, change the
option, then reconfigure.

---

## Full multi-info CAP model

Phase B of the bilingual work. Current shape uses flat `headline` /
`description` / `instruction` with `_alt` sibling fields — sufficient for two
languages. The nested alternative:

```python
@dataclass(frozen=True, slots=True)
class CAPInfo:
    language: str
    headline: str = ""
    description: str = ""
    instruction: str | None = None
    # ... other per-info fields

class CAPAlert:
    # ... identity, classification, timestamps, geography ...
    infos: tuple[CAPInfo, ...] = ()
    # flat fields stay as a "resolved" view for the preferred language
    headline: str = ""  # resolved from infos by coordinator
```

Nothing shipped has forced this, and two candidates that looked like they would
have both been solved without it:

- **WMO.** SWIC bodies are frequently multilingual (46 of the 110 sources
  sampled 2026-08-03), but two languages fit the flat `_alt`-sibling shape, and
  the provider selects one `<info>` plus one alternate (#59).
- **ECCC (#45).** One `<info>` per area group, which is the shape that should
  force nesting — but `_select_region_infos` picks the *region-matching* block
  per language and merges those siblings, so one document still yields one
  alert.

What would actually force it is a document where the user's location matches
several area groups at once and all of them need presenting, or where blocks
differ in more than language. Breaking internal change; best done alongside the
provider that needs it. The card adapter and attribute shape shouldn't need to
change if resolved flat fields are kept.

---

## Periodic WMO source revalidation

`const.py::WMO_UNMIRRORED_SOURCES` is a point-in-time curation (verified
2026-05-24) of registered SWIC sources that 404 on the `severeweather.wmo.int`
mirror. The config-flow dropdown excludes them so users aren't offered broken
sources, but the set goes stale: a newly-mirrored source stays hidden until the
constant is updated by hand (`custom_value` entry is the escape hatch).

**Proposed**: a low-frequency background revalidation (e.g. HEAD-probe the
mirror for registry sources and cache the reachable set on `hass.data` with a
long TTL) so the exclude set self-heals without a code change. Must stay off the
coordinator poll path — it's a config-flow-only concern.

---

## NAAD 48-hour repository as a cold-start source

ECCC streaming shipped in 0.2.0 and its design lives in
[`architecture.md`](architecture.md) → *ECCC — NAAD streaming*. The NAADS
**48-hour HTTP short-term repository** is now in use for the gap it can close
(#164): every heartbeat lists the last ten alerts as `(sender, identifier,
sent)` triples, the repository serves each by a URL built from that triple,
and the coordinator fetches whatever it has not seen. That covers a reconnect
window without depending on the GeoRSS index, which omits live alerts in every
sample taken since 2026-07-30.

What stays open is **cold start**. Setup and the periodic resync still go
through the GeoRSS feed, so the `_fetch_one_feed` truncation guard and the
partial-feed tolerance below still matter, and an alert the alertready index
omitted before setup is only recovered if it is still in the heartbeat's
last-ten window. The repository cannot enumerate (directory paths 404, the
filename encodes a `sent` you only learn from an index), and the one source
that can — `alertsarchive.pelmorex.com`, a `POST datepicker=` HTML listing —
sits on the domain the NAAD decoupling exists to move off, with no successor
staged. Persisting the live doc set across restarts is the fallback design if
the archive goes with the sunset; it has to bound what it writes, since
nothing in the integration persists today and `GeometryStore` is kept
unpersisted over SD-card write amplification.

---

## Remove the pelmorex NAAD host (cleanup)

**When:** after the legacy `rss.naad-adna.pelmorex.com` host retires (~late Sept
2026), or sooner if Pelmorex closes SR #46534 and `rss.alertready.ca`'s omission
set clears.

The GeoRSS host union (#38) fetches both NAAD hosts and unions their entries
because alertready persistently drops live alerts pelmorex carries, while
pelmorex retains a shorter window. Continuous sampling since 2026-07-30 has
found a gap in every sample, so as of this writing the union is still the only
thing making the feed complete.

Until then the sunset repairs (#163, `issues.py`) cover the two configurations
that depend on the host: streaming off, and `feed_source: pelmorex`. Each gets
a repair card with a confirm flow that writes the recommended option.

Once pelmorex is gone it will simply fail every poll, which the union tolerates.
Removing it means: drop `NAAD_FEED_PELMOREX` and the `pelmorex` value from
`NAAD_FEED_HOSTS` / `NAAD_FEED_UNION_ORDER` in `providers/eccc.py`, drop
`pelmorex` from the `CONF_FEED_SOURCE` selector in `flows/eccc.py` (leaving
`auto` = alertready-only), and update the `feed_source` strings. If alertready's
omission set has not cleared by then, escalate before removing. Dropping the
`pelmorex` value retires `ISSUE_ECCC_FEED_SOURCE_PELMOREX` with it (the
streaming-off repair stays, since the thin index is the reason it exists).

---

## Partial-feed tolerance (authoritative vs. best-effort diffing)

The ECCC feed guard (`eccc.py::_fetch_one_feed`) is all-or-nothing: a body that
doesn't arrive complete (non-empty, ending in `</feed>`) is discarded and the
poll fails. This is deliberately **fail-closed**, because `AlertStore.process`
treats any tracked alert *absent* from a poll as ended — so salvaging a
truncated feed would fire false `cap_alert_removed` events, i.e. a false
"all-clear," the worst failure mode for a weather-alert system. Discarding
instead keeps the last-known-good snapshot (the coordinator retains `data` on
`UpdateFailed`; `_sync_alert_entities` computes an empty removal set, so no
alert entities are deleted) at the cost of the poll going stale.

The limitation this leaves: a partial feed still contains valid, parseable
entries, and on a **cold start / backfill** there is no prior snapshot to fall
back to — a persistent truncation yields an empty integration
(`ConfigEntryNotReady`), genuinely withholding alerts that *did* arrive.

**Proposed** — make the diff **state-aware** rather than always-authoritative,
by threading a "feed completeness" signal from the provider into the store:

1. Complete feed → authoritative: process adds *and* removals (current
   behavior).
2. Partial feed, steady state → **suppress removals** (absence is "unknown," not
   "ended"); optionally still surface the adds/updates that arrived. An alert
   that genuinely ended lingers until the next complete feed — the safe
   direction.
3. Partial feed, cold start → **salvage** the arrived entries (nothing is
   tracked yet, so there is zero false-clear risk) instead of coming up empty.

Requires: `Provider.async_fetch` (or a richer return type) to report
completeness; `AlertStore.process` to take an `authoritative: bool` gating the
removal branch; the coordinator to pass it through. Streaming ingestion must
apply the same rule — its initial/gap backfill is exactly case 2/3, and it must
fail-closed on an incomplete *steady-state* backfill for the same reason.

**Open decision — entity availability on a failed/partial poll.** Today a failed
poll marks every entity `unavailable` (the #16 contract, pinned by
`test_mobile_lifecycle.py`). That is correct for *location-resolution* failures
(offline tracker / unresolvable country: we don't know where the user is, so we
can't assert their alerts). A pure *transport* failure at a fixed location is
different: the last-known alert set is still valid, so staying available with
stale data — staleness surfaced by the last-updated sensor — is arguably safer
than graying out an active warning. Reconciling the two needs
**failure-type-aware** availability, not a blanket override, which would reverse
#16 for the mobile case.
