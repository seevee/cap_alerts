# Frontend Hints

Copy-paste references for cards and automations consuming `cap_alerts`.

This page is the consumer contract: what an alert entity looks like, how to
find one, and how to get its polygon. For the event bus, see
[`events.md`](events.md); for why the model is shaped this way, see
[`architecture.md`](architecture.md).

Nothing here requires a bespoke adapter. One entity per alert plus a flat
attribute surface means generic cards (`auto-entities`, `flex-table-card`,
markdown/template cards) work without any integration-specific code.

---

## Entity model

A configured location produces one device carrying:

| Entity | State | Notes |
| :-- | :-- | :-- |
| `sensor.cap_alerts_<provider>_alert_count` | number of active alerts | Attributes `active` / `upcoming` split it on `onset`. Diagnostic. |
| `sensor.cap_alerts_<provider>_last_updated` | ISO timestamp | `device_class: timestamp`. Diagnostic. |
| `sensor.cap_alerts_<provider>_cap_alert_<event>_<hash>` | **normalized severity** | One per active alert, created and removed each poll cycle. |
| `button.cap_alerts_<provider>_refresh` | — | Forces an off-cycle fetch. Diagnostic. |
| `binary_sensor.cap_alerts_eccc_real_time_stream` | NAAD socket state | ECCC-with-streaming only. Diagnostic. |

### The alert entity

The thing most cards get wrong on first attempt: **the state is the normalized
severity, not the headline.**

```yaml
state: severe            # extreme | severe | moderate | minor | unknown
name: Severe Thunderstorm Warning     # the CAP <event> string
icon: mdi:weather-lightning           # dispatched from event type
```

Entity ids follow `{device name}_cap_alert_{slugified event}_{8-char hash}` — for
example `sensor.cap_alerts_nws_cap_alert_severe_thunderstorm_warning_a1b2c3d4`.
The hash derives from the entity's `unique_id`
(`{entry_id}_{provider}_{alert_id}`) and disambiguates two concurrent alerts of
the same type. The device prefix is Home Assistant's, applied because these
entities set `has_entity_name`, so it follows the device name and changes when a
user renames the device.

**Do not parse the entity id.** The integration only suggests the object id, so
users can rename it, the device prefix moves with the device, and the event slug
changes with the alert language. Discover
entities by device or by attribute (see [Discovery](#discovery) below), and
read identity from the `id` attribute.

Alert entities are created and removed dynamically. A card must tolerate an
entity disappearing between renders — that is the normal end-of-alert path,
not an error.

---

## Attribute schema

`to_attributes()` is **sparse**: every empty string, `None`, and empty
collection is omitted rather than emitted as a null. Check for presence; do
not assume a key exists. Tuples serialize as JSON lists.

Seven keys are guaranteed on every alert entity. `id`, `provider`, and
`phase_changed` are always set on the model; `phase`, `severity_normalized`,
and `icon` are filled in by normalization, so they survive even a message that
arrived nearly empty; `incident_platform_version` is stamped on by the entity.
Everything else in the tables below may be absent.

The split is worth internalizing: those seven come from the integration, and
everything else comes from the feed. Feed completeness varies enormously
between authorities — `severity` itself is missing on MeteoAlarm, which
publishes awareness levels rather than CAP severity — so read anything outside
the guaranteed set defensively.

### Identity and lifecycle

| Attribute | Type | Notes |
| :-- | :-- | :-- |
| `id` | `str` | Stable lifecycle-aware hash. Survives Update/Cancel — this is the key to correlate across polls. Called `incident_id` on bus events. |
| `url` | `str` | Canonical alert page at the provider. |
| `identifier` | `str` | Raw CAP `<identifier>`. |
| `provider` | `str` | `nws` / `eccc` / `meteoalarm` / `wmo`. |
| `phase` | `str` | Always present. `new` / `update` / `cancel` / `expired`. |
| `previous_phase` | `str` | Phase at the previous poll. |
| `phase_changed` | `bool` | Always present. `true` on first sighting or on a phase transition. |
| `lifecycle_status` | `str` | Provider-native termination vocabulary (ECCC `ended` / `transitioned_out`). |
| `references` | `list[str]` | CAP `<references>` identifiers. |
| `replaced_by`, `replaced_at` | `str` | Supersession pointers when published. |
| `stale` | `bool` | Present **only when true**. The latest poll did not see this alert, but it was kept rather than removed, because one missed observation is not proof an alert ended. Grey it out; do not treat it as gone. |
| `last_confirmed` | `str` | ISO timestamp of the last poll that did see it. Only present alongside `stale`. |
| `incident_platform_version` | `str` | Contract version (currently `"1.0"`). Branch on this, not on integration version. |

### Classification

| Attribute | Type | Notes |
| :-- | :-- | :-- |
| `event` | `str` | Free-text event name. Also the entity's display name. |
| `severity_normalized` | `str` | Always present. `extreme` / `severe` / `moderate` / `minor` / `unknown`. **Prefer this over `severity`** — it is normalized across providers. |
| `severity` | `str` | Raw CAP value, as received. Absent where severity is derived rather than transmitted (MeteoAlarm). |
| `msg_type`, `status`, `scope`, `category`, `urgency`, `certainty`, `response_type` | `str` | Raw CAP 1.2 values. |
| `icon` | `str` | Always present. mdi icon name, dispatched from event type. |
| `event_code_nws`, `event_code_same` | `str` | Provider event codes where published. |

### Timing

| Attribute | Type | Notes |
| :-- | :-- | :-- |
| `sent`, `effective`, `onset`, `expires` | `str` | ISO 8601, as received. |
| `ends` | `str` | Present only when the provider distinguishes it from `expires`. |

An alert with `onset` in the future is **upcoming**, not active — this is the
split the count sensor exposes. Cards showing a "now" view should honour it.

Beware the no-warning marker: some senders signal "no alert" with
`expires <= onset`. Test both `<` and `==`.

### Content

| Attribute | Type | Notes |
| :-- | :-- | :-- |
| `headline`, `description`, `instruction`, `note`, `web` | `str` | Primary-language content. |
| `language` | `str` | BCP-47 of the primary content (e.g. `en-CA`). |
| `event_alt`, `headline_alt`, `description_alt`, `instruction_alt` | `str` | Alternate-language siblings, present for bilingual feeds. |
| `language_alt` | `str` | BCP-47 of the alternate content. |

`event_alt` exists for classification, not display — `<event>` is CAP free
text, so a localized one matches no icon keyword.

### Geography

| Attribute | Type | Notes |
| :-- | :-- | :-- |
| `area_desc` | `str` | Human-readable affected areas, comma-joined. |
| `bbox` | `list[float]` | `[min_lon, min_lat, max_lon, max_lat]`. Cheap enough to render before fetching the polygon. |
| `geometry_ref` | `str` | Handle for the full polygon. See [Geometry](#geometry). |
| `points` | `list[[lon, lat]]` | Point locations from zero-radius CAP `<circle>` elements. |
| `geocodes` | `dict[str, list[str]]` | Every area geocode the feed published, keyed by raw CAP `valueName`. The complete surface. |
| ~~`geocode_ugc`, `geocode_same`, `geocode_clc`, `geocode_sgc`~~ | — | **Removed.** They republished codes `geocodes` already carried — the geocode surface twice on every alert. Read the container and take every scheme, well-known or not. |
| `affected_zones`, `affected_zone_uris` | `list[str]` | Zone codes and their provider URIs. |
| `is_marine` | `bool` | Present **only when true**. Absence means "not marine". |

Full `geometry` is **never** an attribute — see below.

### Provider extras

| Attribute | Type | Notes |
| :-- | :-- | :-- |
| `sender`, `sender_name` | `str` | Issuing office. |
| `vtec` | `list[str]` | Raw VTEC strings (NWS). |
| `vtec_office`, `vtec_phenomena`, `vtec_significance`, `vtec_action`, `vtec_tracking` | `str` | Parsed VTEC components (NWS). |
| `parameters` | `dict` | Provider `<parameter>` catch-all. Shape varies by source — treat as untyped. Present on the live state, but declared unrecorded, so it is absent from history. |
| `episode_days` | `list[dict]` | Per-day profile of a merged MeteoFrance episode, ordered by date. Keys: `date`, `onset`, `expires`, `severity`, `awareness_level`, `event`, `headline`, `area_desc`. |

### Oversized alerts

Home Assistant's recorder drops a state's attributes wholesale once they
serialize past 16 KB, so an alert that would overflow is trimmed before it is
published. Rare — nothing in a 443-alert live sweep of NWS and ECCC needed it,
now that the geocode surface is published once instead of twice — but the
consequences are visible to a card, so read them defensively:

1. `description_alt`, then `instruction_alt`, then `description`, then
   `instruction` are truncated (trailing `…`) or dropped, in that order. The
   alternate language pays before the primary, and the instruction outlives the
   description within a language.
2. `affected_zone_uris` is dropped — a fixed prefix plus the codes already in
   `affected_zones`.

The trim is display-side only: the integration keeps the full text internally,
so `changed_fields` on the event bus never reports a truncation as a reword.

---

## Geometry

Polygons are held out of band. They routinely exceed Home Assistant's 16 KB
attribute limit — the exact problem this integration exists to avoid — so
`to_attributes()` omits `geometry` unconditionally and publishes a
`geometry_ref` handle instead.

Render `bbox` immediately, then fetch the polygon lazily when the alert is
actually on screen.

### Websocket (preferred for live cards)

```ts
const result = await hass.connection.sendMessagePromise({
  type: "cap_alerts/geometry",
  geometry_ref: attrs.geometry_ref,
});
// result = { type: "FeatureCollection", features: [ { type: "Feature", geometry, properties: { ref } } ] }
```

Unknown refs send an error frame (`not_found`).

### REST

```sh
curl -H "Authorization: Bearer $HA_TOKEN" \
     "http://homeassistant.local:8123/api/cap_alerts/geometry/01JABCDEF:nws:3f9c1a2b7d04"
```

Returns the same `FeatureCollection`; 404 for unknown refs.

Refs are shaped `{entry_id}:{provider}:{alert_id}` — namespaced by config
entry because the geometry store is a process-wide singleton, so two entries
on the same provider would otherwise mint colliding refs. Treat the whole
string as opaque and pass it through unparsed.

Refs are purged when the alert leaves the active set, so a stale ref 404s
rather than returning someone else's polygon. Treat 404 as "this alert is
gone" and drop the layer.

Not every alert has geometry. Zone-based alerts — the majority of the NWS
feed — carry `affected_zones` and no polygon at all. Fall back to `bbox`, or
resolve zone shapes yourself from the provider.

---

## Discovery

### auto-entities

One entity per alert means no adapter is needed:

```yaml
type: custom:auto-entities
card:
  type: entities
  title: Active weather alerts
filter:
  include:
    - integration: cap_alerts
      attributes:
        severity_normalized: severe
  exclude:
    - attributes:
        is_marine: true
sort:
  method: state
```

### Template

```jinja
{% set alerts = states.sensor
     | selectattr('attributes.incident_platform_version', 'defined')
     | selectattr('attributes.phase', 'in', ['new', 'update'])
     | list %}
{{ alerts | map(attribute='attributes.event') | join(', ') }}
```

Selecting on `incident_platform_version` is the cheapest reliable test for
"this is a cap_alerts alert entity" — the diagnostic sensors do not carry it.

### Count sensor

For a badge or a conditional card, read the count sensor rather than
enumerating entities:

```yaml
type: conditional
conditions:
  - entity: sensor.cap_alerts_nws_alert_count
    state_not: "0"
card:
  type: custom:your-card
```

Its `active` / `upcoming` attributes split the total on `onset`, so a card can
show "2 now, 1 later" without walking every entity.

---

## Events

The integration fires `incident_created`, `incident_updated`, and
`incident_removed` on the HA event bus. A card wanting push updates rather
than polling entity state should subscribe to those.

Full payload schema, terminal-phase semantics, and `removal_reason` are
documented in [`events.md`](events.md).

---

## Stability

`incident_platform_version` is the contract version. Within a major version:

- Attributes are added, never renamed or removed. A single oversized alert can
  still arrive missing keys it would otherwise carry (see
  [Oversized alerts](#oversized-alerts)) — that is sparseness on one state, not
  a change to the schema.

The one removal so far predates the first stable release: the `geocode_*`
aliases came off the attribute surface during the 0.x alpha line, because they
republished codes `geocodes` already carried on every alert. The marker stays at
`1.0` — the promise above binds from the stable release onward, not across the
alphas. Read `geocodes`, which has carried every scheme since it landed.
- `severity_normalized`, `phase`, and `id` keep their value vocabularies.
- The `geometry_ref` → `FeatureCollection` shape is stable across both the WS
  command and the REST view.

Sparseness is part of the contract, not an implementation detail: a card that
assumes a key exists will break on the next provider, because feed
completeness varies enormously between sources. Guard every read.
