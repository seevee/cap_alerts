# Roadmap

Future work tracked for context — none of these are in scope until prioritized.

For the design of shipped behavior, see [`architecture.md`](architecture.md).

---

## Idempotent merge logic (AlertStore)

The current store diffs by ID presence — if a provider recomputes IDs differently across message replacements, it causes entity churn (remove + re-create). The shipped WMO provider sidesteps this by hashing the sender-scoped CAP `<identifier>` (stable across Update/Cancel) and resolving revision chains, so it does not currently churn. The fallback below is still wanted for sources that do full-message replacement *without* a stable identifier or explicit Cancel.

**Proposed**: merge-on-content-match as a fallback when IDs change but the event is the same (same `event` + `area_desc` + close `sent` timestamp).

---

## Provider capability flags

Declare what each provider supports so normalization can branch on capabilities rather than `if provider == "nws"` checks:

```python
@dataclass(frozen=True)
class ProviderCapabilities:
    vtec: bool = False
    multi_language: bool = False
    geometry: Literal["none", "optional", "primary"] = "optional"
    lifecycle: Literal["explicit", "implicit"] = "explicit"
```

Lets the normalization layer stay provider-agnostic as the provider set grows.

---

## Identity strategy config

User-facing option:

- `stable_event` (current default) — one entity per logical event, survives Update/Cancel. Best for automations.
- `message_strict` — one entity per CAP message, exactly what the provider emitted. Best for accuracy-focused users.

Now that WMO has landed (it hashes the sender-scoped CAP `<identifier>`, so `stable_event` holds), this is most relevant for a future source with weaker lifecycle guarantees where `stable_event` collapsing is harder to justify.

---

## Parameter extraction policy

Selective extraction of `<parameter>` fields with size budgeting. WMO feeds can have heavy parameter usage that risks the 16 KB attribute limit — the exact problem this integration was created to avoid. Needs either:

- a provider-declared allowlist of parameter keys to retain, or
- a per-alert size cap that drops low-priority parameters when exceeded.

---

## Integration-level language selection

Promote `CONF_LANGUAGE` from an options-flow setting to a more prominent concept — possibly to the config flow (identity-level) since language affects *which alerts you see* for bilingual providers, not just how they're presented.

---

## Full multi-info CAP model

Phase B of the bilingual work. Current shape uses flat `headline` / `description` / `instruction` with `_alt` sibling fields — sufficient for two languages. For WMO providers with multiple `<info>` blocks (each with its own `xml:lang` and non-duplicated content), refactor to:

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

Breaking internal change — best done alongside a new provider that actually needs it. The shipped WMO provider did *not* trigger this: SWIC bodies are frequently multilingual (46 of the 110 sources sampled 2026-08-03), but two languages fit the flat `_alt`-sibling shape, and the provider selects one `<info>` block plus one alternate (issue #59). What would force the nested shape is a source emitting one `<info>` *per area group* — `ca-aema-xx` does, and so does ECCC (#45). The card adapter and attribute shape shouldn't need to change if resolved flat fields are kept.

---

## Periodic WMO source revalidation

`const.py::WMO_UNMIRRORED_SOURCES` is a point-in-time curation (verified 2026-05-24) of the registered SWIC sources that 404 on the `severeweather.wmo.int` mirror. The config-flow dropdown excludes them so users aren't offered broken sources, but the set goes stale: a newly-mirrored source stays hidden until the constant is updated by hand (`custom_value` entry is the escape hatch in the meantime).

**Proposed**: a low-frequency background revalidation (e.g. HEAD-probe the mirror for registry sources and cache the reachable set on `hass.data` with a long TTL) so the exclude set self-heals without a code change. Must stay off the coordinator poll path — it's a config-flow-only concern.

---

## ECCC NAAD streaming socket (push ingestion)

**Status: shipped in 0.2.0** as the ECCC default (`CONF_STREAMING` options toggle, default on, GeoRSS-polling escape hatch). The full design — endpoint, `NAADStreamClient` transport, coordinator ingest, availability semantics, backfill throttling, observability — lives in [`architecture.md`](architecture.md) → *ECCC — NAAD streaming*.

Ends still open on this roadmap:

- The NAADS **48-hour HTTP short-term repository** (and alertsarchive.pelmorex.com) is unused as a gap-backfill source — all backfill goes through the GeoRSS feed today, so the `_fetch_one_feed` truncation guard and the partial-feed tolerance below still matter.
- Pelmorex host removal (next section) and the deployment-scaling numbers parked in the RFC incident follow-ups pair with this work.

---

## Remove the pelmorex NAAD host (cleanup)

**When:** after the legacy `rss.naad-adna.pelmorex.com` host retires (~late Sept 2026), or sooner if Pelmorex closes SR #46534 and `rss.alertready.ca`'s omission set clears.

The GeoRSS host union (issue #38, `fix/eccc-naad-feed-union`) fetches both NAAD hosts and unions their entries because alertready persistently drops ~10 live alerts pelmorex carries, while pelmorex retains a shorter window. Once pelmorex is gone (it will simply fail every poll, which the union tolerates), remove it: drop `NAAD_FEED_PELMOREX` and the `pelmorex` value from `NAAD_FEED_HOSTS` / `NAAD_FEED_UNION_ORDER` in `providers/eccc.py`, drop `pelmorex` from the `CONF_FEED_SOURCE` selector in `config_flow.py` (leaving `auto` = alertready-only), and update the `feed_source` strings. If alertready's omission set has not cleared by then, escalate before removing — the union is the only thing making the feed complete.

---

## Partial-feed tolerance (authoritative vs. best-effort diffing)

The ECCC feed guard (`eccc.py::_fetch_one_feed`) is all-or-nothing: a body that doesn't arrive complete (non-empty, ending in `</feed>`) is discarded and the poll fails. This is deliberately **fail-closed**, because `AlertStore.process` treats any tracked alert *absent* from a poll as ended — so salvaging a truncated (tail-missing) feed would fire false `cap_alert_removed` events, i.e. a false "all-clear," the worst failure mode for a weather-alert system. Discarding instead keeps the last-known-good snapshot (the coordinator retains `data` on `UpdateFailed`; `_sync_alert_entities` computes an empty removal set, so no alert entities are deleted) at the cost of the poll going stale until a clean fetch.

The limitation this leaves: a partial feed still contains valid, parseable entries, and on a **cold start / backfill** there is no prior snapshot to fall back to — a persistent truncation yields an empty integration (`ConfigEntryNotReady`), genuinely withholding alerts that *did* arrive in the partial body.

**Proposed** — make the diff **state-aware** rather than always-authoritative, by threading a "feed completeness" signal from the provider into the store:

1. Complete feed → authoritative: process adds *and* removals (current behavior).
2. Partial feed, steady state → **suppress removals** (absence is "unknown," not "ended"); optionally still surface the adds/updates that arrived. An alert that genuinely ended lingers until the next complete feed — the safe direction.
3. Partial feed, cold start → **salvage** the arrived entries (nothing is tracked yet, so there is zero false-clear risk) instead of coming up empty.

Requires: `Provider.async_fetch` (or a richer return type) to report completeness/partiality; `AlertStore.process` to take an `authoritative: bool` gating the removal branch; the coordinator to pass it through. The streaming-socket ingestion above must apply the same rule — its initial/gap backfill is exactly case 2/3, and it must fail-closed on an incomplete *steady-state* backfill for the same false-clear reason.

**Open decision — entity availability on a failed/partial poll.** Today a failed poll marks every entity `unavailable` (the issue #16 contract, pinned by `test_mobile_lifecycle.py`). That is correct for *location-resolution* failures (offline tracker / unresolvable country: we don't know where the user is, so we can't assert their alerts). A pure *transport* failure at a fixed location (truncated ECCC feed) is different: the last-known alert set is still valid, so staying available with stale data — staleness surfaced by the last-updated sensor — is arguably safer than graying out an active warning. Reconciling the two needs **failure-type-aware** availability (distinguish "location unresolved" from "fetch failed"), not a blanket override, which would reverse #16 for the mobile case.
