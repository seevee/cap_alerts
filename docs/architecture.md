# Architecture

Design-level reference for `cap_alerts`. The README covers entity model, file layout, data flow, and key design decisions; this document captures the rationale behind choices that aren't self-evident from the code, plus field-mapping tables.

For in-progress items and ideas not yet landed, see [`roadmap.md`](roadmap.md).

---

## Alert Identity

The `id` field on `CAPAlert` must remain **stable across the lifecycle of a single weather event** — not just a single API message. When NWS issues an Update or Cancel for an existing warning, the new message gets a new URI (`@id`). Hashing the URI would make the entity vanish and a new one spawn, breaking Home Assistant state history.

### Strategy: lifecycle-aware hashing

**NWS (VTEC-bearing alerts)** — Hash the VTEC event identity tuple: `office.phenomena.significance.tracking.year`. A VTEC like `/O.NEW.KILN.SV.W.0001.250412T1430Z-250412T1530Z/` encodes these components plus an action code and time window. The action code (`NEW`, `CON`, `CAN`, `EXP`) changes across the event lifecycle, and the time window changes on extensions (`EXT`). Both must be excluded from the hash; tracking number alone disambiguates concurrent events of the same type from the same office.

```
VTEC: /O.NEW.KILN.SV.W.0001.250412T1430Z-250412T1530Z/
  → stable identity: KILN.SV.W.0001.2025
  → id = sha256("KILN.SV.W.0001.2025")[:12]
```

**NWS (non-VTEC alerts)** — Some alerts (Air Quality Alerts, Special Weather Statements, certain advisories) lack VTEC. `_compute_alert_id` still falls back to `sha256(url)[:12]`, but that message-level identity is *not* sufficient on its own: these products carry no supersession protocol at all, so NWS re-transmits them as a fresh `messageType: Alert` with a new `urn:oid:` identifier and an empty `<references>`, leaving the message they replace active until its own `expires`. One running advisory therefore spawns an entity per transmission. Measured on the national feed 2026-08-06: 23 of 65 active non-VTEC alerts were surplus re-issues (35%), the deepest cluster six messages of one Air Quality Alert, with `<references>` populated on none of the 65.

`conventions.collapse_nws_reissues` — a `merge` stage on the `"nws"` table entry, run by the provider once pagination completes — collapses each group to its newest transmission and re-mints the id from a content key: `sha256("{sender}|{AWIPSidentifier}|{event}|{UGC set}")[:12]` via the shared `episode_id`. `AWIPSidentifier` names the product *and* the issuing office (`AQABOU` = Air Quality Alert out of Boulder), which is the slot a re-transmission supersedes; the UGC set keeps genuinely concurrent advisories apart. No window enters the key, which is what retires a finished-but-unexpired advisory — NWS stamps these with an `expires` well past the window they describe. VTEC-bearing alerts and degenerate keys (no product, no areas) are passed through untouched, so the collapse never fires on an unknown.

**ECCC** — Stable key is `sha256(sender + sent + primary_CAP-CP_eventCode + polygon_hash)[:12]`. Fields are language-independent: `<sender>` is the issuing office, `<sent>` is the second-precision issue timestamp, the CAP-CP `<eventCode>` value (e.g. `freezing-drizzle`) is a language-independent profile code, and `<area>/<polygon>` is geometric. Urgency is deliberately excluded — urgency shifts between revisions (`Likely` → `Observed`) and would produce different IDs on each update. Two language siblings of the same revision share identical values for all four inputs and therefore hash to the same ID, enabling bilingual merge. Revision chains (NEW → UPDATE → CANCEL) are resolved to the leaf via CAP `<references>` before the key is computed, so only the current revision's key is active.

### Why not just hash the URL?

Message-level identity, not event-level. NWS alerts go through `NEW → CON → CAN` with a different URI at each stage — hashing the URL would churn entities through every phase change. VTEC exists precisely to express event identity across messages.

### Scope of the problem

The `/alerts/active` endpoint only returns current alerts. The failure mode is: entity `_abc123` disappears and entity `_def456` appears, and HA sees them as unrelated. State history for the warning splits across two entity IDs. Lifecycle-aware hashing prevents this.

---

## Entity Identity & Registry Discipline

Implements RFC §2.2.1 (stable entity_id derivation) and §2.5 (registry cleanup).

### entity_id shape

The integration suggests

```
cap_alert_<slug(event)>_<8-hex>
```

where `<8-hex>` is `sha1(unique_id)[:8]`. The hash disambiguates alerts that share an event name (e.g. two concurrent "Severe Thunderstorm Warning" entries from different offices) without relying on Home Assistant's `_2`/`_3` numeric-suffix fallback, which can outlive its source and break history when the originally-suffixed entity is removed.

What actually lands in the registry carries the device name in front:

```
sensor.cap_alerts_nws_cap_alert_tornado_warning_1f0c6a62
```

This is Home Assistant's doing, and the naming makes it easy to miss. `AlertEntity.suggested_object_id` does *not* become the registry's `suggested_object_id`: `entity_platform._async_derive_object_ids` routes an integration-provided value into `object_id_base` instead, and the registry's own contract is that "`suggested_object_id` will not be prefixed with the device name; `object_id_base` will be prefixed with the device name if `has_entity_name` is True". These entities set `has_entity_name`, so the prefix is applied — measured on a live instance, 70 of 70 alert entities carry it, none match the unprefixed shape this document described until 2026-08-12.

The prefix therefore follows the *device*, which users can rename: a device renamed to "CAP Alerts METEOALARM Cher" yields `sensor.cap_alerts_meteoalarm_cher_cap_alert_…`. Dropping `has_entity_name` would restore the unprefixed form and rename every alert entity in every existing install, which is not worth it — the entity_id was never the identity anyway (see below), and `docs/frontend_hints.md` already tells cards not to parse it.

`unique_id` is unchanged (`{entry_id}_{provider}_{alert_id}`), so the recorder links survive any entity_id rename.

### Batched sync

Each coordinator callback computes full `to_add` / `to_remove` sets from `set(coordinator.data)` vs. the tracked dict and then issues a single `async_add_entities(...)` followed by idempotent `async_remove` calls (gated on `ent_reg.async_get(entity_id)` so double-removal is a no-op). This avoids the per-entity churn RFC §2.5 flags as the anti-pattern.

### Restart grace

Registry entries hydrated at startup are seeded into a `_grace_ids` set. On the first sync, any grace ID not yet present in `coordinator.data` is exempted from removal. After that first sync, `_grace_ids` is cleared unconditionally — on the next poll, any still-absent alert is removed through the normal path. This tolerates the RFC §2.5 scenario where HA restarts in the window between an upstream cancellation and the coordinator observing it, at a cost of up to one extra `scan_interval` of lingering entities in genuinely-cleared cases.

---

## Shared Normalization (`normalize.py`)

Providers map API fields to `CAPAlert` as directly as possible — they do not normalize. All cross-provider normalization lives in `normalize.py`, called by the coordinator after fetching. Single source of truth for how raw provider values map to the integration's semantic fields.

### Severity

CAP canonical values: `extreme`, `severe`, `moderate`, `minor`, `unknown`. Provider-aware dispatch because the right signal differs:

- **NWS** — CAP `<severity>` is unreliable. VTEC significance is authoritative: `W` (Warning) → severe, `A` (Watch) → moderate, `Y` (Advisory) → minor, `S` (Statement) → unknown. Specific phenomena override the significance-tier default (e.g. Tornado Warning, Extreme Wind Warning → extreme).
- **ECCC and other CAP-native providers** — CAP `<severity>` is trustworthy; lowercase it. Values outside the canonical set clamp to `unknown`.
- **MeteoAlarm** — when the `awareness_level` parameter (`"N; color; Label"`) is present, the color token drives the canonical severity (`yellow` → moderate, `orange` → severe, `red` → extreme, `green` → unknown). EUMETNET members publish color consistently while CAP `<severity>` is sometimes blank or off-axis, so color is the authoritative signal. Falls back to lower-cased CAP `<severity>` when the parameter is missing or malformed.
- **Future non-CAP providers** (DWD level codes, BoM title inference) — register a new branch in `_normalize_severity` keyed on `provider`.

### Lifecycle filtering is centralized

Providers used to filter `msgType=Cancel` themselves. That's a semantic decision, not a fetch decision — it belongs in the normalization layer. The pipeline is:

```
fetch → normalize (sets phase) → filter_active_alerts (drops Cancel/expired) → store.process
```

Different providers express cancellation differently (NWS: VTEC `CAN` + `msgType=Cancel`; WMO: `msgType=Cancel` plus revision-chain resolution via CAP `<references>`; MeteoAlarm: status/absence from the country feed). ECCC is the exception that proves the rule: it does **not** signal termination through `msgType` at all — an ended alert keeps `msgType=Update` and marks the area group `ended`/`transitioned_out` in the `Alert_Location_Status` CAP parameter (see *ECCC — NAAD Atom mapping → Lifecycle*). The provider reads that into `CAPAlert.lifecycle_status`, which `_compute_phase` maps to `cancel` when `expires` is still in the future and `expired` once it has passed (issue #95) — an announced early end must not be reported as a run to completion, which is the same distinction `store._infer_terminal_phase` already draws for an alert that vanishes silently. Normalization maps all of these to a terminal `phase`, and filtering happens once — not N times in each provider.

### State truncation

The `event` field becomes the entity's `native_value` (state), which HA caps at 255 characters. `normalize.py` truncates with an ellipsis. Relevant for international CAP providers that sometimes put full descriptions in `<event>`.

### Calendar correction (Buddhist-Era years)

Some feeds — notably Thailand's TMD, surfaced via WMO SWIC — emit Buddhist-Era years (Gregorian + 543) in CAP dateTime fields, e.g. `2568-08-05T22:50:00+07:00`. Left uncorrected, `_compute_phase` never expires the alert and the card renders nonsense ("STARTS IN 198034d"). `normalize._gregorian` rewrites **only the year** when it is at or above `MIN_BUDDHIST_ERA_YEAR` (2400) — the Thai solar calendar is Gregorian apart from the era number, so month, day, time, and UTC offset are preserved verbatim. Detection is value-based and provider-agnostic: no Gregorian weather alert carries a year near 2400, while every BE year is 2543+, so the threshold cannot mangle a valid timestamp. The threshold/offset constants live in `const.py` and are reused by the WMO provider to correct the RFC-2822 `cap:expires` in the RSS envelope (the pre-filter runs before normalization, so the body-level fix can't reach it).

---

## Provider Layer (`providers/`)

The `AlertProvider` protocol isolates API-specific logic behind a uniform interface:

```python
class AlertProvider(Protocol):
    @property
    def name(self) -> str: ...
    async def async_fetch(
        self,
        session: aiohttp.ClientSession,
        config: Mapping[str, Any],
        options: Mapping[str, Any],
    ) -> list[CAPAlert]: ...
```

### Coordinator-side resolution

Providers are decoupled from HA internals. The coordinator resolves these **before** calling the provider:

- **Tracker mode** → resolves `device_tracker` entity to lat/lon; provider sees `CONF_GPS_LOC` only.
- **Language `"auto"`** (ECCC) → resolves to `en-CA` or `fr-CA` using `hass.config.language`.

Keeps providers testable without a running HA instance.

### Why a separate layer

1. **Batching varies.** NWS takes multi-zone queries (`?zone=OHC049,OHC035`). ECCC returns a national feed with no server-side filtering. BoM, DWD, MeteoAlarm each differ.
2. **Parsing varies wildly.** GeoJSON features (NWS), Atom XML with CAP extensions (ECCC, MeteoAlarm), flat JSON (BoM), JSONP keyed by warncell (DWD). One coordinator method can't sanely handle all of them.
3. **Testing.** Providers run against recorded API responses without a coordinator or HA.

### Shared CAP parsing (`cap.py`)

ECCC and WMO both carry standard CAP 1.2 documents inside different envelopes (Atom vs RSS), so the CAP body parser is factored into `providers/cap.py` — a provider-neutral module exposing the `CAPDoc` / `CAPInfoDoc` containers, `parse_cap_alert` (namespace-agnostic), and `resolve_chain_leaves`. It depends on nothing else in the package — providers import the parser, never each other — so a third CAP-based provider reuses it without touching ECCC. Envelope-specific concerns (Atom pre-filtering and bilingual merge in `eccc.py`, RSS link extraction and expiry pre-filter in `wmo.py`, event-name recovery) stay in their respective provider modules.

### Area geocodes

`CAPAlert.geocodes` is the **complete** area-geocode surface for every provider: a
`{scheme: (codes…)}` container keyed by the raw CAP `valueName` (or raw GeoJSON geocode
key for NWS) exactly as the feed publishes it — `UGC`, `SAME`, `EMMA_ID`, `NUTS3`,
`layer:EC-MSC-SMC:1.0:CLC`, `profile:CAP-CP:Location:0.3`, whatever a source invents next.
Raw keys are used deliberately: a normalization table would need editing per source and
can mislabel a scheme (an `EMMA_ID` once shipped as `geocode_same` — issue #24), whereas a
raw key cannot lie about its origin.

Providers build the container with `model.geocodes_from()` — the one funnel, which
de-duplicates values per scheme order-preserving (a value repeated across `<area>` blocks
is one code), drops empty schemes and values, and returns an immutable mapping.
`providers/cap.py` de-duplicates at parse time too, so `CAPInfoDoc.geocodes` is already
clean for its non-model consumers (ECCC province matching, marine detection). Serialization
is sparse: `geocodes` is omitted entirely when empty.

Well-known schemes are additionally **promoted** to flat `geocode_*` attributes, declared
once in `model.GEOCODE_SCHEME_ALIASES` and derived as read-only properties — never stored,
so the container stays the single source of truth:

| Alias | Scheme(s) | Consumer |
| --- | --- | --- |
| `geocode_ugc` | `UGC` | `weather_alerts_card` zone filter |
| `geocode_same` | `SAME` | `weather_alerts_card` zone filter |
| `geocode_clc` | `layer:EC-MSC-SMC:1.0:CLC` | ECCC marine detection (`00…` = water zone) |
| `geocode_sgc` | `profile:CAP-CP:Location:0.3` | visibility into what ECCC province filtering matches |

Promotion policy: **a new scheme needs no model change** — it lands in `geocodes` for free.
An alias is only added when a scheme has a named consumer, which is what keeps "add a
scheme" from meaning "add a field". Each alias maps to an *ordered accept-list* of
`valueName`s (first non-empty wins) so a source bumping its scheme version
(`…:1.0:CLC` → `…:1.1:CLC`) costs no provider edit.

`is_marine` is **not** read back off the container — each provider computes it locally
before constructing the alert (NWS from UGC + zone codes, ECCC from the CLC prefix), so
the marine filter does not depend on promotion.

### Geocode-prefix filter (issue #73)

An opt-in, **provider-neutral** narrowing keyed on the container above:
`geocode_prefixes` (options flow, `list[str]`, absent by default) keeps only alerts
carrying an area code that starts with one of the configured prefixes. It runs in
`coordinator._apply` beside the marine filter — normalize → marine → geocode → geometry →
store — so it applies to every provider and to every ingestion path (poll, stream, backfill)
identically. It is a *layer*, not a mode: it narrows whatever the entry's location filter
already returned rather than replacing it.

Motivating case: WMO SWIC sources that publish no per-alert geometry have exactly one
usable location mode, country-wide. `cn-cma-xx` (China, CMA) is the confirmed one — 0 of 60
sampled bodies carried a `<polygon>`, so GPS mode tripped the fail-loud guard on every poll
and the entry never initialized, while country-wide yielded 234 active alerts for a single
entry (enough to destabilize the frontend: *"Client unable to keep up with pending messages"*).
The same source carries the `zh-CN` `<info>` blocks issues #59/#72 exist to select, so
Chinese-language users were forced into that mode. Measured 2026-08-04, prefixes reduce the
234 to 1 (`11`, Beijing), 4 (`31`, Shanghai), 9 (`44`, Guangdong), 21 (`37`, Shandong),
28 (`13`, Hebei).

Design points:

- **Prefix, not exact match.** Area codes are hierarchical, so a prefix is a scope:
  `13` = Hebei, `1307` = Zhangjiakou, `130709000000` = Chongli. Exact matching would make
  a user enumerate every county code — 28 of them for Hebei alone.
- **Scheme-agnostic.** There is no cross-provider scheme-priority registry to resolve
  "the" code from (MeteoAlarm's is country-scoped; WMO sources publish arbitrary schemes —
  CMA's `CPEAS Geographic Code` appears in no existing table), so every value under every
  `valueName` is compared. A colliding prefix over-matches — keeping extra alerts — rather
  than dropping wanted ones.
- **Variable code lengths.** Of 488 sampled CMA codes, 481 were 12 characters and 7 were 6
  (Chongqing districts). Matching is a plain `startswith` with no zero-padding in either
  direction; the consequence — a pasted full-length code will not match a shorter sibling —
  is documented in the field description rather than worked around, and locked by a test.
- **Fail-loud only on a capability failure.** `UpdateFailed` when the feed returned alerts
  but *none* carry any geocode, the exact parallel of "this source publishes no per-alert
  geometry" in the GPS filters. Zero *matches* is not a failure: "no alerts in my area" is
  the normal steady state, and failing there would leave the entry unavailable most of the
  time, inverting what unavailability means. A typo'd prefix instead surfaces as a one-shot
  `WARNING` (naming codes the feed actually publishes, re-armed on the first match), the
  same pattern as `_tracker_resolve_warned`.
- **Free-text, not a picker.** Building a picker would need a full CAP-body sweep (~500
  fetches for CMA) to enumerate areas, since the RSS envelope carries no geocode.
  Unacceptable latency inside a config flow. Worth revisiting if SWIC ever exposes an area
  index.

### Error contract

- `UpdateFailed` for transient errors (network, 5xx, parse issues). HA handles retry.
- `ConfigEntryError` for permanent misconfig (invalid zone, unknown province).

---

## NWS — GeoJSON mapping

**API**: `https://api.weather.gov/alerts/active` — GeoJSON FeatureCollection.

**Edge cases**:
- NWS occasionally returns `200 OK` with a Problem object instead of a FeatureCollection. Validate `data.get("type") == "FeatureCollection"` before parsing.
- Pagination via `pagination.next` is unlikely for zone-filtered queries; follow up to 5 links as a defensive cap.
- In tracker/GPS mode, round coordinates to 4 decimal places (~11 m) before `?point=` to improve CDN cache hits. Always make the request — alerts change continuously.

**Field mapping**:

| NWS GeoJSON field | CAPAlert field |
|---|---|
| `features[].id` / `properties.id` | `url`, `identifier` |
| `features[].geometry` | `geometry` |
| `properties.event` | `event` |
| `properties.messageType` | `msg_type` |
| `properties.status` | `status` |
| `properties.scope` | `scope` |
| `properties.category` | `category` |
| `properties.urgency` | `urgency` |
| `properties.severity` | `severity` |
| `properties.certainty` | `certainty` |
| `properties.response` | `response_type` |
| `properties.sent` / `effective` / `onset` / `expires` / `ends` | same-named fields |
| `properties.headline` (fallback `parameters.NWSheadline[0]`) | `headline` |
| `properties.description` / `instruction` / `note` / `web` | same-named fields |
| `properties.areaDesc` | `area_desc` |
| `properties.affectedZones` | `affected_zone_uris` → extract codes → `affected_zones` |
| `properties.geocode` (all schemes, keyed as published) | `geocodes`; promoted to `geocode_ugc` / `geocode_same` — see *Area geocodes* |
| `properties.eventCode.NationalWeatherService[0]` | `event_code_nws` |
| `properties.eventCode.SAME[0]` | `event_code_same` |
| `properties.parameters.VTEC` | `vtec` → parsed → `vtec_{office,phenomena,significance,action,tracking}` |
| `properties.sender` / `senderName` | `sender` / `sender_name` |
| `properties.references` / `replacedBy` / `replacedAt` | same-named fields |
| `properties.parameters` | `parameters` (full dict) |

---

## ECCC — NAAD Atom mapping

**API**: two NAAD GeoRSS hosts (national, client-side filtered) — `https://rss.alertready.ca/` (sanctioned) and `https://rss.naad-adna.pelmorex.com/` (legacy). NAAD migrated April 2026 from pelmorex to alertready per the System Governance Council — an intentional domain rebrand off the Pelmorex name, both feeds maintained concurrently for ~6 months (legacy host sunsets ~late Sept 2026). Since 0.2.0 the GeoRSS feed is the **backfill** source: ECCC ingests in real time from the NAAD streaming feed by default (see *NAAD streaming* below), and the GeoRSS poll seeds the set on startup/reconnect + a periodic safety resync. The streaming path is **unaffected** by the host union below — it reads the TCP socket, not GeoRSS.

**Host union (issue #38)**: neither host alone is complete. Simultaneous samples (2026-07-23) showed `rss.alertready.ca` retains ~48 h of history but persistently omits ~10 live `status=Actual` alerts at any moment that pelmorex carries (one OID absent across 110 of 179 probe samples, ~11.5 h); `rss.naad-adna.pelmorex.com` retains only ~13.5 h and drops older alerts alertready still serves. The `CONF_FEED_SOURCE` option (`feed_source`, ECCC options flow, default `auto`) selects the source: `auto` fetches both hosts and unions their entries; `alertready` / `pelmorex` pin a single host as an escape hatch. An absent option means `auto`, so existing entries get the fix with no reconfigure. Hosts are fetched sequentially in `NAAD_FEED_UNION_ORDER = ("alertready", "pelmorex")` and their entries concatenated in that order; per-host failure is tolerated (one warning per streak per host), and only an all-hosts failure raises `UpdateFailed`. When pelmorex retires (~Sept 2026) it simply fails every poll and is skipped — removing it is a cleanup, not an outage. (Note: the alertready Atom `<id>` authority is `rsstrainingdqs.alertready.ca`; that is the feed-generator instance's tag-URI authority, not a per-alert test marker — all its entries carry it and resolve to the same alerts pelmorex serves — so it is *not* filtered on.)

**Cross-host deduplication** happens in two stages so the union costs no extra CAP-body fetches. First, at the **envelope** stage in `_collect`: after an entry survives the status + region pre-filter, the first survivor per CAP OID (`_entry_oid`, read from the Atom `<id>`'s `urn:oid:…`) wins — so a cross-host duplicate is collapsed to the first host in union order (**alertready**), whose CAP body is fetched over HTTPS rather than pelmorex's plain HTTP. Dedup runs on *survivors*, not raw entries, because entries of one document are per (language × area group) with *different* polygons — collapsing before the region test could keep an area group the user is not in and drop a document their own group matched (the issue #45 shape). Second, `build_alerts_from_cap_docs` de-duplicates the parsed docs by CAP `<identifier>` as before, covering feeds whose ids carry no OID (`_entry_oid` fails open to the whole `<id>` there).

**Feed shape**: each Atom `<entry>` links to a per-entry CAP XML document via `<atom:link rel="alternate">`. The link may carry `type="application/cap+xml"` (legacy) or no `type` at all (alertready.ca), so `_pick_cap_link` resolves it by MIME type when present and falls back to the `.cap`/`.xml` href extension otherwise. The CAP file is the authoritative source for all `CAPAlert` fields. The Atom envelope is used for: (1) pre-fetch filtering — the `status` category and the `<georss:polygon>` are evaluated before the CAP file is fetched, avoiding bandwidth waste (GPS mode point-in-polygon; province mode a coarse polygon-bbox vs province-bbox test); (2) the Atom `<id>` element supplies `CAPAlert.url`. All CAP-1.2 fields (`identifier`, `sent`, `effective`, `onset`, `expires`, `headline`, `description`, `instruction`, `references`, etc.) come from the CAP body. Note: the alertready.ca envelope no longer carries the `geocode`/`areaDesc` categories the legacy feed had, so province cannot be *confirmed* pre-fetch — the envelope bbox is a coarse gate only, authoritatively confirmed from the CAP body afterwards — see Location matching.

**Feed shape (measured)**: the envelope emits **one Atom `<entry>` per (language × area group)**, not simply twice per alert. ECCC segments a single CAP document into one `<info>` block per (language × area group), and the envelope mirrors that: a national snapshot taken 2026-07-22 carried 211 entries for 100 CAP documents (78 documents × 2 entries, 11 × 4, 11 × 1). Every entry of one document points at the **same** `rel="alternate"` CAP URL — the entries differ only in their `<georss:polygon>`, `language=` category, and a title suffixed `in effect` / `en vigueur` / `ended` / `terminé` / `changed`. Because they share a body, the GeoRSS path parses the same document several times; `build_alerts_from_cap_docs` de-duplicates by CAP `identifier` so the bilingual merge is not handed the same document twice.

Bilingual — each document carries both an `en-CA` and an `fr-CA` `<info>` block for each area group. The provider merges language siblings using a language-independent bilingual key and stores the alternate-language content in `headline_alt` / `description_alt` / `instruction_alt` / `language_alt`. The **preferred language always becomes primary** — it is read from the CAP body directly, never from the Atom entry's `language=` category (that is an artefact of feed ordering, not a user preference).

**Lifecycle**: CAP `<references>` is parsed to build a revision chain. Within a poll, `resolve_chain_leaves` (in the shared `cap.py` module) drops superseded revisions (alerts whose `<identifier>` appears in another alert's `<references>` list). Only the leaf revision (the current UPDATE) is exposed. Across polls, the `AlertStore` detects cross-poll supersession when an incoming alert's `<references>` contains the CAP `<identifier>` of a disappearing previous-poll alert — it fires `incident_updated` instead of `incident_removed`.

**Termination is never carried by `msgType`.** ECCC keeps `msgType=Update` when an alert ends and leaves up to an hour of `<expires>` on the clock; the end-of-life signal lives in the per-area-group `Alert_Location_Status` CAP parameter instead. In the 2026-07-22 sample only 1 of 92 `Actual` documents was ever a `Cancel`, and that came from a non-ECCC sender:

| `Alert_Location_Status` | urgency | meaning | `expires − sent` |
|---|---|---|---|
| `active` | Future / Immediate / Expected | in effect | median ~16 h |
| `ended` | `Past` | ended for that area group | ~1 h |
| `transitioned_out` | Immediate | area moved to a *different* alert (arrives as its own document) | ~0 h |
| absent | — | non-ECCC sender (Amber, flood, 911) | — |

Both a `1.0:` and a `1.1:` layer of the parameter occur (`1.1` preferred when both are present). The provider reads it via `_location_status` into `CAPAlert.lifecycle_status`; `normalize._compute_phase` maps `ended`/`transitioned_out` to `phase=cancel` while `expires` is still in the future and `phase=expired` once it has passed (issue #95), so the store retires the entity and fires `incident_removed` with a true terminal phase.

The two tokens are not interchangeable either, and `phase` cannot express the difference — both end the alert early, but `transitioned_out` means the area moved to a *different* alert whose own `incident_created` is already carrying the news. The `eccc` entry in the convention table maps each token to a neutral reason (`ended`, `superseded`) in `lifecycle_removal_reasons`, and `store._fire_event` publishes it as `removal_reason` on `incident_removed` (issue #108). The mapping's keys double as the terminal set `_compute_phase` tests against, so a token cannot retire an alert without declaring why. Independent of `phase` by design: `transitioned_out` documents carry `expires ≈ sent`, so most of them are already `expired` when they arrive.

**Area-group selection (`_select_region_info`)**: because a document holds an `<info>` block per area group, "the block for this language" is ambiguous the moment the document's areas are at different lifecycle stages. The rule is: among the blocks whose `<area>` matches the configured region, prefer a non-terminal one; if *every* region-matching block has ended, the alert is terminal here and that block is returned so its `lifecycle_status` retires the entity. Fail-open at every branch — an absent parameter is `active`, and no region-matching block means the document is skipped (not treated as terminal). Province mode keeps province granularity: an SGC prefix cannot distinguish sub-province areas, so "any in-province block still active ⇒ still active" is the intended reading — a false all-clear to users in the still-active part would be the worst failure. Streaming admission (`doc_matches_region`) is deliberately looser: it matches on *any* block, terminal included, so the update that ends a tracked alert is retained long enough for the rebuild to act on it.

**Location matching**:
- Province mode — **coarse pre-filter + post-fetch confirmation**. The alertready.ca envelope carries no geographic category, but every `status=Actual` entry still carries a `<georss:polygon>`. Fetching every national entry's CAP body just to read its province (~1800 alerts) cannot complete inside the poll timeout, so a coarse gate runs first: `_province_bbox_intersects` rejects an entry whose polygon bounding box does not intersect the configured province's box (`_PROVINCE_BBOX`, padded `_PROVINCE_BBOX_PAD_DEG = 0.5°`). Survivors' CAP bodies are then filtered authoritatively by the `profile:CAP-CP:Location:0.3` geocode: its first two digits are the StatCan SGC province/territory code (`_PROVINCE_TO_SGC`). SGC is preferred over the CLC prefix — present on effectively every alert and correct for water zones (all CLC `00…`, which carry no province). The bbox is deliberately generous (over-inclusion is cleaned up by SGC) and fails open: an entry with no parseable polygon, or an unknown province code, skips the gate and defers to SGC. CAP fetch failures fail closed (the alert is dropped, since province can't be verified without the CAP body). Bounded by the shared `CAPContentCache`, so steady-state re-polls are cheap.
- GPS / device-tracker mode — **pre-fetch**. Point-in-polygon against `<georss:polygon>` using a pure-Python ray-caster (no `shapely`; not in HA core). Fetch failures fall back to a metadata-only alert (location already verified by the envelope polygon).

**Concurrency**: CAP XML is fetched with `asyncio.Semaphore(5)` for bounded concurrency. A shared `CAPContentCache` (LRU-256, Future-based in-flight coalescing) lives on `hass.data[DOMAIN]` and is reused across polls. Since CAP files are immutable per URL (each revision gets a new URL), cached bodies need no TTL. XML parsing is offloaded to `loop.run_in_executor` so a Canada-wide storm with dozens of CAP files doesn't block the event loop.

**XML parsing**: `defusedxml.ElementTree` — already an HA core dependency.

**Field mapping (two-tier)**:

*Atom-sourced:*

| NAAD Atom field | CAPAlert field |
|---|---|
| `<atom:id>` (entry) | `url` |
| `sha256(sender + sent + CAP-CP_eventCode + polygon_hash)[:12]` | `id` |

*CAP body-sourced (all other fields):*

| CAP field | CAPAlert field |
|---|---|
| `<identifier>` | `identifier` |
| `<sender>` | `sender` |
| `<sent>` | `sent` |
| `<status>` | `status` |
| `<msgType>` | `msg_type` |
| `<scope>` | `scope` |
| `<references>` (flattened to identifier strings) | `references` |
| `<info>/<language>` | `language` |
| `<info>/<category>` (first value) | `category` |
| `<info>/<event>` | `event` (title case as issued, e.g. `Freezing Drizzle Advisory`) |
| `<info>/<urgency>` / `<severity>` / `<certainty>` | same-named fields |
| `<info>/<effective>` / `<onset>` / `<expires>` | same-named fields |
| `<info>/<senderName>` | `sender_name` |
| `<info>/<headline>` / `<description>` / `<instruction>` | same-named fields |
| `<info>/<web>` (fallback: Atom `text/html` link) | `web` |
| `<info>/<area>/<areaDesc>` | `area_desc` |
| `<info>/<area>/<polygon>` | `geometry` (GeoJSON Polygon or MultiPolygon) |
| `<info>/<eventCode>` blocks merged into `parameters` | `parameters` |
| `<info>/<parameter>` blocks | `parameters` (merged; parameters win on key collision) |
| `<info>/<area>/<geocode>` (all schemes, keyed by `valueName`) | `geocodes`; promoted to `geocode_clc` / `geocode_sgc` / `geocode_same` — see *Area geocodes* |
| `<info>/<parameter>` `Alert_Location_Status` (1.1 preferred over 1.0) | `lifecycle_status` (ECCC-native `active`/`ended`/`transitioned_out`; drives `phase` and `removal_reason`) |

The mapped `<info>` is the region-matching block, preferring a non-terminal one — see *Area-group selection* above. Note: `event_code_same` and `event_code_nws` remain empty for ECCC. CAP-CP profile codes (e.g. `profile:CAP-CP:Event:0.4 → freezing-drizzle`) flow through `parameters` under their `valueName` keys. Every area geocode scheme in the CAP body lands in `geocodes` (see *Area geocodes*): `geocode_clc` is the promoted Canadian Location Code (province-numbered for land, `00…` for marine/water zones), and `geocode_sgc` the promoted StatCan SGC code — the signal province filtering actually matches on, so a province mismatch is now inspectable from the entity attributes.

## ECCC — NAAD streaming

**Default since 0.2.0.** The NAADS 2.0 LMD User Guide documents the TCP streaming feed — not the auxiliary GeoRSS feed — as the correct channel for 24/7 automated systems, and it removes the ~7 MB-per-poll GeoRSS transfer. ECCC streams by default; the `CONF_STREAMING` option (ECCC options flow, default on) is an operational escape hatch back to GeoRSS polling if the endpoint degrades. Toggling it reloads the entry.

**Endpoint**: `streaming.alertready.ca:8443` (TLS 1.3, no client cert, no subscribe handshake). The deprecated pelmorex `streaming1/2:8080` plain-TCP hosts are gated to registered LMDs and sunset ~Sept 2026 — not targeted.

**Client** (`providers/naad_stream.py`, `NAADStreamClient`): a standalone TLS client owning only the transport. The wire is a continuous byte stream of concatenated CAP-CP documents (XML declaration + `<alert>…</alert>`); the client reassembles complete `<alert>…</alert>` frames (bounded buffer guards a missing close tag), classifies heartbeats (`<sender>` starts `NAADS-Heartbeat`, `<status>System</status>`, emitted ≥ every 60 s), and hands raw alert-doc strings to the coordinator — it parses no alert semantics. A read that returns no bytes within the heartbeat timeout doubles as the liveness watchdog and forces a reconnect; reconnects use bounded exponential backoff with jitter. The TLS connection is created through an injectable `connect` callable so it is unit-testable with a scripted reader. On each *reconnect* (not the first connect) it requests a GeoRSS backfill to recover alerts issued while disconnected.

**Coordinator integration**: the coordinator stays the single source of truth for `data`, entity sync, store diffing, and availability. It holds a live `dict[identifier → CAPDoc]` (`_live_docs`) guarded by an `_ingest_lock`, and every ingest rebuilds the `CAPAlert` list from that set through the shared `build_alerts_from_cap_docs` (extracted from `eccc.py`) → `_apply` (normalize → marine filter → geometry → `AlertStore.process`), so stream and poll converge on one pipeline (identical revision-chain resolution, bilingual merge, transition events). Three mutators — a streamed alert doc, a heartbeat (docs `[]`; the rebuild ages out expired alerts with no network I/O), and a backfill — are serialized by the lock. Docs older than 48 h are pruned from the live set (the NAAD feeds carry a rolling 48 h window). The GeoRSS `async_fetch_docs` is the backfill source, so the `_fetch_one_feed` truncation guard still protects it. Unlike `async_fetch` it has no metadata-only fallback: an entry whose CAP body cannot be fetched is simply omitted and picked up on a later backfill, since the live set is cumulative. That is a behaviour difference for GPS/tracker entries, which under polling surface a metadata-only alert from the Atom envelope in that case (province mode fails closed either way, having no SGC code to verify).

`last_update_success_time` — the "Last updated" sensor — is stamped only by the fetch-backed paths (the poll and either backfill), not inside `_apply`, so heartbeat rebuilds do not advance it. It reports when data was last fetched, not when the active set was last recomputed.

**What enters the live set**: the socket carries every alert in Canada, so streamed docs are screened by `doc_matches_region` (`_admit`) *before* insertion — otherwise the set, and the rebuild it feeds on every stream event, would be sized by national volume rather than by the configured region. A doc that fails the region test is still kept when it references an identifier already tracked, so an update or cancellation can supersede an alert we hold even if its revised geometry no longer covers the user. Non-`Actual` documents (`Test`, `Exercise`, `Draft`) are dropped in `build_alerts_from_cap_docs` — the GeoRSS path filters them on the Atom envelope before fetching a body, so the rule lives on the shared path where both sources hit it — and `_admit` applies `is_actual` up front as well, because the references escape bypasses `doc_matches_region` entirely and a heartbeat's `<references>` lists recent alert OIDs.

**Availability (issue #16)**: only a *backfill* drives `last_update_success`. The periodic `_async_update_data` backfill raising `UpdateFailed` flips entities `unavailable`; a transient socket disconnect, or a stream-triggered reconnect backfill that fails, does **not** — the last-known active set is retained while the client reconnects, avoiding availability flapping. Stream pushes therefore go through `_async_push_data` (assign `data`, notify listeners) rather than `async_set_updated_data`: the latter asserts `last_update_success`, letting a heartbeat mark entities available while the authoritative backfill is failing, *and* resets the refresh timer, which would let ~60 s heartbeats defer the 30-minute resync indefinitely so it never ran.

**Reconnect backfill throttle**: a reconnect-triggered backfill is skipped when one ran within `NAAD_STREAM_BACKFILL_MIN_INTERVAL_S` (the old GeoRSS poll cadence, 300 s). The client's backoff only grows for connections that delivered *nothing*, so an endpoint that sends a heartbeat and then drops — or goes half-open, which the watchdog also scores as productive — reconnects every 60–130 s with the backoff pinned at its floor, and each reconnect would otherwise pay a full ~7 MB fetch. The floor bounds a flapping socket to no worse than the polling it replaced. The periodic resync is never throttled: that fetch is the availability signal.

**Observability**: the socket's state is surfaced as a diagnostic **Real-time stream** `binary_sensor` (`binary_sensor.py`, `BinarySensorDeviceClass.CONNECTIVITY`), created only for streaming entries — with the registry entry removed if streaming is later turned off, so it cannot linger as an unavailable orphan. Connectivity rather than a "last stream event" timestamp: Canada is often quiet for hours, so an idle healthy socket and a dead one produce the same timestamp, while `last_changed` on a connectivity entity gives "connected since" / "down since" for free. Like the Refresh button it is deliberately **not** a `CoordinatorEntity` — that base ties `available` to `last_update_success`, which would blank it exactly when a user is working out whether the socket or the backfill is the broken half. The client reports transitions through an edge-triggered `on_connection_change` callback (sync; it only flips a flag and notifies listeners), and `run()`'s `finally` publishes the disconnect even under cancellation. In the log, connect failures are transition-based: the first failure of a streak warns and names the consequence, recovery logs at `info`, repeats stay at `debug` — so a dead socket is visible without enabling debug, and a flapping one does not spam.

**Forcing a refresh**: each config entry gets a diagnostic **Refresh** button (`button.py`, all providers) whose press calls `async_request_refresh()` — a GeoRSS backfill when streaming, an early poll otherwise. It goes through the coordinator's debouncer so repeated presses cannot hammer the ~7 MB feed, and unlike the data entities it stays available across a failed update, which is when it is most useful. The equivalent service call is `homeassistant.update_entity` against any entity of the entry.

---

## MeteoAlarm — CAP JSON mapping

**API**: per-country aggregate JSON at
`https://feeds.meteoalarm.org/api/v1/warnings/feeds-{country-slug}` plus a
companion region index at
`https://feeds.meteoalarm.org/api/v1/regions/feeds-{country-slug}` (one
pair per ISO 3166-1 alpha-2 code, slug table in
`const.py::METEOALARM_COUNTRY_SLUGS`). Currently ~37 European member
services covered by the EUMETNET aggregator. One config entry per
country; users who want multiple countries add multiple entries.

**Feed shape**: the warnings endpoint returns
`{"warnings": [{"alert": {...}, "uuid": "..."}, ...]}` where each `alert`
is a CAP-1.2 document containing one or more `info` blocks — typically
the local language and English. The provider picks the `info` whose
`language` 2-letter prefix matches the configured language (falls back to
`en`, then to the first block in document order) and stores the next
remaining block in `headline_alt` / `description_alt` /
`instruction_alt` / `language_alt`. Warnings whose `status` is set and
not `Actual` are skipped at parse time.

A small equivalence table (`_LANG_EQUIVALENTS`) lets one prefix satisfy a
request for another, an exact match still winning. It holds a single group,
`{no, nb, nn}`: met.no tags its blocks with the Norwegian macrolanguage `no`,
which is not a Home Assistant locale, so a Norwegian install — whose
`language` option resolves to `nb` or `nn` — read English until the group was
added (issue #79). The other feed languages no HA locale can reach (`cnr`,
`rm`, `kl`) have no locale to be reached *from*, so they get no group.

**Location matching** (mutually exclusive, picked in the config flow):
- **Country-wide** — return every `Actual` warning for the country.
- **GPS polygon** — parses each warning's `area.polygon` (CAP whitespace-
  separated `lat,lon` pairs) into a GeoJSON ring and keeps warnings whose
  ring contains the configured point. Fails loud with `UpdateFailed` when
  the page has warnings but none carry polygons (the country does not
  publish per-warning geometry); matches the ECCC GPS-mode contract.
- **Region picker** — multi-select of region codes. Feeds carry a mix of
  area-geocode schemes across countries (`EMMA_ID` for most, `NUTS3` for
  FR/BG/RO/MK, `NUTS2` for HU/BE; sub-region cell schemes `WARNCELLID`/`CISORP`
  co-occur with these). A single scheme-priority resolver
  (`METEOALARM_REGION_SCHEMES = ("EMMA_ID", "NUTS3", "NUTS2")`, `areaDesc` as
  last resort) drives **both** picker population and the per-warning filter, so
  the value stored in `CONF_REGIONS` and the value matched at fetch time are
  always the same scheme for a given feed. Eleven countries (CH, EE, IE, IL,
  LU, NO, SE, SI, UA, UK, LV) publish no region-selectable scheme at all and
  land on the `areaDesc` fallback in both places.

  The list is derived from the **warnings feed**; there is no regions endpoint
  to consult. `feeds.meteoalarm.org/api/v1/regions/feeds-{slug}` is 404 for all
  38 countries, the official successor `api.meteoalarm.org/metadata/v1` needs a
  re-user API key an integration cannot ship, and the public endpoint behind
  meteoalarm.org's own map keys areas by internal UUID rather than by any CAP
  geocode (all verified 2026-08-04). Deriving from warnings is not the
  degradation it reads as: members publish green/no-warning entries for every
  area, so a live feed enumerates the country's full administrative tree —
  measured the same day at DE 408 regions, PL 383, ES 233, CZ 206, CH 151,
  AT 116, FR 90, SK 72. Only Iceland and Malta named nothing, and the flow
  aborts with `no_regions_available` there rather than offering an empty form.

  A **static bundled catalog was considered and rejected**: it would have to be
  per-country namespace-aware (an `EMMA_ID` entry for a `NUTS3` or `areaDesc`
  country produces a selection that silently matches nothing), and the only
  public `EMMA_ID` list is GPL-3.0 and stale against live feeds (0 of 206 codes
  matched for CZ, 0/28 BG, 0/42 RO, 0/7 HU, 4/90 FR).

  Harvesting reads **one `<info>` block per warning**, chosen by the configured
  language via `_pick_info_blocks` — the same selection `async_fetch` makes.
  Reading every block instead would offer each region once per published
  language, and for the `areaDesc` countries those repeats are distinct codes
  no de-duplication can merge (Norway offered 26 entries for 13 regions before
  this). One consequence to know: for those countries the stored code *is* a
  localized string, so changing the language option after setup can leave a
  stored selection matching nothing. Reconfigure harvests in the configured
  language, so the fix is a re-pick.

  An area may publish **several region codes under one `areaDesc`** — FMI names
  four sea areas in a single string with one `EMMA_ID` per area — and the filter
  matches on all of them, so the picker offers all of them (#48). Labels come
  from the most specific source that stays honest: per-code names when the
  description splits 1:1 with the codes (including the Czech
  `Kraj (Okres, Okres, …)` shape), the block name qualified by the code
  (`Kreis Göttingen (DE151)`) when the description carries a single name for
  several codes, and the bare code when neither mapping holds. Single-code
  areas skip the derivation entirely, so names that contain a comma or
  parentheses (`Ibiza y Formentera (Illes Balears)`) are untouched.

  The derived list can only name regions that a **currently live warning**
  mentions, so the selector accepts typed-in codes; reconfigure carries stored
  codes forward even when the current fetch doesn't offer them. Selected
  `code → label` pairs are persisted as `CONF_REGION_LABELS` so the device
  title can show readable region names (e.g. `MeteoAlarm DE — Bavaria +2`)
  without re-fetching; a code with no known name maps to itself.

**Severity**: when an `info` block carries an `awareness_level` parameter
(format `"N; color; Label"`, e.g. `"3; orange; Severe"`), the color token
is mapped to canonical severity (`yellow` → moderate, `orange` → severe,
`red` → extreme, `green` → unknown). EUMETNET members publish color
reliably while CAP `<severity>` is often blank or inconsistent, so color
is the authoritative signal here. When `awareness_level` is missing or
malformed, falls back to lower-cased CAP `severity` via the standard
non-NWS branch. The full `awareness_level` string is preserved verbatim
in `parameters` for cards that want the numeric tier or label.

**Identity**: dispatched per sender. Every authority **except the episode
dialects** uses `sha256(cap.identifier)[:12]` (falling back to the warning
`uuid` when the identifier is missing) — there, identifier collisions across a
poll are genuinely-distinct concurrent warnings (e.g. Italy/Austria publish one
region-and-time-window warning each), so the per-message identifier is the
correct key.

MeteoFrance is the exception (issue #37): its CAP `identifier` embeds a
per-message issue timestamp, so every re-issue of the same logical warning mints
a fresh identifier — hashing it spawned a duplicate entity each poll. For
`sender == vigilance@meteo.fr` the id is instead a content key
`sha256("{sender}|{event_key}|{region_key}|{window_key}")[:12]` where:

- `event_key` = the leading numeric token of `awareness_type` (language-
  independent phenomenon code; falls back to the casefolded `event`),
- `region_key` = the sorted resolved region codes — in region-picker mode
  scoped to the *intersection with the configured regions* (so a fixed
  department is stable even as other departments enter/leave the bulletin);
  the full resolved set otherwise,
- `window_key` = the `YYYY-MM-DD` date of `onset` (then `effective`, then
  `sent`) — the forecast day. **Empty for a merged episode** (see below); it
  survives only as a tie-breaker when one region and phenomenon somehow carry
  two live, non-adjacent runs.

Severity/color (`awareness_level`) is deliberately excluded so an orange→red
escalation updates the existing entity rather than spawning a new one. Existing
MeteoFrance entities recompute once on upgrade (stale ones are safe to delete);
all other authorities are byte-for-byte unchanged.

The same content key (`conventions.episode_id`) mints every **shipped** id for
an episode dialect, MeteoFrance and FMI alike, because the merge recomputes it
last. What differs is only what happens *before* the merge: MeteoFrance declares
an `identity` hook so pre-merge records already carry the content key (its
green-marker drop depends on a marker and its bulletin hashing alike), while FMI
declares none and carries the identifier hash until the merge replaces it.

**Episode merge**: two EUMETNET members publish one continuous warning as a
*chain of messages* — MeteoFrance one per calendar day, FMI one per window — and
with a window component in the id each chain became one entity per message, the
id rolling over mid-episode. That is the defect reported in #37 (MeteoFrance)
and #98 (FMI). Chaining is each service's deliberate product model, not a feed
quirk: the vigilance map is two panels, today and tomorrow, each department
colored per day. The defect was in this integration's 1:1 mapping of that model
onto durable HA entities, and the merge re-maps it rather than corrects it.

The pipeline that collapses a run of messages into a single alert, keyed
*without* the window component, is **one implementation** shared by both
senders. Only the predicate deciding which consecutive messages are one episode
differs, and that predicate is the declared field of a
`conventions.EpisodeDialect`:

- **Region-picker mode explodes first**, into one alert per configured region, so
  the episode key is `(sender, phenomenon, one region)`. This is what makes it
  stable: the *set* of regions a message covers moves from message to message — a
  France thunderstorm bulletin was measured going from 83 departments to 54, and
  the sampled FMI wildfire chain grew from one region to five — so any
  set-derived key, including an intersection with a multi-region config, would
  split the episode anyway. It also replaces an `area_desc` listing up to 83
  departments with the one the user selected. The split is per *region entry*,
  not per `<area>` block, because the two senders package areas differently:
  France publishes one block per department (one name, one NUTS3 code) while FMI
  packs every warned region into a single block holding N `EMMA_ID` codes and an
  `areaDesc` naming all N. The entries come from the provider's own region-picker
  resolver (`_region_entries`), so an exploded entity's name is the label the
  user selected, by construction.
- **The most severe message supplies the content wholesale**, tie-broken to the
  earliest onset; `onset`/`expires` widen to span the run. Blending fields would
  let the record contradict itself, since `severity_normalized` derives from
  `awareness_level` and the icon from `event`. Per-message truth goes to the
  `episode_days` attribute (`date`, `onset`, `expires`, `severity`,
  `awareness_level`, `event`, `headline`, `area_desc`), which stays absent for a
  single-message run because it would only restate the alert's own fields.
- **Finished messages are dropped before merging.** Without that, a finished run
  and an upcoming run for the same key collide on the window-free id, and the
  alert store — which keys by id — would silently drop one. This makes the
  provider clock-dependent, so `async_fetch` takes an injectable `now`.
- **The second and later runs re-add their first message's window** to the key,
  so two live runs of one phenomenon and region can never collide — churning only
  the pending entity, never the one in effect. The window is keyed at the
  dialect's own granularity (`EpisodeDialect.window_key`), which must be exactly
  as fine as its run rule can cut: the forecast day for MeteoFrance, whose
  re-issues move the onset time within a stable day; the verbatim
  `onset`/`expires` pair for FMI, whose runs can split sub-day and would
  otherwise collide two disjoint same-day advisories onto one id.
- **Country-wide mode keeps the full-set key** and therefore still splits an
  episode when the footprint moves. Known limitation for both senders, accepted
  because exploding per region there would turn France into roughly 150 entities.

**The run rule per sender** is the one thing that could not be shared:

- *MeteoFrance — consecutive forecast days.* Messages are grouped by the
  `YYYY-MM-DD` of `onset`, and a gap of more than one calendar day starts a new
  run, read as a genuinely separate episode. That has never been observed live
  (0 of 227 samples), so the reading is unproven; getting it wrong degrades to
  two entities rather than losing anything. Two messages on one day are resolved
  by `(severity, sent)` — severity first, so send order can never seat a weaker
  record.
- *FMI — contiguous windows.* Sorted by onset, a message joins the current run
  when it starts at or before the run's furthest reach. A message whose onset
  cannot be parsed is contiguous with nothing and gets its own run.

Applying MeteoFrance's rule to FMI would be actively wrong, which is why the
predicate is declared rather than unified. FMI does not publish one message per
day: two `FI809` wind advisories were live for the same day an hour apart
(09:00–21:00 and 22:00–00:00), and a calendar-day collapse would keep one and
silently discard the other — with its `(severity, sent)` tie-break unable to
even choose, because FMI stamps a whole batch with a single `sent` (12 of 23
sampled warnings shared a timestamp to the second). The converse fails too:
MeteoFrance re-issues a forecast day with `onset` clipped to the issue time, so
two re-issues of one day *overlap*, and a contiguity rule would merge them into
a bogus two-day episode with `onset` widened back to the superseded issue time.

Measured against a live France feed at a fixed instant: 256 entities across 88
departments become 149, the most any one department carries drops from 6 to 3,
and no (department, phenomenon) cell is lost. On Finland (sampled 2026-08-05) a
nine-message wildfire chain, most of it ending exactly at the midnight the next
message starts on, becomes one entity per configured region.

A horizon/outlook filter was considered and **rejected** — the merge subsumes it.
Live depth is two forecast days, not the four the J/J+1/J+2/J+3 framing suggests
(the larger figure counted superseded messages), and both days are collapsed
rather than hidden.

MeteoFrance entity ids change once on upgrade for a second time; stale entities
are safe to delete. FMI entity ids change once, for the same reason.

Two FMI behaviours are **out of scope** and left as-is. Its identifiers embed an
issue timestamp with an otherwise-stable token, so a re-issue that *moves* the
window (rather than extending it) still mints a fresh entity — a supersession
the merge does not model. And Finland publishes no green/no-warning entries, so
the warnings-derived region picker can only enumerate regions with a live
warning, unlike DE/PL/ES where it lists the full administrative tree.

**MeteoFrance "no warning" markers**: MeteoFrance encodes green/no-warning as an
`Actual` message with a degenerate window, in two shapes — `expires < onset`
(supersede marker, where `expires` carries the *replacement's* issue time) and
`expires == onset` (zero-length). Roughly three quarters of a live France feed is
one shape or the other. Neither is a warning, but both carry the same `event`
text, `awareness_type`, and areas as the bulletin they refer to, so the content
key above — which excludes severity by design — hashes a marker and its bulletin
to the *same* id. Since `AlertStore.process` keys incoming alerts by id, whichever
arrived last won, and a green marker could silently displace a live warning
(observed: markers and bulletins issued 2 seconds apart, so the outcome hung on
upstream send order).

Both shapes are therefore dropped for `sender == vigilance@meteo.fr` before any
mode filter, via `expires > onset` on parsed timestamps. The comparison must stay
strict: a zero-length marker's `expires` is a *future* day boundary, so no
`expires <= now` liveness check catches it. An absent or unparseable window fails
open (the warning is kept) so a feed format change can never silently drop real
alerts, and the rule is gated on the sender — the convention is unverified for
other MeteoAlarm authorities, whose degenerate windows are left alone.

**Where the dialects live** (issues #88, #98): all four MeteoFrance rules above —
identity, the marker drop, the region explode, the episode merge — are declared
by the `meteoalarm/vigilance@meteo.fr` entry in `conventions.py`, not branched on
in the provider. Identity and the marker drop are per-alert callables
(`identity`, `keep`); the two that are list-shaped are `PipelineStage` entries
bound to the `explode` and `merge` slots. The provider owns the order and runs
the slots:

```
construct → [identity] → [explode] → [keep] → mode filters → [merge] → return
```

`meteoalarm/cap@fmi.fi` declares the same two stages via
`episode_stages(FMI_EPISODES)` and nothing else: no `keep` (Finland publishes no
green/no-warning markers to drop) and no `identity` (the merge re-mints every
shipped id; MeteoFrance's identity hook is load-bearing there only because of the
green-marker collision FMI does not have). Registering a third such sender is a
table entry plus a run rule.

Sender-scoped entries *replace* the provider's rather than layering on it, so
both entries restate the MeteoAlarm `awareness_level` severity derivation. A
stage receives the whole batch and passes through what is not its sender's,
which keeps a dialect's own ordering intact and lets two dialects coexist in one
page.

**Field mapping**:

| MeteoAlarm JSON path | CAPAlert field |
|---|---|
| `warnings[].uuid` | identifier-fallback source for `id` (senders with no episode dialect) |
| `warnings[].alert.identifier` | `identifier`; primary source for `id` (senders with no episode dialect) |
| `warnings[].alert.sender` | `sender` |
| `warnings[].alert.sent` | `sent` |
| `warnings[].alert.status` | `status` (warnings with status ≠ `Actual` skipped) |
| `warnings[].alert.msgType` | `msg_type` |
| `warnings[].alert.scope` | `scope` |
| `alert.info[].language` | `language` (primary), `language_alt` (alt block) |
| `alert.info[].event` | `event` |
| `alert.info[].category[0]` | `category` |
| `alert.info[].urgency` | `urgency` |
| `alert.info[].severity` | `severity` |
| `alert.info[].certainty` | `certainty` |
| `alert.info[].responseType[0]` | `response_type` |
| `alert.info[].onset` / `expires` | same-named fields |
| `alert.info[].senderName` | `sender_name` |
| `alert.info[].headline` / `description` / `instruction` / `web` | same-named fields |
| `alert.info[].parameter[]` (valueName/value pairs) | `parameters` dict |
| `alert.info[].area[].areaDesc` | `area_desc` (joined across area blocks) |
| `alert.info[].area[].geocode[]` (all schemes, keyed by `valueName`) | `geocodes` — scheme-keyed container (`{"EMMA_ID": (...), "NUTS3": (...)}`); drives the region-picker filter. MeteoAlarm publishes no `SAME` scheme, so `geocode_same` stays empty (EMMA_ID is not a SAME code) |
| `alert.info[].area[].polygon` | `geometry` (GeoJSON Polygon or MultiPolygon, lon/lat) |
| `sha256(identifier)[:12]` (or `sha256(uuid)[:12]` fallback) | `id` — replaced by `conventions.episode_id` for an episode dialect |

---

## WMO CAP — Severe Weather Information Centre (SWIC)

**API**: per-source RSS 2.0 feed at
`https://severeweather.wmo.int/v2/cap-alerts/{source-id}/rss.xml`. Source IDs
follow `{country}-{agency}-{lang}` (e.g. `mx-smn-es` for Mexico's SMN Spanish
feed). One config entry per source; users wanting multiple sources add multiple
entries. Covers countries without a dedicated provider (Mexico, Brazil, Japan, …).

**Source dropdown** (config flow only — never touched at poll time): populated
from the live SWIC registry at `https://severeweather.wmo.int/v2/json/sources.json`
via `wmo.fetch_wmo_sources` (mirroring `meteoalarm.fetch_regions_for_country`).
The only filter is `const.py::WMO_UNMIRRORED_SOURCES` — the ~21 WMO-category
sources whose feeds live only on national domains and 404 on the mirror this
provider fetches from. There is **no** cross-provider uniqueness filtering:
feeds also served by MeteoAlarm or NWS are listed too, since `custom_value`
would bypass any such filter anyway and the dedicated providers are merely
*recommended*, not enforced (the field description points EU/US users at them
and notes that US alerts fetched via WMO hash the CAP `<identifier>` rather than
VTEC, so their entities churn across NEW→CON→CAN). On any fetch/parse failure
the flow falls back to the static `const.py::WMO_SOURCE_NAMES` catalog (verified
entries), so setup never hard-fails — and the *alert fetch* always uses the
mirror URL template, independent of the registry. `WMO_UNMIRRORED_SOURCES` is a
point-in-time curation (verified 2026-05-24); a newly-mirrored source stays
hidden until the set is updated, but `custom_value` lets users enter any ID.

**Feed shape**: the RSS envelope is an index, not the payload. Each `<item>`
carries a plain-text `<link>` pointing to an individual CAP 1.2 XML document
(unlike Atom, where the URL is an `href` attribute). The provider fetches the
RSS feed, extracts the per-item links, then fetches and parses each CAP file —
the CAP body is the authoritative source for every `CAPAlert` field. WMO feeds
ship one language per source, so there is no bilingual merge: `headline_alt` /
`description_alt` stay empty.

**Expiry pre-filter**: the mirror enriches each `<item>` with CAP-namespace
extensions (`cap:expires`, `cap:severity`, `cap:areaDesc`, …). `_parse_rss_links`
parses `cap:expires` (namespace-agnostic) and skips items already expired, so
only currently-active alerts trigger a CAP-body fetch. This is essential for
high-volume sources: PAGASA's feed lists ~500 items, nearly all expired —
fetching every CAP file would blow the coordinator's per-poll timeout and mark
the whole entry unavailable, whereas the ~9 live items fetch in seconds. Items
without a parseable `cap:expires` are kept (fail-open), so feeds lacking the
extension behave as before; the CAP body's own `<expires>` remains the final
authority via normalization. Buddhist-Era years in the RFC-2822 `cap:expires`
(Thai TMD feeds) are corrected to Gregorian before the comparison — sharing the
`const.py` threshold with `normalize._gregorian` — so genuinely-expired Thai
alerts are pre-dropped instead of read as ~543 years in the future.

**Shared CAP parsing**: the CAP body parsing lives in the provider-neutral
`providers/cap.py` module, used verbatim by both WMO and ECCC. `parse_cap_alert`
is namespace-agnostic (handles the `urn:oasis:names:tc:emergency:cap:1.2`
namespace), and the `CAPDoc` / `CAPInfoDoc` containers and `resolve_chain_leaves`
revision logic are imported from there — `cap.py` depends on nothing else in the
package, so providers import the parser, never each other. This keeps `wmo.py`
thin without duplicating ~150 lines of well-understood parsing. WMO builds its
own `CAPAlert` (`provider="wmo"`) rather than reusing ECCC's, since ECCC's
event-name recovery is specific to its bilingual colour-warning headlines.

**Lifecycle**: same revision-chain resolution as ECCC. Within a poll,
`resolve_chain_leaves` (shared `cap.py`) drops superseded revisions (whose
`<identifier>` appears in another alert's `<references>`); only the leaf revision
is exposed.

**Location matching** (mutually exclusive, picked in the config flow):
- **Country-wide** — return every alert published by the source.
- **GPS polygon / GPS tracker** — parses each alert's CAP `<polygon>` into a
  GeoJSON ring and keeps alerts whose ring contains the configured point (static
  coordinates, or a `device_tracker` resolved per poll by the coordinator). Fails
  loud with `UpdateFailed` when the feed has alerts but none carry polygons (the
  source does not publish per-alert geometry); matches the ECCC/MeteoAlarm
  GPS-mode contract.

WMO CAP has no standardized sub-country region code, so there is no *mode* keyed
on area codes. Sources that publish neither geometry nor a known region scheme —
`cn-cma-xx` is the confirmed case — are narrowed instead by the provider-neutral
`geocode_prefixes` option, which layers on top of any of the modes above (see
*Area geocodes → Geocode-prefix filter*).

**Language**: SWIC bodies are frequently multilingual and document order is not
language order — of the 110 sources sampled on 2026-08-03, 46 carried more than
one `<info>` block and 25 of those led with a non-English one. The source-ID's
trailing segment is *not* a language: `at-zamg-en` leads with `de-DE`,
`ch-meteoswiss-de` leads with `en`, 17 IDs end in `-xx`, one ends in `-marine`,
and 15 of the 110 disagree with their body's first block. `_select_info`
therefore matches the `language` option (or `hass.config.language`, passed
verbatim so `en-GB`/`en-US` and `pt-PT`/`pt-BR` stay distinct) against each
block's `<language>`: casefolded exact, then BCP 47 primary subtag, then any
English block, then document order. The English step is what makes the fallback
predictable when a document lacks the preferred language; ECCC deliberately has
no such step, since a French Canadian must not silently receive English. The
first non-selected block populates the `*_alt` fields, as ECCC and MeteoAlarm
already do. Duplicate tags resolve first-match-wins, which is why a source
emitting one `<info>` per *area group* (`ca-aema-xx`) still surfaces only its
first group — the pre-existing limitation ECCC #45 covers, not a language
concern. All three multi-language providers now select a block; their ladders
differ by design and stay per provider.

**Severity**: standard CAP `<severity>` passthrough — WMO has no dedicated
branch in `_normalize_severity`, so it falls through to the generic non-NWS
path (lowercase the CAP value, clamp off-axis values to `unknown`).

**Concurrency**: CAP XML is fetched with `asyncio.Semaphore(5)` and the shared
`CAPContentCache`; parsing is offloaded to `loop.run_in_executor`. CAP fetch
failures are skipped gracefully (the alert is dropped for that poll), not
surfaced as metadata-only entries.

**Identity**: `sha256(identifier)[:12]`. WMO CAP identifiers are sender-scoped
and stable across `Update`/`Cancel` re-issues for one logical event. Falls back
to hashing the CAP URL when the identifier is missing.

**Field mapping**:

| CAP field | CAPAlert field |
|---|---|
| `<identifier>` | `identifier`, primary source for `id` |
| `<sender>` | `sender` |
| `<sent>` | `sent` |
| `<status>` / `<msgType>` / `<scope>` | same-named fields |
| `<references>` (flattened to identifier strings) | `references` |
| `<info>/<language>` | `language` |
| `<info>/<category>` | `category` |
| `<info>/<event>` (fallback `<headline>`) | `event` |
| `<info>/<urgency>` / `<severity>` / `<certainty>` | same-named fields |
| `<info>/<responseType>` | `response_type` |
| `<info>/<effective>` / `<onset>` / `<expires>` | same-named fields |
| `<info>/<senderName>` | `sender_name` |
| `<info>/<headline>` / `<description>` / `<instruction>` / `<web>` | same-named fields |
| `<info>/<area>/<areaDesc>` | `area_desc` |
| `<info>/<area>/<polygon>` | `geometry` (GeoJSON Polygon or MultiPolygon) |
| `<info>/<eventCode>` + `<parameter>` blocks merged | `parameters` (parameters win on collision) |
| `<info>/<area>/<geocode>` (all schemes, keyed by `valueName`) | `geocodes`; promoted to `geocode_same` — see *Area geocodes*. WMO's sources are heterogeneous, so non-`SAME` schemes are surfaced rather than dropped |
| RSS `<item>/<link>` (CAP XML URL) | `url`, identifier-fallback source for `id` |
| `sha256(identifier)[:12]` (or `sha256(url)[:12]` fallback) | `id` |

---

## Alert Store (`store.py`)

Holds the previous poll's alerts in memory and diffs incoming alerts to detect new / phase-change / removed transitions. Only stateful component between polls — providers and the coordinator remain stateless.

### Design notes

- **In-memory only.** No disk persistence. After a restart, `_previous` is empty and the first poll treats every alert as new (`incident_created` for each). This is semantically correct — a restart is a cold start and these alerts are new to us.
- **Events are lightweight.** Payload contains only the RFC §2.3 schema plus two project extensions (`entry_id`, `area_desc`). Automations that need full details read the entity attributes — avoids duplicating the CAP payload on the bus. See [`events.md`](events.md) for the full schema.
- **Runs after normalization.** `phase` must be set before diffing.
- **Filter is internal to `store.process`.** The coordinator hands in the full normalized list (including `cancel`/`expired`). The store fires `incident_removed` with the true terminal phase and then drops those alerts from the returned active set — so the event payload's `phase` distinguishes cancel from expired directly. Alerts that vanish silently between polls are inferred as `expired` when past their `expires` timestamp, otherwise `cancel`.

---

## Config Flow

Split into two concerns, both wired in `config_flow.py`, which composes one
mixin per provider out of `flows/` (Home Assistant registers one flow class per
domain, and hassfest requires it to live in a file named `config_flow.py`):

- **Reconfigure flow** — identity (provider, zone / GPS / tracker / province / country / regions / area-code prefixes). Shows the same top-level provider menu as initial setup, so NWS / ECCC / MeteoAlarm switches work without remove/re-add.
- **Options flow** — behavior (scan interval, timeout, language, area-code prefixes). Applied live: updates `coordinator.update_interval` and timeout in place and calls `async_request_refresh()`. No reload, no coordinator teardown.

### The update listener owns every reload decision

`_async_entry_updated` in `__init__.py` is the single place that decides whether
an entry update needs a rebuild. The reconfigure flow deliberately calls
`async_update_and_abort`, **not** `async_update_reload_and_abort`.

This is not stylistic. Home Assistant deprecated pairing a config-entry update
listener with a reloading config-flow method in 2026.6 — the entry reloads twice
and the two paths can race — and makes it an error in 2026.12. Of the sanctioned
migrations, keeping the listener and dropping the flow's reload is the one that
fits: the alternative, removing the listener and letting the flow reload
unconditionally, would tear down and re-establish the ECCC NAAD stream socket
every time someone nudges a scan interval.

So the listener reloads when entry **data** changed (compared against the
snapshot the coordinator was built from, `entry_data_changed`) or when the
streaming toggle flipped, and otherwise applies options in place. Anything read
once at construction — provider, location, source id, stream wiring — belongs in
the first category; anything read per-poll in `_apply`, such as
`exclude_marine` and `geocode_prefixes`, belongs in the second. A test asserts
`async_update_reload_and_abort` appears nowhere in the flow modules, since
reintroducing it would break every reconfigure flow on HA 2026.12.

Entry title is derived programmatically from config data (`_compute_device_title`) — no `CONF_NAME` field. Shared by initial setup and reconfigure so the device name stays in sync.

---

## Diagnostics (`diagnostics.py`)

The config-entry diagnostics download (issue #134). The three diagnostic
*entities* are dashboard surfaces; this is the support artifact — one file that
answers "what is this entry actually doing" without asking a reporter to enable
debug logging and paste back a wall of text.

**Read, never re-derive.** Everything in the payload is read off the coordinator
as it stands. The *resolved* config — tracker → coordinates, country entity →
ISO-2, language `auto` → a concrete tag — is the pair the last update recorded
(`coordinator.resolved_config` / `resolved_options`), never a fresh
`_resolve_config()` call. That resolution owns the scope key retention is
decided against (see *Absence handling*), so running it from a diagnostics
download would consume a scope change the next real cycle needs to see. Before
the first refresh lands, the properties fall back to raw entry data, so an entry
that never came up is still diagnosable.

**Failures outlive their recovery.** `_async_update_data` wraps the fetch to
stamp `last_update_failure` and `last_update_failure_time`, and neither is
cleared on the next success — a dump is read after the fact, and "it broke at
04:12 and has been fine since" is what a report needs. The base coordinator
still owns availability, logging and backoff; the wrapper records and re-raises.

**Redaction is the reason the endpoint list is built here.** A diagnostics
download usually ends up in a public issue, so GPS coordinates, the tracker
entity and the MeteoAlarm country-source entity are redacted wherever they
appear. That includes derived values: NWS puts the location into its query
string, so the endpoint is rendered as `…/alerts/active?point=**REDACTED**`
rather than reproduced. Credential keys go through the same list even though no
shipped provider authenticates today. Alert body text and geometry are omitted
outright — they are large, and geometry is already externalized (§2.4) for that
reason.

**Curated, not `as_dict()`.** The common core pattern is
`{"entry": entry.as_dict(), "data": coordinator.data}`, and both halves are
wrong here. `as_dict()` carries `title`, and titles are derived from config data
(`_compute_device_title`), so a GPS entry's reads `CAP Alerts ECCC
(53.209258,-105.721127)` — the redaction defeated by the very field it is meant
to protect. `coordinator.data` is `dict[str, CAPAlert]` with full description,
instruction and geometry, which is what the payload deliberately omits.

**Everything is sized for a paste.** Alert rows are sparse on the same rule as
`CAPAlert.to_attributes()` (empty, `None` and `False` dropped), capped at
`MAX_ALERT_ROWS` = 25 with the overflow counted in `truncated`, and the totals
above them stay exact whatever the cap drops. Each row names its `entity_id`
from the registry, since a reporter quotes the entity, not the alert id. The
resolved pair reports only the keys resolution *changed*: for a static location
with a pinned language it changed nothing, and repeating the stored config there
would bury the entries where it did. Measured on an ECCC entry: 1.5 KB idle,
4.9 KB at seven alerts, 12.4 KB at the cap and above.

**Convention rows are the point.** Once a report involves a per-sender dialect,
the first question is which row matched, and `conventions_for()` returns the row
without saying which key produced it. The payload names the key
(`meteoalarm/vigilance@meteo.fr` vs. plain `meteoalarm`), lists the senders that
landed on each, and renders the row by walking the dataclass — so a rule added
to the table shows up in the next dump with no change here. Hooks render by
function name, stages by slot.

---

## Future Providers

These are documented for architecture planning; the provider protocol accommodates each without changes to the coordinator, sensor, or entity model.

### BoM — Bureau of Meteorology, Australia

- **API**: `https://api.weather.bom.gov.au/v1/warnings` — flat JSON array.
- Returns all active warnings nationally; client-side filter by state/location.
- No CAP urgency/certainty fields — remain empty.
- Severity inferred from title text ("Severe Thunderstorm Warning" → severe).
- Phase values: `new`, `update`, `renewal`, `upgrade`, `downgrade`, `final`, `cancelled`.
- No geometry — zone is `area_id` (e.g. `NSW_FL049`). Location search via `/v1/locations?search=…`.
- Config flow: state selector or GPS.

### DWD — Deutscher Wetterdienst, Germany

Supersedes an earlier sketch here that proposed the `warnapp/json/warnings.json`
JSONP endpoint. That endpoint is a lossy view: DWD publishes real CAP, so the
provider is a CAP consumer rather than a bespoke JSON mapping.

- **Feed**: `https://opendata.dwd.de/weather/alerts/cap/` — CAP 1.2 XML, no auth.
  The tree is *area granularity* (`COMMUNEUNION` / `DISTRICT`) × *product*
  (`DWD` / `CELLS` / `EVENT`) × *mode* (`STAT` snapshot / `DIFF` increment).
  `COMMUNEUNION_DWD_STAT` is the natural fit: a full current-state snapshot,
  which is what `AlertStore`'s authoritative diffing already expects.
- **Bodies**: one zip per language — `…_COMMUNEUNION_{DE,EN,ES,FR,MUL}.zip`,
  each containing one CAP XML per alert. Language selection is a URL choice
  here, not an `<info>` walk, so it sidesteps the multi-info work entirely.
- **Identity**: `2.49.0.0.276.0.DWD.PVW.<epoch>.<uuid>.<LANG>` — a WMO-style OID,
  stable across the update chain, with `<references>` populated. `sha256` of the
  identifier plus `resolve_chain_leaves` applies unchanged.
- **Geometry**: inline `<polygon>` in `<area>`, so `providers/cap.py` and
  `geometry.py` handle it as-is. Geocode scheme is `WARNCELLID`.
- Full CAP classification: `severity` Minor→Extreme, `urgency`, `certainty`,
  `responseType`, and `eventCode` entries (`II`, `LICENSE`, `PROFILE_VERSION`).
- Config flow: warncell ID or region name; language picks the archive.

#### Germany has three routes to DWD, and they are not interchangeable

MeteoAlarm (shipped), BBK / NINA (issue #66) and this feed all carry DWD
warnings. Measured 2026-08-07/08:

| | MeteoAlarm `feeds-germany` | DWD opendata | BBK / NINA |
| :-- | :-- | :-- | :-- |
| Origin | DWD only — 656/656 entries sender `opendata@dwd.de` | DWD | DWD subset + MoWaS / KATWARN / BIWAPP / LHP |
| Severity | all bands: Minor 259, Moderate 318, Severe 79 | all | Warnstufen 3–5 only |
| Granularity | Kreis (`WARNCELLID` `1…`) | COMMUNEUNION *or* DISTRICT | own |
| Geometry | none | inline `<polygon>` | separate `.geojson` fetch |
| Languages | 8 inline | one per archive | 8–9 inline |
| Geocodes | `EMMA_ID` + `WARNCELLID` | `WARNCELLID` | ARS / `AreaId` |

**MeteoAlarm Germany is a pure DWD relay**, so the overlap with a direct DWD
provider is total — but it is not the same slice BBK carries. BBK relays only the
upper warning levels (*"In der Warn-App NINA werden die DWD-Warnungen zu den
Warnstufen 3 bis 5 dargestellt"*), so the yellow "Amtliche WARNUNG" band never
reaches it. Measured: three `severity: Minor` thunderstorm alerts live on the DWD
CAP feed over Berchtesgadener Land, Traunstein and Rosenheim, live 40+ minutes
per their `<references>` chain, while `api31/dwd/mapData.json` and all three
district dashboards returned empty and the BBK API was otherwise healthy
(`mowas` had 8 entries). The same warning batch *is* present in the MeteoAlarm
archive at that `sent` epoch, at Kreis rather than commune granularity. Whether
`Moderate` (orange) crosses the BBK threshold is unmeasured — nothing orange was
live at sampling time.

Consequences for provider design:

- **MeteoAlarm already covers the German weather half**, at every severity band,
  with `EMMA_ID` for the region picker. What a direct DWD provider adds over it
  is narrow: inline polygons and commune-level granularity. Not nothing —
  MeteoAlarm ships no geometry in any country (0 polygons across ~5,900 areas
  sampled over DE/FR/IT/AT), so `geometry_ref` and `bbox` are permanently empty
  for MeteoAlarm entities — but it is a geometry argument, not a coverage one.
- **BBK's civil-protection channels are the real gap.** MoWaS / KATWARN /
  BIWAPP / LHP traffic has no MeteoAlarm equivalent, no DWD equivalent, and no
  other HA path.
- **Cross-provider duplication is already reachable.** A German user running
  MeteoAlarm alongside a future BBK entry gets duplicate entities for every
  Warnstufe 3–5 warning: separate config entries, separate coordinators, no
  cross-entry dedupe. Adding BBK collides with a shipped provider, not a
  hypothetical one. The identifiers do share a core — BBK emits `dwd.<OID>.MUL`
  where MeteoAlarm relays `<OID>.MUL` and opendata `<OID>.<LANG>` — so dedupe is
  a string operation, but it has nowhere to run today.

---

## RFC Schema Alignment (platform v1.0)

The integration implements the `IncidentEntity` contract from `rfc.md` §2.2, §2.2.2, §2.4, §2.6, §2.7.

### Phase vocabulary

`phase` attribute values are **lowercase**: `new`, `update`, `cancel`, `expired`. `expired` is computed in `normalize.py` by comparing the `expires` timestamp against the current time; cancelled and expired alerts are dropped by `filter_active_alerts`. Automations that string-matched the previous title-case (`"New"` / `"Update"` / `"Cancel"`) must be updated.

### Icon policy

Every alert entity exposes `icon: mdi:…` derived from the event type. The taxonomy lives in `icons.py` — NWS entries match full event names; ECCC and MeteoAlarm entries match substrings against their respective hazard vocabularies. Unknown events fall back to `mdi:alert`. Severity still drives entity state; the icon indicates hazard.

MeteoAlarm is classified on its `awareness_type` code first, and only falls through to the event tables when the code is absent or unrecognized. Event text is free-form CAP prose, so it classifies nothing on the 30-odd non-English member services; the code is the EUMETNET hazard key and is REQUIRED on every MeteoAlarm alert. The code-to-icon table is pinned to MeteoAlarm CAP Profile v2.0 §2.2.17, which is why it has no entry 11 (the profile skips it). No other provider publishes the parameter, so WMO and ECCC keep classifying on their English alternate `<info>` block.

### Platform version

`PLATFORM_VERSION = "1.0"` is exposed on every alert entity as the `incident_platform_version` attribute. Card consumers can branch on this when the contract evolves.

### bbox

When alert geometry is present, every alert entity exposes a 4-element `bbox: [min_lon, min_lat, max_lon, max_lat]` attribute (derived from Point / LineString / Polygon / MultiPolygon).

### Absence handling

An alert missing from a reconciliation is **retained**, not removed, while it is
still within its published `expires`. `store._retain_on_absence` decides this
from the source's `SourceConventions.absence_policy`, resolved per sender so a
dialect entry can carry its own policy; retained alerts carry `stale=True` and
`last_confirmed`, and fire no event. Absence still terminates when the source
declares `ABSENCE_ENDS` (the only case where absence itself is authoritative,
and a property of the source's contract rather than of any message), when the
query scope changed (`scope_changed`, computed by the coordinator from the
resolved config and options), or when the alert was superseded by a document
the region filter dropped before it reached the store
(`superseded_identifiers`, supplied from `_live_docs`).

**Retention requires an exit.** An alert publishing no `expires` cannot be ended
by time, so it is retained only when the source can end it some other way:
`lifecycle_removal_reasons` (a terminal vocabulary it publishes) or
`discovers_terminations` (a provider that fetches terminations the active feed
omits — NWS). With neither, absence stays authoritative for that alert, because
retaining it would leave an entity nothing could ever remove. `test_conventions`
guards the combination so a new source cannot land on the default retain policy
with no way out.

The case is measured, not defensive: of 113 WMO authorities serving CAP, 20 of
510 `<info>` blocks carried no `<expires>`, and Macao (`mo-smg-xx`) and Curaçao
(`cw-meteo-en`) published none on any alert, with no `cap:expires` in the RSS
envelope either. Hong Kong omits it on 44% of blocks and China on 16%. WMO has
neither exit, so those alerts terminate on absence.

One known limitation: a MeteoFrance warning lifted early via a green marker is
retained stale until its day-end expiry, because the marker is dropped by the
`keep` hook before the store can read it as a signal — see
`meteofrance_is_live_warning` in `conventions.py` for why it cannot be
forwarded as a terminal record.

NWS gets an explicit termination signal instead of relying on that fallback:
`NWSProvider._fetch_cancellations` queries `?message_type=cancel` on the
all-messages endpoint, scoped to the same zone or point as the active fetch.
Cancellations are never published to `/alerts/active` — 101 of 101 in a measured
six-hour national window were absent from it — so without this a cancelled alert
would be indistinguishable from a dropped one and would linger to its expiry.
VTEC identity ignores the action code, so a `CAN` product lands on the same
alert id as the warning it ends. An id stays eligible for this lookup for
exactly as long as `store._retain_on_absence` would keep the alert — until its
own expiry passes, or indefinitely when it published none — so a lookup that
fails in the cycle an alert vanishes retries on later cycles rather than
forgetting the alert. The two windows have to match: an expiry-less alert is
retained until an explicit terminal signal, and this lookup is the only way NWS
ever supplies one, so ageing its id out would pin the alert live permanently
while disabling the one mechanism that could end it. Ids kept on that branch
have no timestamp to age them out, so `_MAX_CANCELLABLE_IDS` bounds the set.
Zone scoping is verified
against the live API; **point scoping is not** — if `point=` turns out not to
compose with `message_type=cancel`, GPS-mode entries silently fall back to
expiry-bounded retention, which is safe but lingers.

### Geometry externalization (§2.4)

Full GeoJSON polygons are **not** entity attributes. The coordinator writes them
to a process-wide in-memory `GeometryStore` (LRU-bounded by total serialized
bytes, cap 5 MB, keyed by `geometry_ref = "{entry_id}:{provider}:{alert_id}"`)
and entities expose only the opaque `geometry_ref` handle. Nothing is persisted
to `.storage`: geometry is ephemeral, re-fetchable from the feed, and writing
hundreds of KB per poll cycle would accelerate SD-card wear on the Pi-class
hardware much of the HA install base runs on. The store starts empty after a
restart and the next poll refills it.

The `entry_id` prefix is load-bearing rather than cosmetic. The store is a
singleton shared across config entries, so a `{provider}:{alert_id}` key would
collide whenever two entries on the same provider both see an alert — the
overlapping-NWS-zones case — and the second write would win. The same prefix is
what lets `purge_missing()` scope a sweep to one entry's refs.

Consumers fetch polygons out-of-band:

- REST: `GET /api/cap_alerts/geometry/{geometry_ref}` → `FeatureCollection`
- Websocket: `{type: "cap_alerts/geometry", geometry_ref}` → `FeatureCollection`

Both require HA auth. The coordinator purges refs for expired/cancelled alerts
in the same cycle that drops the entity — the store reflects live state. The old
`CONF_INCLUDE_GEOMETRY` option is gone; its recorder-ceiling footgun no longer
exists because geometry never touches attributes.

### Soft-cap on long text

`description` and `instruction` are truncated to 4096 UTF-8 bytes with a trailing `…`, at a UTF-8 character boundary. The full text remains available on the underlying `CAPAlert` dataclass for future out-of-band retrieval.

### Event payload schema (§2.3)

See [`events.md`](events.md) for the full schema, including the project
extensions (`entry_id`, `area_desc`) and the rationale for the
`{entry_id}_{provider}_{alert_id}` `unique_id` shape vs. the RFC's bare
lifecycle hash.

### Sub-incident relationships (§6.3)

`CAPAlert.parent_id` is reserved for linking a sub-incident to its parent
event (the RFC calls out aftershocks-of-earthquake and
evacuation-zone-of-wildfire as motivating cases). The field is present
but never populated in v1; `to_attributes()` skips empty strings so the
attribute stays absent until a future provider sets it. Adding the hook
now means no schema migration when support lands.
