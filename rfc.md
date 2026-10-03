# RFC: The `incident` Integration Domain for Home Assistant Core

**Status:** Public working draft, not yet submitted. Please don't submit it to the Home Assistant Architecture repository; the maintainer will, once the reference implementation has gathered enough field testing.

**Author:** @seevee (`cap_alerts` maintainer)

**Date:** May 2026. Revised July, August and October 2026; split into this document and [`docs/evidence/`](docs/evidence/README.md) in October 2026.

**Audience:** Home Assistant Core developers and the Architecture Working Group, plus weather-alert integration maintainers.

**Reading this document.** This file is the proposal: requirements, the recommended binding, the contract, the path. [`docs/rfc-summary.md`](docs/rfc-summary.md) is the one-page version. The evidence lives in [`docs/evidence/`](docs/evidence/README.md), one page per finding, each naming the section it supports and how it reproduces, and the *reference implementation* is the [`cap_alerts`](README.md) custom integration. A claim is marked *shipped* only where running code backs it. Dates are UTC.

**What is being proposed.** Two things, and they are separable. The first is an **abstraction**: a first-class incident, a normalized and lifecycle-aware representation of an externally sourced structured event, with stable identity across provider revisions, one severity vocabulary, an event contract and a bounded payload. The second is a **binding**: how that abstraction attaches to Home Assistant's runtime. This RFC recommends dynamic `incident.*` entities. §1.4 states the abstraction's requirements without assuming a binding, §1.5 lists three candidates, §2 argues for the entity one. A reviewer who accepts the first and rejects the second has not rejected the proposal. The schema, identity model, events and geometry API in §2 port unchanged to the alternatives in §3.6 and §6.1.

---

## 1. Problem Statement

### 1.1 The 16 KB Recorder Ceiling

Home Assistant stores an entity's attributes in one database column capped at 16,384 bytes (`MAX_STATE_ATTRS_BYTES`). On overflow the recorder does not fail the write. It logs a warning, stores `{}` in place of the attributes, and commits the state row. For a packed-attribute alert sensor, whose state is a count and whose content is all in attributes, history keeps the number and loses the alerts, and nothing in the UI marks the row as damaged ([evidence](docs/evidence/the-recorder-keeps-the-state-and-drops-the-attributes.md)).

The problem is not that 16 KB is small. It is that **the number of simultaneously active incidents is unbounded and the storage unit is fixed.** Packing N incidents into one unit scales the payload with N against a constant ceiling, so loss gets more likely as the situation gets worse.

Two separate bounds fix it. One incident per storage unit makes the recorder footprint independent of incident cardinality. That is the invariant this RFC rests on. It does not bound any single incident, since one long description, a localized copy of it or a multipolygon can exhaust the budget alone, so §2.4 bounds that second dimension explicitly. The reference implementation satisfies both. §7.2 has the numbers, including the two overflows it took to get the second bound right.

### 1.2 Lifecycle Fragmentation

Most integrations treat each API response as independent. When a provider updates a message, the update often arrives under a different URI. A naïve integration retires the old entity and creates a new one, breaking history mid-event. The `nws` code owner met this in [home-assistant/core#37415](https://github.com/home-assistant/core/pull/37415): "it is common that one alert will replace another".

The reference implementation holds identity steady with provider-specific keys, and the point is how many it needed. Seven providers use six strategies, and three providers use more than one, chosen per message:

| Provider | Identity key | Why |
| :-- | :-- | :-- |
| NWS | VTEC string; content key for products without one | 35% of non-VTEC alerts on one national sample were hourly re-issues under fresh ids |
| ECCC | `sha256(sender + sent + eventCode + polygon_hash)` | Language-independent, so en/fr siblings share one entity; urgency excluded, it churns per revision |
| MeteoAlarm | CAP `<identifier>`; content key for MeteoFrance and FMI | Two senders re-mint the identifier per re-issue or per forecast day |
| WMO SWIC | `sha256(<identifier>)` | The identifier is stable across Update and Cancel |
| GDACS | `sha256(eventtype:eventid)` | The identifier embeds an episode counter that increments per update |
| BBK / NINA | `sha256(<identifier>)`, one per revision | Each revision names its predecessor in `<references>`, so the chain collapses to its leaf |
| Australia | `<incidents>` + event code; area description for Queensland warnings; identifier for WA | Two agencies re-mint the identifier per update; Queensland re-mints the warning id too and names no predecessor |

No single CAP field is reliably the identity. It is a per-sender property, not even a per-provider one. The core model therefore requires only that an integration supply *some* stable hash, and leaves the derivation to the provider layer (§5). The per-provider record, with sample sizes and copied identifiers: [identity is a per-sender property](docs/evidence/identity-is-a-per-sender-property.md).

### 1.3 Inconsistent Data Models

Users and card authors face different shapes across NWS, Environment Canada, MeteoAlarm and the rest. There is no shared vocabulary for severity, phase or identity, so universal dashboards and automations are impractical.

### 1.4 What Any Solution Must Provide

The failures in §1.1 to §1.3 imply requirements that hold whatever the binding:

1. **Normalized vocabulary.** CAP fields (severity, urgency, certainty, phase, timestamps, area) are normalized once, centrally.
2. **Stable lifecycle identity.** One logical incident keeps one identity across updates, URI changes and `<references>` chains.
3. **Bounded footprint.** No single item approaches the recorder ceiling. Heavy payloads are externalized, not inlined.
4. **Concurrent multiplicity.** Many incidents coexist without truncation or dropout (the MeteoAlarm single-slot failure, §3.3).
5. **Restart survival without disk-wear cost.** Active incidents survive a restart on HA-native persistence, with no per-poll writes of large payloads to `.storage/`.
6. **Dynamic active set.** Items appear on issue and disappear on cancel or expiry, with expiry honored from feed metadata (the DWD reset bug, §8.1).
7. **Automation surface.** Automations trigger on arrival, update and termination without hand-wiring against entities that don't exist yet.
8. **Tolerance of imperfect sources.** Termination is not driven by one observation of absence. Real feeds omit live alerts intermittently (§2.5) and real authorities signal end-of-life outside `msgType` (§2.2).
9. **Ingest-mode neutrality.** The model holds for polling and for a pushed stream, and does not assume a poll interval exists.
10. **Readable by a dashboard.** Core presentation data, and changes to it, are available through a subscribed, contract-stable read path that both declarative and custom dashboard consumers can use. For an entity binding that is the state machine. Externalized payloads stay reachable through a frontend-native path, with subscribed state carrying the handle and the change signal (§2.4). A mechanism that serves the incident body only through an action with response data fails this requirement (§1.6). So does a frontend that fetches CAP itself and reaches neither the recorder nor an automation (§3.8).

Requirements 8 and 9 came out of field-testing the reference implementation. Requirement 10 was added after core review steered two integrations, across four PRs, to the action-response pattern (§8.1). It is neutral about storage and deliberate about the consumer surface.

### 1.5 Candidate Bindings

Three mechanisms can satisfy §1.4. They differ only in how the incident binds to HA's runtime; the data model, events and geometry API are the same in each.

- **Entity-based `incident` domain (recommended, §2).** One entity per active incident, created and removed with it. Reuses the recorder, the visual trigger editor, `RestoreEntity` and every entity-aware card. Cost: registry mutation at incident boundaries (§2.5).
- **Static entity pool (§6.1).** A fixed pool of slots, filled and drained. No registry churn. Cost: permanent entity cardinality, and an empty-slot filter pushed onto every card and automation.
- **Dedicated `incident_registry` (§3.6).** A new registry beside `issue_registry`, ingesting CAP directly. No entities at all. Cost: rebuilding history, triggers, Lovelace and restart survival from scratch.

A reviewer can accept §1.4 in full and prefer a different binding. Only rejecting §1.4 defeats the proposal.

### 1.6 The Fourth Mechanism: Action With Response Data

Core review currently prefers a fourth model: a thin entity, usually a count, plus an action with `SupportsResponse` that returns the alert bodies on demand. It is not listed above because it fails requirement 10, and the failure needs stating precisely because the imprecise version is refutable.

The case for it is real. Long attributes are written to the recorder on every state change, shipped to every client and included in every state dump. An action response is computed on request, delivered once and never recorded. For automations it is the better design, and requirement 3 agrees with its premise.

What it withholds is everything requirement 10 asks for around the call. Declarative surfaces, stock cards, `auto-entities`, templates and the visual editors, cannot invoke an action to obtain data. There is no change signal: a card holding a response has a snapshot, and "the count is still 3" cannot distinguish an unchanged set from a same-sized set with different members. And a payload that lives only in a response never reaches the state machine, so there is no history, no recorder row and nothing a state trigger can reference.

Three things about where this stands. The convention is unwritten: no ADR, no quality-scale rule and no developer-docs statement recommends actions over attributes, so there is no place a requirement like 10 can be raised against it. Core has already answered the frontend half once: when forecasts left `weather.*` attributes for `weather.get_forecasts`, the same migration shipped `weather/subscribe_forecast` so cards never lived on action calls. And the tension is live in [architecture#1357](https://github.com/home-assistant/architecture/discussions/1357) and [#1360](https://github.com/home-assistant/architecture/discussions/1360), which propose a forecast contract for sensors on the same reasoning and name the same frontend gap. The threads, quoted: [core review moved alert bodies into actions](docs/evidence/core-review-moved-alert-bodies-into-actions.md) and [the frontend has no way to read an action result](docs/evidence/the-frontend-has-no-way-to-read-an-action-result.md).

This RFC does not conclude that attributes are the right home for incident bodies in perpetuity. It concludes that requirement 10 is a requirement, that the action model does not meet it, and that a domain shaped for incidents is where it gets a first-class answer, as `weather` already has.

---

## 2. Recommended Implementation: the `incident` Domain

This section binds the §1.4 requirements onto HA's entity model. Where it says "the entity", a reviewer preferring another binding can read "the slot" or "the registry record". The schema (§2.1), event contract (§2.3) and geometry API (§2.4) are common to all three.

### 2.1 Entity Model

The `incident` platform defines a new domain with `IncidentEntity` as its base class. One entity is one incident, created on first sighting and removed on cancel or expiry. Providers normalize CAP 1.2 once, identity is stable across updates (§2.2), attributes are sparse, and heavy payloads are referenced rather than inlined (§2.4).

**State** is the normalized severity: `extreme`, `severe`, `moderate`, `minor` or `unknown`.

**Platform-guaranteed attributes** are set by the platform, so a consumer may read them unguarded:

- `id`: the stable lifecycle identifier (§2.2)
- `phase`: `new`, `update`, `cancel` or `expired`, derived, defaulting to active (§2.2)
- `severity_normalized`: the state, repeated for template and table consumers that read attributes
- `icon`: an `mdi:*` handle keyed on event type (§2.6)

**Feed-supplied attributes** are emitted only when populated, and CAP completeness varies enormously between authorities. A card that assumes one exists will break on the next provider rather than the current one.

- `event`, `headline`, `description`, `instruction`. `event` drives the entity id and is near-universal, but CAP makes it mandatory only inside an `<info>` block; consumers should fall back to `category`.
- `severity`: the raw provider value. Absent where severity is derived rather than transmitted (MeteoAlarm publishes awareness levels). Present and misleading where the authority publishes a uniform CAP severity and ranks on something else: the Australian feeds carry the Australian Warning System tier in a parameter, and raw `severity` disagrees with the normalized value on 22 of 108 live alerts. Read `severity_normalized`.
- `urgency`, `certainty`, `msg_type`, `status`, `category`. `category` is the cross-domain discriminator: `Met` for weather, `Infra` for a 911 outage, `Other` for an AMBER alert, all three observed in one NAAD sample (§4.1).
- `sent`, `effective`, `onset`, `expires`, `ends`
- `area_desc`, `affected_zones`. `area_desc` is not guaranteed descriptive; GDACS writes the literal `Polygon` there and the provider substitutes the country name.
- `bbox` (four floats), `points` (`[[lon, lat], …]`, published alongside a polygon, not instead of one) and `geometry_ref`, the handle for §2.4.
- `language`, and `headline_alt`, `description_alt`, `instruction_alt`, `language_alt` when the provider emits a second language (§2.7)
- `stale`, `last_confirmed`: set on an incident the current reconciliation did not confirm but the platform kept, either retained through an absence (§2.5) or restored after a restart and not yet re-validated. Both clear when a reconciliation sees it again.
- `parent_id`: reserved (§6.3), unset in v1
- Provider-specific fields, for example `vtec`

**Severity normalization** is deterministic and central. Providers that do not emit CAP severity adapt to this table in their own layer; the core entity never sees a provider vocabulary. National CAP deployments already do this by hand, folding Myanmar's color codes and the Philippine storm-signal numbers onto the same five tiers (§8.4).

| CAP `severity` | Entity `state` |
| :------------- | :------------- |
| `Extreme`      | `extreme`      |
| `Severe`       | `severe`       |
| `Moderate`     | `moderate`     |
| `Minor`        | `minor`        |
| `Unknown` / missing / non-CAP | `unknown` |

**Device grouping.** All incidents from one config entry sit under one device in v1. Per-issuer grouping (one device per upstream office) is the most likely v1.1 change, held out to keep the batching contract in §2.5 simple while the platform stabilizes. A regional event can touch ten upstream offices in one reconciliation, so per-issuer grouping would move the churn from the entity registry to the device registry.

### 2.2 Identity and Lifecycle

`unique_id` is the provider's stable lifecycle hash (§1.2). `entity_id` is `incident.<slug(event)>_<short_hash>`, where the suffix is the first eight hex characters of SHA-1 over `unique_id`. Deriving the suffix from the hash avoids HA's `_2` numeric fallback, which otherwise disconnects history from the stable identity each time a collision resolves differently.

| Phase       | Behavior                                                                            |
| :---------- | :---------------------------------------------------------------------------------- |
| Creation    | Spawned on first sighting of a new hash. `incident_created` fires.                  |
| Update      | State and attributes refreshed in place. `incident_updated` fires on phase or field delta. |
| Termination | `incident_removed` fires. Entity and registry record are purged (see §2.5).         |

**Three incident shapes.** A *warning* has a bounded active window and traverses the phases above (NWS, ECCC, MeteoAlarm, WMO, the DWD half of BBK). An *event report* is a past, point-in-time event with `urgency=Past` and no meaningful `expires`; it appears, is occasionally revised and leaves when the feed's retention window closes (GDACS). An *open-ended incident* is live now with no published end, revised in place and ended only when the authority withdraws it (the Australian bushfire feeds, whose `expires` is a regeneration TTL the provider drops). MoWaS civil-protection warnings sit between the first and third: no expiry, but an explicit all-clear when one is issued. The domain serves all three. Requirement 6 covers reports and open-ended incidents through feed presence where it covers warnings through expiry. Each shape is backed by a shipped provider; the one path still unobserved is a report being revised in place, which GDACS signals through an episode counter ([evidence](docs/evidence/gdacs-ends-by-withdrawal.md)).

**Phase is best-effort; the event stream is authoritative.** `phase` derives from CAP `msgType`. That is a convenience, because `msgType` is not how authorities signal termination. In a 211-entry NAAD snapshot ECCC emitted `Cancel` once, from a non-ECCC sender; every real ending travelled in a vendor parameter, `Alert_Location_Status`, while `msgType` stayed `Update`. Providers therefore supply a normalized termination hint (`lifecycle_status` in the reference implementation) and the platform retires the incident on a recognized terminal value. Recognition fails open: absent or unknown means active.

**The clock decides which terminal phase, not the signal.** A terminal status while `expires` is still ahead is `cancel`: the authority ended it early. `expired` is reserved for an incident that ran to its published expiry, and is checked first. The inferred path in §2.5 lands on the same answer, so announced and inferred endings cannot drift.

The weaker failure runs the other way. GDACS publishes `Alert` on every re-issue, so `phase` can read `new` across a real revision. `incident_updated` still fires off the field delta, so consumers wanting exact transitions should trust the events and `changed_fields` over `phase`.

**One CAP document is not necessarily one incident.** ECCC segments a document into one `<info>` block per language and area group, each with its own polygons, severity, expiry and status. One document can be `active` over one region and `ended` over another; 19 of 92 `Actual` documents in the snapshot were. Two consequences. Selection is region-scoped: an integration chooses the block matching the user's configured region, never `infos[0]`, and treats the incident as terminal only when every matching block is. And identity is per incident per region, so two consumers of one document in different regions legitimately see different lifecycles. The provider resolves area groups before a `CAPAlert` exists; the contract must not forbid that by defining identity at document granularity ([evidence](docs/evidence/eccc-ends-alerts-outside-msgtype.md)).

### 2.3 Event Schema

All three events carry one payload, so automations need not branch on event type:

```yaml
event_type: incident_created | incident_updated | incident_removed
data:
  entity_id: incident.<slug>_<hash>   # omitted only when no entity ever existed; see below
  incident_id: <unique_id>            # stable lifecycle hash
  event: <short event name>
  severity: extreme|severe|moderate|minor|unknown
  phase: new|update|cancel|expired    # terminal on incident_removed
  phase_changed: bool                 # true when this fire represents a phase transition
  changed_fields: [<attr>, ...]       # populated on incident_updated; empty list otherwise
  removal_reason: superseded|ended    # incident_removed only; omitted when unknown
  superseded_by: <CAP identifier>     # incident_removed only, with removal_reason=superseded; omitted when unpublished
```

**`changed_fields` is a notify list, not a diff.** The reference implementation reports `headline`, `description`, `instruction`, `severity_normalized`, `phase`, `expires` and `area_desc`, the fields a consumer would re-notify on. Timestamps that move on every re-issue are excluded on purpose. A field's absence from the list does not mean it is unchanged, and a core contract should either rename it or define it in exactly these terms.

**`entity_id` is present on every event for an incident that has an entity.** An earlier draft said it was absent on creation, and described a defect as a design property. Creations now wait in the store until the entity has been added and fire after its first state write, so a listener can read the attributes on the event. The key is omitted only where no entity ever existed: a first sighting that is already terminal, or an entity the registry holds but never added ([evidence](docs/evidence/restart-revalidates-instead-of-reannouncing.md)).

**Removal carries the terminal phase.** `phase` on `incident_removed` is always `cancel` or `expired`, never the phase the incident held while live. An incident that vanishes before its `expires` is `cancel`; for an automation, "the authority dropped it" and "the authority cancelled it" are the same fact.

**An ending is announced once.** Feeds keep publishing ended records, ECCC for 48 hours, and a store that forgets a terminal incident fires its removal again on every reconciliation, about sixty times an hour under streaming. The contract is one ending, one removal. The implementation remembers announced ids and the CAP identifiers behind them, so a re-issue of the same ending under a new revision is also silent, while a live sighting clears the memory and fires `incident_created` again. The memory ages out after 48 idle hours, so consumers stay idempotent ([evidence](docs/evidence/one-ending-one-removal.md)).

**`removal_reason` says why; `phase` says when.** An all-clear and a supersession look identical under `phase`, yet one means the hazard is over and the other means it got worse. The two values are `superseded` (the area moved to another incident, which fires its own creation) and `ended`. They pair independently with either phase:

| `phase` | `removal_reason` | Reading |
| :-- | :-- | :-- |
| `cancel` | `ended` | Stood down early |
| `cancel` | `superseded` | Replaced early; the successor carries the news |
| `expired` | `superseded` | The routine ECCC `transitioned_out` shape, `expires ≈ sent` |
| any | *(omitted)* | The provider published no recognized signal. Never read as `ended` |

Today ECCC is the only shipped source that supplies a reason. The field is scoped to the area group, not the document.

**`superseded_by` names the successor by CAP identifier, when the source does.** CAP `<references>` never carries this at ECCC, where watches and warnings are separate chains; a CAP-CP parameter does, and the reference implementation publishes it. It is a hint, since 18 of 27 measured targets never appeared on the feed, present only with `removal_reason: superseded`. It is an identifier and not an `incident_id` because sources name documents, not incidents ([evidence](docs/evidence/superseded-by-dangles-two-times-in-three.md)).

**Extension fields.** The reference implementation also carries `entry_id` and `area_desc` on every event. Neither is proposed for core.

### 2.4 Geometry Handling

A severe-weather multipolygon can exceed 16 KB alone. Putting it in the state machine recreates the failure this RFC fixes.

- The state machine holds a bounding box (`bbox`) and a `geometry_ref` handle.
- Full GeoJSON lives in a byte-bounded in-memory store inside the integration, served by a `HomeAssistantView` at `GET /api/incident/geometry/{geometry_ref}` and by a websocket command returning the same `FeatureCollection`. `camera` and `media_source` deliver large payloads the same way.
- Cards fetch lazily. Most renders never need geometry; a map card fetches once per visible incident.
- Long-form text is bounded at the payload, not the field. `description`, `instruction` and their localized siblings are published in full, and the serialized attribute set is measured the way the recorder measures it. Only an incident that would not fit is trimmed, in a fixed priority order (§7.2). Trimmed text is not recoverable from the platform; a consumer follows `web` to the authority's copy.

**The handle is namespaced by the scope that owns the store.** `{provider}:{alert_id}` collides as soon as two config entries see the same alert, and whichever writes second wins the slot. The reference implementation keys on `{entry_id}:{provider}:{alert_id}` and treats the composite as opaque. A core store namespaces at least as widely as it is shared.

**Geometry is more than polygons.** CAP gives an `<area>` `<polygon>` and `<circle>`; a point arrives as a zero-radius circle. The richer areal shape takes the `geometry` slot, points publish alongside it in `points`, and a point becomes the geometry only when no polygon exists. A circle with a real radius is left unmaterialized rather than approximated. Separately fetched geometry is also the less reliable half of the record, and a core implementation should degrade it independently: a slow polygon ships its alert without a shape for a cycle, a failed fetch keeps the last good one ([evidence](docs/evidence/geometry-budget-must-be-per-entry.md)).

**Both an HTTP view and a websocket command.** An earlier draft rejected the websocket command as surface for a one-shot fetch. Building the card showed the cost of HTTP is not subscription but authentication: a card already holds an authenticated websocket and would need a bearer token out of band. `weather/subscribe_forecast` is the precedent; neither surface here subscribes.

**In memory, not `.storage/`.** Much of the install base runs on SD cards, polygons update every few minutes during an outbreak, and geometry is re-fetchable. There is no correctness requirement that it survive a restart.

**The bound is in bytes, never entries.** Real geometry is heavy-tailed: across all 11,888 NWS forecast zones the median is about 190 points and the largest 93,667, so an entry cap sized to the median under-provisions by three orders of magnitude against the coastal zones a user there cares most about. The reference store budgets 5 MB per config entry and accounts each entry by serialized length. The number and the per-entry accounting are implementation parameters; the byte unit is the contract ([evidence](docs/evidence/zone-geometry-is-heavy-tailed.md)).

**A `geometry_ref` miss is normal.** The store is empty after a restart until the next reconciliation refills it, and a retained incident (§2.5) was by definition not observed, so its polygon is dropped while the entity stays live. The handle is a cache key, not a promise. Consumers keep `bbox` as the fallback and treat `404` as "draw the box".

**Size budget.** With geometry externalized a single incident models out at about 6.9 KB typical, and the recorded payload is bounded at 15,800 bytes by construction (§7.2). A shared cross-integration store is a v2 question (§6.2).

### 2.5 Entity Registry Cleanup

Incidents are transient. Leaving registry entries behind would accumulate dozens of dead `incident.*` entries per storm season.

- On `cancel` or `expired` the integration removes the registry entry in the same cycle that fires `incident_removed`.
- Registry mutations are batched per cycle: one `async_add_entities()` call, all removals together. This holds for a pushed stream too, by coalescing arrivals into one update pass.
- Device entries are retained; one device per config entry.
- Recorder history is untouched. State rows survive, but the History dashboard renders a removed entity by slug only. Rich audits subscribe to `incident_removed` (§6.4).
- Removal is idempotent.
- `IncidentEntity` inherits `RestoreEntity`, so a restart shows the last recorded state immediately, flagged stale, and the first successful *reconciliation* after boot is authoritative. "Reconciliation" is deliberate: a streaming provider reaches the same state through its reconnect backfill, not a timer (requirement 9, [the socket does not share the index gap](docs/evidence/the-streaming-socket-does-not-share-the-gap.md)).
- Startup reconciliation scrubs orphans. Registry entries whose termination was never observed are terminated on the first reconciliation that lacks them, subject to the absence rule below.

**Absence is not termination.** Two sanctioned ECCC endpoints for the same national system, sampled at once, disagreed on live `Extreme` alerts, the set churning minute to minute. NWS publishes cancellations where `/alerts/active` never shows them (0 of 174 terminal products in six hours). An integration's view of the active set is one source's current answer, not reality. The rule, as an algorithm, on an incident absent from the incoming set:

```
if the provider signalled termination:    terminate (cancel; removal_reason where published)
elif now >= expires:                      terminate (expired)
elif the source declares absence-ends:    terminate (cancel)
elif expires is published:                retain, mark stale, record last_confirmed
elif the source can still end it:         retain, mark stale, record last_confirmed
else:                                     terminate (cancel)
```

There is no count of consecutive misses: a count assumes rounds, which requirement 9 forbids, and couples safety to the poll interval. Retention is bounded by the authority's own `expires`. **Retention requires an exit.** An expiry-less incident can only be kept if a terminal vocabulary or a termination lookup can still end it; a source with neither has absence as its only exit, and three shipped sources (GDACS, the Australian feeds, MoWaS) end that way through the last branch. The third branch, a declared absence-ends policy, has no user among shipped sources. A change of scope suspends retention for that cycle, and a supersession the platform can see is not absence. Evidence: [two NAAD hosts disagree](docs/evidence/two-naad-hosts-disagree-on-live-alerts.md), [NWS cancellations never reach the active endpoint](docs/evidence/nws-cancellations-never-reach-the-active-endpoint.md), [retention needs an exit](docs/evidence/retention-needs-an-exit.md).

**Restored data is bounded, not trusted.** A restored incident past its `expires` is terminated at boot before any reconciliation. One still within `expires` keeps its state and content and carries `stale: true` and `last_confirmed` until re-validated. It is **not** set `unavailable`, which would make stock cards drop it during the window it might still matter. The residual exposure is an incident cancelled early while HA was also offline, shown unbadged on a card that ignores `stale`. A blank dashboard mid-storm is worse.

**The reference implementation re-validates from the registry alone.** Its entities do not yet inherit `RestoreEntity` (§5), so the store seeds its known set from the registry at construction. A known id still live fires nothing. An unknown live id fires `incident_created`. A known id is announced removed only when a second fetch-backed reconciliation still lacks it, since a stream rebuild a minute after boot cannot recover what the seed missed. A record already terminal on the first fetch is old news and fires nothing. That removal carries only what the registry kept, the id and the event name.

**Why the churn is deliberate.** Holding incidents only in memory fails the power-blip case. Persisting CAP to `.storage/` every poll wears the SD card §2.4 protects. The entity registry plus `RestoreEntity` plus the recorder survives a restart on HA-native machinery with only sparse attributes touching disk. The cost is registry traffic at incident boundaries, batched. The usual objection, lost customizations, presupposes the entity is a customization target; a warning gone in fifteen minutes is not.

At any moment, `incident.*` entries correspond one to one with active incidents.

### 2.6 Presentation Hints

- `icon` conveys event type (Tornado Warning → `mdi:weather-tornado`) and stays stable across severity changes.
- Severity is the entity `state`, styled by CSS as `weather` and `binary_sensor` already are. No per-severity icons.
- `phase` is an attribute and on the events. Cards surface transitions how they like; the domain exposes the signal.

**No acknowledgment or dismissal service.** Entities mirror upstream reality, and a user dismissing a warning on their phone changes nothing for anyone else in the household. "Seen" state is a card or automation concern.

**Capability detection is by domain, not a version string.** The reference implementation stamps `incident_platform_version` on every entity because a custom component cannot mint a domain and `state.domain == "sensor"` answers nothing. Adopting `incident` retires it.

**Dynamic entities are consumed two ways.** Automations subscribe to the §2.3 events, which fire regardless of entity timing. Display goes through a domain-aware card that renders whatever `incident.*` entities exist and shows all-clear when none do, the pattern `auto-entities` already uses.

### 2.7 Internationalization

State values are stable English tokens and are never localized at the entity. Display translation uses HA's standard `translations/<lang>.json` mechanism, as `weather` and `cover` do.

Provider text is handled in the provider layer. Each integration exposes a `language` option. One-language providers fill the primary fields and set `language`. Multi-language providers (ECCC, MeteoAlarm, WMO) select the user's language for the primary fields and expose the alternate as `*_alt` with `language_alt`. Which block is the alternate is a rule, not document order: English when the primary is not English, else the first other language. The alternate text sits inside the §2.4 bound and is the first spent when an incident does not fit; in a live sweep the localized copy ran longer than the primary 69% of the time (§7.2). Identity is computed from language-independent fields so the two languages share one entity ([evidence](docs/evidence/the-alternate-language-is-a-rule-not-document-order.md)).

---

## 3. Comparison to Existing Solutions

### 3.1 Built-in `alert` Integration

Monitors an internal condition and notifies until it clears. No severity, geometry, lifecycle identity or feed ingestion.

### 3.2 Alert2 (HACS Custom Component)

Rich notification UX over existing entities, templates and events. No schema for external sources and no CAP normalization. A natural downstream consumer of `incident`.

### 3.3 Legacy Weather Alert Sensors

One sensor with alerts packed into attributes, or one `binary_sensor` holding one alert. Failures: 16 KB truncation under load, concurrent-alert dropout (filed against MeteoAlarm in core three times since 2024, §8.2), fragmented history on re-issue, and Jinja for basic automation. A reviewer on #37415 called the packed sensor "an ugly hack or workaround" in 2020, and the per-alert alternative stalled for want of a platform ([the 2020 thread](docs/evidence/one-sensor-per-alert-was-proposed-in-2020.md), [the MeteoAlarm reports](docs/evidence/meteoalarm-shows-one-alert-when-there-are-several.md)).

### 3.4 Domain Naming: `alert` vs `incident`

`alert.*` is internal, user-configured monitoring. `incident.*` is external, structured, ingested. The two are complementary and no change to `alert` is proposed.

### 3.5 Core `issue_registry` / Repairs Dashboard

Repairs surfaces problems the administrator can fix. Incidents are events the household receives and cannot. Forcing CAP onto the Repairs dashboard would either bury actionable items or need an "informational" filter that recreates this proposal.

### 3.6 A Dedicated `incident_registry`

A new registry beside `issue_registry`, ingesting CAP with no entities, is coherent and sidesteps registry churn. It forfeits what entities get for free and would rebuild each: history, the visual state-trigger editor, declarative Lovelace and restart survival. Core does build UI for non-entity primitives (Repairs, Backups, Areas), but each is one bounded admin destination. Incidents need composition: beside a thermostat on a dashboard, filtered by `auto-entities`, used as a state trigger. If the AWG prefers a registry anyway, the schema, events and geometry API port unchanged.

### 3.7 The `geo_location` Platform

The closest existing analogue, and the wrong shape: state is a distance, attributes are an integration-defined bag, there is no identity contract, and off the map there is nothing to show. Two live comparisons exist. Core's `gdacs` and the reference implementation read the same GDACS feed, one onto pins and one onto incidents with severity and episode-stable identity. Core's `nsw_rural_fire_service` and the reference implementation's Australian provider read the same NSW RFS feed the same two ways. `geo_location` answers "what is near me"; `incident` answers "what is happening that I need to act on".

### 3.8 A Frontend-Only Implementation

A card can fetch CAP in the browser; [`weather-radar-card`](https://github.com/jpettitt/weather-radar-card) does, for NWS. It is structurally the only kind of feed it can support. Of fourteen CAP endpoints the reference implementation ingests, probed with an `Origin` header, two send `Access-Control-Allow-Origin` (NWS and NSW RFS). The rest, MeteoAlarm, WMO, both NAAD hosts, GDACS, BBK and three Australian agencies, do not, so a page cannot read them without a server-side intermediary, and in an HA deployment that intermediary is an integration ([evidence](docs/evidence/cors-two-of-fourteen-endpoints.md)).

The stronger objection survives even where the fetch works. A card-local fetch reaches no recorder, no state machine and no automation, and exists only while that dashboard is open. That is requirement 10 failing from the opposite side to §1.6.

---

## 4. Scope and Boundaries

### 4.1 What Belongs on `incident`

External, structured incidents the home receives as a recipient:

- Weather warnings (NWS, ECCC, MeteoAlarm, WMO, the DWD relay inside BBK / NINA, Australian SES storm advice): *shipped*
- AMBER alerts and civil emergency broadcasts: *shipped*, via NAAD and BBK / NINA
- Infrastructure and public-safety notices: 911 outages, evacuations, hazmat, utility outages: *shipped*, via NAAD and BBK / NINA
- Geophysical and natural-hazard disaster alerts (GDACS): *shipped*
- Bushfire incidents on the Australian Warning System ladder (NSW RFS, QFD, DFES, TasALERT): *shipped*
- Utility-issued notifications: grid load, rolling blackouts, water quality. Water contamination is *shipped* where a civil-protection authority relays it (BBK / NINA); the rest is intended reach.
- ISP or upstream service outages via public status feeds: intended reach

**The non-weather claim is load-bearing, and three shipped providers back it.** NAAD is an all-hazards aggregator, and one live sample ingested through the ECCC provider carried a `911 Service Inoperative` at `Extreme` from a provincial emergency office and two AMBER alerts from police, normalized by the same code path as a thunderstorm warning. GDACS adds the geophysical class from a source with no CAP body at all. BBK / NINA is a feed whose primary content is civil protection, arriving as CAP serialized as JSON, and it runs the shared parser after a one-function conversion. Evidence: [NAAD and BBK carry non-weather hazards through the weather code path](docs/evidence/naad-carries-non-weather-hazards-through-one-code-path.md).

The domain spans the three shapes in §2.2. Events from an external authority that concern the household, a gas-leak notice, a fire ban, also belong here. The trait is a structured CAP-like message with HA as the consumer.

### 4.2 What Does Not

Internal device state. A failing disk, a smoke detector, a low battery, a failed backup are `binary_sensor` (`problem` or `safety`) or a purpose-built sensor. Reported to the home from an outside issuer: `incident`. Occurs inside the home's own hardware or software: `binary_sensor`.

### 4.3 Gray Area: User-Constructed Incidents

A power user may promote a sustained `binary_sensor.ups_on_battery` to an `incident.power_outage` with onset and expiry. Opt-in, never automatic.

### 4.4 Why the Boundary Matters

Without it `incident` absorbs `binary_sensor` responsibilities and every "is this an incident?" question splinters the ecosystem. The CAP model does not fit a SMART error and the binary-sensor model does not fit a tornado warning.

---

## 5. Implementation Path

1. Introduce the `incident` domain and `IncidentEntity` in core, with the geometry view (§2.4) and the registry contract (§2.5).
2. Port the **provider-independent** half and its conformance tests: the schema, severity vocabulary, lifecycle and phase semantics, event contract and geometry contract. CAP parsing, profile interpretation and per-source conventions stay in the integrations. Core owning a CAP parser would make it the maintainer of every national profile's quirks.
3. Ship two reference integrations at launch: NWS (VTEC identity, US coverage) and ECCC (composite identity, bilingual, streaming), which also carries the non-weather NAAD traffic. The launch set stays small because the platform already asks core for a domain, a lifecycle, an event contract, a geometry API and registry semantics in one change.
4. GDACS as the first post-adoption provider, carrying the event-report shape. It ships in the reference implementation, so this is a port, not a build.
5. Phase migration of alert handling in `weather` and affected custom integrations. Opt-in, non-breaking (§5.1).
6. Native Lovelace support, building on `weather_alerts_card`, including on-demand geometry.

**How step 2 is expected to be reached.** Two moves, neither shipped yet. First an in-repo neutrality pass (reference implementation issue #216), until no provider name appears in the modules that would move. Then that half is extracted into a hub the per-service integrations depend on through manifest `dependencies`, with the reference implementation as first consumer and two prospective outside maintainers (an ECCC integration and a Brazilian INMET one) as the next. A core proposal with three consumers behind it is a different proposal from one with a single reference implementation.

**What the reference implementation proves today.** Everything in §2 except restart content restore, below. Two cards consume the contract: the companion `weather_alerts_card`, and since October 2026 `ha-alert-card`, maintained independently, which added a `device:` source at this project's request. Its first contact read raw `severity` rather than `severity_normalized` and followed `url` before `web`, the distinction §2.1 draws, met by a second implementer ([evidence](docs/evidence/a-second-card-read-the-raw-severity.md)).

**Restart survival is the principal remaining gap.** The entities do not inherit `RestoreEntity`, so no content is restored, and a failed first reconciliation after a power cut leaves the dashboard blank, the case §2.5's stale flag exists for. Offline expiry has the same status. The re-validation half did land in October 2026 (§2.5). Both restore mechanisms are specified for core and neither is exercised in the field; a reviewer should weigh them accordingly.

Provider quirks stay the integration's job, and the division is measured: a British Columbia ECCC configuration dedups 211 envelope entries to 100 documents, pre-filters ~1,800 candidate bodies to ~7 by bounding box, selects area groups and applies filters, and hands the platform **9 entities**. The 16 KB ceiling and the churn arguments are sized against that number ([evidence](docs/evidence/the-provider-layer-hands-core-single-digits.md)).

### 5.1 Migration Strategy for Legacy Consumers

Packed-attribute sensors such as `sensor.nws_alerts` run in parallel with `incident.*` for six months, HA's standard deprecation window, marked deprecated in logs and docs. A core-provided template or blueprint reconstructs the old flat list from the new entities for unmigrated automations. Legacy sensors are then removed in the affected integrations; the platform itself has nothing to deprecate. `climate` and `water_heater` took the same path.

### 5.2 Test Coverage Requirements

Core test suites for this platform must cover:

- Registry purge across cycles, including storm-scale fan-out (50+ incidents in one cycle).
- Restart between upstream cancellation and local observation; a half-applied removal; an `event` rename that changes the slug.
- Restart staleness (§2.5): a restored incident past `expires` is terminated before any reconciliation; one within `expires` carries `stale` until cleared and never goes `unavailable`.
- Startup reconciliation: orphans scrubbed, still-valid incidents missing from one flaky reconciliation retained.
- Geometry bounds (§2.4): the byte ceiling evicts, and both surfaces return not-found for an evicted or unknown ref rather than raising.
- Provider-supplied termination (§2.2): a terminal lifecycle status with `msgType=Update` resolves to `cancel` while `expires` is ahead and `expired` after; unknown status fails open.
- Region-scoped area groups (§2.2): one document live for one region and terminal for another.
- Ingest-mode neutrality (§2.5): identical lifecycle assertions under poll and stream, including a reconnect backfill clearing staleness.
- Lossy-source tolerance (§2.5): absent once but within `expires` is not terminated; absent from one source but present on another is retained.
- Removal payload (§2.3): terminal `phase` only; `removal_reason` emitted only on a recognized signal, absent otherwise, pairing with either phase.
- Announced-once endings (§2.3): a terminal record republished for 48 hours fires one removal; the same ending re-issued under a new revision fires none; a non-terminal reissue fires a creation and a later ending fires again.
- Boot re-validation (§2.5): known live fires nothing, unknown live fires a creation, known and absent on two fetch-backed reconciliations fires a removal with what the registry kept, terminal on the first fetch fires nothing, and a stream rebuild before the first fetch counts toward none of it.
- Geometry handle namespacing (§2.4): two entries on one provider see one alert and hold distinct refs; purging one leaves the other.
- Payload bound (§7.2): a bilingual incident over the ceiling is published under it, alternate text spent before primary, a field below the readable floor dropped not stubbed, measured the way the recorder measures.

---

## 6. Future Work and Alternatives Considered

### 6.1 Fallback: Static Entity Pool

If the AWG rejects dynamic creation, each config entry pre-allocates N slots (`incident.<slug>_slot_1` … `_N`). Slots fill and drain, assignment is sticky for the incident's life, `unique_id` is the slot and the lifecycle hash moves to an `incident_id` attribute. It buys a static registry and stable History names. It costs 30 to 50 permanent entities per entry showing `unknown` on quiet days, an empty-slot filter in every card and automation, history keyed by attribute rather than entity, and a deterministic assignment algorithm so concurrent churn cannot swap slots. It satisfies every §1.4 requirement and the schema, events and geometry API are unchanged. @pyspilf's fixed-slot MeteoAlarm implementation is the prior art ([forum thread](https://community.home-assistant.io/t/getting-all-active-meteoalarm-alerts-weather-alerts-card-integration/1006597)).

### 6.2 Cross-integration Geometry Store

A core-managed store, like `image` or `media_source`, would share county polygons across integrations, survive restarts without re-polling, and clean up by reference count. The prize is the tail, not bulk: a cold render of every zone a nationwide alert set references is about 1.78 MB across 265 requests, a rounding error on broadband and real for a rate-limited provider. Out of scope for v1; the §2.4 view is backend-agnostic so a store can land behind it ([evidence](docs/evidence/zone-geometry-is-heavy-tailed.md)).

### 6.3 Sub-incident Relationships

`parent_id` (§2.1) is the hook. No v1 provider produces hierarchy. CAP's `<incidents>` element is the wire mechanism; the Australian provider reads it, but as identity (the fire's incident number), not as a parent link. When it lands, children carry `parent_id` and parents do not enumerate children ([evidence](docs/evidence/australian-feeds-publish-no-end-time.md)).

### 6.4 Long-term Archival Hook

Durable records subscribe to the events and forward payloads to an external sink. **The removal event must be self-sufficient, and a consumer must not dereference the entity.** §7.3 removes the entity and purges its geometry in the same cycle the event fires, so a fetch after the event races and loses silently. `created` establishes the record, `updated` mutates it, `removed` closes it with identity, terminal phase and reason. One exception: an incident superseded by a revision the platform can see is dropped without a removal, because the successor's event carries the news; close those on the successor's arrival. A reference blueprint ships at [`blueprints/cap_alerts_archive_incident_removed.yaml`](blueprints/cap_alerts_archive_incident_removed.yaml).

### 6.5 Per-zone Sub-device Grouping

One sub-device per `affected_zones` entry multiplies registry churn under fan-out, and per-issuer grouping (§2.1) probably obviates it. Deferred.

### 6.6 Bundled Zone-Geometry Artifact: Considered and Rejected

Precompute the simplification, not the distribution. The best bundled artifact of all NWS land zones is 4.91 MB gzipped; resolving on demand costs about 1.78 MB for a nationwide render and about 20 KB for a realistic viewport, and the artifact pins every install to its release date. Two methodological points generalize: sampling cannot estimate a heavy-tailed geometry population (a 115-zone sample was off by 6x to 49x per type), and gzip on coordinate JSON is about 4:1, not the 10:1 the pretty-printed NWS API suggests ([evidence](docs/evidence/zone-geometry-is-heavy-tailed.md)).

---

## 7. Appendix

### 7.1 Example Entity State

A live NWS Severe Thunderstorm Warning:

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

The `_7c4e1f9a` suffix is the §2.2 short hash, not an office code. `friendly_name` is the CAP `event` with no office suffix; `sender` carries provenance.

A non-weather incident from a live NAAD message, same shape, different `category`:

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

`category: Infra` lets a card filter this from weather without matching on `event`, and the severity scale is doing real work: a 911 outage is `Extreme` on the same scale as a tornado.

### 7.2 Attribute Size Budget

Modeled size of a CAP-rich incident after externalization, and the bound the reference implementation enforces. The schema can emit 65 attribute keys, about forty on a typical incident. The bound moved from the field to the payload after a 2026-08-16 sweep of 9,604 messages found a per-field cap failing both ways: it shredded a tropical statement that fit at 14,290 bytes, and it could not rescue an air-quality warning that overflowed at 19,084 bytes on 9,535 bytes of text.

| Field group | Typical bytes | Modeled cap |
| :---------- | ------------: | ----------: |
| `id`, `url`, `identifier` | 190 | 370 |
| `event`, `headline` | 145 | 290 |
| long-form text: `description`, `instruction`, `description_alt`, `instruction_alt` | 4,600 | **whatever the 15,800-byte budget leaves** |
| `event_alt`, `headline_alt`, `language`, `language_alt` | 150 | 350 |
| severity trio, `status`, `scope`, `category`, `response_type` | 110 | 190 |
| `phase`, `msg_type`, `lifecycle_status`, `previous_phase`, `phase_changed` | 70 | 120 |
| 5× timestamps | 160 | 200 |
| `area_desc` | 200 | **on the trim ladder, after the alternate text** |
| `affected_zones`, `affected_zone_uris` | 240 | 900 |
| `geocodes` | 350 | **unrecorded, outside the bound** |
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
| **Total, as the recorder measures it** | **~6.9 KB** | **~6.0 KB structural, plus the trimmable text and area list** |

The implementation serializes what it is about to publish, measures it as the recorder does (`state.attributes` minus the domain exclusions and the entity's unrecorded attributes), and trims only past 15,800 bytes: `description_alt`, `instruction_alt`, `area_desc`, `description`, `instruction`, each spent in full before the next, a field under 160 bytes dropped rather than stubbed. `parameters` and `geocodes` are unrecorded, the two source-controlled lists no cap can bound; the second joined after a 291-area frost advisory carried 14,072 bytes of area names and 12,245 of codes against 1,837 of text. The model keeps the full text, so the lifecycle diff runs on what the source sent ([sweep](docs/evidence/per-field-text-caps-fail-both-ways.md), [overflow](docs/evidence/area-lists-overflow-after-text-is-spent.md)).

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

A signalled termination arrives on a *present* record and resolves on the `updated IDs` path, or on `new IDs` when a first sighting is already terminal, firing `incident_removed` in place of `incident_created`. Supersession the platform can see drops the predecessor without an event; the successor already carried the news. Step 1 is skipped for an ending already announced (§2.3), and the first reconciliation after a boot re-validates rather than re-announces (§2.5). A retained incident takes none of the four steps ([evidence](docs/evidence/one-ending-one-removal.md)).

---

## 8. Prior Art & Acknowledgements

### 8.1 Related Home Assistant Core Work

- [home-assistant/core#164481](https://github.com/home-assistant/core/pull/164481) (@michaeldavie), combining ECCC alerts into one packed-attribute sensor. Closed unmerged 2026-06-08 after review asked for "actions with return values" instead; its successor [#172393](https://github.com/home-assistant/core/pull/172393) took that route and merged. ECCC's richest alert data is now reachable by automations and not by cards, which is requirement 10 failing in production ([evidence](docs/evidence/core-review-moved-alert-bodies-into-actions.md)).
- [home-assistant/core#161882](https://github.com/home-assistant/core/pull/161882) and [#166125](https://github.com/home-assistant/core/pull/166125) (@DeerMaximum): NINA's attributes replaced by per-field sensors plus a `nina.get_details` action, with `description` and `recommended_actions` existing only in the response after HA 2026.11. The same resolution, incomplete by construction.
- [architecture#1357](https://github.com/home-assistant/architecture/discussions/1357) and [#1360](https://github.com/home-assistant/architecture/discussions/1360) (@jpbede): a forecast contract for sensor entities, on the same reasoning and naming the same frontend gap. Forecasts and incidents are one shape of problem.
- [home-assistant/core#37415](https://github.com/home-assistant/core/pull/37415) (@MatthewFlamm) and [#100009](https://github.com/home-assistant/core/pull/100009) (@IceBotYT): both closed. The first thread reached this RFC's conclusions years earlier, one sensor per alert and `references` for lifecycle, and stalled for lack of a platform.
- [home-assistant/core#103352](https://github.com/home-assistant/core/issues/103352) and [#150737](https://github.com/home-assistant/core/issues/150737): the DWD warning that did not reset after the event ended, filed twice two years apart. The upstream `EXPIRES` was exposed as an attribute and not used to end the warning until [#163096](https://github.com/home-assistant/core/pull/163096) merged on 2026-02-25.

### 8.2 Reference Integrations

- `nws_alerts` (@finity69x2, @firstof9): the canonical 16 KB failure and the original motivation.
- Environment Canada (core, @michaeldavie et al.): lifecycle-aware handling of a CAP-adjacent feed; much of the ECCC field vocabulary.
- MeteoAlarm, BoM and DWD (core): the concurrent-alert dropout filed as [#108908](https://github.com/home-assistant/core/issues/108908), [#131045](https://github.com/home-assistant/core/issues/131045) and [#156838](https://github.com/home-assistant/core/issues/156838), the missing `unique_id` in [#103132](https://github.com/home-assistant/core/issues/103132), and forum threads across nine countries since 2019.
- `gdacs`, `nsw_rural_fire_service` and `nina` (core): the three core integrations whose sources the reference implementation also ingests, two onto `geo_location` (§3.7) and one through the action migration (§1.6).

### 8.3 Complementary Projects

- Built-in `alert` and Alert2: internal monitoring and notification UX, complementary (§3.1, §3.2).
- `weather_alerts_card`: the companion card.
- [`ha-alert-card`](https://github.com/DTekNO/ha-alert-card) (@DTekNO): the first consumer of the entity contract outside this project (§5).
- [`weather-radar-card`](https://github.com/jpettitt/weather-radar-card): the working frontend-only example and where its boundary falls (§3.8).

### 8.4 Standards & Specifications

- OASIS CAP 1.2 (ITU X.1303): the normalization target.
- Waidyanatha, Bhandari & Frommberger, *ITU X.1303 International Warning Standard: Lessons from an Asian Implementation*, J. ICT Standardization 4(3), 2017, [doi:10.13052/jicts2245-800X.431](https://doi.org/10.13052/jicts2245-800X.431). Field evidence from the issuer side for the same five severity tiers, externalized polygons, expiry-based deletion and one language per feed.
- NWS VTEC (10-1711): lifecycle identity on US products.
- GeoJSON (RFC 7946): the geometry representation.

### 8.5 Acknowledgements

Thanks to the maintainers of `nws_alerts`, Environment Canada, Alert2, MeteoAlarm and DWD for years of field-testing the problem space, and to the Architecture Working Group for the conventions this builds on. Thanks to @pyspilf, whose fixed-slot MeteoAlarm implementation is the prior art for §6.1 and who gave the first external review. And to @DTekNO, who shipped a device source in `ha-alert-card` within a day of being asked.

---

## 9. Conclusion

Structured external notifications are central to Home Assistant's role in emergency awareness, and today's approaches degrade exactly as the number of relevant incidents rises.

The proposal has two parts. The first is that Home Assistant needs a first-class incident abstraction: normalized severity, identity stable across revisions, a lifecycle that trusts neither `msgType` nor a single missed observation, an event contract, and a payload bounded in both dimensions. §1.4 states that case without reference to a binding, and each requirement is backed by observed provider behavior rather than specification reading. That is the claim this RFC most wants tested.

The second is that dynamic `incident.*` entities are the right binding, because they inherit the recorder, the trigger editor, `RestoreEntity` and the card ecosystem at the cost of batched registry churn. The case is good and not conclusive; §3.6 and §6.1 set out the alternatives, and the contract ports to either. A reviewer who accepts the abstraction and rejects the binding has moved the discussion to where it should be.

I invite collaboration on any part of this, and disagreement on the second part most of all.
