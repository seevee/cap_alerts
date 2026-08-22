# RFC: The `incident` Integration Domain for Home Assistant Core

**Status:** Internal Draft / DO NOT CIRCULATE
This document is a working draft for a future Home Assistant architecture proposal. It does not represent an accepted standard. Please do not submit this to the Home Assistant Architecture repository; the maintainer will do so when the reference implementation has gathered sufficient field testing.

**Author:** @seevee (`cap_alerts` maintainer)

**Date:** May 2026 (revised July and August 2026)

**Conventions:** all dates and timestamps in this document are UTC unless stated otherwise. Where it cites the *reference implementation*, it means the `cap_alerts` custom integration (§3.3), and claims are marked *shipped* only where running code backs them.

**Revision note:** the July 2026 revision reconciles the document against the reference implementation as actually shipped. Since the May draft the ECCC provider has gained real-time streaming ingest, area-group lifecycle handling, and multi-host feed unioning; months of live field observation have corrected several claims the May draft made about geometry handling, lifecycle identity, and provider behavior, and have added two requirements (§1.4, items 8–9). Claims are marked *shipped* only where running code backs them. The August 2026 revision adds requirement 10 (§1.4) and §1.6, meeting the action-with-response-data convention directly after core review resolved the same tension the same way in two integrations (§8.1). A second August pass re-checked every *shipped* claim against the tree, and the largest correction is GDACS: the provider has landed, so what this document previously reported as design analysis from probing that feed is now backed by running code (§4.1) — with one substantive change, since the CAP endpoint that probing used turned out to be unusable and the shipped provider builds alerts from the RSS index instead. The same pass corrected the terminal-phase claim in §2.2, the geometry inventory in §2.4, the retention markers in §2.1, and the attribute budget in §7.2, each of which the implementation had moved past. A follow-up measurement then replaced the budget's remaining guesswork about long-form text with a live sweep of all five providers (§7.2), which both sized the localized-duplicate hole and retired §2.4's claim that truncated text could be retrieved. A third pass records where that led: the per-field text cap is gone, the reference implementation bounds the serialized payload instead (§2.4, §7.2), and the question the sweep had left open about the localized fields is settled in favor of keeping them (§2.7).

**Audience:** Home Assistant Core developers and the Architecture Working Group, plus weather-alert integration maintainers

**Subject:** Standardizing external structured notifications via a dedicated, lifecycle-aware incident model

**What is being proposed, precisely.** Two things, and they are separable. The first is the **abstraction**: a first-class `incident` — a normalized, lifecycle-aware representation of an externally sourced structured event, with stable identity across provider revisions, a single severity vocabulary, an event contract, and a bounded payload. The second is the **binding**: how that abstraction attaches to Home Assistant's runtime, for which this RFC recommends dynamic `incident.*` entities. §1.4 states the abstraction's requirements without assuming any binding; §1.5 lists three candidate bindings against them; §2 argues for the entity binding in particular. A reviewer who accepts the first and rejects the second has not rejected this proposal — the schema, identity model, event contract, and geometry API in §2 all port unchanged to the alternatives in §3.6 and §6.1. The abstraction is the load-bearing claim; the binding is the recommendation.

---

## 1. Problem Statement

### 1.1 The 16 KB Recorder Ceiling
Home Assistant stores an entity's attributes in a single database column capped at 16,384 bytes (`MAX_STATE_ATTRS_BYTES`). During severe weather outbreaks or complex infrastructure incidents, the combined metadata — descriptions, instructions, area polygons — for many simultaneous items routinely exceeds it.

What happens on overflow is worth stating precisely, because it is not what earlier drafts of this section claimed. The recorder does not fail to commit the state change. `StateAttributes.shared_attrs_bytes_from_event` (`homeassistant/components/recorder/db_schema.py`, checked against HA 2026.7.3) serializes the attributes, compares the result against the cap, and on overflow logs a warning — *"State attributes for `<entity_id>` exceed maximum size of 16384 bytes. This can cause database performance issues; Attributes will not be stored"* — then persists `{}` in place of the payload. The state row commits and history keeps the state value; every attribute on it is discarded. For a packed-attribute alert sensor, whose state is typically a count and whose entire content lives in attributes, history therefore retains the number and loses the alerts, with nothing in the UI distinguishing that row from a healthy one.

The architectural point underneath is not that 16,384 bytes is a small budget. It is that **the number of simultaneously active incidents is unbounded while the storage unit is fixed.** Packing N incidents into one storage unit makes the payload scale with N against a constant ceiling, so the probability of loss rises with the number of simultaneously relevant incidents — which is to say, with the severity of the situation.

Separating the two bounds is what fixes it, and they are separate proofs. One incident per storage unit makes the recorder footprint **independent of incident cardinality**, which is the invariant this RFC rests on. It does not by itself bound the payload of any single incident — a long description, a localized duplicate of it, or a multipolygon can exhaust the budget alone — so §2.4 bounds that second dimension explicitly, by externalizing geometry and bounding the serialized payload. Both are required: the first stops the failure scaling with the weather, the second stops one verbose issuer reintroducing it. The reference implementation currently satisfies the first and only partly the second; §7.2 measures the shortfall and names what closes it, and the localized duplicate in that list of three is there because it is the one the implementation missed.

### 1.2 Lifecycle Fragmentation
Most current integrations treat each API response as independent. When a provider issues an update (for example, NWS promotes a message from `NEW` to `CON` or `EXT`), the new message often arrives with a different URI. Naïve integrations treat this as a brand-new event, retiring the old entity and breaking state history mid-event. The `nws` code owner ran into this directly in [home-assistant/core#37415](https://github.com/home-assistant/core/pull/37415), noting that "it is common that one alert will replace another" and that CAP `references` are "useful when an alert updates a previous alert" (see §8.1).

`cap_alerts` uses lifecycle-aware hashing to hold identity steady across updates. Everything below ships. The list runs one bullet per provider rather than one per strategy, because two of the five providers run more than one — which is the point the list closes on:
- NWS: VTEC-based identity (`office.phenomena.significance.tracking.year`) — but only where a VTEC string exists. Products published without one have no supersession protocol at all: each re-transmission is a fresh `Alert` with a new `urn:oid:` identifier and an empty `<references>`, so hashing the identifier mints an entity per transmission. On the national feed on 2026-08-06, 23 of 65 active non-VTEC alerts were surplus re-issues — 35%, across six offices, the deepest cluster six messages deep, and `<references>` was populated on none of the 65. Those collapse onto a content key instead (sender + AWIPS product identifier + event + sorted UGC set), keeping the newest by `sent`. One provider, two identity strategies, chosen per message
- ECCC: bilingual key `sha256(sender + sent + CAP-CP_eventCode + polygon_hash)[:12]` — language-independent fields shared byte-for-byte by en/fr siblings; urgency excluded by design so revision-churn does not produce duplicate identities
- MeteoAlarm: the per-message CAP `<identifier>` hashed, for every authority whose conventions do not override it. Two do. MeteoFrance publishes an identifier embedding the issue timestamp, so every re-issue of one logical warning mints a fresh one, and identity there is a content key (sender + awareness type + region set + forecast window); FMI, which splits a continuous warning at the window edge rather than the calendar day, reaches the same key through its own run rule. Identity is a per-*sender* property, not a per-provider one
- WMO SWIC: `sha256(<CAP identifier>)[:12]` — the sender-scoped CAP `<identifier>` is stable across Update/Cancel re-issues, so the simplest possible key suffices wherever the provider supplies a durable identifier; revision chains still collapse to the leaf via CAP `<references>`
- GDACS: `sha256("<eventtype>:<eventid>")[:12]`. GDACS embeds a per-update *episode id* in its CAP `<identifier>` (`GDACS_<type>_<eventid>_<episodeid>`) that increments on every re-issue, so hashing the identifier whole would mint a new identity each update and fragment history. Identity keys on the pair that names the event rather than the message, discarding the volatile episode segment. Design analysis said so first; the shipped provider confirms it, from the RSS envelope rather than from a CAP body, because GDACS has no per-event CAP endpoint that works (§4.1)

The progression makes the broader point directly: no single CAP field is reliably the identity. WMO can trust `<identifier>` where GDACS cannot; NWS can trust an external standard for the products that carry one and nothing for the products that do not; MeteoAlarm can trust the identifier for most of Europe and not for France or Finland. Identity derivation is irreducibly source-specific — down to the individual sender, not just the provider — and the core model requires only that the integration supply *some* stable hash (§5).

A single entity then persists across every update until cancellation or expiration.

### 1.3 Inconsistent Data Models
Users and card authors face wildly different shapes across NWS, Environment Canada, MeteoAlarm, and others. There is no shared vocabulary for severity, phase, or identity, which makes universal dashboards and automations impractical.

### 1.4 What Any Solution Must Provide

The failures in §1.1–§1.3 imply a set of requirements that any incident-handling abstraction must satisfy, independent of implementation. They are stated here so the rest of the RFC can be read against them; the choice of binding mechanism (§1.5) is a separate question.

1. **Normalized vocabulary.** CAP fields (severity, urgency, certainty, phase, timestamps, area) are normalized once, centrally, so downstream consumers see one shape across NWS, ECCC, MeteoAlarm, DWD, and future providers.
2. **Stable lifecycle identity.** A single logical incident keeps one identity across provider message updates (`NEW`→`CON`→`EXT`, URI changes, CAP `references` chains), so state history stays continuous across an event rather than fragmenting per re-issue.
3. **Bounded footprint.** No single item's metadata approaches the 16 KB recorder ceiling; heavy payloads (geometry, long-form text) are externalized rather than inlined.
4. **Concurrent multiplicity.** Many simultaneous incidents coexist without truncation or dropout (the MeteoAlarm single-slot failure, §3.3).
5. **Restart survival without disk-wear cost.** Active incidents survive an HA restart mid-event on HA-native persistence, without per-poll writes of large payloads to SD-backed `.storage/` (§2.4, §2.5).
6. **Dynamic active set.** The set of live incidents tracks the upstream feed — items appear on issue and disappear on cancel/expiry — with expiry honored from feed metadata (the DWD reset bug, §8.1).
7. **Automation surface.** Automations can trigger on incident arrival, update, and termination, carrying severity/phase/changed-fields, without hand-wiring against items that do not exist until an event occurs.
8. **Tolerance of imperfect sources.** Termination is not driven by a single observation of absence, because real feeds intermittently omit live alerts (§2.5) and real authorities signal end-of-life through vendor-specific fields rather than CAP `msgType` (§2.2). The active set converges on upstream reality without a momentary gap in one source destroying an entity's history.
9. **Ingest-mode neutrality.** The model holds whether incidents arrive by scheduled polling or by a pushed stream, and does not assume a poll interval exists (§2.5).
10. **Readable by a dashboard.** Stated bindingly-neutrally: *core presentation data, and changes to it, must be available through a subscribed, contract-stable read path usable by both declarative and custom dashboard consumers.* The wording below names the state machine because that is the mechanism the recommended binding uses, and because the concrete failures this requirement exists to exclude are only legible against a concrete mechanism; a reviewer preferring another binding should read "the state machine" as "whatever that binding's subscribed surface is" and the requirement stands unchanged. The frontend can consume an incident through read paths it actually has. The core presentation fields — severity, phase, timestamps, headline, area — arrive over a surface the frontend already subscribes to (for an entity binding, the state machine), so declarative consumers — stock cards, `auto-entities`, templates — render them without custom code, and changes reach every consumer by push. Payloads externalized under requirement 3 stay reachable through a frontend-native, contract-stable read path, with subscribed state carrying the handle and the change signal that tell a card when to fetch (§2.4). An action with response data provides neither half: declarative surfaces cannot invoke it, nothing tells a caller its snapshot is stale, and data living only in a response never reaches the recorder, history, or a state trigger (§1.6). A mechanism that satisfies requirements 1–9 but serves the incident body only through such an action fails the primary consumer this abstraction exists to serve. The requirement cuts the other way too: a frontend that fetches and renders CAP entirely in the browser satisfies its own display needs while reaching neither the recorder, the state machine, nor any automation — and, as §3.8 measures, cannot reach most of the world's CAP publishers in the first place.

These requirements are provider-neutral, and requirements 1–9 assume no particular binding. Requirement 10 needs a more careful statement, because taken literally its wording names entity-ecosystem concepts — stock cards, `auto-entities`, templates, state triggers, the state machine — and a reviewer preferring a registry could fairly call that circular. The precise position: **requirement 10 is neutral about the storage and runtime mechanism, and deliberately specific about the consumer surface.** Whatever binding is chosen must expose incident state *and changes to it* through a frontend-native subscribed contract that declarative and custom consumers can both read. Entities satisfy that today by inheriting it; a registry could satisfy it too, but only by growing the subscription, card-composition, trigger and history surfaces §3.6 costs out — which is an argument about the price of a binding, not evidence that the requirement was written around one. Requirements 8 and 9 were added in the July 2026 revision; both are consequences of field-testing the reference implementation rather than of the original design analysis. Requirement 10 was added in the August 2026 revision, after core review steered two integrations, across four PRs, to the same resolution — payloads out of attributes and behind action responses (§1.6, §8.1); it states explicitly a constraint the earlier drafts assumed without argument.

### 1.5 Candidate Bindings

Three mechanisms can satisfy §1.4. They differ only in how the normalized, lifecycle-tracked incident binds to HA's runtime; the data model, event contract (§2.3), and geometry API (§2.4) are identical across all three. This is the section where the proposal becomes falsifiable in two independent places: a reviewer may reject the abstraction's requirements (§1.4), or accept them and prefer a different binding here. Only the first would defeat the proposal.

- **Entity-based `incident` domain (recommended; §2).** One entity per active incident, created and removed with the incident. Reuses the recorder, the visual state-trigger editor, `RestoreEntity`, and existing entity-aware cards without new core surface. Cost: entity- and registry-mutation traffic at incident boundaries (§2.5).
- **Static entity pool (§6.1).** A fixed pool of pre-allocated slots, filled and drained rather than created and destroyed. Zero registry churn; cost is up-front permanent entity cardinality and a client-side empty-slot filter pushed onto every card and automation.
- **Dedicated `incident_registry` (§3.6).** A new non-entity registry sibling to `issue_registry`, ingesting CAP directly. No entity churn at all; cost is rebuilding the history, trigger, Lovelace, and restart-survival surfaces that the entity model gets from core.

This RFC recommends the entity-based domain and argues the case in §2, then evaluates the two alternatives against the same requirements in §6.1 and §3.6. A reviewer can accept §1.4 in full while preferring a different binding; the schema, events, geometry, and normalization sections apply unchanged in that case.

### 1.6 The Fourth Mechanism: Action With Response Data

A fourth binding is not listed above because it does not satisfy §1.4, but it is the one core review currently prefers, and this RFC must meet it directly rather than by omission. In this model the integration keeps a thin entity — typically a count, or a `binary_sensor` indicating "something is active" — and serves the incident bodies through an action with `SupportsResponse`, which automations invoke on demand.

The argument for it is real and should be stated at full strength. Long, variable-length payloads in `extra_state_attributes` are written to the recorder on every state change, shipped to every connected client on every update, and included in every state dump; the developer documentation warns about exactly this, recommending that entities generating frequent state changes minimize their attributes or split data into separate entities. Actions with response data have none of those costs: the payload is computed on request, delivered to one caller, and never recorded. For automation and script consumers, it is a better design, and §1.4 requirement 3 (bounded footprint) is an agreement with its premise, not a rejection of it.

It fails on requirement 10, and the failure must be stated precisely, because the imprecise version is refutable. A custom card *can* invoke an action and await its response — `hass.callService` exposes `returnResponse` in the frontend's public interface, over the same authenticated websocket every other frontend read uses. What the action model withholds is not the call but everything requirement 10 asks for around it. Declarative surfaces are excluded entirely: stock cards, `auto-entities`, markdown templates, and the visual editors cannot invoke an action to obtain data, and surfacing action results on a dashboard is an open, unanswered frontend request — [home-assistant/discussions#655](https://github.com/orgs/home-assistant/discussions/655). There is no change signal: a card that has called the action holds a snapshot, and with only a thin count entity to watch, "the count is still 3" cannot distinguish an unchanged set from a same-sized set with different members, so the card is reduced to re-polling for data the integration already knew had changed. And a payload that exists only in a response never touches the state machine — no recorder row, no history, nothing a state trigger or condition can reference. (The §2.4 geometry command is also request/response; the difference lies on exactly these axes — subscribed state carries its handle and its change signal, and the command is a typed frontend contract rather than an automation surface.) The model relocates the incident body where the frontend's declarative surface cannot follow at all, and its imperative surface can follow only by polling.

Three points about where this argument stands:

**The convention is unwritten.** As of August 2026 there is no ADR on the subject (`home-assistant/architecture/adr`, 0001–0022), no integration quality-scale rule (55 rules; none concerns attributes), and no developer-documentation statement recommending actions over attributes — the `core/entity` guidance addresses recorder size and update frequency, and does not mention action responses. The practice is consistent and its rationale is sound; what it lacks is a written form, and therefore a place where a requirement like 10 can be raised against it. That is the gap this RFC is filling rather than contesting.

**Core has already answered the frontend half of it once — at design time, not as an afterthought.** Weather forecasts took exactly this path: they moved off `weather.*` attributes into `weather.get_forecasts`, and core did not point cards at the action. The same migration shipped the frontend a purpose-built read path — the `weather/subscribe_forecast` WebSocket command, in `homeassistant/components/weather/websocket_api.py` today — so at no point was the card ecosystem expected to live on action calls. That is the precedent this RFC builds on: when a first-class domain moves structured data out of the state machine, core supplies the frontend with a contract for reading it. §2.4 proposes the same arrangement for geometry, and the same reasoning extends to any part of the incident payload a future reviewer wants externalized. The disagreement is therefore narrower than it appears — not *whether* payloads may leave attributes, but whether a domain that moves them out owes the frontend a way back in.

**The same tension is live in the architecture repo.** [architecture#1357](https://github.com/home-assistant/architecture/discussions/1357) and [#1360](https://github.com/home-assistant/architecture/discussions/1360) (@jpbede, both opened March 2026, the second revised through July) propose a forecast contract for sensor entities on identical reasoning — "Custom integrations often stuff long forecast arrays into state attributes, which is inefficient and hard to standardize"; "Keep forecast data out of state attributes and fetch on demand" (#1357) — and name the same gap in the same breath: "the frontend has no unified way to retrieve and render forecasts for sensor values" (#1360; #1357's wording is "sensor forecasts"). Both have been taken to the architecture meeting. Forecasts and incidents are the same shape of problem (structured, multi-item, time-bounded data hanging off an entity), and they should not be solved twice by different means.

The conclusion this RFC draws is not that attributes are the right home for incident bodies in perpetuity. It is that requirement 10 is a requirement, that the action-response model does not meet it, and that a domain shaped for incidents is the natural place to give it a first-class answer — as `weather` already has.

---

## 2. Recommended Implementation: the `incident` Domain

This section specifies the recommended candidate from §1.5: a binding of the §1.4 requirements onto HA's entity model. §6.1 and §3.6 evaluate the static-pool and registry alternatives against the same requirements. Where this section says "the entity," a reviewer favoring a different binding can substitute the corresponding slot or registry record; the schema (§2.1), event contract (§2.3), and geometry API (§2.4) are common to all three.

### 2.1 Entity Model

The `incident` platform defines a new domain with `IncidentEntity` as the base class. One entity represents one incident. The entity is created when the incident first appears and is removed when it cancels or expires.

Core properties of the model:

- Provider layers perform CAP 1.2 normalization once (severity tiers, phase, truncation), so downstream code sees a single vocabulary.
- Identity is stable across provider message updates (see §2.2).
- Attributes are sparse: only populated fields are serialized.
- Heavy payloads (geometry, long-form text) are referenced rather than inlined, which keeps the attribute footprint bounded (see §2.4).

**State** is the normalized severity, one of `extreme`, `severe`, `moderate`, `minor`, `unknown`.

**Platform-guaranteed attributes.** These are set by the platform rather than copied from the feed, so they are present on every entity regardless of how sparse the upstream message was:

- `id`: stable lifecycle-aware identifier (§2.2)
- `phase`: one of `new`, `update`, `cancel`, `expired` — derived, and defaulting to active when the provider signals nothing (§2.2)
- `severity_normalized`: the same value carried in `state`, denormalized onto attributes so that template and table consumers, which read attributes rather than state, do not have to special-case it
- `icon`: `mdi:*` handle keyed on event type (§2.6)

The distinction matters because it is the only part of the schema a consumer may read unguarded. Everything below is feed-supplied, and CAP completeness varies enormously between authorities, so a card that assumes a key exists will break on the next provider rather than on the current one.

**Feed-supplied attributes**, emitted only when populated:

- `event`: short event name, drives `entity_id` derivation. Near-universal in practice and required by every profile we ingest, but CAP itself makes `<event>` mandatory only within an `<info>` block, so an entity built from a message carrying none has no event name to emit. Consumers keying on `event` should fall back to `category`.
- `severity`: raw provider value. Absent for providers whose severity is *derived* rather than transmitted — MeteoAlarm publishes awareness levels, not CAP `severity`, so the provider layer synthesizes the normalized tier with no raw value to preserve. Read `severity_normalized`; `severity` is for consumers that specifically want the untranslated original.
- `headline`, `description`, `instruction` (published in full, bounded at the payload, see §2.4)
- `urgency`, `certainty`, `msg_type`, `status`
- `category`: CAP category enum (`Geo`, `Met`, `Safety`, `Security`, `Rescue`, `Fire`, `Health`, `Env`, `Transport`, `Infra`, `CBRNE`, `Other`) — the cross-domain discriminator that tells a weather warning (`Met`) apart from a 911 outage (`Infra`) or an AMBER alert (`Other`). This is the primary "what kind of incident is this" axis for cards (§2.6), and it is not a hypothetical axis: a single live sample of the Canadian NAAD feed, ingested through the reference implementation's one ECCC provider, carried `Met` warnings from ECCC storm-prediction centres alongside `Infra` (Manitoba Emergency Management Organization, `911 Service Inoperative`, `severity=Extreme`) and `Other` (RCMP "E" Division and Calgary Police Service AMBER alerts), all `status=Actual`/`scope=Public` (§4.1). The reference implementation already emits this attribute
- `sent`, `effective`, `onset`, `expires`, `ends`
- `area_desc`, `affected_zones` — `area_desc` is the provider's human-readable area string where one exists, but is not guaranteed descriptive: the GDACS CAP bodies write the literal `"Polygon"` in this slot and carry the location only in `headline`, which is why the shipped provider substitutes the country name from the feed envelope rather than passing the field through. A provider layer can paper over this; the platform contract cannot assume it away, so consumers must not treat `area_desc` as presentable on its own
- `bbox`: `[min_lon, min_lat, max_lon, max_lat]` for map previews (~64 bytes, always safe to inline)
- `points`: `[[lon, lat], …]` for alerts whose area includes point locations (§2.4). Published alongside `geometry` rather than replacing it, so an alert carrying both a fire-ground polygon and a location marker keeps each
- `geometry_ref`: opaque handle for full polygon retrieval via the API in §2.4
- `language`: BCP-47 tag for the primary text fields (e.g., `"en-US"`, `"fr-CA"`); see §2.7
- `headline_alt`, `description_alt`, `instruction_alt`, `language_alt`: populated only when the provider emits a second language for the same incident (see §2.7)
- `stale`, `last_confirmed`: set on any incident the current reconciliation did not confirm but the platform kept anyway. That covers two cases, and an earlier draft of this line named only the second: an incident *retained through an absence* under the §2.5 rule, and a restored incident not yet re-validated after a restart. `stale: true` flags that the content predates the latest reconciliation; `last_confirmed` is the ISO timestamp of the last one that observed it. Both clear as soon as a reconciliation sees the incident again. The retention case is the one the reference implementation exercises today — it stamps both in the store's absence path — and it is the more common of the two by a wide margin, since feed dropouts are routine where restarts mid-incident are not.
- `parent_id`: reserved for future sub-incident relationships (§6.3); unset in v1
- Provider-specific fields, for example `vtec` or `event_code_nws`

**Severity normalization.** The mapping is deterministic and implemented once, centrally:

| CAP `severity` | Entity `state` |
| :------------- | :------------- |
| `Extreme`      | `extreme`      |
| `Severe`       | `severe`       |
| `Moderate`     | `moderate`     |
| `Minor`        | `minor`        |
| `Unknown` / missing / non-CAP | `unknown` |

Providers that do not emit CAP `severity` directly (for example, MeteoAlarm colour codes) must adapt to this table in their provider layer. The core entity never sees provider-specific severity vocabularies. This is the same mapping national CAP deployments already perform by hand: implementing the standard across Myanmar, the Maldives, and the Philippines, SAMBRO operators folded disparate local scales — Myanmar's cyclone colour codes, the Philippine Public Storm Warning Signal numbers (1–5), and the Maldives intensity colours — onto these exact five `severity` tiers (§8.4). That independent convergence on the same target table is evidence the normalization belongs in one central place rather than in every consumer.

**Device grouping.** All incidents from one config entry belong to a single device in v1. An alternative is per-issuer device grouping, with one device per upstream authority ("NWS OKX", "Environment Canada Prairie Storm Prediction Centre"), which maps more naturally onto the hub-and-peripheral model hardware integrations use. We are open to adopting it if the AWG prefers; the single-device choice in v1 is about keeping device count small and predictable while the platform stabilizes. A second argument for per-issuer is UI legibility: during a regional event a single config entry can carry 15+ concurrent incidents, and one device page listing all of them, each appearing and disappearing, is hard to scan. This is the most likely v1.1 change; it is held out of v1 to keep the device model and the §2.5 batching contract simple while the platform stabilizes. One concrete concern with per-issuer: during multi-regional events, a single config entry can see alerts from 10+ upstream offices in a single poll (e.g., a mid-Atlantic derecho routinely touches LWX, AKQ, PHI, CTP, RNK, and more NWS WFOs at once). Per-issuer trades per-entity registry churn for per-device registry churn under the same fan-out conditions §2.5 is most worried about, so if the AWG prefers per-issuer, the batched-mutations rule in §2.5 needs to extend to device registry writes as well. Per-zone sub-device grouping (by `affected_zones`) is a separate question, deferred; §6.5 has the rationale.

### 2.2 Identity and Lifecycle

The `unique_id` for an entity is the provider's stable lifecycle hash: VTEC for NWS; for ECCC, `sha256(sender + sent + primary_CAP-CP_eventCode + polygon_hash)[:12]` (language-independent; en/fr siblings produce the same key; urgency excluded to survive revision-churn); for WMO SWIC, `sha256(<CAP identifier>)[:12]` over the sender-scoped CAP identifier (falling back to the CAP URL when an identifier is absent).

`entity_id` is derived as `incident.<slug(event)>_<short_hash>`, where `short_hash` is the first 8 hex characters of SHA-1 over `unique_id`. Deriving the suffix from the hash avoids HA's numeric-suffix fallback (`..._2`, `..._3`), which otherwise disconnects state history from the stable lifecycle identity each time a collision resolves differently. Slugification uses HA's standard `slugify()` applied to `event`.

The lifecycle has three phases:

| Phase       | Behavior                                                                            |
| :---------- | :---------------------------------------------------------------------------------- |
| Creation    | Spawned on first sighting of a new hash. `incident_created` fires.                  |
| Update      | State and attributes refreshed in place. `incident_updated` fires on phase or field delta. |
| Termination | `incident_removed` fires. Entity and registry record are purged (see §2.5).         |

**Two incident shapes.** The phases above describe a *warning*: a future or ongoing hazard with a bounded active window (NWS, ECCC, MeteoAlarm, WMO). A second shape exists — the *event report*: a past, point-in-time event carrying `urgency=Past` and no meaningful `expires`, the GDACS earthquake being the motivating case. Reports do not traverse `NEW→CON→EXT`; they appear, are occasionally revised, and are removed when they fall out of the upstream feed's retention window rather than on a CAP cancel or expiry. The domain serves both shapes: §4.1 draws the boundary, and the dynamic-active-set requirement (§1.4 item 6) covers reports through feed-presence where it covers warnings through expiry.

The shape is now shipped rather than projected. Every GDACS alert carries `urgency=Past` and no `expires` — `<gdacs:todate>` looks like one and is the last observation time, in the past for all 315 events in a sampled current-events feed, so mapping it to `expires` would mark every alert terminal on arrival. Nor does the feed publish a terminal vocabulary: `iscurrent` goes false for droughts and nothing else, while every earthquake, volcano, cyclone, flood and wildfire observed stayed true right up to the poll it vanished on. Withdrawal from the feed is the whole of the end-of-life signal, and the §2.5 rule already ends such an incident on absence without a special case, because a source with no expiry, no terminal vocabulary and no termination lookup has no other exit. Retention then scales with significance rather than with a configured window: a major earthquake holds an entity about four days, a small one about a day, a drought a year. What is still unproven is the *revision* half of the shape — a report being substantively revised in place — which the feed does through an episode counter and which no field observation has yet exercised end to end.

**Phase is best-effort; the event stream is authoritative.** `phase` is derived from CAP `msgType` (`Alert→new`, `Update→update`, `Cancel→cancel`). That derivation is a convenience, not a contract, because **`msgType` is empirically not how real CAP authorities signal termination.**

The reference implementation's ECCC provider is the worked case, and it is a strong one because the finding is quantified against live data rather than inferred from the specification. In a 211-entry NAAD snapshot (100 unique CAP documents, 92 `status=Actual`), ECCC emitted `msgType=Cancel` exactly **once**, and that single instance came from a non-ECCC sender. Every genuine end-of-life transition was instead carried in a vendor parameter, `layer:EC-MSC-SMC:1.0:Alert_Location_Status`, taking the values `active`, `ended`, or `transitioned_out`, while `msgType` remained `Update` throughout. An integration that trusted `msgType` would hold an ended warning live until its `expires` timestamp — which for `ended` blocks in that sample was a median of one hour past issue, and for `transitioned_out` blocks was *zero*, i.e. already expired at the moment of issue. This is the same defect class filed twice against DWD (§8.1), arriving from a different authority through a different mechanism.

The core model therefore does not require `phase` to be a pure function of `msgType`. Providers supply a normalized termination hint — `CAPAlert.lifecycle_status` in the reference implementation — and the platform retires the incident on a recognized terminal value regardless of what `msgType` claims. Recognition fails open: an absent or unrecognized status means active, so a provider that does signal termination conventionally is unaffected.

**Which terminal phase it lands on is decided by the clock, not by the signal.** An earlier draft of this paragraph said a recognized terminal status maps to `expired`, and that is wrong in the case the distinction exists for. A status observed while `expires` is still in the future means the authority ended the incident *early*, which is `cancel`; `expired` is reserved for an incident that ran to its published expiry, and is checked first so an incident already past that timestamp stays `expired` even when the feed also marks it ended. Collapsing the two would throw away exactly the news a consumer wants — ending early is a fact about the hazard, reaching a published expiry is a fact about a timestamp — and it would put an announced early ending on a worse footing than a silent one, since a vanished incident is already inferred as `cancel` when its expiry is still ahead (§2.5). The reference implementation resolves both in one place, so the announced and inferred paths cannot drift.

A second, weaker failure mode runs the other way. A provider may revise an incident substantively without changing `msgType` at all; GDACS publishes `Alert` on every re-issue regardless of how many an event has had, so its revisions are visible only through `sent` and the episode id. The shipped provider keeps that as published rather than synthesizing an `Update` the feed never claims. Here `phase` can read `new` across a real revision. The `incident_updated` event (§2.3) still fires correctly off the field delta, so automations stay reliable. Consumers needing exact transition semantics should trust the event stream and `changed_fields` over the `phase` attribute.

**One CAP document is not necessarily one incident.** The model above quietly assumes a document maps to an incident. Live CAP breaks that assumption, and the platform has to say what the identity unit actually is.

ECCC segments a single CAP document into one `<info>` block per (language × area-group). Each block carries its own `<area>` polygons and geocodes, and its own severity, urgency, headline, `expires`, and `Alert_Location_Status`. The lifecycle is therefore **per area-group, not per document**: one document can be simultaneously `active` over one set of regions and `ended` over another, as a storm clears part of a warning area while continuing over the rest. In the snapshot above, 19 of 92 `Actual` documents were mixed or wholly-ended in exactly this way. The Atom envelope mirrors the split, emitting a separate entry per (language × status group) — all pointing at the same shared CAP body — which is why 211 envelope entries collapsed to 100 documents.

Two consequences for the platform contract:

1. **Selection is region-scoped.** An integration must choose the `<info>` block matching the *user's configured region*, not the document's first block. Taking `infos[0]` is the natural implementation and is wrong: in the sample it was always the `active` block, so a warning that had ended over the user's actual location would present as live, with the wrong severity, headline, and expiry attached. The reference implementation resolves this in `_select_region_info`, preferring a non-terminal block among those whose area matches the configured region, and treating the incident as terminal for that region only when every region-matching block is terminal.
2. **Identity remains per-incident-per-region, not per-document.** Because the user subscribes to a location, the incident the entity represents is the intersection of the document and the configured region. The `unique_id` contract in §2.2 is unaffected — the lifecycle hash is computed from the selected block — but the platform must not assume that two consumers of the same document in different regions see the same lifecycle. They legitimately do not.

This is provider-specific in its encoding and general in its shape: any authority that issues one message covering multiple areas with independently-evolving conditions has the same problem. The domain does not need to model area-groups natively — the provider layer resolves them before a `CAPAlert` is produced — but the platform contract must not forbid it by defining identity at document granularity.

### 2.3 Event Schema

All three integration-fired events carry the same payload, so automations can be written against the schema without branching on event type:

```yaml
event_type: incident_created | incident_updated | incident_removed
data:
  entity_id: incident.<slug>_<hash>   # omitted on first sighting; see below
  incident_id: <unique_id>            # stable lifecycle hash
  event: <short event name>
  severity: extreme|severe|moderate|minor|unknown
  phase: new|update|cancel|expired    # terminal on incident_removed
  phase_changed: bool                 # true when this fire represents a phase transition
  changed_fields: [<attr>, ...]       # populated on incident_updated; empty list otherwise
  removal_reason: superseded|ended    # incident_removed only; omitted when unknown
```

`changed_fields` is an allowlist, not a diff of everything that moved. The reference implementation reports `headline`, `description`, `instruction`, `severity_normalized`, `phase`, `expires`, and `area_desc` — the fields a consumer would re-notify on. Timestamps that shift without changing what a user needs to know (`sent` on every re-issue, `onset` as a merged multi-day episode rolls off its earliest day) are deliberately excluded, because an allowlist that includes them turns every poll into an update event.

**The name oversells it, and a v1 contract should say so explicitly rather than leave consumers to discover it.** `changed_fields` is *a consumer-relevant subset of what changed*, not a complete object diff: a field's absence from the list does not mean the field is unchanged. A consumer needing true diff semantics has to compare states itself. The honest name is closer to `notify_on`, and a core platform adopting this contract should either rename it or define it in exactly these terms at the point of specification — the failure mode is a consumer that trusts the list to be exhaustive and silently misses a change, which is worse than having no list.

`entity_id` is absent on the first `incident_created` for an incident, because the event fires from the store before the entity has been registered. Automations keying on `entity_id` must tolerate its absence on creation; this is the one place the "same payload regardless of event type" property does not hold, and it is a consequence of the event stream being authoritative over entity timing rather than the reverse (§2.6).

**Removal carries the terminal phase, not the phase it had while alive.** An earlier build of the reference implementation emitted the *previous* phase on `incident_removed` — typically `new` or `update` — which made the payload useless for the question automations actually ask at that moment. The contract is now that `phase` on removal is always `cancel` or `expired`: `cancel` when the provider explicitly cancelled *or* the incident vanished from the feed before its published `expires`, and `expired` when `expires` has passed. Inferring `cancel` from an early disappearance is deliberate — for automation purposes "the authority dropped it" and "the authority cancelled it" are the same fact, and §2.2 has already established that authorities frequently do the former while never issuing the latter.

**`removal_reason` says why; `phase` only says when.** This is the payload consequence of the `Alert_Location_Status` finding in §2.2, and it is the reason that finding matters beyond correcting a single provider's expiry handling. `phase` locates the ending relative to the published `expires` and stops there, which collapses two endings a consumer must treat differently. An all-clear and a supersession — a watch upgraded to a warning over the same area — are identical under `phase` alone, yet one means the hazard is over and the other means it just got worse. A notification automation that cannot tell them apart either stays silent on an escalation or announces an all-clear that is false.

The two recognized values are `superseded` (the area moved to a different incident, which arrives as its own document and fires its own `incident_created`, so a message-budgeted automation can skip the removal because the creation carries the same news) and `ended` (the incident stood down for this area). They are an axis independent of `phase`: either value pairs with either terminal phase, so the two must be read separately rather than collapsed into one status enum. `expired` + `superseded` is the routine shape for ECCC `transitioned_out`, whose documents carry `expires ≈ sent`; `cancel` + `ended` is a plain early stand-down.

Three constraints on reading it. **It is omitted, not defaulted, when unknown** — absence means the provider published no recognized signal, never "not superseded". Today ECCC is the only source in the reference implementation that supplies one at all; every other provider terminates without a stated reason, so a consumer that treats absence as `ended` will be wrong on the majority of feeds rather than the minority. **It is scoped to the area group the entity represents**, not to the CAP document (§2.2): one document can be superseded over one region while remaining live over another, so this is never an all-clear for a neighbouring area. And **the successor's `incident_id` is not on the payload.** Publishing it would be more useful, but whether CAP `<references>` reaches across event types — an ECCC watch and warning are separate chains — is a feed-behavior question that needs a captured upgrade to answer, and the platform should not specify a link it cannot yet populate truthfully. A v1 contract can add the pointer later; it cannot withdraw one that providers turn out to be unable to supply.

**Extension fields.** The reference implementation additionally carries `entry_id` (which config entry produced the incident — needed once an install has two, say adjacent NWS zones) and `area_desc` (denormalized onto the event so an automation can compose a notification without a state lookup against an entity that may already have been removed). Neither is proposed for the core contract: `entry_id` has no meaning in a core platform that has not settled its device and entry model (§2.1), and `area_desc` is a convenience that duplicates state. They are recorded here because the reference implementation emits them and a reviewer comparing the two surfaces will see them.

### 2.4 Geometry Handling

Complex multipolygon GeoJSON for a severe-weather warning can easily exceed 16 KB on its own. Storing it in the state machine would recreate the failure mode this RFC is trying to fix.

The design:

- The state machine stores only a bounding box (`bbox`, 4 floats) and a `geometry_ref` handle.
- Full GeoJSON is held in a bounded in-memory LRU cache within the integration, keyed by `geometry_ref`, and is served by a standard `HomeAssistantView` at `GET /api/incident/geometry/{geometry_ref}` — keyed by the same handle the cache is keyed by, so that geometry shared between incidents resolves once, and a websocket command carrying the same handle (see below). `camera` proxies image streams and `media_source` serves local files the same way: an established route for delivering large payloads that don't belong in the state machine, with authentication delegated to the view's standard `requires_auth = True` decorator.
- Frontend cards fetch geometry lazily. Typical Lovelace renders never need it; map cards fetch once per visible incident.
- Long-form text is bounded at the payload, not the field. `description`, `instruction` and their §2.7 localized siblings are published in full, and the serialized attribute set is measured against the recorder ceiling the way the recorder measures it (§7.2). Only an incident that would not fit is trimmed, in a fixed priority order that spends the alternate language before the primary and the description before the instruction. An earlier draft of this bullet soft-capped `description` and `instruction` at 4 KB each and required the same of the localized copies. Measurement retired it: a per-field cap fails in both directions at once, shredding an 8,871-byte tropical statement that serialized to 14,290 bytes and fit, while leaving untouched the air-quality warning that overflowed at 19,084 bytes on 9,535 bytes of text (§7.2). Trimmed text is truncated with a trailing `…` and the discarded text is **not** recoverable from the platform. The geometry endpoint serves geometry and has no text equivalent, and inventing one is not warranted: the distribution of description sizes has no tail the budget fails to bound, which is exactly what distinguishes text from the polygons this section externalizes. A consumer needing the untrimmed text follows `web` or `url` to the issuing authority's copy, which is a real limitation and worth stating rather than papering over, and one that §7.2's sweep found no live incident currently paying.

**The handle must be namespaced by the scope that owns the store, not by the provider alone.** This reads like an implementation detail and is not one: it is the difference between a correct lookup and a silent cross-tenant leak. The obvious `{provider}:{alert_id}` shape breaks as soon as one store serves more than one configuration, because provider alert ids are unique only within a provider's own feed. Two config entries against the same provider — the overlapping-zone case §2.2 already invokes to justify prefixing `unique_id` — mint identical handles for an alert covering both, and whichever entry writes second wins the cache slot. The reference implementation therefore keys on `{entry_id}:{provider}:{alert_id}` and treats the composite as opaque, and a core store must namespace at least as widely as it is shared: per config entry for the v1 in-integration store, and per *integration* as well for the cross-integration store sketched in §6.2, where the collision domain widens to every provider any integration ships. Consumers parse no part of the handle; it is a cache key that happens to be a string, and constraining its internal structure is what lets the namespacing widen later without a client-visible change.

**Geometry is more than polygons.** CAP 1.2 §3.2.4 gives an `<area>` two coordinate-bearing shapes, `<polygon>` and `<circle>` (center point plus radius), alongside the code-bearing `<geocode>`. There is no `<point>` element — an earlier draft of this paragraph invented one — and a point arrives as a circle of radius zero, which is how NSW RFS publishes a street-address incident marker. The reference implementation materializes polygons, and points from those degenerate circles; a circle with a real radius it deliberately leaves unmaterialized, since GeoJSON has no circle type and approximating one as a ring would invent precision the feed never published. The `bbox` + `geometry_ref` contract accommodates all three without change — a circle's `bbox` is its bounding square, and whatever the store holds is served through the same endpoint.

CAP defines an area carrying several shapes as their *union*, with no precedence among them, so a v1 platform has to say which one the single `geometry` slot holds. The reference answer: the richer areal shape wins the slot, points are published alongside it in a separate `points` attribute (§2.1), and points become the geometry only when no polygon exists at all — which is what gives a point-only incident a usable degenerate `bbox`. Representing the union honestly would mean a `GeometryCollection`, and every consumer downstream of the slot (the `bbox` derivation, the point-in-polygon filters, the card's map) would have to learn it for a case none of them has yet met.

Point and circle events also sharpen the externalization argument with a non-weather case. GDACS encodes even a point hazard as an area — an earthquake ships a ~100 km circle around the epicentre, rendered as a many-vertex ring approximating it — so a single point event inflates to KB of redundant coordinates, precisely the inline-payload bloat this section externalizes, now arriving from a geophysical feed rather than a squall line. This was first observed while probing the GDACS CAP endpoint during design work; the shipped provider gets there by a different route, fetching a per-episode GeoJSON file (2–10 KiB for every hazard but cyclones) because that endpoint turned out unusable (§4.1).

**Both an HTTP view and a websocket command, after implementation experience.** An earlier draft of this RFC argued against a bespoke websocket command in v1, on the reasoning that geometry is a one-shot fetch per card render and the HTTP view delivers the same payload with less new core surface. Building the companion card falsified the premise behind that reasoning. The argument was about *subscription* value — polygons change slowly, so live push buys little — but the actual cost of the HTTP path is not subscription, it is authentication. A Lovelace card already holds an authenticated websocket connection to HA; fetching over HTTP instead requires the card to obtain and attach a bearer token out of band, which is friction on every card that wants a map and a second auth path to get wrong.

The reference implementation therefore ships both: `GET /api/cap_alerts/geometry/{geometry_ref}` for external and non-frontend consumers, and a `cap_alerts/geometry` websocket command returning the same GeoJSON `FeatureCollection` for cards. They are thin wrappers over one store, so the duplication is a few dozen lines rather than a second subsystem. A v1 `incident` platform should expose both for the same reason, and the original rejection is retained here only because the reasoning behind it — that geometry does not need a live *subscription* — still holds. Neither surface subscribes; both are request/response.

**This is not a novel surface; `weather` already ships it.** When forecasts moved out of `weather.*` attributes and into the `weather.get_forecasts` action, core did not leave cards to call the action: the same migration shipped a domain-specific WebSocket command for the frontend, `weather/subscribe_forecast`, in `homeassistant/components/weather/websocket_api.py`, carrying the change signal an action response lacks (§1.6). The geometry contract proposed here is the same arrangement with a weaker requirement: `weather` needed a live subscription because forecasts refresh on a schedule, whereas polygons change slowly enough that request/response suffices. A reviewer who accepts `weather/subscribe_forecast` has already accepted the shape of `incident/geometry`, and the precedent generalizes past geometry — it is the mechanism by which any part of the incident payload could be externalized in future without stranding the cards that consume it.

**Why in-memory instead of `.storage/`.** A large fraction of Home Assistant deployments run on Raspberry Pi hardware with SD-card root filesystems. Routing full GeoJSON payloads through flat-file `.storage/` would trade the 16 KB recorder ceiling for a different failure mode: during severe-weather outbreaks, CAP polygons update every few minutes as storm cells move, and sustained writes of hundreds of KB per cycle would meaningfully accelerate SD wear. Geometry is also ephemeral: it has no value once an incident expires, and the integration can always re-fetch it from the upstream feed. Because there is no correctness requirement that it survive a restart, disk I/O isn't indicated. The in-memory cache recovers on its own: on restart, the next successful poll repopulates it.

**Cache lifecycle and memory bounds.** The geometry cache is a bounded LRU keyed by `geometry_ref`, modeled on the reference implementation's existing `CAPContentCache` (a fixed-capacity `OrderedDict` that evicts the least-recently-used entry past a hard ceiling). Two mechanisms evict entries. An incident's geometry is dropped when it terminates (§2.5 `cancel`/`expired`); separately, the entry cap evicts the least-recently-used polygon whenever the cache is full. The cap is what bounds the cache if a termination is never observed (say a provider drops an alert from the feed without issuing a cancel), so a missed cancel costs some extra retention rather than a slow leak. The cache is bounded by **total bytes**, not just entry count. This is the single most important detail in the section, because an entry count is the obvious bound and it does not work: real CAP geometry is heavy-tailed, so a cap of N admits a worst case of N times the *largest* polygon rather than N times a typical one.

The distribution is measurable, and the measurement below is **NWS-specific evidence for a general cache-design principle** — it characterizes NWS forecast-zone geometry, not CAP polygons at large, and no claim is made that ECCC, MeteoAlarm, or an arbitrary publisher shares its exact shape. What generalizes is the failure mode: any population with a heavy tail defeats an entry count, and geometry is heavy-tailed wherever administrative boundaries are, which is everywhere coastlines and archipelagos are. A core implementer needs the bound to be byte-based; they do not need these particular numbers to be theirs.

Taking NWS forecast zones as the population — the shapes a zone-based alert resolves to, and the largest such set that is publicly enumerable — a full census of all 11,888 zone records from the NWS bulk shapefiles gives 8,975,685 coordinate pairs, a median zone of roughly 190 points (~2 KB serialized), and 75% of zones under 400 points. The largest single zone, `AKC198` (Prince of Wales-Hyder, Alaska), is 93,667 points — roughly three-quarters of a megabyte in a single fetch: a 1,700x span between the smallest and largest member of the same population, with the median sitting near the bottom of it. An entry-count cap sized against the median under-provisions by three orders of magnitude against the tail, and the tail is not rare in the way its share of the population suggests — Alaskan and coastal zones appear in exactly the marine and winter-storm alerts a user in those regions cares most about. A hard byte ceiling keeps the footprint flat regardless of polygon size, evicting least-recently-used entries until the incoming one fits.

The reference implementation reached the same conclusion the hard way in a different cache and generalized it: `CAPContentCache`, which holds fetched CAP XML bodies, was entry-count-bounded until body sizes were found to span two orders of magnitude, and moved to a byte budget for precisely this reason. The failure mode is worth naming for a core implementer because it does not present as a memory bug — it presents as a browser or integration that worked all season and then evicted its whole cache during the one storm that mattered. The reference implementation's `GeometryStore` sets that ceiling at 5 MB (roughly 500 alerts at 10 KB each) and accounts each entry by its serialized length on insert. Memory is therefore capped by the byte ceiling rather than growing with polygon size or with the number of incidents seen over a session. **The contract is that the bound is expressed in bytes; 5 MB is an implementation parameter, not part of it.** A core implementation should be free to tune the number, or make it configurable, without that being a change to the platform contract — what it must not do is substitute an entry count for it.

One implementation note, recorded because it is a real residual cost rather than a solved problem: the reference store keeps decoded objects and re-encodes per request, so a dashboard with several map cards open re-serializes the same polygon repeatedly on the event loop. Caching the serialized bytes alongside the object would remove that, at the cost of holding both representations or of serving pre-encoded bodies the websocket layer would have to re-parse. A core implementation should make this choice deliberately; the RFC does not mandate either, and the byte-ceiling contract above is unaffected by it.

On restart the cache starts empty and the next poll refills it. If upstream is unreachable then, `bbox` still lives in the state machine and is restored by `RestoreEntity` (§2.5), so map cards draw the bounding box while the full-polygon endpoint returns `404` until a poll succeeds. An incident shows as a rectangle until the next poll re-fetches its detailed shape, rather than vanishing. That is the specified behavior rather than an observed one: the reference implementation's alert entities do not inherit `RestoreEntity` yet, so today nothing is restored and the rectangle is not drawn either (§5).

**A retained incident degrades the same way, and the contract should say so.** The reconciliation that retains an incident under §2.5 is by definition one that did not observe it, so it carries no geometry to re-insert, and the reference implementation drops the polygon on that cycle while the entity stays live and stale. The handle on the entity therefore outlives the payload it points at: `geometry_ref` is still published, and the endpoint answers `404` for as long as the retention lasts. That is the same rectangle-not-nothing degradation as the restart case, arriving from the other direction, and it is a consequence of geometry being ephemeral rather than a defect in the retention rule. A core implementation may prefer to hold a retained incident's polygon until it terminates; what the contract requires either way is that a `geometry_ref` is a cache handle whose miss is normal, never a promise of retrievability, so consumers keep `bbox` as the fallback and treat `404` as "draw the box".

**Optional v2: shared geometry store.** A core-managed geometry store (analogous to `image` or `media_source`) would enable cross-integration polygon reuse, for instance NWS and a local emergency feed sharing county geometry, and would survive restarts without re-polling upstream APIs. This is an attractive direction but explicitly orthogonal to v1 and not required for core adoption. The HTTP view is storage-backend-agnostic, so a store can plug in behind it without a client-visible change. See §6.2. Independent CAP deployments reach the same conclusion from the payload side: SAMBRO implementers recommend the rendering agent keep its own copy of the geocode/shape-file database rather than inlining polygons in every message, precisely to cut payload size and network load when one alert fans out to many recipients (§8.4) — the same reasoning that keeps geometry out of the state machine here.

**Size budget.** With geometry externalized, a single incident's attribute payload models out at roughly **7.2 KB typical** (§7.2), against the 16,384-byte ceiling, and the recorded payload is bounded at 15,800 bytes by construction: the entity measures what it is about to publish and trims long-form text until it fits. That is the bound §1.1's second claim needs, and it is a claim about the schema rather than about the data. An earlier revision of this paragraph modeled a 27 KB cap, 1.7x over the ceiling, and named three groups of fields as the cause: localized duplicates of the then-capped long-form text, provider passthrough, and a geocode surface stored three times over. §7.2 records how each was closed. The first bound stands on its own regardless: one incident per storage unit makes the recorder footprint independent of incident cardinality however the per-incident payload is bounded.

### 2.5 Entity Registry Cleanup

Incidents are transient. A naïve implementation that leaves registry entries behind would accumulate 50+ dead `incident.*` entries per storm season, bloating `.storage/core.entity_registry` and cluttering the UI.

Rules:

- On `cancel` or `expired` phase, the integration calls `entity_registry.async_remove(entity_id)` in the same coordinator cycle that fires `incident_removed`.
- Registry mutations are batched per coordinator cycle. All additions in a cycle go through a single `async_add_entities()` call, and all removals are issued together before the coordinator yields. This avoids the sequential-per-entity I/O churn on `core.entity_registry` that core reviewers have flagged as an anti-pattern, even during a regional outbreak that adds and removes dozens of incidents in one poll.
- Device registry entries are retained (one device per config entry); we do not create a device per incident.
- Recorder history is untouched at the database level: state rows for the removed `entity_id` are not purged, and time-range queries (`states_during_period` and friends) still return them. There is a real tradeoff here, though. Once the entity is gone from the registry, the native HA History dashboard renders past incidents with only the slugified `entity_id`, without friendly name, icon, or area mapping. The state series is preserved; the UI polish around it is not. Users who need rich historical audits (after-action reports, insurance timelines, compliance logs) should subscribe to `incident_removed` and forward the full payload to an external sink — InfluxDB, Postgres, a notification service — rather than rely on the built-in History UI. §6.4 describes the recommended archival pattern.
- Removal must be idempotent: calling it on an already-missing entity is safe.
- Restart mid-storm: the integration does not persist coordinator state or geometry to disk and does not write to `.storage/` beyond what every HA entity already does. Continuity across restarts uses only HA-native mechanisms. The entity registry (already in `.storage/core.entity_registry`) keeps the entity's existence across the restart. `IncidentEntity` inherits `RestoreEntity`, so HA restores the last recorded state and attributes from the recorder database between entity add and first `async_write_ha_state`, making an active warning visible immediately rather than flashing `unknown` while the first poll runs. Restored state is validated against the incident's own data rather than trusted outright; see **Restored data is bounded, not trusted** below. The first successful *upstream reconciliation* after boot is authoritative: if the incident is still present upstream, the entity is re-validated with fresh data and any staleness marker is cleared; if it is gone (cancelled or expired during downtime), the normal §2.5 termination path executes. "Reconciliation" rather than "poll" is deliberate — see **Ingest mode is not part of the contract** below, since a streaming provider reaches the same authoritative state through a backfill fetch rather than a scheduled poll. The idempotent-removal rule covers the case where a removal was partially applied before the restart.
- Startup reconciliation scrubs orphans. A hard crash can leave the registry holding `incident.*` entries whose termination was never observed — the crash beat the batched removal, or the incident was cancelled while HA was down. On the first successful poll after boot, the integration diffs the registry's `incident.*` entries for this config entry against the active set the poll returns and runs the termination path for any entry not present, subject to the expiry rule below so a still-valid incident missing from a single flaky poll is not removed prematurely. The registry converges back to the 1:1 active-incident invariant within one poll of recovery, without depending on having observed the cancellation phase.

**Upstream feeds are lossy, and the platform must not treat feed absence as ground truth.** The reconciliation rule above says an incident missing from the active set is terminated, hedged with "subject to the expiry rule so a still-valid incident missing from a single flaky poll is not removed prematurely." That hedge is doing more work than it appears to, and field observation is the reason it exists.

While migrating the ECCC provider between two officially sanctioned endpoints for the same national alerting system, we sampled both simultaneously and found they did not agree. Alerts that were live, `status=Actual`, `scope=Public`, and well inside the documented retention window were present on one endpoint and entirely absent from the other — including, in one observation, an evacuation alert carrying `severity=Extreme`/`urgency=Immediate`/`certainty=Observed` for a wildfire. Repeated sampling showed the disagreement is *unstable* rather than a fixed omission: the set of missing alerts churned from one sample to the next, with alerts absent in one minute's fetch present in the next and different ones missing instead. Back-to-back fetches were byte-identical, so this is temporal variation in what the endpoint serves, not per-request load balancing; every response parsed cleanly and ended well-formed, so it is not truncation either.

Two caveats bound this evidence, and both matter. The sampling window was minutes, not days, so the churn *rate* is uncharacterized — we can say the disagreement is real and not a one-off, but not how often a given alert is affected. And the specific alert identities are deliberately not reproduced here: the set self-falsifies within minutes, so a static list would read as a stronger and more permanent claim than the data supports. The condition has been reported to the feed operator.

**The rule, stated as an algorithm**, because prose about "gating on expiry" left it underspecified and the reference implementation drifted into doing none of it. On reconciling an incident that is absent from the incoming set:

```
if the provider signalled termination:    terminate (cancel; removal_reason where published)
elif now >= expires:                      terminate (expired)
elif the source declares absence-ends:    terminate (cancel)
elif expires is published:                retain, mark stale, record last_confirmed
elif the source can still end it:         retain, mark stale, record last_confirmed
else:                                     terminate (cancel)
```

There is deliberately no count of consecutive misses. A count assumes discrete reconciliation rounds, which requirement 9 forbids the model from assuming — under a pushed stream absence is never observed at all, only inferred from a periodic backfill — and it couples the safety margin to the poll interval, an unrelated user preference. Retention is bounded instead by the authority's own `expires`, which is self-limiting, ingest-neutral, and needs no constant anyone would have to justify. The third branch is the only place where a missing observation is, by itself, treated as a termination signal, and it is a property of the *source's contract* rather than of any one message: a source may declare that withdrawing a record is genuinely how it announces an ending. An incident that publishes no `expires` is the case that makes the distinction matter, and it is where the rule earns its last two branches. Such an incident cannot be terminated by time, so retaining it is only safe if *something else* can end it. **Retention requires an exit**, and there are exactly two others: a source that publishes a terminal vocabulary, or a provider that goes and fetches the terminations its active feed omits. A source with neither has no exit at all, and retaining its expiry-less incidents would leave entities that outlive the hazard indefinitely — so for that case, and only that case, absence stays authoritative.

The distinction is measured rather than defensive. Across the 113 WMO authorities serving CAP, 20 of 510 `<info>` blocks published no `<expires>`, and for two of them — Macao and Curaçao — it was every alert, with nothing in the RSS envelope to fall back on. WMO publishes no terminal vocabulary and fetches no terminations, so an expiry-less incident there would never have been removed. The lesson for a platform contract is to derive the rule from what the source can do rather than from what one message happens to contain: a field omitted from a single message is not a statement about the source's contract, but a source that can never say "this ended" is, and the two have to be told apart.

Two notes on the state of this rule, since it is stated as an algorithm and an algorithm invites checking. The reference implementation now matches it branch for branch, including the scope-change suspension and the supersession carve-out below; that was not true when the rule was first written, and the §5 note about the RFC being right where the code was wrong refers to this rule. And the third branch has **no user among the shipped sources**. Every provider in the reference implementation declares the retaining policy, GDACS included — the one source whose incidents genuinely end by withdrawal reaches termination through the *last* branch instead, having no expiry, no terminal vocabulary and no termination lookup to retain on. The branch is worth keeping in a core contract because it is the honest place for a source that announces endings by dropping records, but a v1 implementation should expect to ship it unexercised, and should resist the temptation to declare it for a source merely because that source's incidents keep disappearing.

Two clarifications this needs to be safe. Retention compares like with like, so a change of **scope** — a tracker crossing a border, a reconfigured zone, a filter toggled — suspends it for that cycle: those incidents have gone out of scope, not unobserved, and holding them would strand a warning from a place the user has left. And **supersession the platform can see but the incoming list cannot** must not read as absence: a revision whose geometry moved off the user is filtered out before reconciliation, yet the incident it replaces has genuinely been replaced.

**Absence is not the only thing a lossy endpoint hides.** NWS is the sharper case, because there the missing signal is the *termination* rather than the incident. Cancellations are published as first-class VTEC `CAN` products, and in a six-hour national window 101 of 101 of them were absent from `/alerts/active`, the endpoint an integration would naturally poll — 0 of 174 terminal products appeared there at all. An integration polling only the active endpoint therefore cannot distinguish a cancellation from a dropped record, and expiry-gated retention would hold every cancelled warning to its published expiry. The reference implementation now queries the cancellation endpoint separately, scoped to the configured zone, which returns single digits. The general lesson for a core platform: where a source publishes termination somewhere other than the endpoint carrying the active set, retrieving it is provider-layer work, and the platform's absence policy is the fallback for when that retrieval is unavailable rather than the primary mechanism.

The design consequence is what belongs in a platform contract, independent of which operator or which country: **an integration's view of "the active set" is a view of one source's current answer, not of reality.** The reference implementation's response was to stop trusting either endpoint alone and union both, but that is a provider-layer remedy. What the core platform owes is the weaker guarantee that absence from a single observation does not immediately destroy an entity — which is why termination is gated on expiry where a feed supplies one (§2.5, mechanism 1), and why the `stale` flag exists rather than an immediate purge. A platform that treated every feed as authoritative and every absence as a cancel would, against a real government feed observed in the field, have silently cleared an Extreme wildfire evacuation alert from a user's dashboard while it was still in effect.

**Ingest mode is not part of the contract.** This RFC was drafted assuming a polling coordinator, and much of the language above still reads that way. Implementation experience says the platform must not assume it. The reference ECCC provider now defaults to a persistent TLS socket to the NAAD streaming endpoint, receiving CAP documents as the authority emits them, with the GeoRSS feed demoted from primary source to periodic backfill and gap-recovery path. Alerts arrive in seconds rather than at the top of a poll interval, which for a tornado warning is the difference that matters.

Three things follow for the core contract, none of which require the platform to know how data arrived:

- **"Poll" is the wrong word in the contract; "reconciliation" is the right one.** Every place this RFC says the first successful poll is authoritative, the requirement is really that the integration has re-established a known-good view of the upstream active set. A streaming provider does that by completing its reconnect backfill. The startup-reconciliation rule (above) and the staleness-clearing rule are both stated against that event, not against a timer.
- **Liveness becomes observable state.** A poller that fails is visibly failing — the coordinator raises and HA marks entities unavailable. A socket that silently stops delivering looks exactly like a quiet weather day, which is the more dangerous failure. The NAAD feed's 60-second heartbeat is what distinguishes them, and the reference implementation surfaces socket state as a diagnostic connectivity entity so the condition is visible to the user and automatable. A core `incident` platform should expect providers to expose ingest health somewhere; this RFC does not prescribe the shape, but "the integration is connected and silent" and "the integration is broken" must be distinguishable.
- **Push ingest changes the batching calculus, not the batching rule.** Streamed documents arrive one at a time rather than in poll-sized groups, so the naive implementation performs a registry mutation per alert. The batching requirement above still holds; a streaming provider satisfies it by coalescing arrivals into a single update pass rather than by relying on the poll cycle to group them for free.

**Restored data is bounded, not trusted.** Restoring last-known state across a restart raises a life-safety hazard: if the same outage that rebooted HA also took out upstream connectivity, a restored entity would assert an active Tornado Warning that may have expired or been cancelled during the outage, with no indication to the user that the data is stale. Two mechanisms bound this without blanking the dashboard:

1. **Expiry is enforced offline, where a feed supplies it.** An incident that carries an `expires` (and `ends`) timestamp has it restored alongside the rest of its attributes. At boot, before any poll, the integration drops or terminates any restored incident already past `expires`; it does not need upstream to know that a warning's stated lifetime is over, so the "expired 45 minutes ago" case resolves locally. Dropping a consumed alert once its `expires` has passed is the same rule national CAP brokers apply to bound their own stores — SAMBRO deletes a relayed message from its database on the `<expire>` timestamp of the CAP message (§8.4). Not every feed supplies an expiry, though: some alerts are open-ended, valid until the issuer cancels them. An expiry-less incident cannot be aged out offline, so it stays flagged stale (mechanism 2) until a poll reaches upstream — at which point the authoritative-poll path above either re-validates it or, if it is gone from the feed, terminates it. Such an incident therefore does not survive a successful re-poll that no longer lists it; what it loses, relative to an alert with `expires`, is the ability to self-invalidate while upstream stays unreachable.
2. **Unverified incidents are flagged, not hidden.** A restored incident still within `expires` keeps its `state` (severity) and full content but carries `stale: true` and `last_confirmed` (§2.1) until the first successful poll re-validates it. It is **not** set `unavailable`: an `unavailable` state makes stock cards and `auto-entities` drop the entity entirely, which would blank the warning during the window it might still matter. Keeping the entity live and visible, with staleness exposed as an attribute, lets the domain-aware card (§2.6) badge it ("as of HH:MM, unverified") and lets automations self-invalidate against `expires`, while keeping the incident visible across the restart.

The residual exposure is narrow: an incident cancelled by the issuer before its stated `expires`, while HA is also offline, shown unbadged on a stock card that ignores the `stale` flag. The alternative, a blank dashboard mid-storm, is worse. Staleness is surfaced as a badge rather than as an `unavailable` state for that reason.

**Why the churn is deliberate.** Registry mutation on terminate is the most-criticized part of this design. We chose it because persisting temporal external data across the restarts that accompany severe weather forces a three-way choice:

1. **Hold active incidents only in memory.** Fails on restart: a power blip or brownout mid-storm leaves the dashboard blank until the next poll completes, and leaves the home blind if the same outage took out upstream connectivity. Unacceptable for life-safety data.
2. **Persist CAP data to a custom `.storage/` store.** Survives restart, but writes CAP payloads to disk every poll cycle as storm cells update: the SD-card wear failure mode §2.4 rejects.
3. **Use the entity registry + `RestoreEntity` + recorder (this RFC).** Survives restart on HA-native machinery, with only sparse attributes (never geometry) touching disk. The cost is registry add/remove traffic at incident boundaries, batched per cycle (above).

So the churn is the cost of surviving a restart without accelerating disk failure, and the batching rule keeps it bounded even under regional fan-out. It is not the only restart-survivable design (a non-entity abstraction could persist sparse data just as cheaply, §3.6), but it is the only one that also keeps native UI and history integration.

**On lost customizations.** The objection usually bundled with registry churn is that deleting an entity destroys user customizations: renamed entities, custom icons, area assignment. That presupposes the entity is a customization target. Incidents are not: many last minutes (a tornado warning may expire in fifteen minutes), are dictated entirely by an external authority, and carry nothing the user can edit. Nobody opens the settings dialog to rename a warning that will be gone before they finish typing. The registry value the objection protects (durable, user-curated handles for persistent hardware) does not apply to ephemeral external events. The one customization that does make sense for an incident, a per-event-type icon, is set by the integration from the event taxonomy rather than per-entity by the user (§2.6).

The net effect is that at any moment, `incident.*` entries correspond 1:1 with currently active incidents.

### 2.6 Presentation Hints

The entity contract separates what happened from how bad it is, so frontends can style both axes independently without re-parsing attributes.

- Icon conveys event type. The integration sets `icon` from the provider's event taxonomy (Tornado Warning → `mdi:weather-tornado`; Boil Water Advisory → `mdi:water-alert`). Icons stay stable across severity changes for the same event class.
- Severity is the entity `state`. Cards and themes style by state (`extreme`, `severe`, `moderate`, `minor`, `unknown`) using standard CSS. No per-severity icon variants are needed. `weather` and `binary_sensor` already theme by state the same way.
- Phase (`new`, `update`, `cancel`, `expired`) is available as an attribute and through the `incident_updated` and `incident_removed` events. Frontends are encouraged to surface phase transitions (striking through cancelled incidents, badging updates), but the specific presentation is up to card authors. The domain exposes the signal and does not prescribe the UI.

Integrations must populate `icon`; they should not encode severity into it.

**No acknowledgment or dismissal service.** The domain intentionally does not expose `incident.dismiss` or `incident.acknowledge`. Entities mirror upstream reality: a tornado warning is active until NWS cancels or expires it, and a user clicking "dismiss" on their phone does not change that fact for anyone else in the household, nor should it. Local "I've seen this" state is a frontend concern, handled by cards via browser local storage (keyed on `incident_id`) or by user-level automations that maintain a dismissed-hash list. Keeping the entity pure preserves multi-client consistency and prevents the backend from growing a per-user UI-state layer it has no business owning. Card authors are expected to surface dismissal UX; the domain surfaces the ground truth the UX operates on.

**Capability detection is by attribute and domain introspection, not by a version string.** Downstream consumers (custom cards, Alert2, blueprints) check `state.domain == "incident"` or probe for specific attributes, as elsewhere in HA core.

The reference implementation appears to contradict this — it stamps an `incident_platform_version` attribute on every alert entity, and its card-author documentation instructs consumers to branch on it — so the divergence is worth stating plainly rather than leaving a reviewer to find it. The version string exists there precisely *because* the domain does not. A custom component cannot mint a domain, so every entity it publishes is a `sensor.*` indistinguishable by domain from a thermostat's battery readout; `state.domain` answers nothing, and a card asking "is this an incident?" has no primitive to ask with. The version attribute is a stand-in for the missing domain, doing double duty as a contract marker because there is no platform whose version could be inferred from the HA release. Adopting `incident` into core retires it: `state.domain == "incident"` becomes available, versioning follows the HA release as it does for every other platform, and the attribute can be dropped from the schema. That the reference implementation had to invent one is evidence for the domain, not an argument for standardizing a version field in it.

**Consuming dynamic entities.** Because incident entities spawn and despawn with the events they represent, they are meant to be consumed two ways, and the model should be judged against these rather than against hand-placed entity cards:

- **Automations subscribe to events.** The §2.3 events (`incident_created` / `incident_updated` / `incident_removed`) are the intended trigger surface. A user cannot pre-wire a UI state trigger against an entity that does not exist until the storm hits, but the event payload already carries what an automation needs (`severity`, `phase`, `changed_fields`) and fires regardless of entity timing. A reference blueprint (§6.4) ships the pattern, so this does not fall to hand-written Jinja.
- **Display goes through a domain-aware card.** Dropping `incident.<slug>_<hash>` into a stock entity card would throw "entity not found" once the incident clears, but that is not the intended usage. The native card (§5, item 6; prototyped in `weather_alerts_card`) renders whatever `incident.*` entities currently exist and shows "all clear" when none do, the pattern community cards like `auto-entities` already use to render a varying entity set.

Because an entity exists only while its incident is live, a card can render every `incident.*` it sees without filtering empty slots, the burden the static-pool fallback pushes onto every card and automation (§6.1).

### 2.7 Internationalization

The `state` values (`extreme`, `severe`, `moderate`, `minor`, `unknown`) are stable English tokens and MUST NOT be localized at the entity level. Display translation happens through HA's standard mechanism: the `incident` domain ships `translations/<lang>.json` files under `component.incident.entity_component._.state.*`, and cards and the state UI render the localized label while automations continue to match on the stable token. `weather`, `cover`, and other state-bearing core domains handle i18n the same way.

Provider-supplied localized content (`headline`, `description`, `instruction`, `area_desc`) is handled at the provider layer, not the core platform:

- Each integration exposes a `language` option in its config/options flow.
- When a provider emits only one language, those fields carry that language's text, and the `language` attribute records which language (e.g., `"en-US"`, `"en-CA"`, `"fr-CA"`).
- When a provider emits multiple languages for the same incident (ECCC's bilingual English/French feed, MeteoAlarm's per-country multilingual payloads), the integration selects the user's preferred language for the primary fields and exposes the alternate as `headline_alt`, `description_alt`, `instruction_alt`, with `language_alt` naming the alternate locale. This lets cards offer a "show in other language" affordance without requiring a second fetch or a second entity. Which block becomes the alternate is a rule, not document order: the English block when the primary is not English, else the first other-language block (reference implementation issue #154; 46 of 110 sampled WMO SWIC sources carry more than two languages, and document order handed a `zh` reader Portuguese). The alternate long-form fields sit inside the §2.4 payload bound on the same terms as the primary ones and are the first text spent when an incident does not fit: a second language can double the long-form text a bilingual incident inlines, and in the reference implementation's sweep the localized copy ran longer than the primary 69% of the time (§7.2). Whether the alternate belongs inline at all was asked and answered in issue #151: kept, best-effort under the bound, and revisited only if a consumer asks for a working language toggle. At that point a handle in the §2.4 shape is the design, and "without a second fetch" above is the sentence to amend.
- Lifecycle identity is computed from language-independent fields — for ECCC, sender + issued timestamp + event code + polygon hash, per §2.2; for NWS, VTEC — so that language variants of the same incident share one entity, not two. Note that ECCC's key deliberately *excludes* urgency, which shifts between revisions of the same incident and caused a duplicate-identity bug when it was included. Fields chosen for language-independence must also be checked for revision-stability; the two properties are not the same.

The reference `cap_alerts` ECCC provider already implements this exact pattern and can serve as the worked example in the detailed-spec phase.

---

## 3. Comparison to Existing Solutions

Home Assistant already provides alerting capabilities, but none address ingestion, normalization, and persistent tracking of *external* structured incidents.

### 3.1 Built-in `alert` Integration
The core `alert` integration creates entities (for example, `alert.garage_door_open`) that monitor a condition and repeatedly notify until it clears.

- Strengths: simple for user-defined internal monitoring ("door left open").
- Limitations: built for internal conditions, not external feed ingestion. No severity tiers, geometry, multi-timestamp metadata, zones, or lifecycle-aware identity. Does not address the 16 KB limit or history fragmentation.

### 3.2 Alert2 (HACS Custom Component)
Alert2 significantly extends the built-in `alert` with expressive conditions, throttling, snoozing, acknowledgment, superseding, and dedicated Lovelace cards.

- Strengths: excellent UX for *internal* rule-based alerts.
- Limitations: operates on user-configured rules over existing HA entities, templates, or events. Provides no standardized schema for external sources, no CAP normalization, and no stable event-level identity across provider updates.

### 3.3 Legacy Weather Alert Sensors
Most weather integrations expose alerts as a single sensor with items packed into attributes. A handful go further and expose alerts as a single `binary_sensor` that can hold only one alert at a time, which drops concurrent alerts entirely rather than truncating them.

- Common failures: 16 KB truncation under load on packed-attribute sensors; concurrent-alert dropout on single-slot binary sensors (the MeteoAlarm community has been raising this since at least 2022 across multiple European countries — see §8.2 for thread links); fragmented history when providers re-issue URIs; complex Jinja2 required for even basic automation; inconsistent UX across providers.

Core contributors who built this pattern have said as much themselves: in [home-assistant/core#37415](https://github.com/home-assistant/core/pull/37415) a reviewer noted that storing alerts in a single sensor's attributes "always felt like an ugly hack or workaround," and the one-sensor-per-alert alternative proposed afterwards stalled for lack of a platform to support it (see §8.1).

`cap_alerts` was developed specifically to overcome these limits and serves as the reference implementation for this RFC.

### 3.4 Domain Naming: `alert` vs `incident`
The `alert` domain is already owned by the built-in integration. To avoid namespace collision and user confusion, this RFC proposes the distinct domain `incident`.

- `alert.*`: internal, user-configured monitoring rules. Focus: notification, repetition, acknowledgment.
- `incident.*`: external, structured incidents ingested from feeds or APIs. Focus: CAP metadata, stable lifecycle identity, dynamic creation and removal.

The two are complementary and non-overlapping. No changes to the existing `alert` integration are proposed.

### 3.5 Core `issue_registry` / Repairs Dashboard

Home Assistant core already has a dynamic-item API: `issue_registry`, which drives the Repairs dashboard. A reviewer seeing "dynamic creation and destruction of items with severity and metadata" could reasonably ask why `incident` is not just `issue_registry` with a wider schema.

The two address different problem spaces:

- `issue_registry` surfaces **actionable HA-internal problems** that the user or an integration author can resolve: deprecated YAML keys, integrations that failed to load, expired auth, misconfigured helpers. Every repair has a "fix flow" or a remediation step. The audience is the person administering the HA instance.
- `incident` surfaces **external environmental events** the user is a passive recipient of and cannot resolve: a tornado warning does not have a "mark as fixed" button; the user waits for it to expire. The audience is everyone who lives in the home.

The data model differs accordingly. Repairs items are keyed on `(domain, issue_id)` chosen by the integration, carry a translation key and severity tier drawn from a short fixed set, and are expected to persist only as long as the underlying misconfiguration does (minutes to weeks, driven by user action). Incidents are keyed on provider-stable lifecycle hashes, carry a full CAP vocabulary (urgency, certainty, onset/expires, geometry, zones), and are driven by external clocks the user has no control over.

Forcing CAP onto `issue_registry` would either bloat the Repairs dashboard with non-actionable items (degrading its signal-to-noise as an admin tool) or require a parallel "informational" filter that recreates the distinction this RFC is proposing anyway. Keeping the two separate preserves Repairs as the actionable-admin surface and gives external incidents a domain shaped for their data and lifecycle.

### 3.6 A Dedicated `incident_registry`

A natural follow-on to §3.5: if `issue_registry` is the wrong *existing* registry, why not build a *new* one, a parallel `incident_registry`, sibling to `issue_registry`, that ingests CAP data directly and never creates entities at all? It would track lifecycle hashes natively and drop incidents on expiry with no `async_remove` traffic, which sidesteps the registry-churn objection (§2.5).

This is a coherent design. We do not adopt it because it forfeits everything the entity model provides for free, and would have to rebuild each piece from scratch:

- **Native History.** Entities land in the recorder automatically; a registry would need its own history surface to match.
- **State-trigger UI.** Users build automations against entity states in the visual editor. A registry needs an all-new trigger type and editor support before a non-technical user can act on an incident at all.
- **Declarative Lovelace.** Every existing card consumes entities. A registry needs bespoke cards for any display whatsoever.
- **Restart survival.** `IncidentEntity` inherits `RestoreEntity` and rides the recorder across reboots (§2.5). A registry would need its own persistence, and persisting CAP data to `.storage/` reintroduces the SD-wear tradeoff weighed in §2.5, while holding it only in memory fails the power-outage test.

**On the precedent that core builds non-entity UI.** A reviewer may point out that core already gives first-class UI to several non-entity primitives: Repairs, Assist, Backups, Areas, and Voice pipelines all have dedicated dashboards or editors, and none are entities. The frontend can clearly surface a registry. The relevant distinction is composition, not capability. Each of those surfaces is a single, bounded admin destination with one canonical view; none is consumed by the community cards, blueprints, and the visual automation editor that already speak entities. A repair cannot be dropped onto a dashboard beside a thermostat, filtered by `auto-entities`, or used as a state trigger without support built for it specifically. Incidents need that composition: to sit next to a weather card, fire a `notify` automation through the stock state-trigger editor, and be themed by `state` the way `weather` and `binary_sensor` are. An entity gets that from the existing ecosystem; a bespoke registry surface serves one destination and does not interoperate with the cards and blueprints already deployed. The Repairs precedent shows that rebuilding those surfaces for a registry is possible, not that it is cheap.

The arguments against dynamic entities (poor interaction with the declarative UI and the state-trigger builder, §2.6) apply more severely to a registry, which has no entities for those surfaces to bind to at all. The entity model stays UI- and history-compatible while still surviving restart. A registry trades a bounded, batched mutation cost for a large new core surface and a new primitive the ecosystem has to learn. If the AWG prefers a non-entity abstraction anyway, the entity schema, event contract (§2.3), and geometry API (§2.4) port to it unchanged; only the binding to the state machine differs.

### 3.7 The `geo_location` Platform

`geo_location` is Home Assistant's existing home for external, feed-sourced, georeferenced events, and the closest current analogue to what `incident` proposes — the `gdacs`, `usgs_earthquakes_feed`, `nsw_rural_fire_service`, and `geonetnz_*` integrations all build on it. A reviewer could reasonably ask why disaster feeds need a new domain when this one already ingests them.

It is the wrong shape for structured incidents, for the same reasons #37415 abandoned it (§8.1):

- A `geo_location` entity's `state` is a *distance* (kilometres from home), not a severity. There is no severity axis, no phase, and no normalized vocabulary — the §1.3 problem, unaddressed.
- Attributes are a thin, integration-defined bag with no shared CAP schema, so every consumer re-parses per source.
- There is no lifecycle-identity contract: entries are keyed per feed item and re-created on churn, fragmenting history exactly as §1.2 describes.
- It is built for map display; off the map, a `geo_location` entity has almost no presentable content.

GDACS is the live demonstration, and it is now a two-sided one. Today Home Assistant's `gdacs` integration renders earthquakes as map pins through `geo_location`, where the entity state is a distance and the severity, lifecycle and metadata the feed carries are discarded (§8.2). The reference implementation ingests the same feed onto the incident model and keeps them: the alert level becomes a normalized severity, the event id becomes a lifecycle-stable identity across episodes, and the geometry is externalized behind a handle rather than reduced to a pin (§4.1). Same source, same data, two bindings — which is as close to a controlled comparison as this proposal gets. `incident` is the domain that `geo_location` events which are genuinely *incidents* — as opposed to, say, nearby transit vehicles — should graduate to. The two are complementary, not redundant: `geo_location` answers "what is near me," `incident` answers "what is happening that I need to act on."

### 3.8 A Frontend-Only Implementation

The cheapest objection to this entire RFC is that it proposes backend machinery for what is visibly a display problem: a Lovelace card can fetch CAP itself, parse it in the browser, and draw it, with no new domain, no entity churn, and no core review. This is not hypothetical. [`weather-radar-card`](https://github.com/jpettitt/weather-radar-card) — MIT, in the HACS default store, ~430 stars — ships exactly that: its watches-and-warnings overlay polls `api.weather.gov` directly from the browser and resolves zone polygons on demand into an IndexedDB cache. It works, it is popular, and it required nothing from core.

It is also, structurally, the *only* feed it can ever support. The browser's same-origin policy makes cross-origin reads conditional on the server opting in, and the CAP publishing world has not. Probing each endpoint the reference implementation ingests, with an `Origin` header, on 2026-08-08:

| Endpoint | Authority | HTTP | `Access-Control-Allow-Origin` |
| :-- | :-- | :-- | :-- |
| `api.weather.gov/alerts/active` | NWS (US) | 200 | `*` |
| `feeds.meteoalarm.org/api/v1/warnings/feeds-{country}` | MeteoAlarm / EUMETNET | 200 | *(none)* |
| `severeweather.wmo.int/v2/json/sources.json` | WMO SWIC | 200 | *(none)* |
| `severeweather.wmo.int/v2/cap-alerts/{source}/rss.xml` | WMO SWIC | 200 | *(none)* |
| `rss.alertready.ca` | Pelmorex / NAAD (CA) | 200 | *(none)* |
| `rss.naad-adna.pelmorex.com` | Pelmorex / NAAD (CA) | 200 | *(none)* |
| `cap.alertready.ca/{date}/{id}.xml` | Pelmorex / NAAD (CA) | 200 | *(none)* |

Every response is a successful `200`; only the American one is readable from a page. The precise claim is therefore narrower than "frontend-only is impossible", and stating it narrowly makes it harder to dispute: **a browser-only implementation cannot consume these sources directly without a cooperating server-side intermediary.** A proxy defeats the CORS boundary, and nothing here says otherwise. What it does say is that the moment such an intermediary exists, the architecture under discussion is no longer frontend-only — and in a Home Assistant deployment the intermediary that already exists, is already authenticated, and is already trusted with the user's location is an integration. So the US-only scope of the card above is not a product decision its author could revisit with more effort within the browser; it is the boundary of what a page can read unaided. The last row is the sharpest case: `cap.alertready.ca` is where the CAP bodies live, so even a card willing to re-implement CAP 1.2 parsing cannot reach the documents to parse.

**The stronger objection is not CORS at all**, and it survives even where the fetch succeeds. A card-local fetch is invisible to the rest of Home Assistant: nothing reaches the recorder, so there is no history; nothing reaches the state machine, so there are no state triggers and no notification automations; and the data exists only while that dashboard is open in that browser. That is requirement 10 (§1.4) failing from the opposite direction to the action-response mechanism in §1.6 — there, the data is in the backend but unreachable declaratively; here, it is renderable but reaches nothing else. Even a frontend implementation with a proxy solving every CORS row above would still fail on this, which is why it, rather than the header table, is the load-bearing argument.

A smaller note in passing: browsers cannot set `User-Agent`, a forbidden header under the Fetch spec, so a card cannot send the contact identification NWS asks API consumers for regardless of what its documentation claims. That constrains politeness rather than feasibility — NWS serves the request either way — so it is recorded and not leaned on.

None of this argues against the card. The right split is the one the same project already demonstrates: it sources its lightning layer from the Blitzortung *integration* while fetching NWS itself, because for NWS the browser can do it and for everything else something in the backend must. `incident` is what that something binds to, and normalization living behind it is what keeps every future card from re-deriving severity tiers per provider.

---

## 4. Scope and Boundaries

### 4.1 What Belongs on `incident`

The domain is for external, structured incidents that the home consumes as a recipient, not as an observer of its own hardware. Typical sources:

- Weather warnings (NWS, ECCC, MeteoAlarm, BoM, DWD, WMO CAP) — *shipped*
- AMBER alerts and civil emergency broadcasts — *shipped, via ECCC/NAAD*
- Infrastructure and public-safety notifications: 911 service outages, evacuation orders, shelter-in-place notices — *shipped, via ECCC/NAAD*
- Geophysical and natural-hazard disaster alerts: earthquakes, volcanic activity, tsunamis, plus cyclones, floods, droughts and wildfires (GDACS) — *shipped*
- Utility-issued notifications: grid load warnings, rolling blackouts, municipal water quality advisories
- ISP or upstream service outages published via public status feeds

**The non-weather claim is load-bearing, so it is stated precisely.** A reasonable objection to this RFC is that it generalizes from weather to "incidents" without evidence that anything but weather uses the model — a weather abstraction wearing a generic name. Two shipped answers now exist, and the older one is still the stronger. Canada's NAAD system, which the shipped ECCC provider already consumes, is not a weather feed at all: it is an all-hazards aggregator that carries any authority's CAP messages. A single live sample, ingested through the existing provider with no code specific to any of it, contained:

| Sender | `category` | `event` | `severity` |
| :--- | :--- | :--- | :--- |
| ECCC storm prediction centres | `Met` | Wind Warning, Squall Warning, air quality, … | Moderate–Severe |
| Manitoba Emergency Management Organization | `Infra` | `911 Service Inoperative` | **Extreme** |
| RCMP "E" Division | `Other` | `AMBER Alert` | Moderate |
| Calgary Police Service | `Other` | `AMBER Alert` (Alberta Emergency Alert) | Moderate |

All `status=Actual`, `scope=Public`. These are not adapted or synthesized: they are CAP messages from provincial emergency management and municipal police, normalized by the same code path that handles a thunderstorm warning, differing only in `category` and `event`. The domain's cross-hazard claim therefore rests on running code rather than on projected reach.

GDACS is the second answer, and it adds the *geophysical* hazard class the NAAD sample does not carry: earthquakes, volcanoes and tsunamis alongside cyclones, floods, droughts and wildfires, from a global aggregator rather than a national one. It is worth separating what each proves. NAAD proves that one provider's code path carries hazards from unrelated authorities without hazard-specific handling. GDACS proves something narrower and harder — that a source with no CAP body at all, no `expires`, no terminal vocabulary and an identity that has to be reconstructed from the feed envelope still lands on the same `CAPAlert` shape. It is the second provider (after NWS, which reads GeoJSON) to build alerts without a CAP parser, which is the clearest evidence available that `CAPAlert` is a target shape rather than a wire format.

The remaining categories are illustrative of intended reach rather than built today.

**Two incident shapes.** The domain spans both a bounded-window *warning* (a tornado warning, active until cancelled or expired) and a past, point-in-time *event report* (an earthquake, carrying `urgency=Past` and no meaningful `expires`). §2.2 draws the boundary and now backs it with a shipped provider, where earlier revisions of this document could only project it from design analysis. The residual caveat is narrower than it was: the *arrival and removal* halves of the report shape are exercised in running code, while in-place revision of a report is not, so a core implementation should still expect the revision path to be the one that surprises it.

Events that originate from an external authority but concern the household also belong here: a gas leak notice from the utility, a regional fire ban, a neighbourhood security bulletin. The distinguishing trait is that the provider issues a structured CAP-like message and HA is the consumer.

### 4.2 What Does Not

Internal device state is not an incident. A failing disk on a Proxmox node, a smoke detector triggering, a battery dropping below threshold, a failed backup job: these are device state changes. They belong on `binary_sensor` (usually `device_class=problem` or `device_class=safety`), or on a purpose-built sensor.

The rule of thumb:
- Reported to the home from an outside issuer: `incident`.
- Occurs inside the home's own hardware or software: `binary_sensor` or a dedicated sensor.

### 4.3 Gray Area: User-Constructed Incidents

Nothing prevents a power user from synthesizing `incident` entities from internal state via an automation or a thin custom integration. For instance: promoting a sustained `binary_sensor.ups_on_battery` to an `incident.power_outage` with onset and expires timestamps. This is an opt-in choice, not something the domain does automatically.

### 4.4 Why the Boundary Matters

Without it, `incident` starts absorbing `binary_sensor` responsibilities and the ecosystem splinters on every "is this an incident?" question. The CAP data model (issuer, sent timestamp, area, expires) does not fit a disk SMART error, and the `binary_sensor` model does not fit a tornado warning. Keeping the two domains separate keeps both coherent.

---

## 5. Implementation Path

1. Introduce the `incident` domain and `IncidentEntity` base class in Home Assistant Core, including the geometry HTTP view (§2.4) and registry cleanup contract (§2.5).
2. Port the **provider-independent** incident model and its conformance tests: the normalized schema, the severity vocabulary, the lifecycle and phase semantics, the event contract, and the geometry contract. CAP XML parsing, CAP profile interpretation, and per-source convention handling stay in the integrations. This is a narrower step than earlier drafts proposed, and deliberately so: core owning a general CAP parser would make it the maintainer of every national profile's quirks — Buddhist-Era years, vendor lifecycle parameters, per-sender identity rules — which is exactly the work §5's closing paragraphs argue belongs in the provider layer. What core owns is the shape every integration must normalize *to*; how a given feed reaches that shape is the integration's business. `cap_alerts` supplies a working reference for both halves, but only the first half is being proposed for core.
3. Ship reference integrations in core at launch to prove the platform across CAP dialects and incident shapes. The launch set is deliberately small, because the platform is already asking core to accept a new domain, a new entity lifecycle, an event contract, a geometry API, and registry semantics in one change:
   - NWS: a port of `nws_alerts` on top of the new platform (VTEC lifecycle identity, US coverage).
   - ECCC: building on home-assistant/core#164481 (Atom/WFS, composite-key lifecycle identity, international and CAP-generic coverage).
   - Non-weather coverage comes from ECCC rather than from a third integration: the same provider already carries `Infra` (a 911 outage) and `Other` (AMBER alerts) incidents from the NAAD feed today, with no hazard-specific code (§4.1). That is the claim a third integration would have been there to prove, and it is proved by shipped code rather than by design analysis.
4. GDACS as the first post-adoption provider, carrying the *event-report* shape — a past, point-in-time incident with `urgency=Past` and no `expires` (§2.2) — that neither launch integration exercises. It ships in the reference implementation, so this is no longer a bet on an unbuilt provider; it is held out of the launch set only to keep that set at two, and the reason for holding it has changed accordingly. It remains the natural first test of whether the platform generalizes past warnings, and the port is now a port rather than a build.
5. Phase migration and deprecation of alert-handling code in `weather` and affected custom integrations. Opt-in, non-breaking (see §5.1).
6. Add native Lovelace support for `incident` entities, building on the existing `weather_alerts_card`, including on-demand geometry fetch.

The `cap_alerts` custom integration already implements most of the required core behaviors and serves as a working blueprint: lifecycle hashing, sparse attributes, dynamic entity spawn and remove, registry purge on terminate (§2.5), provider-supplied termination hints (§2.2), region-scoped area-group selection (§2.2), the absence rule branch for branch (§2.5), streaming and polling ingest behind one coordinator (§2.5), and geometry externalization — the last now shipping as a byte-bounded in-memory store behind both an HTTP view and a websocket command (§2.4), where the May draft still listed it as an outstanding addition.

**Restart survival is the principal remaining gap** between the reference implementation and what §2.5 specifies for core. Alert entities there are coordinator-backed and do not inherit `RestoreEntity`, so nothing restores last-known state across a restart: the entities are re-created from the registry, and if the first post-boot reconciliation fails — the power-cut-took-the-router case §2.5 exists for — the config entry retries setup and the dashboard shows nothing at all, which is precisely the failure mode mechanism 2 (`stale: true`, never `unavailable`) was written to prevent. Offline expiry enforcement (mechanism 1) has the same status: the phase computation would age a restored incident out correctly if one were ever restored. Both mechanisms are specified for the core platform and neither is exercised in the field yet, which a reviewer should weigh against them accordingly. An earlier revision named registry-purge-on-terminate as the principal gap; that was wrong at the time it was written and is corrected here — the purge has been in the reference implementation since its first commit.

Requirement 8 was a gap of the same kind until recently, and the way it was found is worth recording: the reference implementation terminated an incident on its first missed reconciliation, with no gating at all, while §1.4 asserted the opposite. The RFC was right and the code was wrong — the inverse of the §6.4 archival case, where the shipped blueprint was right and the prose was wrong, and of the registry-purge claim above, where the prose invented a gap the code did not have. All three were caught by checking rather than by reasoning from the document, which is the standard every implementation-backed claim in this RFC has to meet now that it makes so many of them.

Provider-specific quirks remain the integration layer's responsibility under this design. CAP authorities interpret the protocol differently across jurisdictions, and core and custom integrations already absorb those differences today. The `incident` domain narrows what an integration author has to build by lifting state management out of their concern, so they can focus on feed semantics. The WMO provider is a concrete illustration: its SWIC sources range from a handful of alerts to ~500 RSS items per poll (the Philippines feed is nearly all expired entries), so the provider pre-filters on the feed's own expiry metadata before materializing any entity. The active set the core platform sees stays bounded (typically single digits) without core needing to know anything about RSS framing or CAP expiry semantics. ECCC is the more thoroughly field-tested illustration, and gives concrete answers to "what happens at scale?". The Canadian national feed carried roughly 220 entries / 1.4 MB on one endpoint and 970 entries / 4.7 MB on the other in a July 2026 sample, with earlier samples reaching ~1,800 `Actual` entries and ~7 MB. Four reduction stages run entirely inside the provider before any entity exists:

1. **Envelope dedup.** Entries are per (language × area-group), so many resolve to one CAP document — 211 entries collapsed to 100 documents in one measured sample.
2. **Geographic pre-filter.** A coarse bounding-box test against the configured province, run on the envelope polygon before any CAP body is fetched. This is not an optimization but a correctness requirement: fetching every national CAP body to read its province code could not complete inside the coordinator's 30-second timeout, so province-mode setup failed outright until the pre-filter landed. It cut Ontario from ~1,800 candidate body fetches to ~7.
3. **Area-group selection.** The region-matching `<info>` block is chosen and terminal groups resolved (§2.2).
4. **Domain filters.** Marine-zone exclusion and similar user-facing filters.

What the core platform sees at the end is single digits: a British Columbia configuration on live data produced **9 alert entities**. The 16 KB ceiling, registry churn, and fan-out concerns in §2.5 are all sized against that number, not against the feed's raw entry count — which is the division of labor this section is arguing for.

GDACS is a further illustration of the same division of labor, and the most extreme one, because the provider absorbs a source that does not cooperate at all. It signals revisions through an episode counter rather than CAP `msgType`; it embeds a volatile episode id in the identifier, forcing identity onto the event id (§2.2); it publishes a `todate` that reads like an expiry and is not; it answers a missing geometry file with HTTP 200 and an HTML page, so a content check is the only way to detect one; and its per-event CAP endpoint ignores the event id it is given, returning one body for every event of a type. Two global indexes are unioned, filtered by hazard type and by the feed's own Green/Orange/Red impact scale before any geometry is fetched — 315 items in a sampled current-events index, 280 of them green wildfires — and forecast-only shapes (a cyclone's track cone and its ~40 wind-radii rings) are excluded so a point-in-polygon test answers "am I in the affected area" rather than "might this reach me eventually". None of that reaches the core platform, which sees only the normalized `CAPAlert`. The multi-provider layout in `cap_alerts` exists for prototyping and cross-feed investigation; in practice we recommend one focused integration per upstream service.

The presentation layer also exists in working form. The companion `weather_alerts_card` is a Lovelace card that consumes the entity model exposed by `cap_alerts` (severity-driven theming, phase-transition badging) against live NWS, ECCC, MeteoAlarm, and WMO feeds, and additionally adapts the attribute shapes of existing NWS, MeteoAlarm, BoM, and DWD integrations. Because the card and the integration are co-developed by the same author, the entity contract has been shaped by a real UI rather than designed in isolation. The reference UI exists as public code today.

### 5.1 Migration Strategy for Legacy Consumers

Users and blueprint authors today depend on packed-attribute sensors (for example, `sensor.nws_alerts` with an `alerts` list). A hard cutover would break every existing automation on the release where the new platform lands, so this RFC proposes a parallel-surface transition.

- The transitional window is six months, matching HA's standard cadence for breaking-change deprecations. Affected legacy integrations run both surfaces in parallel: the existing packed-attribute sensor is retained and marked deprecated in logs and docs, while `incident.*` entities are emitted alongside. Six months gives blueprint authors and downstream consumers (Alert2, HACS community cards) two minor release cycles to migrate, which history suggests is the realistic floor for ecosystem-wide shifts of this size.
- A core-provided compatibility template or blueprint demonstrates how to reconstruct the old flat list from the new entity set for automations that have not yet been migrated. The specific form (template helper vs. blueprint vs. both) is open.
- At the end of the window, legacy sensors are removed in the affected integrations. The `incident` platform itself has no legacy surface to deprecate.

The `climate` and `water_heater` migrations employed the same pattern: parallel surfaces long enough for blueprint authors to update, then a clean cutover.

### 5.2 Test Coverage Requirements

Core test suites for this platform must cover:

- Registry purge path: additions and removals across multiple coordinator cycles, including storm-scale fan-out (50+ incidents in a single cycle).
- Coordinator restart scenarios: HA restart between upstream cancellation and local observation; restart with a half-applied removal; restart with an entity whose `unique_id` hashes to a different slug after a provider `event` rename.
- Restart staleness handling (§2.5): a restored incident already past `expires` is terminated at boot before any poll; a restored incident within `expires` carries `stale: true`/`last_confirmed` until the first poll clears it, and never goes `unavailable`.
- Startup reconciliation (§2.5): a hard crash leaves orphaned `incident.*` registry entries, and the first successful poll scrubs those absent from the active set while retaining still-valid incidents missing from a single flaky poll.
- Geometry cache bounds (§2.4): the byte ceiling evicts LRU entries when a large polygon would exceed it, and both the HTTP view and the websocket command return `404`/`ERR_NOT_FOUND` for an evicted or never-populated `geometry_ref` rather than raising.
- Provider-supplied termination (§2.2): a document whose `msgType` remains `Update` but which carries a terminal lifecycle status resolves to `expired`; an absent or unrecognized status fails open to active.
- Region-scoped area groups (§2.2): a document that is `active` over one region and `ended` over another yields a live incident for a consumer in the first region and a terminated one for a consumer in the second; a document whose region-matching blocks are all terminal never produces a live entity.
- Ingest-mode neutrality (§2.5): the same lifecycle assertions hold whether incidents arrive by poll or by pushed stream, including that a stream reconnect's backfill clears staleness markers exactly as a first successful poll does.
- Lossy-source tolerance (§2.5): an incident absent from a single observation but still within `expires` is not terminated; an incident absent from a source that another source still reports is retained.
- Removal payload semantics (§2.3): `incident_removed` carries a terminal `phase` — never the phase the incident held while live — with an early disappearance resolving to `cancel` and a post-`expires` one to `expired`; `removal_reason` is emitted only when the provider published a recognized signal, is absent rather than defaulted otherwise, and pairs independently with either terminal phase.
- Geometry handle namespacing (§2.4): two config entries against the same provider, both seeing an alert that covers each of their areas, resolve to distinct `geometry_ref` values and each retrieves its own polygon; purging one entry's refs leaves the other's intact.
- Attribute payload bounds (§7.2): a bilingual incident whose primary and alternate long-form fields together would serialize past the recorder ceiling is published under it, with the alternate-language text spent in full before the primary loses a byte, and a field trimmed below a readable floor dropped rather than left as a stub. The measurement must be the recorder's own: the attribute set minus the domain exclusions and the entity's unrecorded attributes, not the full `to_attributes()` output. This is the assertion whose absence let the localized duplicates escape the old per-field cap in the reference implementation, so a core suite should carry it from the start.

---

## 6. Future Work and Alternatives Considered

### 6.1 Fallback: Static Entity Pool

If the AWG rejects dynamic entity creation and destruction in favor of a stable-entity, "permanent registry objects" philosophy, the second candidate from §1.5 applies: a static entity pool.

Under this model, each config entry pre-allocates N incident slots (`incident.<config_slug>_slot_1` through `incident.<config_slug>_slot_N`). Slots are filled and drained rather than entities created and destroyed:

- When a new incident is observed, the coordinator assigns it to the lowest-numbered empty slot and populates that slot's state and attributes.
- When an incident terminates, its slot is emptied: state → `unknown`, attributes cleared, but the entity persists.
- Slot assignment is sticky for the incident's lifetime. Once assigned to slot 3, an incident stays in slot 3 until termination, even across coordinator cycles.
- `unique_id` is slot-based and permanent; the lifecycle hash moves into an `incident_id` attribute rather than driving the entity identity.

What this buys: a completely static registry, no `async_add_entities` or `async_remove` traffic, History UI that shows consistent friendly names (`Incident Slot 3 — OKX`) for the entity's whole existence, and every objection around registry churn disappears.

What it costs:

- N has to be chosen generously. For a medium-sized US region during a tornado outbreak, 30–50 slots per config entry is not unreasonable, which means 30–50 persistent entities per config entry on quiet days, most showing `unknown`. Entity cardinality becomes up-front and permanent instead of demand-driven.
- The frontend burden inverts. A dynamic model lets a card render every `incident.*` entity it sees and trust that each one is live; a slot model forces every card, automation, and dashboard to filter out empty slots client-side (`auto-entities` templates, Jinja `{% if states(...) != 'unknown' %}` guards, and similar). This pushes the "active vs. inactive" distinction, which the backend already knows, back into every piece of user configuration. The companion `weather_alerts_card` would need a slot-aware filter layer that the dynamic model does not require at all.
- History queries by incident identity require filtering on `incident_id` in attributes rather than selecting by entity, which is awkward in the default History UI and undermines one of the dynamic model's main wins.
- Slot flapping under concurrent churn (two incidents expire and three new ones arrive in the same cycle) has to be handled deterministically (a stable assignment algorithm, documented in the platform contract) to prevent incidents from swapping slots and shredding history continuity.
- The 16 KB ceiling now applies per slot rather than per active incident, which is equivalent in practice but loses the "bounded by incident identity" framing.

We prefer the dynamic model, but the static pool is a complete §1.5 candidate: it satisfies every §1.4 requirement and stands as a full architecture if the AWG declines dynamic lifecycle. The entity schema, event contract, geometry API, and severity normalization are unchanged between the two models; only the lifecycle management differs.

Approach demonstrated by @pyspilf: https://community.home-assistant.io/t/getting-all-active-meteoalarm-alerts-weather-alerts-card-integration/1006597

### 6.2 Cross-integration Geometry Store

v1 holds geometry in memory (§2.4). A future core-managed geometry store, analogous to `image` or `media_source`, would offer:

- Cross-integration polygon reuse. NWS, a local emergency management feed, and a community MeteoAlarm integration could all reference the same county geometry instead of each caching a copy.
- Restart survival without re-polling upstream APIs, which matters most for rate-limited providers.
- Automatic orphan cleanup via reference-counting or TTL.

The prize is worth sizing before anyone builds it, because the census in §2.4 bounds it from both directions. Across all 11,888 NWS zones the median is ~190 points and 75% sit under 400, so per-incident reuse saves kilobytes, not megabytes — the win is not bulk. It is the tail: a shared store is what stops three integrations each holding their own copy of the 93,667-point Alaskan zone, and it is what lets a restart avoid re-fetching it. Sizing the whole opportunity, a cold render of *every* zone referenced by a nationwide alert set is about 1.78 MB across 265 requests, which is the ceiling on what any cross-integration cache can save in a single session. That is a real saving for a rate-limited provider and a rounding error for a broadband user, which is the honest case for treating this as a v2 nicety rather than a v1 requirement.

This is out of scope for v1. The HTTP view in §2.4 is backend-agnostic, so a store can land behind it without a client-visible change. Concrete design depends on core appetite for a new storage subsystem and is left to a follow-up RFC.

### 6.3 Sub-incident Relationships

Some CAP-adjacent workflows produce hierarchical incidents. A parent "Severe Weather Event" might have child advisories — Tornado Warning, Severe Thunderstorm Warning, Flash Flood Warning — each with its own lifecycle but sharing a root event.

The reserved `parent_id` attribute (§2.1) is the hook for this. No v1 provider produces such relationships directly (NWS and ECCC both flatten in their public feeds), so the feature is deferred until a concrete source demands it. CAP already specifies the wire mechanism: the `<incidents>` element — "a group listing of referent incident(s) of the alert message" — is how a provider declares grouping, and GDACS was observed populating it during design probing (`<incidents>1543517</incidents>`). Read that sighting narrowly, because the endpoint it came from is the one §4.1 condemns. `cap.aspx` returns one body per event *type* regardless of the event id it is given, so the element demonstrably exists in a GDACS CAP body, but the id inside it cannot be tied to the event that was requested. No shipped provider surfaces it, and no shipped feed has been checked systematically for it, so treat this as evidence that the element is used at all rather than as established practice. The shipped GDACS provider does not change that: it builds alerts from the RSS index and fetches no CAP body, so the element the probe saw is not on a path any running code reads. The expected shape, when it lands: children carry `parent_id` set to the parent's `incident_id`; parents do not enumerate children since reverse lookup is a frontend concern and keeping it out of the payload avoids attribute bloat on the parent.

### 6.4 Long-term Archival Hook

For users and organizations that need durable, rich historical records (after-action reports, insurance timelines, regulated compliance logs, climatological research, etc.) the recommended pattern is to subscribe to `incident_removed` (and optionally `incident_created` / `incident_updated`) and forward full payloads to an external sink.

**The removal event must be self-sufficient, and an archival consumer must not dereference the entity.** An earlier draft of this section suggested subscribers resolve `incident_id` against the state machine and fetch geometry from the §2.4 view "before the entity is torn down." That is a race, and the RFC specifies the losing side of it: §7.3 fires `incident_removed` and removes the entity within the same coordinator cycle, and §2.4 purges the incident's geometry in that cycle too, so the polygon endpoint returns `404` to anyone who arrives after the event. A synchronous listener may win; an automation with any queueing, a `notify` platform with network latency, or an external sink consuming the event stream out of process will not, and the failure is silent and intermittent — the worst shape for a records path someone is relying on for an after-action report.

The contract is therefore that `incident_removed` carries what termination processing needs (§2.3), and that anything richer must be captured earlier. A consumer wanting full descriptions, instructions, or geometry archives them from `incident_created` and `incident_updated`, where the entity and its geometry are both live, and treats `incident_removed` as the signal to close the record rather than to go fetch it. This is the same reasoning that put `area_desc` on the event payload in the first place. A core implementation that wants archival to be a first-class pattern has the option of widening the removal payload instead; what it must not do is document a retrieval step that its own teardown ordering defeats.

**Stated as a contract, so "self-sufficient" is not overread:** `incident_removed` is a terminal *lifecycle* event and is not guaranteed to carry a complete historical record of the incident. It carries what closing a record requires — identity, terminal phase, reason where published. A consumer that needs to reconstruct the full incident must archive the creation and update events; removal closes the record rather than triggering a final-state fetch. The intended model is `created` establishes the record, `updated` mutates it, `removed` closes it.

One exception to that model, and an archival consumer has to handle it: an incident superseded by a revision the platform can see is dropped without firing `incident_removed`, because the successor's own event already carries the news (§7.3). A records path that closes only on removal will therefore leave those records open indefinitely. Closing them on the successor's arrival is the intended handling; a core implementation that would rather emit a removal for the predecessor is free to, provided it accepts that consumers then see both events for one transition.

Natural sinks include InfluxDB via the existing HA integration, Postgres via AppDaemon or a small custom component, SQLite for self-contained setups, and notification platforms (Slack, PagerDuty) for incident-response workflows.

A reference blueprint demonstrating this pattern already ships in the reference implementation, at [`blueprints/cap_alerts_archive_incident_removed.yaml`](blueprints/cap_alerts_archive_incident_removed.yaml); a core platform should ship its equivalent. It forwards `trigger.event.data` to a notify service and dereferences nothing, which is the pattern above rather than the one the earlier draft described. It complements rather than replaces the native History UI: the UI remains useful for at-a-glance review of active and recently-cleared incidents, while the archival hook is for records that need to outlive the entity.

### 6.5 Per-zone Sub-device Grouping

An alternative device layout would create one sub-device per zone in `affected_zones`, so a warning covering three counties appears grouped under each county's device. This is appealing for users who already organize dashboards by county or region.

It is deferred for two reasons. First, it multiplies registry churn: a single incident now touches N device-registry entries instead of one, which directly conflicts with the batched-mutations rule in §2.5 during storm-scale fan-out. Second, the better long-term shape is probably per-issuer grouping (see §2.1 device grouping discussion), which is orthogonal to per-zone and may obviate it. The feature returns to the table if (a) per-issuer grouping lands and registry writes become demonstrably cheap under fan-out, or (b) a clear UX demand emerges that neither single-device nor per-issuer layouts can satisfy.

### 6.6 Bundled Zone-Geometry Artifact — Considered and Rejected

Many alerts carry no polygon at all — the bulk of the NWS feed is zone-based, publishing only the `affected_zones` codes of §2.1 — so a consumer wanting a shape has to resolve each code against the provider. The obvious fix is to precompute: ship a simplified zone-geometry artifact with the integration, or serve one from a core endpoint, and resolve locally with no upstream fetch at all. It is recorded here because it is the first thing an implementer proposes and the measurement against it is unintuitive.

**The reusable conclusion is to precompute the *simplification*, not the *distribution*.** Serving a simplified polygon per response is a clear win — `AKC198` at a 400-point budget is 234x fewer points, and a server can simplify before sending where a browser must download the full geometry first. Precomputing the whole corpus is not, and the measurement below is why.

It was built and measured, and it loses. The best artifact — land zone types only, Douglas-Peucker simplified at tolerance 0.005 — is **4.91 MB gzipped**. A cold *nationwide* render that instead resolves each referenced zone on demand is **~1.78 MB across 265 requests**, so the artifact costs 2.8x more bytes to serve the worst case it was designed for, and a realistic home viewport touches about three zones, roughly 20 KB. Shipping megabytes to every install to save kilobytes at the point of use is the wrong trade in both directions, and it adds a staleness problem the on-demand path does not have: zone boundaries are revised, and a bundled artifact pins every install to its release date.

Two methodological notes, because both misled the analysis before the census corrected it. **Sampling cannot estimate this population.** An initial 115-zone sample got per-type means wrong by 6.4x for counties, 2.5x for public zones, and 49x for offshore zones; the distribution is heavy-tailed enough (§2.4) that only a census gives usable numbers, and every intermediate estimate built on sampling was wrong by more than the margin the decision turned on. **Gzip on coordinate JSON is about 4:1, not 10:1.** The ~10:1 an implementer will observe against the NWS API is an artifact of that API pretty-printing its responses; compressing compact four-decimal-place coordinates gets nothing like it, and assuming otherwise inflates every artifact estimate by roughly 2.5x.

So the conclusion at the top of this section is the part that generalizes past NWS. The artifact numbers are one population's; the principle — simplify per response, do not precompute the corpus — holds wherever geometry is heavy-tailed.

---

## 7. Appendix

### 7.1 Example Entity State

Sample `developer-tools/state` output for a live NWS Severe Thunderstorm Warning:

```yaml
entity_id: incident.severe_thunderstorm_warning_7c4e1f9a
state: severe
attributes:
  id: OKX.SV.W.0042.2026
  event: Severe Thunderstorm Warning
  headline: Severe Thunderstorm Warning issued April 14 at 3:47PM EDT until April 14 at 4:45PM EDT by NWS New York NY
  description: |
    At 347 PM EDT, a severe thunderstorm was located near Yonkers,
    moving east at 35 mph. HAZARD...60 mph wind gusts and quarter
    size hail. SOURCE...Radar indicated.
  instruction: |
    For your protection move to an interior room on the lowest
    floor of a building.
  severity: Severe
  urgency: Immediate
  certainty: Observed
  msg_type: Alert
  status: Actual
  phase: new
  sent: "2026-04-14T15:47:00-04:00"
  effective: "2026-04-14T15:47:00-04:00"
  onset: "2026-04-14T15:47:00-04:00"
  expires: "2026-04-14T16:45:00-04:00"
  ends: "2026-04-14T16:45:00-04:00"
  area_desc: "Southern Westchester, NY; Bronx, NY"
  affected_zones:
    - NYZ071
    - NYZ072
  bbox: [-73.98, 40.85, -73.74, 41.02]
  geometry_ref: 01J8Z3K5R7Q9X2M4N6P8T0V1W3:nws:OKX.SV.W.0042.2026
  language: "en-US"
  vtec: "/O.NEW.KOKX.SV.W.0042.260414T1947Z-260414T2045Z/"
  event_code_nws: SV.W
  friendly_name: Severe Thunderstorm Warning
  icon: mdi:weather-lightning
```

The `_7c4e1f9a` suffix on the `entity_id` is the §2.2 short hash — the first 8 hex characters of SHA-1 over the entity's `unique_id` — not an issuing-office code. Both examples in this appendix carry one, because the derivation is unconditional: it is what keeps two concurrent warnings of the same event type from colliding into HA's `..._2` numeric fallback, and it is therefore present even when nothing else would collide. The `friendly_name` is the CAP `<event>` string with no office suffix. §2.2 derives the `entity_id` slug from `event` but the RFC states no separate display-name rule, so the examples take the plain event name rather than encoding provenance that the `sender` and `sender_name` attributes already carry.

A non-weather incident from the same reference implementation, showing that the shape is unchanged across hazard classes — only `category`, `event`, and the provider-specific fields differ. Drawn from a live NAAD message (§4.1):

```yaml
entity_id: incident.911_service_inoperative_b8d0e274
state: extreme
attributes:
  id: 3f2a9c14b7d2
  event: 911 Service Inoperative
  headline: 911 Service Disruption
  description: |
    911 service is currently unavailable in the affected area.
    If you have an emergency, contact your local emergency
    services at the alternate number listed below.
  severity: Extreme
  urgency: Immediate
  certainty: Observed
  category: Infra
  msg_type: Alert
  status: Actual
  phase: new
  sent: "2026-07-23T14:12:00-05:00"
  effective: "2026-07-23T14:12:00-05:00"
  expires: "2026-07-24T02:12:00-05:00"
  area_desc: "Rural Municipality of Springfield, MB"
  language: "en-CA"
  friendly_name: 911 Service Inoperative
  icon: mdi:phone-alert
```

Note that `category: Infra` is what lets a card group or filter this apart from weather without string-matching on `event` (§2.1, §2.6), and that severity normalization is doing real work here: a 911 outage is `Extreme` on the same scale as a tornado warning, which is the correct answer and one a weather-specific severity model could not express.

### 7.2 Attribute Size Budget

Modeled size of the sparse attribute payload for a single CAP-rich incident, post-externalization, and the bound the reference implementation enforces on it. The table does not account for multi-byte encoding of non-Latin content, JSON escaping, or attributes a future revision adds. It is a budget to design against; the bound is the guarantee.

The table below is a rewrite, twice over. An earlier revision modeled fourteen rows, and the reference implementation's schema had since grown past them: a re-check against `to_attributes()` found a schema that can emit **65** attribute keys, roughly forty of them on a typical incident, with three groups missing from the budget entirely. The schema number is the one that matters here, since §1.1's second bound is a claim about what the schema permits rather than about what a given feed sends. That audit put the modeled cap at 27,300 bytes, 1.7x over the ceiling, and is the reason the bound moved from the field to the payload (issue #150). The rows below reflect the schema after that change: long-form text carries no per-field cap, `parameters` is declared unrecorded and so sits outside what the recorder measures, and the `geocode_*` aliases are no longer serialized.

| Field group | Typical bytes | Modeled cap |
| :---------- | ------------: | ----------: |
| `id`, `url`, `identifier` | 190 | 370 |
| `event`, `headline` | 145 | 290 |
| long-form text: `description`, `instruction`, `description_alt`, `instruction_alt` | 4,600 | **whatever the 15,800-byte budget leaves** |
| `event_alt`, `headline_alt`, `language`, `language_alt` | 150 | 350 |
| severity trio, `status`, `scope`, `category`, `response_type` | 110 | 190 |
| `phase`, `msg_type`, `lifecycle_status`, `previous_phase`, `phase_changed` | 70 | 120 |
| 5× timestamps | 160 | 200 |
| `area_desc` | 200 | 600 |
| `affected_zones`, `affected_zone_uris` | 240 | 900 |
| `geocodes` | 350 | 1,200 |
| `bbox`, `points` | 48 | 260 |
| `geometry_ref` | 80 | 128 |
| `sender`, `sender_name`, `web`, `note` | 160 | 400 |
| `references`, `replaced_by`, `replaced_at` | 0 | 300 |
| `parameters` (provider passthrough) | 400 | **unrecorded, outside the bound** |
| VTEC block (6 fields, NWS) | 180 | 300 |
| `event_code_nws`, `event_code_same`, `is_marine`, `parent_id` | 30 | 90 |
| `episode_days` (merged episodes) | 0 | 1,200 |
| `provider`, `icon`, `severity_normalized`, `stale`, `last_confirmed`, `incident_platform_version` | 180 | 260 |
| JSON overhead | 300 | 600 |
| **Total, as the recorder measures it** | **~7.2 KB** | **~7.8 KB structural, plus long-form text** |

Both totals are decimal KB, as were the 11 KB and 27 KB the previous tables reported, so the four are comparable. The ceiling itself is 16,384 bytes and is quoted in bytes throughout, since the argument turns on the exact figure rather than on a rounded one. Read the cap column as a subtraction: everything that is not long-form text sums to about 7.8 KB at its modeled worst, so the four text fields have roughly 8 KB between them on the most elaborate incident the schema permits and about 13 KB on a typical one, before the bound starts spending them.

**The bound is the payload, not the field, and that is what changed.** The previous revision of this section found that four long-form fields at any per-field cap large enough to carry a real description consume the whole 16,384-byte ceiling on their own, before `id` or `event` arrive, so bounding passthrough and de-duplicating geocodes could not recover the difference. Two shapes closed it, dropping the localized long-form duplicate or budgeting long-form text in aggregate across the four fields, and the choice between them was left open as issue #151. The reference implementation took the aggregate shape: the entity serializes the attributes it is about to publish, measures them the way `recorder.db_schema.shared_attrs_bytes_from_event` does, which is `state.attributes` minus `ALL_DOMAIN_EXCLUDE_ATTRS` and minus the entity's own `_unrecorded_attributes`, and trims only when the result exceeds 15,800 bytes, the ceiling less 584 bytes reserved for the `friendly_name` and `icon` HA appends after the entity returns. Trimming is strict priority, not proportional shaving: `description_alt`, then `instruction_alt`, then `description`, then `instruction`, each spent in full before the next gives up a byte, because proportional shaving damages the field the user needs in order to spare the one nobody reads. A field that would survive at under 160 bytes is dropped instead, since a fragment tells a consumer less than an absence does, and `affected_zone_uris`, a fixed prefix over codes `affected_zones` already carries, is dropped after the text. The model object keeps the full text throughout; the bound is applied at publication, so the lifecycle diff (§2.3) runs on what the source sent rather than on a platform artifact.

**Localized duplicates were the hole.** The previous revision soft-capped `description` and `instruction` at 4 KB each while §2.7 had the platform publish a second language's text for the same incident whenever a provider emits one. The reference implementation capped the primary fields and let the `_alt` copies through at whatever length the feed sent, so a bilingual incident could carry twice the long-form text with half of it unbounded. §1.1 names this exact case in passing, "a long description, a localized duplicate of it, or a multipolygon can exhaust the budget alone", and an earlier §7.2 modeled the description and the multipolygon while omitting the duplicate. Under the payload bound the localized copies are inside the measurement by construction, and §2.7 makes them the first text spent.

**A live sweep put numbers on that row, and then on the cap itself.** On 2026-08-16, 9,604 CAP messages were pulled across 172 scopes, the NWS national active set, all 13 ECCC provinces, all 38 MeteoAlarm countries, 119 WMO SWIC sources and GDACS global, and measured on raw provider output, before normalization, in UTF-8 bytes. Of 9,381 non-empty descriptions, 13 crossed 4,096 bytes: 0.14%, all of them NWS, and all of them tropical products. Outside tropical the largest description anywhere in the sample was 3,757 bytes, and `instruction` never came close at all, 6,158 values, longest 1,835. So on the primary pair the old cap was dormant almost everywhere and then bit hard where it mattered most: 13 of the 47 tropical NWS messages in the draw, 28%, and the largest an 8,783-byte Tropical Cyclone Local Statement that lost more than half its text to a truncation nothing could undo.

The localized row is where the sweep changed a conclusion rather than confirming one. `description_alt` produced six over-cap values, three ECCC BC air quality warnings, each seen twice because WMO's `ca-msc-xx` source mirrors the same MSC feed, whose English text ran 2,976 to 3,757 bytes and whose French ran 4,115 to 5,081. Those were the *only* values in a 9,604-message global sample that exceeded 4 KB without being an NWS tropical product, and every one of them reached the state machine uncapped. Nor is it a quirk of three bulletins: across the 7,907 alerts in the sample carrying both a primary and a localized description, the localized copy is longer 69% of the time, median ratio 1.08 and mean 1.28. A 4 KB cap on the primary field was therefore about a 5.5 KB cap on the localized one, and the entire overrun landed on the half that had no cap. Two caveats on the draw: it is a single snapshot, and it caught live Atlantic tropical activity, which makes it favorable to finding long text rather than adverse. Neither caveat touches the localized finding, which is a property of how long French runs against English rather than of the weather.

The second measurement is the one that retired the cap rather than widening it. Serializing 425 of those alerts through the real attribute path (`scripts/text_size_sweep.py`) found the per-field cap failing in both directions at once. The 8,871-byte tropical statement serialized to 14,290 bytes and fit with 2,094 to spare; the cap was shredding it for nothing. The one alert that did overflow, a BC air-quality warning at 19,084 bytes, carried only 9,535 bytes of long-form text, so no text cap at any value could have rescued it. Set the cap low enough to bound the schema and it destroys incidents that fit; set it high enough to spare them and it bounds nothing. The overflow was not hypothetical either: nine rows in the reference implementation's own database carried a real state and empty attributes, all ECCC air-quality warnings, which is §1.1's failure happening in production rather than waiting to.

**Provider passthrough is outside the bound by declaration.** `parameters` carries whatever the source publishes that has no CAP home, for NWS the raw `parameters` object from the API, re-issue references included. A budget row of "provider-specific: 300 bytes" was a guess at the size of a field the platform never inspects, and nothing here can bound a source-controlled term. The reference implementation declares it unrecorded: it stays on the state for templates and cards and never lands in history, which takes it out of the recorder's measurement for free. A core contract should do the same or exclude it from the schema and make providers promote what they need.

**Geocodes are published once.** The same zone codes used to appear in `affected_zones`, in the `geocodes` container under their raw CAP `valueName`, and again under whichever `geocode_*` aliases were promoted. Each copy was defensible on its own, but together they tripled the largest variable-length field on a zone-based incident, 5,510 bytes of the overflowing air-quality warning, and it is the field that grows with the size of a warned area. The aliases are no longer serialized; they remain as read-only accessors for integration code, and `geocodes` is the single published surface, with versioned schemes folded under a canonical key (#156). The de-duplication is at the source rather than a rung of the trim order on purpose: paying it back only under pressure would have left every other incident carrying the same waste.

What the shipped numbers look like against this: on the repository's fixtures, an incident with a 100-byte headline and a 120-byte description still serializes to 1.0–1.6 KB across 29–39 keys, so roughly a kilobyte is structural before any content arrives. After the bound landed, a 443-alert live sweep of NWS and ECCC found nothing that needed trimming, and the incident that had been overflowing serializes under the ceiling on the geocode de-duplication alone. The point of the bound is not that trimming is common; it is that the per-incident bound §1.1 promises is now a property of the schema rather than of feeds being terse, which is the distinction §1.1's argument rests on. The design question the previous revision left open (issue #151) is settled on the same evidence: the localized fields stay, best-effort under the bound, and the deletion path is only worth its deprecation cycle if nobody ever asks for the second language. §2.7 records the terms.

### 7.3 Registry Cleanup Sequence

```
Reconciliation → provider returns list[CAPAlert]
  └─ store.process() diffs against the previous cycle
      ├─ new IDs       → async_add_entities + fire incident_created
      ├─ updated IDs   → entity.async_write_ha_state + fire incident_updated
      └─ missing IDs   → apply the absence rule (§2.5); absence alone is not
          │              termination, so most of these branches retain:
          ├─ superseded out of region ───────────────────→ DROP, fire nothing
          ├─ scope changed ──────────────────────────────→ terminate
          ├─ now >= expires ─────────────────────────────→ terminate (expired)
          ├─ source declares absence-ends ───────────────→ terminate (cancel)
          ├─ expiry published and still ahead ───────────→ RETAIN, mark stale
          ├─ no expiry, but the source can still end it ─→ RETAIN, mark stale
          └─ no expiry and no exit at all ───────────────→ terminate (cancel)
              └─ for each terminated entity:
                  1. fire incident_removed (automations consume this)
                  2. platform.async_remove_entity(entity_id)
                  3. entity_registry.async_remove(entity_id)
                  4. (recorder history retained; registry now reflects only
                     active incidents)
```

The absence rule's first branch in §2.5 — the provider signalled termination — is not repeated here, because a signalled termination arrives on a *present* record, and nothing absent can have signalled anything. It resolves on whichever present-record path the incident came in by, and there are two. An incident already tracked reaches a terminal phase on the `updated IDs` path. An incident whose *first* sighting is already terminal takes the `new IDs` path instead, and fires `incident_removed` there rather than the `incident_created` that path otherwise fires — the box above simplifies that case away. Either way the removal fires and the incident is absent from the active set the cycle ends with.

The two orderings say the same thing about the same incidents. This one follows the code for the first two branches, which are genuinely tested first, and keeps §2.5's order for the rest: the implementation happens to test the absence-ends declaration before `expires`, and cannot tell the difference, because the terminal phase is derived from `expires` separately from whichever branch decided to terminate.

**Supersession drops the incident without announcing it, and that is deliberate.** The superseding revision has already fired its own `incident_created` or `incident_updated` carrying the same news, so an `incident_removed` on top of it would double-report one event to every consumer, and the first branch above exists to prevent that. The entity still goes away — the active set no longer holds the id, so the registry catches up on the same cycle — but no lifecycle event marks it. This is the one path on which an entity disappears silently, and an archival consumer (§6.4) should key its record-closing on the successor rather than expecting a removal for the predecessor. Note that this is a different mechanism from `removal_reason: superseded`, which rides on a *present* record whose provider published a terminal token (§2.3); the two are not alternate spellings of one thing.

A retained incident takes none of the four termination steps: it stays in the active set with `stale` set and `last_confirmed` recorded, and fires nothing. Registry mutation happens only on genuine termination, which is also why the churn §2.5 costs out is bounded by real incident endings rather than by feed reliability.

---

## 8. Prior Art & Acknowledgements

This RFC builds on substantial prior work inside and outside the Home Assistant ecosystem. The sections below document the references that shaped the design and credit the maintainers whose integrations revealed both the problem space and the partial solutions this proposal generalizes.

### 8.1 Related Home Assistant Core Work

- [home-assistant/core#164481](https://github.com/home-assistant/core/pull/164481), *"Expose richer alert data and combine alert sensors in Environment Canada"* (@michaeldavie). Collapses the five per-category ECCC alert sensors into a single combined `sensor.<name>_alerts` with an `alerts` list in attributes, sourced from the richer GeoMet WFS API. This PR illustrates both the motivation and the ceiling of the current model: it meaningfully improves the data available to users, yet by design packs every active alert into one entity's attributes, which is identified in §1.1 as brittle under load. The field set it exposes (`title`, `issued`, `color`, `expiry`, `area`, `status`, `confidence`, `impact`, `alert_code`, `type`) also maps cleanly onto the CAP vocabulary proposed here, which reinforces that providers are converging on the same data shape independently.

  **The PR was closed unmerged on 2026-06-08**, and its closure is the clearest single piece of evidence for §1.6. It was blocked by a `CHANGES_REQUESTED` review arguing the alerts should not live in `extra_state_attributes` at all: "I would argue that we shouldn't store this in extra state attributes, but instead use actions with return values to return a list of all the alerts". Two direct questions on the thread asking what the intended path for dashboards was — given that the built-in `meteoalarm` and `dwd_weather_warnings` integrations expose alert data via attributes for exactly that reason — received no answer over the following three months. The successor, [home-assistant/core#172393](https://github.com/home-assistant/core/pull/172393), *"Environment Canada integration: add get_alerts action"* (@gwww), took the action-with-response route and merged. The code owner described the choice on [home-assistant/discussions#3130](https://github.com/orgs/home-assistant/discussions/3130) as taking a pragmatic path forward that avoided a custom integration. The outcome is what matters here rather than the reasoning: ECCC's richest alert data is now reachable by automations and unreachable by cards, which is requirement 10 failing in production rather than in theory.

- [home-assistant/core#161882](https://github.com/home-assistant/core/pull/161882), *"Replace NINA attributes with sensors"*, and [home-assistant/core#166125](https://github.com/home-assistant/core/pull/166125), *"Use actions in NINA to allow accessing data"* (both @DeerMaximum, merged 2026). The same resolution reached independently in a second integration, and the more instructive of the two cases because the migration is *incomplete by construction*. NINA's binary-sensor attributes — the whole CAP-shaped warning — are deprecated for removal in HA 2026.11, replaced by eight per-field diagnostic sensors plus a `nina.get_details` action. Three of the eight sensors (`sent`, `start`, `expires`) ship `entity_registry_enabled_default=False`; `affected_areas` becomes a truncated short form; and `description` and `recommended_actions` receive **no sensor at all**, existing only in the action response. After 2026.11 a NINA warning therefore cannot be reassembled from entity state by any means: the fields a card most needs exist only in an action response, outside the declarative surface and carrying no change signal (§1.6). Two integrations, four PRs, one consistent outcome — payloads out of attributes, actions as the sanctioned replacement — this is a settled convention being applied, not an isolated decision, which is why §1.6 addresses the convention rather than the individual reviews.

- [home-assistant/architecture#1357](https://github.com/home-assistant/architecture/discussions/1357), *"Allow sensors to report forecast data"*, and [#1360](https://github.com/home-assistant/architecture/discussions/1360), *"Introduce forecast providers for sensor entities"* (@jpbede, both March 2026, the latter revised through July 2026). The same tension in the venue where it can actually be resolved, applied to forecasts instead of incidents. #1357 opens on the premise — "Custom integrations often stuff long forecast arrays into state attributes, which is inefficient and hard to standardize", with an explicit goal to "Keep forecast data out of state attributes and fetch on demand" — and both name the resulting gap in the same breath: "the frontend has no unified way to retrieve and render forecasts for sensor values" (#1360; #1357 has it as "sensor forecasts"). Both have been discussed at the architecture meeting (@MartinHjelmare). Forecasts and incidents are structurally the same problem — multi-item, time-bounded, structured data hanging off an entity, wanted by both automations and dashboards — and the case for solving them under one contract rather than two is straightforward. This RFC's §2.4 geometry API and #1357's forecast API are the same mechanism at different scales.

- [home-assistant/core#37415](https://github.com/home-assistant/core/pull/37415), *"Add alert sensor platform to NWS"* (@MatthewFlamm, `nws` code owner), and its 2023 successor [home-assistant/core#100009](https://github.com/home-assistant/core/pull/100009), *"Add support for OpenWeatherMap national weather alerts"* (@IceBotYT). Both are closed, never merged. Together they show the gap this RFC addresses is not a question of effort. The #37415 review thread reached the same conclusions this RFC reaches, years before it: a core contributor observed that packing alerts into a single sensor's attributes "always felt like an ugly hack or workaround," and floated one-sensor-per-alert (the model adopted here), only to be stopped by the lack of any way to reference dynamically created entities from automations (see the proposed solution in §2.3). The same thread independently identified lifecycle identity as the hard part ("it is common that one alert will replace another"; CAP `references` are "useful when an alert updates a previous alert"), which §2.2 and the `references` chain resolution address directly. #37415 was ultimately closed because adapting it to the `geo_location` platform was too complex; when #100009 revisited the same data three years later, the same maintainer confirmed that neither `geo_location` nor a custom event had ever been a satisfactory home for it.

- [home-assistant/core#103352](https://github.com/home-assistant/core/issues/103352), *"DWD Weather warning status doesn't reset after warning end"* (2023, closed), and its 2025 recurrence [home-assistant/core#150737](https://github.com/home-assistant/core/issues/150737), *"...doesn't reset after warning end (again)"*. The warning sensor stays active after the event has ended ("I have a weather warning active although it was over two days ago"), and manual resets are overwritten on the next poll. The 2025 report notes that the upstream feed already carries an `EXPIRES` timestamp the integration does not act on. The same defect, filed twice two years apart, is a case of per-provider expiry handling (see §1.2, §2.5).

### 8.2 Reference Integrations

- `nws_alerts` (custom integration, @finity69x2, @firstof9): the canonical example of the 16 KB failure mode under severe-weather load, and the original motivation for the `cap_alerts` project.
- Environment Canada core integration (@michaeldavie et al.): demonstrates lifecycle-aware handling of a CAP-adjacent Atom/WFS feed, and supplied much of the field vocabulary adopted by the ECCC provider in `cap_alerts`.
- MeteoAlarm, BoM, and DWD integrations: independent confirmation that a shared CAP-based model is needed across providers. MeteoAlarm's concurrent-alert dropout has been filed in core several times: [home-assistant/core#108908](https://github.com/home-assistant/core/issues/108908) ("showing only one alert, however should be several"), [home-assistant/core#131045](https://github.com/home-assistant/core/issues/131045), and the same report a year later as [home-assistant/core#156838](https://github.com/home-assistant/core/issues/156838) (still open), all tracing to the integration processing only the first warning the API returns, so a coincident wind warning is dropped. [home-assistant/core#103132](https://github.com/home-assistant/core/issues/103132) (open since 2023) reports that MeteoAlarm entities have no `unique_id`, with the reporter asking for a config field to set one by hand (see §2.2). The forum threads cover the same problems across countries: [Multiple alerts](https://community.home-assistant.io/t/meteoalarm-multiple-alerts/393707) (open since 2022, still active in 2026) and [Integration not working](https://community.home-assistant.io/t/meteoalarm-integration-not-working/120069) (reports across France, Denmark, Switzerland, Austria, Slovakia, Italy, Belgium, and the UK from 2019 onward).
- GDACS on `geo_location` (core integration): the disaster-feed analogue of the #37415 problem. Home Assistant's built-in `gdacs` integration ingests the same feed the reference implementation now ships a provider for, but binds it to the `geo_location` platform — yielding a map marker and a thin attribute bag while discarding the severity tiers, lifecycle identity, and history continuity the feed actually carries. It is live evidence that disaster feeds, exactly like the NWS alerts in #37415, get forced onto `geo_location` for want of a domain shaped for them (see §3.7). Note that the feed's usable content is its RSS index rather than CAP: GDACS advertises per-event CAP documents and neither of the two routes to one works, so "the CAP the integration discards" would overstate it — what is discarded is the impact scale, the episode-stable event identity, and the geometry, all of which the index and its companion GeoJSON do carry.

### 8.3 Complementary Projects

- Built-in `alert` integration: internal user-configured monitoring, complementary to `incident` (see §3.1, §3.4).
- Alert2 (HACS): rich notification UX layered over HA entities; a natural downstream consumer of `IncidentEntity` (see §3.2).
- `weather_alerts_card`: the companion Lovelace card that implements the one-entity-per-incident presentation model end to end.
- [`weather-radar-card`](https://github.com/jpettitt/weather-radar-card) (MIT, HACS default store): a radar card whose watches-and-warnings overlay fetches NWS directly from the browser, and which already sources its lightning layer from the Blitzortung integration. Cited in §3.8 as the working example of a frontend-only implementation and of where that approach's boundary falls.

### 8.4 Standards & Specifications

- OASIS CAP 1.2 (ratified by the ITU as Recommendation X.1303): the data model vocabulary (severity, urgency, certainty, phase, area, timestamps) this RFC adopts as its normalization target.
- Waidyanatha, Bhandari & Frommberger, *"ITU X.1303 International Warning Standard: Lessons from an Asian Implementation"* (Journal of ICT Standardization 4(3), 177–198, 2017; [doi:10.13052/jicts2245-800X.431](https://doi.org/10.13052/jicts2245-800X.431)). Peer-reviewed field evidence, from outside the Home Assistant ecosystem, that CAP/X.1303 is the right normalization target. Deploying it across Myanmar, the Maldives, and the Philippines (the Sahana SAMBRO broker, the "CAP on a Map" project), the authors independently hit the same problems this RFC centralizes: mapping disparate national severity scales onto CAP's five `severity` tiers (§2.1), externalizing polygon payloads rather than inlining them in every message (§2.4, §6.2), terminating consumed alerts on their `<expire>` timestamp (§2.5), and the one-language-per-feed constraint of CAP-over-RSS (§2.7). The paper sits on the issuer/broker side of the CAP pipeline where this RFC sits on the recipient side, so it corroborates the normalization and lifecycle layer (§1.4 reqs 1–3, 6) rather than the entity-binding mechanism — but it is direct evidence that the data model is sound and the problems are real across jurisdictions.
- NWS VTEC (10-1711): the basis for lifecycle-stable identity hashing on U.S. weather products.
- GeoJSON (RFC 7946): the geometry representation assumed by the out-of-band API in §2.4.

### 8.5 Acknowledgements

Thanks to the maintainer of `nws_alerts`, the Environment Canada core integration maintainers, Alert2, MeteoAlarm, and DWD integrations for years of field-testing the problem space, and to the Home Assistant Architecture Working Group for the conventions this RFC builds on.

Thanks also to @pyspilf, whose fixed-slot MeteoAlarm implementation is the prior art behind the static entity pool in §6.1, and who gave the first external review of an early draft; that feedback shaped §6.1 and §2.6.

---

## 9. Conclusion

Structured external notifications are central to Home Assistant's role in emergency awareness and home operations, and today's ad-hoc approaches degrade exactly as the number of simultaneously relevant incidents rises.

The proposal has two parts, and they should be weighed separately. The first is that Home Assistant needs a first-class **incident abstraction**: normalized severity, identity stable across provider revisions, an explicit lifecycle that does not trust `msgType` or a single missed observation, an event contract, and a payload bounded in both dimensions. §1.4 states that case without reference to any binding, and §§2.2–2.5 back each requirement with observed provider behavior rather than with specification reading. That is the claim this RFC most wants tested.

The second is that dynamic `incident.*` entities are the right **binding** for it — recommended here because they inherit the recorder, the state-trigger editor, `RestoreEntity`, and the existing card ecosystem without new core surface, at the cost of registry mutation at incident boundaries (§2.5). The case is good and it is not conclusive; §3.6 and §6.1 set out the alternatives fairly, and the schema, identity model, event contract, and geometry API port to either. A reviewer who accepts the abstraction and rejects the binding has moved the discussion to where it should be.

A dedicated domain along these lines would give external incidents the handling other first-class domains already get, serving today's weather systems and the public-safety, utility, and infrastructure feeds arriving behind them. We invite collaboration on any and all parts of this proposal, and disagreement on the second part most of all.
