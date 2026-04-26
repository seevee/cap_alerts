# RFC: The `incident` Integration Domain for Home Assistant Core

**Status:** Draft / Request for Comments

**Author:** @seevee (`cap_alerts` maintainer)

**Date:** April 2026

**Audience:** Home Assistant Core developers and the Architecture Working Group, plus weather-alert integration maintainers

**Subject:** Standardizing external structured notifications via a dedicated, lifecycle-aware entity model

---

## 1. Problem Statement

### 1.1 The 16 KB Recorder Ceiling
Home Assistant stores entity attributes in a single database column capped at 16,384 bytes. During severe weather outbreaks or complex infrastructure incidents, the combined metadata (descriptions, instructions, area polygons) for many simultaneous items routinely exceeds this limit.

When it does, the recorder silently fails to commit state changes at the exact moments where reliable data matters most.

`cap_alerts` avoids the ceiling by creating one dedicated entity per active incident. Each entity stays comfortably under the limit on its own.

### 1.2 Lifecycle Fragmentation
Most current integrations treat each API response as independent. When a provider issues an update (for example, NWS promotes a message from `NEW` to `CON` or `EXT`), the new message often arrives with a different URI. Naïve integrations treat this as a brand-new event, retiring the old entity and breaking state history mid-event.

`cap_alerts` uses lifecycle-aware hashing to hold identity steady across updates:
- NWS: VTEC-based identity (`office.phenomena.significance.tracking.year`)
- ECCC and others: stable composite keys (`event + area + issued_date`)

A single entity then persists across every update until cancellation or expiration.

### 1.3 Inconsistent Data Models
Users and card authors face wildly different shapes across NWS, Environment Canada, MeteoAlarm, and others. There is no shared vocabulary for severity, phase, or identity, which makes universal dashboards and automations impractical.

---

## 2. Proposed Architecture: the `incident` Domain

### 2.1 Entity Model

The `incident` platform defines a new domain with `IncidentEntity` as the base class. One entity represents one incident. The entity is created when the incident first appears and is removed when it cancels or expires.

Core properties of the model:

- Provider layers perform CAP 1.2 normalization once (severity tiers, phase, truncation), so downstream code sees a single vocabulary.
- Identity is stable across provider message updates (see §2.2).
- Attributes are sparse: only populated fields are serialized.
- Heavy payloads (geometry, long-form text) are referenced rather than inlined, which keeps the attribute footprint bounded (see §2.4).

**State** is the normalized severity, one of `extreme`, `severe`, `moderate`, `minor`, `unknown`.

**Required attributes.** An entity is valid only if all of these are present:

- `id`: stable lifecycle-aware identifier (§2.2)
- `event`: short event name, drives `entity_id` derivation
- `severity`: raw provider value
- `phase`: one of `new`, `update`, `cancel`, `expired`
- `icon`: `mdi:*` handle keyed on event type (§2.6)

**Optional attributes**, emitted only when populated:

- `headline`, `description`, `instruction` (soft-capped, see §2.4)
- `urgency`, `certainty`, `msg_type`, `status`
- `sent`, `effective`, `onset`, `expires`, `ends`
- `area_desc`, `affected_zones`
- `bbox`: `[min_lon, min_lat, max_lon, max_lat]` for map previews (~64 bytes, always safe to inline)
- `geometry_ref`: opaque handle for full polygon retrieval via the API in §2.4
- `language`: BCP-47 tag for the primary text fields (e.g., `"en-US"`, `"fr-CA"`); see §2.7
- `headline_alt`, `description_alt`, `instruction_alt`, `language_alt`: populated only when the provider emits a second language for the same incident (see §2.7)
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

Providers that do not emit CAP `severity` directly (for example, MeteoAlarm colour codes) must adapt to this table in their provider layer. The core entity never sees provider-specific severity vocabularies.

**Device grouping.** All incidents from one config entry belong to a single device in v1. An alternative worth discussing is per-issuer device grouping, with one device per upstream authority ("NWS OKX", "Environment Canada Prairie Storm Prediction Centre"), which maps more naturally onto the hub-and-peripheral model hardware integrations use. We are open to adopting it if the AWG prefers; the single-device choice in v1 is about keeping device count small and predictable while the platform stabilizes. One concrete concern with per-issuer that is worth calling out: during multi-regional events, a single config entry can see alerts from 10+ upstream offices in a single poll (e.g., a mid-Atlantic derecho routinely touches LWX, AKQ, PHI, CTP, RNK, and more NWS WFOs at once). Per-issuer trades per-entity registry churn for per-device registry churn under exactly the fan-out conditions §2.5 is most worried about, so if the AWG prefers per-issuer, the batched-mutations rule in §2.5 needs to extend to device registry writes as well. Per-zone sub-device grouping (by `affected_zones`) is a separate question, deferred; §6.5 has the rationale.

### 2.2 Identity and Lifecycle

The `unique_id` for an entity is the provider's stable lifecycle hash: VTEC for NWS, `event + area + issued_date` for ECCC and CAP-generic providers.

`entity_id` is derived as `incident.<slug(event)>_<short_hash>`, where `short_hash` is the first 8 hex characters of SHA-1 over `unique_id`. Deriving the suffix from the hash avoids HA's numeric-suffix fallback (`..._2`, `..._3`), which otherwise disconnects state history from the stable lifecycle identity each time a collision resolves differently. Slugification uses HA's standard `slugify()` applied to `event`.

The lifecycle has three phases:

| Phase       | Behavior                                                                            |
| :---------- | :---------------------------------------------------------------------------------- |
| Creation    | Spawned on first sighting of a new hash. `incident_created` fires.                  |
| Update      | State and attributes refreshed in place. `incident_updated` fires on phase or field delta. |
| Termination | `incident_removed` fires. Entity and registry record are purged (see §2.5).         |

### 2.3 Event Schema

All three integration-fired events carry the same payload, so automations can be written against the schema without branching on event type:

```yaml
event_type: incident_created | incident_updated | incident_removed
data:
  entity_id: incident.<slug>_<hash>
  incident_id: <unique_id>          # stable lifecycle hash
  event: <short event name>
  severity: extreme|severe|moderate|minor|unknown
  phase: new|update|cancel|expired
  phase_changed: bool               # true when this fire represents a phase transition
  changed_fields: [<attr>, ...]     # populated on incident_updated; empty list otherwise
```

### 2.4 Geometry Handling

Complex multipolygon GeoJSON for a severe-weather warning can easily exceed 16 KB on its own. Storing it in the state machine would recreate the failure mode this RFC is trying to fix.

The design:

- The state machine stores only a bounding box (`bbox`, 4 floats) and a `geometry_ref` handle.
- Full GeoJSON is held in an in-memory cache within the integration, keyed by `geometry_ref`, and is served by a standard `HomeAssistantView` at `GET /api/incident/{incident_id}/geometry`. This is the same pattern `camera` uses to proxy image streams and `media_source` uses to serve local files — an established route for delivering large payloads that don't belong in the state machine, with authentication delegated to the view's standard `requires_auth = True` decorator.
- Frontend cards fetch geometry lazily. Typical Lovelace renders never need it; map cards fetch once per visible incident.
- Long-form `description` and `instruction` are soft-capped at 4 KB each in attributes. Overflow is truncated with a trailing `…`, and the full text is available via the same API surface.

We deliberately do not add a bespoke websocket command (e.g., `incident/geometry`) in v1. Geometry is a one-shot fetch per card render, rather than a live-subscribed resource, and the HTTP view delivers the same payload with strictly less new surface area in core. A websocket subscription for live geometry updates could be considered in a follow-up if a concrete use case emerges, but CAP polygons update at poll cycle frequency (minutes), so the incremental value from a live subscription is low.

**Why in-memory instead of `.storage/`.** A large fraction of Home Assistant deployments run on Raspberry Pi hardware with SD-card root filesystems. Routing full GeoJSON payloads through flat-file `.storage/` would trade the 16 KB recorder ceiling for a different failure mode: during severe-weather outbreaks, CAP polygons update every few minutes as storm cells move, and sustained writes of hundreds of KB per cycle would meaningfully accelerate SD wear. Geometry is also ephemeral — it has no value once an incident expires, and the integration can always re-fetch it from the upstream feed. Because there is no correctness requirement that it survive a restart, disk I/O isn't indicated. The in-memory cache is simple, fast, and self-healing: on restart, the next successful poll repopulates it, and stale entries are dropped when the incident terminates via §2.5.

**Optional v2: shared geometry store.** A core-managed geometry store (analogous to `image` or `media_source`) would enable cross-integration polygon reuse, for instance NWS and a local emergency feed sharing county geometry, and would survive restarts without re-polling upstream APIs. This is an attractive direction but explicitly orthogonal to v1 and not required for core adoption. The HTTP view is storage-backend-agnostic, so a store can plug in behind it without a client-visible change. See §6.2.

**Size budget.** With geometry and long text externalized, the worst-case attribute payload for a single incident sits under ~3 KB (see §7.2). Even the most verbose providers stay well below the ceiling.

### 2.5 Entity Registry Cleanup

Incidents are transient. A naïve implementation that leaves registry entries behind would accumulate 50+ dead `incident.*` entries per storm season, bloating `.storage/core.entity_registry` and cluttering the UI.

Rules:

- On `cancel` or `expired` phase, the integration calls `entity_registry.async_remove(entity_id)` in the same coordinator cycle that fires `incident_removed`.
- Registry mutations are batched per coordinator cycle. All additions in a cycle go through a single `async_add_entities()` call, and all removals are issued together before the coordinator yields. This avoids the sequential-per-entity I/O churn on `core.entity_registry` that core reviewers have flagged as an anti-pattern, even during a regional outbreak that adds and removes dozens of incidents in one poll.
- Device registry entries are retained (one device per config entry); we do not create a device per incident.
- Recorder history is untouched at the database level: state rows for the removed `entity_id` are not purged, and time-range queries (`states_during_period` and friends) still return them. There is an honest tradeoff to own here, though. Once the entity is gone from the registry, the native HA History dashboard renders past incidents with only the slugified `entity_id`, without friendly name, icon, or area mapping. The state series is preserved; the UI polish around it is not. Users who need rich historical audits (after-action reports, insurance timelines, compliance logs) should subscribe to `incident_removed` and forward the full payload to an external sink — InfluxDB, Postgres, a notification service — rather than rely on the built-in History UI. §6.4 describes the recommended archival pattern.
- Removal must be idempotent: calling it on an already-missing entity is safe.
- Restart mid-storm: the integration does not persist coordinator state or geometry to disk and does not write to `.storage/` beyond what every HA entity already does. Continuity across restarts uses only HA-native mechanisms. The entity registry (already in `.storage/core.entity_registry`) keeps the entity's existence across the restart. `IncidentEntity` inherits `RestoreEntity`, so HA restores the last recorded state and attributes from the recorder database between entity add and first `async_write_ha_state`, preventing a transient `unknown` flash in the UI. The first successful coordinator poll after boot is authoritative: if the incident is still in the upstream feed, the entity is re-validated with fresh data; if it is gone (cancelled or expired during downtime), the normal §2.5 termination path executes. The idempotent-removal rule covers the case where a removal was partially applied before the restart.

The net effect is that at any moment, `incident.*` entries correspond 1:1 with currently active incidents.

### 2.6 Presentation Hints

The entity contract separates what happened from how bad it is, so frontends can style both axes independently without re-parsing attributes.

- Icon conveys event type. The integration sets `icon` from the provider's event taxonomy (Tornado Warning → `mdi:weather-tornado`; Boil Water Advisory → `mdi:water-alert`). Icons stay stable across severity changes for the same event class.
- Severity is the entity `state`. Cards and themes style by state (`extreme`, `severe`, `moderate`, `minor`, `unknown`) using standard CSS. No per-severity icon variants are needed. This matches how `weather` and `binary_sensor` already lean on state-driven theming.
- Phase (`new`, `update`, `cancel`, `expired`) is available as an attribute and through the `incident_updated` and `incident_removed` events. Frontends are encouraged to surface phase transitions (striking through cancelled incidents, badging updates), but the specific presentation is up to card authors. The domain exposes the signal and does not prescribe the UI.

Integrations must populate `icon`; they should not encode severity into it.

**No acknowledgment or dismissal service.** The domain intentionally does not expose `incident.dismiss` or `incident.acknowledge`. Entities mirror upstream reality: a tornado warning is active until NWS cancels or expires it, and a user clicking "dismiss" on their phone does not change that fact for anyone else in the household, nor should it. Local "I've seen this" state is a frontend concern, handled by cards via browser local storage (keyed on `incident_id`) or by user-level automations that maintain a dismissed-hash list. Keeping the entity pure preserves multi-client consistency and prevents the backend from growing a per-user UI-state layer it has no business owning. Card authors are expected to surface dismissal UX; the domain surfaces the ground truth the UX operates on.

Capability detection for downstream consumers (custom cards, Alert2, blueprints) is by attribute and domain introspection, not by a version string: check `state.domain == "incident"` or probe for specific attributes. This follows the convention used elsewhere in HA core.

### 2.7 Internationalization

The `state` values (`extreme`, `severe`, `moderate`, `minor`, `unknown`) are stable English tokens and MUST NOT be localized at the entity level. Display translation happens through HA's standard mechanism: the `incident` domain ships `translations/<lang>.json` files under `component.incident.entity_component._.state.*`, and cards and the state UI render the localized label while automations continue to match on the stable token. This matches how `weather`, `cover`, and other state-bearing core domains handle i18n.

Provider-supplied localized content (`headline`, `description`, `instruction`, `area_desc`) is handled at the provider layer, not the core platform:

- Each integration exposes a `language` option in its config/options flow.
- When a provider emits only one language, those fields carry that language's text, and the `language` attribute records which language (e.g., `"en-US"`, `"en-CA"`, `"fr-CA"`).
- When a provider emits multiple languages for the same incident (ECCC's bilingual English/French feed, MeteoAlarm's per-country multilingual payloads), the integration selects the user's preferred language for the primary fields and exposes the alternate as `headline_alt`, `description_alt`, `instruction_alt`, with `language_alt` naming the alternate locale. This lets cards offer a "show in other language" affordance without requiring a second fetch or a second entity.
- Lifecycle identity is computed from language-independent fields (geocode + issued timestamp + urgency, VTEC, etc.) so that language variants of the same incident share one entity, not two.

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

`cap_alerts` was developed specifically to overcome these limits and serves as the reference implementation for this RFC.

### 3.4 Domain Naming: `alert` vs `incident`
The `alert` domain is already owned by the built-in integration. To avoid namespace collision and user confusion, this RFC proposes the distinct domain `incident`.

- `alert.*`: internal, user-configured monitoring rules. Focus: notification, repetition, acknowledgment.
- `incident.*`: external, structured incidents ingested from feeds or APIs. Focus: rich CAP metadata, stable lifecycle identity, dynamic creation and removal.

The two are complementary and non-overlapping. No changes to the existing `alert` integration are proposed.

### 3.5 Core `issue_registry` / Repairs Dashboard

Home Assistant core already has a dynamic-item API: `issue_registry`, which drives the Repairs dashboard. A reviewer seeing "dynamic creation and destruction of items with severity and metadata" could reasonably ask why `incident` is not just `issue_registry` with a wider schema.

The two address categorically different problem spaces:

- `issue_registry` surfaces **actionable HA-internal problems** that the user or an integration author can resolve: deprecated YAML keys, integrations that failed to load, expired auth, misconfigured helpers. Every repair has a "fix flow" or a remediation step. The audience is the person administering the HA instance.
- `incident` surfaces **external environmental events** the user is a passive recipient of and cannot resolve: a tornado warning does not have a "mark as fixed" button; the user waits for it to expire. The audience is everyone who lives in the home.

The data model differs accordingly. Repairs items are keyed on `(domain, issue_id)` chosen by the integration, carry a translation key and severity tier drawn from a short fixed set, and are expected to persist only as long as the underlying misconfiguration does — minutes to weeks, driven by user action. Incidents are keyed on provider-stable lifecycle hashes, carry a full CAP vocabulary (urgency, certainty, onset/expires, geometry, zones), and are driven by external clocks the user has no control over.

Shoehorning CAP onto `issue_registry` would either bloat the Repairs dashboard with non-actionable items (degrading its signal-to-noise as an admin tool) or require a parallel "informational" filter that recreates the distinction this RFC is proposing anyway. Keeping the two separate preserves Repairs as the actionable-admin surface and gives external incidents a domain shaped for their data and lifecycle.

---

## 4. Scope and Boundaries

### 4.1 What Belongs on `incident`

The domain is for external, structured incidents that the home consumes as a recipient, not as an observer of its own hardware. Typical sources:

- Weather warnings (NWS, ECCC, MeteoAlarm, BoM, DWD, WMO CAP)
- AMBER alerts, evacuation orders, shelter-in-place notices, civil emergency broadcasts
- Utility-issued notifications: grid load warnings, rolling blackouts, municipal water quality alerts, boil-water advisories
- ISP or upstream service outages published via public status feeds

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
2. Port the `CAPAlert` dataclass and normalization logic from `cap_alerts` as the reference implementation.
3. Ship two reference integrations in core at launch to prove the platform across CAP dialects:
   - NWS: a port of `nws_alerts` on top of the new platform (VTEC lifecycle identity, US coverage).
   - ECCC: building on home-assistant/core#164481 (Atom/WFS, composite-key lifecycle identity, international and CAP-generic coverage).
4. Phase migration and deprecation of alert-handling code in `weather` and affected custom integrations. Opt-in, non-breaking (see §5.1).
5. Add native Lovelace support for `incident` entities, building on the existing `weather_alerts_card`, including on-demand geometry fetch.

The `cap_alerts` custom integration already implements most of the required behaviors (lifecycle hashing, sparse attributes, dynamic entity spawn and remove) and serves as a working blueprint. Geometry externalization and registry-purge-on-terminate are the main incremental additions for core adoption.

The presentation layer is already built. The companion `weather_alerts_card` is a working Lovelace card that consumes the exact entity model proposed here — severity-driven theming, phase-transition badging, on-demand geometry fetch — against live NWS and ECCC feeds. Because the card and the integration are co-developed by the same author, the entity contract has been shaped by a real UI rather than designed in isolation. The reference UI exists as public code today, which lowers the "backend without a UI" risk for core reviewers.

### 5.1 Migration Strategy for Legacy Consumers

Users and blueprint authors today depend on packed-attribute sensors (for example, `sensor.nws_alerts` with an `alerts` list). A hard cutover would break every existing automation on the release where the new platform lands, so this RFC proposes a parallel-surface transition.

- The transitional window is six months, matching HA's standard cadence for breaking-change deprecations. Affected legacy integrations run both surfaces in parallel: the existing packed-attribute sensor is retained and marked deprecated in logs and docs, while `incident.*` entities are emitted alongside. Six months gives blueprint authors and downstream consumers (Alert2, HACS community cards) two minor release cycles to migrate, which history suggests is the realistic floor for ecosystem-wide shifts of this size.
- A core-provided compatibility template or blueprint demonstrates how to reconstruct the old flat list from the new entity set for automations that have not yet been migrated. The specific form (template helper vs. blueprint vs. both) is open.
- At the end of the window, legacy sensors are removed in the affected integrations. The `incident` platform itself has no legacy surface to deprecate.

This roughly follows the pattern used for `climate` and `water_heater` migrations: parallel surfaces long enough for blueprint authors to update, then a clean cutover.

### 5.2 Test Coverage Requirements

Core test suites for this platform must cover:

- Registry purge path: additions and removals across multiple coordinator cycles, including storm-scale fan-out (50+ incidents in a single cycle).
- Coordinator restart scenarios: HA restart between upstream cancellation and local observation; restart with a half-applied removal; restart with an entity whose `unique_id` hashes to a different slug after a provider `event` rename.

---

## 6. Future Work and Alternatives Considered

### 6.1 Fallback: Static Entity Pool

If the AWG rejects dynamic entity creation and destruction in favor of a stable-entity, "permanent registry objects" philosophy, the fallback is a static entity pool.

Under this model, each config entry pre-allocates N incident slots (`incident.<config_slug>_slot_1` through `incident.<config_slug>_slot_N`). Slots are filled and drained rather than entities created and destroyed:

- When a new incident is observed, the coordinator assigns it to the lowest-numbered empty slot and populates that slot's state and attributes.
- When an incident terminates, its slot is emptied: state → `unknown`, attributes cleared, but the entity persists.
- Slot assignment is sticky for the incident's lifetime. Once assigned to slot 3, an incident stays in slot 3 until termination, even across coordinator cycles.
- `unique_id` is slot-based and permanent; the lifecycle hash moves into an `incident_id` attribute rather than driving the entity identity.

What this buys: a completely static registry, no `async_add_entities` or `async_remove` traffic, History UI that shows consistent friendly names (`Incident Slot 3 — OKX`) for the entity's whole existence, and every objection around registry churn disappears.

What it costs:

- N has to be chosen generously. For a medium-sized US region during a tornado outbreak, 30–50 slots per config entry is not unreasonable, which means 30–50 persistent entities per config entry on quiet days, most showing `unknown`. Entity cardinality becomes up-front and permanent instead of demand-driven.
- The frontend burden inverts. A dynamic model lets a card render every `incident.*` entity it sees and trust that each one is live; a slot model forces every card, automation, and dashboard to filter out empty slots client-side (`auto-entities` templates, Jinja `{% if states(...) != 'unknown' %}` guards, and similar). This pushes the "active vs. inactive" distinction (which the backend already knows) back into every piece of user configuration, and directly defeats the plug-and-play goal the platform exists to deliver. The companion `weather_alerts_card` would need a slot-aware filter layer that the dynamic model does not require at all.
- History queries by incident identity require filtering on `incident_id` in attributes rather than selecting by entity, which is awkward in the default History UI and undermines one of the dynamic model's main wins.
- Slot flapping under concurrent churn (two incidents expire and three new ones arrive in the same cycle) has to be handled deterministically — a stable assignment algorithm, documented in the platform contract — to prevent incidents from swapping slots and shredding history continuity.
- The 16 KB ceiling now applies per slot rather than per active incident, which is equivalent in practice but loses the "bounded by incident identity" framing.

We prefer the dynamic model and consider the static pool a fallback rather than a co-equal option, but it is a complete architecture if the AWG declines dynamic lifecycle. The entity schema, event contract, geometry API, and severity normalization are unchanged between the two models; only the lifecycle management differs.

Approach demonstrated by @pyspilf: https://community.home-assistant.io/t/getting-all-active-meteoalarm-alerts-weather-alerts-card-integration/1006597

### 6.2 Cross-integration Geometry Store

v1 holds geometry in memory (§2.4). A future core-managed geometry store, analogous to `image` or `media_source`, would offer:

- Cross-integration polygon reuse. NWS, a local emergency management feed, and a community MeteoAlarm integration could all reference the same county geometry instead of each caching a copy.
- Restart survival without re-polling upstream APIs, which matters most for rate-limited providers.
- Automatic orphan cleanup via reference-counting or TTL.

This is out of scope for v1. The HTTP view in §2.4 is backend-agnostic, so a store can land behind it without a client-visible change. Concrete design depends on core appetite for a new storage subsystem and is left to a follow-up RFC.

### 6.3 Sub-incident Relationships

Some CAP-adjacent workflows produce hierarchical incidents. A parent "Severe Weather Event" might have child advisories — Tornado Warning, Severe Thunderstorm Warning, Flash Flood Warning — each with its own lifecycle but sharing a root event.

The reserved `parent_id` attribute (§2.1) is the hook for this. No v1 provider produces such relationships directly (NWS and ECCC both flatten in their public feeds), so the feature is deferred until a concrete source demands it. The expected shape, when it lands: children carry `parent_id` set to the parent's `incident_id`; parents do not enumerate children since reverse lookup is a frontend concern and keeping it out of the payload avoids attribute bloat on the parent.

### 6.4 Long-term Archival Hook

For users and organizations that need durable, rich historical records (after-action reports, insurance timelines, regulated compliance logs, climatological research, etc.) the recommended pattern is to subscribe to `incident_removed` (and optionally `incident_created` / `incident_updated`) and forward full payloads to an external sink.

The event payload (§2.3) includes `incident_id`, `event`, `severity`, `phase`, and `changed_fields`. Subscribers can dereference `incident_id` to fetch full attributes from the state machine and geometry via the §2.4 HTTP view before the entity is torn down. Natural sinks include InfluxDB via the existing HA integration, Postgres via AppDaemon or a small custom component, SQLite for self-contained setups, and notification platforms (Slack, PagerDuty) for incident-response workflows.

A reference blueprint demonstrating this pattern will ship alongside the platform. It complements rather than replaces the native History UI: the UI remains useful for at-a-glance review of active and recently-cleared incidents, while the archival hook is for records that need to outlive the entity.

### 6.5 Per-zone Sub-device Grouping

An alternative device layout would create one sub-device per zone in `affected_zones`, so a warning covering three counties appears grouped under each county's device. This is appealing for users who already organize dashboards by county or region.

It is deferred for two reasons. First, it multiplies registry churn: a single incident now touches N device-registry entries instead of one, which directly conflicts with the batched-mutations rule in §2.5 during storm-scale fan-out. Second, the better long-term shape is probably per-issuer grouping (see §2.1 device grouping discussion), which is orthogonal to per-zone and may obviate it. The feature returns to the table if (a) per-issuer grouping lands and registry writes become demonstrably cheap under fan-out, or (b) a clear UX demand emerges that neither single-device nor per-issuer layouts can satisfy.

---

## 7. Appendix

### 7.1 Example Entity State

Sample `developer-tools/state` output for a live NWS Severe Thunderstorm Warning:

```yaml
entity_id: incident.severe_thunderstorm_warning_okx
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
  geometry_ref: nws:OKX.SV.W.0042.2026
  language: "en-US"
  vtec: "/O.NEW.KOKX.SV.W.0042.260414T1947Z-260414T2045Z/"
  event_code_nws: SV.W
  friendly_name: Severe Thunderstorm Warning (OKX)
  icon: mdi:weather-lightning
```

### 7.2 Attribute Size Budget

Worst-case sparse attribute payload for a single CAP-rich incident (NWS Tornado Warning, post-externalization):

| Field              | Typical bytes | Worst case |
| :----------------- | ------------: | ---------: |
| `id`               |            48 |         64 |
| `event`            |            24 |         48 |
| `headline`         |           120 |        240 |
| `description`      |         1,500 |  4,096 (cap) |
| `instruction`      |           800 |  4,096 (cap) |
| severity trio      |            48 |         64 |
| phase + msg_type   |            24 |         32 |
| 5× timestamps      |           160 |        200 |
| `area_desc`        |           200 |        600 |
| `affected_zones`   |           240 |        800 |
| `bbox`             |            48 |         64 |
| `geometry_ref`     |            48 |         64 |
| provider-specific  |           120 |        300 |
| JSON overhead      |           200 |        400 |
| **Total**          |     **~3.6 KB** | **~11 KB** |

Typical payload fits in ~3–4 KB, well under the 16 KB ceiling. The worst-case figure assumes both long-form fields hit their soft cap simultaneously, which is rare in practice (most providers lean on one or the other). Geometry, the historical offender, no longer participates.

### 7.3 Registry Cleanup Sequence

```
Coordinator poll → provider returns list[CAPAlert]
  └─ store.process() diffs against previous cycle
      ├─ new IDs       → async_add_entities + fire incident_created
      ├─ updated IDs   → entity.async_write_ha_state + fire incident_updated
      └─ missing IDs   → mark as expired (if past expires) or cancel
          └─ for each terminated entity:
              1. fire incident_removed (automations consume this)
              2. platform.async_remove_entity(entity_id)
              3. entity_registry.async_remove(entity_id)
              4. (recorder history retained; registry now reflects only active incidents)
```

---

## 8. Prior Art & Acknowledgements

This RFC builds on substantial prior work inside and outside the Home Assistant ecosystem. The sections below document the references that shaped the design and credit the maintainers whose integrations revealed both the problem space and the partial solutions this proposal generalizes.

### 8.1 Related Home Assistant Core Work

- [home-assistant/core#164481](https://github.com/home-assistant/core/pull/164481), *"Expose richer alert data and combine alert sensors in Environment Canada"* (@michaeldavie). Collapses the five per-category ECCC alert sensors into a single combined `sensor.<name>_alerts` with an `alerts` list in attributes, sourced from the richer GeoMet WFS API. This PR illustrates both the motivation and the ceiling of the current model: it meaningfully improves the data available to users, yet by design packs every active alert into one entity's attributes, which is the exact pattern §1.1 identifies as brittle under load. The field set it exposes (`title`, `issued`, `color`, `expiry`, `area`, `status`, `confidence`, `impact`, `alert_code`, `type`) also maps cleanly onto the CAP vocabulary proposed here, which reinforces that providers are converging on the same data shape independently.

### 8.2 Reference Integrations

- `nws_alerts` (custom integration, @finity69x2, @firstof9): the canonical example of the 16 KB failure mode under severe-weather load, and the original motivation for the `cap_alerts` project.
- Environment Canada core integration (@michaeldavie et al.): demonstrates lifecycle-aware handling of a CAP-adjacent Atom/WFS feed, and supplied much of the field vocabulary adopted by the ECCC provider in `cap_alerts`.
- MeteoAlarm, BoM, and DWD community integrations: independent confirmation that a shared CAP-based model is needed across providers. The MeteoAlarm community has filed long-running issues about concurrent-alert dropout on the single-slot `binary_sensor` representation — see [Multiple alerts](https://community.home-assistant.io/t/meteoalarm-multiple-alerts/393707) (open since 2022, still active in 2026) and [Integration not working](https://community.home-assistant.io/t/meteoalarm-integration-not-working/120069) (reports across France, Denmark, Switzerland, Austria, Slovakia, Italy, Belgium, and the UK from 2019 onward). Both are addressed structurally by the one-entity-per-incident model in this RFC, rather than by patches to the legacy sensor.

### 8.3 Complementary Projects

- Built-in `alert` integration: internal user-configured monitoring, complementary to `incident` (see §3.1, §3.4).
- Alert2 (HACS): rich notification UX layered over HA entities; a natural downstream consumer of `IncidentEntity` (see §3.2).
- `weather_alerts_card`: the companion Lovelace card that implements the one-entity-per-incident presentation model end to end.

### 8.4 Standards & Specifications

- OASIS CAP 1.2: the data model vocabulary (severity, urgency, certainty, phase, area, timestamps) this RFC adopts as its normalization target.
- NWS VTEC (10-1711): the basis for lifecycle-stable identity hashing on U.S. weather products.
- GeoJSON (RFC 7946): the geometry representation assumed by the out-of-band API in §2.4.

### 8.5 Acknowledgements

Thanks to the maintainer of `nws_alerts`, the Environment Canada core integration maintainers, Alert2, and MeteoAlarm community integrations for years of field-testing the problem space, and to the Home Assistant Architecture Working Group for the conventions this RFC builds on.

---

## 9. Conclusion

Structured external notifications are central to Home Assistant's role in emergency awareness and home operations, and today's ad-hoc approaches compromise reliability exactly when users need it most.

A dedicated `incident` domain with a lifecycle-aware, CAP-based `IncidentEntity`, paired with out-of-band geometry, registry cleanup on termination, and a clear boundary against `binary_sensor`, would give external incidents the same robust, scalable, and consistent handling that other first-class domains enjoy. That foundation serves today's weather systems and opens the door to public-safety, utility, and infrastructure feeds as they come online.

We invite collaboration on any and all parts of this proposal.
