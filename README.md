# CAP Alerts

Weather and hazard alerts for Home Assistant, **one entity per active alert**.

Single-entity alert integrations pack every alert into one attribute blob and
run into Home Assistant's 16 KB attribute limit. CAP Alerts gives each alert
its own sensor. The state is a normalized severity, the attributes are the
alert's [CAP 1.2](https://docs.oasis-open.org/emergency/cap/v1.2/CAP-v1.2.html)
fields, and an event fires when an alert is created, updated or removed.

The providers are the visible part. The point of the project is the model
underneath them: one identity per incident across re-issues, a lifecycle that
does not trust feed presence, bounded attributes, and lifecycle events.
[`docs/rfc-summary.md`](docs/rfc-summary.md) is the one-page version of the
proposal: that model as an `incident` integration for Home Assistant Core, with
this integration as its reference implementation. [`rfc.md`](rfc.md) argues it
in full, and [`docs/evidence/`](docs/evidence/README.md) holds the field
evidence behind it, one page per finding.

[![Alerts rendered by the companion Weather Alerts Card](https://raw.githubusercontent.com/seevee/weather_alerts_card/main/img/hero-adaptive.svg)](https://github.com/seevee/weather_alerts_card)

*Shown with the companion
[Weather Alerts Card](https://github.com/seevee/weather_alerts_card). Generic
cards work too, see [Cards and automations](#cards-and-automations).*

## Providers

| Provider | Source | Coverage | Location modes |
|---|---|---|---|
| **NWS** | U.S. National Weather Service | United States | Zone ID (`ILZ014`, comma-separated allowed), GPS `lat,lon`, `device_tracker` entity |
| **ECCC** | Environment and Climate Change Canada | Canada | Province code (`AB`, `BC`, `ON`, …), GPS, `device_tracker` entity |
| **MeteoAlarm** | EUMETNET European aggregator | About 37 national services | Country (`DE`), with an optional GPS filter or a region multi-select |
| **WMO** | WMO Severe Weather Information Centre, plus each service's own feed | About 100 national services with no dedicated provider | Source ID from the live SWIC registry (`mx-smn-es`), with an optional GPS filter |
| **GDACS** | Global Disaster Alert and Coordination System | Worldwide earthquakes, cyclones, floods, volcanoes, droughts, wildfires, tsunamis | Worldwide, GPS, or `device_tracker` entity |
| **BBK** | German federal civil protection (the NINA app backend) | Germany: MoWaS, KATWARN, BIWAPP, LHP flood warnings, DWD weather warnings of level 3 and up | District (Regionalschlüssel, `09564`), GPS, or `device_tracker` entity |
| **AU** | Australian state fire and emergency services | NSW (RFS), Queensland (QFD), Western Australia (DFES), Tasmania (TasALERT): bushfires and incidents, plus SES weather warnings on TasALERT | State |
| **GeoSphere** | GeoSphere Austria (the former ZAMG) Warn API | Austria, per municipality: wind, rain, snow, black ice, thunderstorm, heat and cold warnings | GPS, or `device_tracker` entity |
| **TWC** | U.S. Tsunami Warning Centers (NTWC Palmer, PTWC Honolulu) | Tsunami warnings, advisories, watches and information statements for Alaska, BC and the U.S. West Coast; the U.S. East and Gulf coasts; Hawaii; Guam and the Northern Marianas; American Samoa; Puerto Rico and the U.S. Virgin Islands | Region |

GPS and tracker modes keep only alerts whose affected area contains the point.
GeoSphere resolves the point to its municipality itself, and the alert's
polygon is that municipality.
The Australian feeds have no GPS mode: most of their alerts are a location
marker with no polygon, so a point test would drop them. Filter by distance on
the card instead, which reads the marker from `points`.
TWC has no GPS mode either: the bulletins name the threatened coast in text and
zone codes and carry no shape to test a point against.
The MeteoAlarm region picker lists `EMMA_ID` codes for most countries, `NUTS`
codes for some, and area names where a feed publishes no geocodes.

GDACS and BBK are the non-weather sources, and the reason the model is a CAP
model rather than a weather model. Further providers (BoM, a direct DWD feed, …)
plug in behind the same provider protocol; see
[Contributing](CONTRIBUTING.md#adding-a-provider).

## Limits

The integration shows what the feed publishes, and feeds have gaps. Two
sanctioned hosts of one national feed have been measured
[disagreeing about which alerts are live](docs/evidence/two-naad-hosts-disagree-on-live-alerts.md),
and NWS
[never publishes a cancellation](docs/evidence/nws-cancellations-never-reach-the-active-endpoint.md)
to the endpoint it serves active alerts from. The model is built around that,
an alert missing from one poll is kept and marked `stale` rather than dropped,
but it can't show an alert the feed never carried.

Latency is the poll interval, 5 minutes by default, plus the feed's own delay
behind the issuing office. ECCC streaming is the one path that is not polled.
For a TWC entry, set the interval to 60 s: a tsunami bulletin can matter within
minutes, and a quiet poll there is two requests answered `304 Not Modified`.

Keep the official channel for your area as the primary warning. This is a
second one, and a way to automate on it.

## Installation

Requires Home Assistant 2026.4.3 or newer.

**HACS.** CAP Alerts is not in the HACS default store yet, so it is installed
as a custom repository. This badge adds the repository and opens it in HACS:

[![Open CAP Alerts in HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=seevee&repository=cap_alerts&category=integration)

Or add it by hand: HACS → ⋮ → **Custom repositories**, repository
`https://github.com/seevee/cap_alerts`, type *Integration*. Then install
**CAP Alerts** and restart Home Assistant. Once it lands in the default store
the custom repository can stay in place; HACS treats the two the same for
updates.

**Manual.** Copy `custom_components/cap_alerts/` into your configuration's
`custom_components/` directory and restart.

## Setup

[![Add CAP Alerts](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=cap_alerts)

Settings → Devices & Services → **Add Integration** → *CAP Alerts*. Pick a
provider, then a location mode from the table above. One entry covers one
scope. Add another entry for a second zone, province or country.

- **Reconfigure** changes an entry's provider or location.
- **Options** changes its behavior: polling, language, filters.

### Options

Every provider:

| Option | Range | Default |
|---|---|---|
| Scan interval | 60–3600 s | 300 |
| Timeout | 5–120 s | 30 |

Per provider:

| Option | Providers | What it does |
|---|---|---|
| Language | ECCC, MeteoAlarm, WMO, BBK, GeoSphere | Which `<info>` block to read. ECCC: `auto`, `en-CA` or `fr-CA`. MeteoAlarm: a two-letter prefix (`en`, `de`, `fr`). WMO: `auto` or any BCP 47 tag the source publishes (`zh-Hans`). BBK: `auto`, `de`, `de-LS` (easy-read German), `en` or one of the app's other translations. GeoSphere: `de` (default, the source text) or `en` (GeoSphere's translation). NWS is English only. |
| Real-time streaming | ECCC | Ingest alerts the moment they are issued, over the NAAD socket. Default on. See [ECCC streaming](#eccc-streaming). |
| Feed source | ECCC, WMO | ECCC: which NAAD GeoRSS host serves polling and backfill. `auto` (default) fetches both hosts and unions them. `alertready` or `pelmorex` pins one. WMO: `auto` (default) reads the feed the weather service publishes itself, and merges in the SWIC mirror where it still answers. SWIC retired the mirror for all but `cn-cma-xx` and `au-bom-en` in October 2026, so a mirror that keeps answering 404 is only re-checked every 30 minutes. `mirror` or `authority` reads one only. Both feeds are revalidated with conditional GETs, and an authority index over 256 KB (`cn-cma-xx`'s is ~950 KB) is re-read every 30 minutes with its last link list standing in between, so a country-wide entry no longer downloads a megabyte a poll. |
| Event types | GDACS | Which hazards to track. All by default. Applied before any geometry is fetched, so narrowing it also cuts fetch cost. |
| Minimum alert level | GDACS, AU | GDACS: `Green`, `Orange` (default) or `Red`, on GDACS's own impact scale. The entity state derives from it too: Green → `minor`, Orange → `severe`, Red → `extreme`. AU: `All` (default), `Advice`, `Watch and Act` or `Emergency Warning` on the Australian Warning System; `All` also keeps the informational tiers below the ladder (planned burns, incidents with no warning). |
| Exclude marine alerts | NWS, ECCC | Drop alerts carrying a marine zone code. Default off. |
| Area codes (prefix match) | All but GDACS, BBK, AU and TWC | Keep only alerts whose area codes start with one of the listed prefixes (`13,37`). See [Area-code narrowing](#area-code-narrowing). |

#### ECCC streaming

With streaming on, the GeoRSS feed becomes a startup and reconnect backfill
plus a periodic resync. A diagnostic binary sensor reports the socket state.
Alerts issued while the socket is down are recovered from the NAAD 48-hour
repository, so a reconnect gap does not depend on the GeoRSS index carrying
the alert.

Turning streaming off falls back to GeoRSS polling on the scan interval.

**Deprecation.** The legacy NAAD GeoRSS host (`pelmorex`) retires in late
September 2026. The surviving host omits some live alerts, which a polling
entry reads as ended. Two configurations therefore raise a repair under
Settings → Repairs:

- Streaming off. Submitting the repair turns streaming back on. Ignore it if
  polling is deliberate.
- Feed source pinned to `pelmorex`. Submitting sets it back to `auto`.

#### Area-code narrowing

Opt-in narrowing on top of the location chosen at setup. Codes are
hierarchical, so a shorter prefix covers a wider area: `13` is Hebei, `1307`
is Zhangjiakou. It exists for sources with no per-alert geometry and no region
picker, notably WMO's `cn-cma-xx`, where `13` cuts a country-wide entry from
about 234 alerts to 28. Code lengths vary within a scheme, so prefer the
leading digits over a full code.

## Entities

Every entry produces one device, named `CAP Alerts <PROVIDER>`, grouping:

| Entity | State | Notes |
|---|---|---|
| `sensor.cap_alerts_<provider>_cap_alert_<event>_<hash>` | `minor`, `moderate`, `severe`, `extreme` or `unknown` | One per active alert. Created and removed dynamically each poll. |
| `sensor.cap_alerts_<provider>_alert_count` | number of alerts | Attributes `active` and `upcoming` split the total on whether `onset` has passed. Diagnostic. |
| `sensor.cap_alerts_<provider>_last_updated` | ISO timestamp | When the alert set last changed: an alert issued, updated or ended. Kept across a restart. Not the last poll: that is the count sensor's `last_reported` in templates and the recorder (the REST and websocket state keep the value from its last real change), or `last_success` in the diagnostics download; a failed poll makes the entry unavailable. Diagnostic. |
| `button.cap_alerts_<provider>_refresh` | — | Fetch now, without waiting for the next poll. Diagnostic. |
| `binary_sensor.cap_alerts_eccc_real_time_stream` | `on` / `off` | NAAD socket connected. ECCC with streaming on only. Diagnostic. |

The device name is stable across reconfigures, so entity IDs do not drift when
you change GPS, zone or region. The entry title carries the location detail,
the device name does not: two entries of one provider, two NWS zone groups or
two Australian states, show as two devices named alike in every device picker,
the trigger target among them. Rename the device to tell them apart: Settings
→ Devices & Services → CAP Alerts → the device → the pencil next to its name.
The name you give it survives updates and reconfigures (#325). Alert entities
created after the rename take it as their ID prefix, and the rename dialog
offers to move the existing ones over too.

### Alert attributes

Attributes are a sparse dict of CAP fields: only populated fields appear.
[`docs/frontend_hints.md`](docs/frontend_hints.md) is the consumer-facing
reference; `CAPAlert` in `model.py` is the full schema.

Polygons are never emitted as attributes. Each alert carries a `geometry_ref`
handle plus a `bbox`, and the full GeoJSON `FeatureCollection` comes from:

- REST: `GET /api/cap_alerts/geometry/{geometry_ref}` (HA auth required)
- Websocket: `{"type": "cap_alerts/geometry", "geometry_ref": "<ref>"}`

### Entity IDs

The integration domain is `cap_alerts`. It names the integration in config
entries, device identifiers and event types. It is never the start of an
entity ID. Alert entities are sensors, so their IDs start with `sensor.`.

An alert entity ID is the device name, the alert's `event` text, then an
8-character hash:

```
sensor.cap_alerts_nws_cap_alert_tornado_warning_1f0c6a62
       └── device ──┘ └──── event slug ───────┘ └─ hash ─┘
```

The device prefix is applied by Home Assistant, so renaming a device changes
it. The hash disambiguates two concurrent alerts with the same event name.
Unique IDs are stable across restarts, so the registry keeps identity even
when the entity ID changes.

**Don't pattern-match on the entity ID.** Users can rename it, and the event
slug follows the alert language. Discover alert entities by device, or by the
`incident_platform_version` attribute, which the diagnostic sensors don't
carry.

## Events

Three event types fire on the Home Assistant bus:

| Event | When |
|---|---|
| `incident_created` | A new alert ID appears. |
| `incident_updated` | An alert's phase or another tracked field changed. |
| `incident_removed` | An alert reached a terminal phase, `cancel` or `expired`. |

`incident_removed` carries the terminal `phase`, so an automation can tell an
upstream cancel from a natural expiry. When the provider says why, it also
carries `removal_reason` (`superseded` or `ended`). An automation with a
message budget can skip `superseded`, since the replacement fires its own
`incident_created`.

The same three events show in Activity, which otherwise lists nothing of the
integration's but the last-updated sensor: logbook never keeps an entity's
first state or its removal, and treats the count and alert sensors as
continuous. Each event row names the alert and links to its entity, and the
last-updated change on the same cycle names the event as its cause, or the
refresh button press that asked for it. See
[`docs/events.md`](docs/events.md#activity-and-the-logbook-issue-310).

An alert that merely disappears from the feed is not removed. Feeds have been
measured dropping live alerts, so the alert is retained until its published
expiry. An alert with no expiry, and no other way for its source to end it,
ends on absence instead. That covers every GDACS alert and a few WMO
authorities.

Full payload schema and semantics: [`docs/events.md`](docs/events.md).

### Triggers

The automation editor lists six triggers of the integration's own, each
reading one of the events above for the alerts of a chosen target. The
target picker offers alert entities only, and a location's device covers
alerts that don't exist yet. One field, the severity, sets the lowest rung
that fires; it defaults to `severe`.

| Trigger | Fires on | With the severity at `severe` |
| :-- | :-- | :-- |
| Alert issued (`cap_alerts.incident_arrived`) | `incident_created` | a new alert at severe or extreme |
| Alert updated (`cap_alerts.incident_updated`) | `incident_updated`, any field | any revision of an alert at severe or extreme; `trigger.changed_fields` says what moved |
| Alert escalated (`cap_alerts.incident_escalated`) | `incident_updated` with the severity changed | an alert that rose to severe or extreme from below, so severe to extreme stays quiet |
| Alert downgraded (`cap_alerts.incident_downgraded`) | `incident_updated` with the severity changed | an alert that fell below severe from severe or extreme, so extreme to severe stays quiet |
| Alert ended (`cap_alerts.incident_ended`) | `incident_removed` | an alert at severe or extreme ending |
| Alert starts (`cap_alerts.incident_started`) | the alert's `onset`, offset before or after | an alert at severe or extreme coming into effect |

```yaml
triggers:
  - trigger: cap_alerts.incident_arrived
    target:
      device_id: <the location's device>
    options:
      threshold: severe
```

`any` sits under `minor`, so an authority that publishes no severity, which
normalizes to `unknown`, can still fire. For escalated and downgraded, `any`
means every rise or every fall. The event payload is on `trigger`, so
`trigger.entity_id`, `trigger.severity` and, on a severity change,
`trigger.previous_severity` are there for the action. Downgrades are about one
severity change in five on the dev box: fire tiers stepping down, a cyclone
bulletin easing, a flood advisory's final revision.

The last one is timed, not event-driven. Four in ten ECCC alerts and most
NWS ones arrive before they are in effect, typically half a day early, so
Alert issued is the planner's moment and Alert starts is the kiosk's. It
keeps a timer per targeted alert on its `onset`, with an `offset` and
`offset_type` (`before` / `after`) like the calendar triggers, re-arms when a
revision moves the time, and fires once per value. An onset already past when
the alert is first seen does not fire. Australian state and GDACS alerts
publish no onset, so on those it never fires. There is no "about to expire"
trigger on purpose: CAP `expires` is the message's validity, not the hazard's
end, and ECCC and WMO extend it on nearly every revision. An alert Home Assistant already knew at startup fires
nothing on re-validation, and an escalation that happened while it was down
is missed (#281).

### Keeping history

Once an alert ends, its entity leaves the entity registry, so the History
dashboard shows past alerts by entity ID only. Recorder rows survive. To keep
a readable archive, forward `incident_removed` to a store of your choice.
[`blueprints/cap_alerts_archive_incident_removed.yaml`](blueprints/cap_alerts_archive_incident_removed.yaml)
is a reference blueprint.

## Cards and automations

One entity per alert with flat attributes means generic cards work without an
adapter: `auto-entities`, `flex-table-card`, markdown and template cards.
[`docs/frontend_hints.md`](docs/frontend_hints.md) has copy-paste snippets for
finding alert entities, reading severity, and fetching geometry.

The companion
[Weather Alerts Card](https://github.com/seevee/weather_alerts_card) renders
severity, progress bars, expandable details and an affected-area map.

[HA Alert Card](https://github.com/DTekNO/ha-alert-card) is a compact list
alternative. Its `device:` source follows the alert entities as they come and
go, and from 2026.10.3 it reads `web`, `severity_normalized` and `sent` with no
mapping:

```yaml
type: custom:ha-alert-card
sources:
  - device: <device id>
```

## Diagnostics

Settings → Devices & Services → CAP Alerts → ⋮ on the entry → **Download
diagnostics**. Attach it to a bug report instead of a debug log. It reports:

- provider, scope mode, and the upstream endpoints the next fetch will use
- when the last update succeeded, when one last failed, and with what error
- alert counts plus a per-alert lifecycle row, capped at 25 rows
- active filters and the resolved language
- which per-source convention row is in effect

GPS coordinates, the tracker entity, and the MeteoAlarm country source are
redacted everywhere they appear, including inside provider URLs and in the
last failure message. Alert text and geometry are omitted. The file is safe to
paste into a public issue.

## Removal

1. Settings → Devices & Services → **CAP Alerts** → ⋮ → Delete, once per
   entry. This removes the device, its diagnostic entities, and any active
   alert entities.
2. Uninstall via HACS, or delete `custom_components/cap_alerts/`.
3. Restart Home Assistant.

The integration writes nothing to `.storage/`, so nothing is left behind.
Recorder history for past alert entities ages out under your `recorder` purge
policy.

## Provider notes

- **ECCC** fetches each alert's CAP XML body, not just the Atom envelope, so
  alerts carry the full headline, description and instruction. Revision
  chains (NEW → UPDATE → CANCEL) collapse to the current leaf.
- **WMO** covers national services with no dedicated provider. EU and US users
  are better served by MeteoAlarm and NWS. Already-expired RSS items are
  skipped before fetch, so high-volume feeds fit the poll timeout.
- **GDACS** has no working per-event CAP endpoint, so the RSS item is the
  record and geometry comes from the episode's GeoJSON. Both GDACS indexes
  are polled and unioned, since neither contains the other. No GDACS alert
  carries an `expires`. Withdrawal from the feed ends it, so retention scales
  with significance: days for a major earthquake, a year for a drought.
  Identity is keyed on event type and event ID, so an episode re-issue
  updates the entity rather than minting a new one.
- **BBK** reads the same CAP the NINA app does, serialized as JSON, with the
  polygons from a separate per-warning GeoJSON. The district scope takes the
  Amtlicher Regionalschlüssel and widens it to the district, which is the
  granularity warnung.bund.de answers at. Weather warnings on the DWD channel
  are the same ones MeteoAlarm Germany relays (levels 3 and up only), so a
  German user running both gets those twice. MoWaS messages carry no expiry
  and end when the feed withdraws them. Each alert names its channel in the
  `bbk_channel` parameter.
- **AU** reads one EDXL-wrapped CAP-AU document per state. Severity is the
  Australian Warning System tier (`AlertLevel`: Advice → `moderate`, Watch and
  Act → `severe`, Emergency Warning → `extreme`), not the near-uniform CAP
  severity the feeds publish; WA writes the tier in the headline and the
  provider fills the parameter in. No alert carries an expiry: every feed's
  `expires` is a regeneration TTL, so an incident ends when its feed withdraws
  it. Each alert's location marker is published in `points`, alongside the
  fire-ground polygon where there is one.
- **GeoSphere** asks the Warn API for the configured point and gets back its
  municipality, the municipality's outline and every warning on it, with the
  impacts, recommendations and forecaster's synopsis that the MeteoAlarm relay
  compresses. Coordinates arrive in Austria Lambert (EPSG:31287) and are
  reprojected to WGS84 in-tree, to within a metre. Identity is GeoSphere's
  `warnid`, so a revision updates the entity, and a tracker crossing into the
  next municipality under the same warning keeps it. Don't run it next to a
  MeteoAlarm Austria entry: both carry the same warnings under unrelated
  identifiers, so every warning shows twice. A tracker outside Austria makes
  the entry unavailable rather than reporting all clear.
- **TWC** reads the U.S. Tsunami Warning Centers' per-product slots on
  tsunami.gov, which hold each product's latest bulletin. A region entry
  follows two products, the warning product (Warning, Advisory, Watch,
  ending in a Cancellation) and the information statement product.
  Statements are issued for large earthquakes that pose no threat to the
  region and come in at `minor`, so a trigger set to Moderate or above skips
  them. Identity is the product plus the event, so a Watch upgraded to an
  Advisory updates one entity. Warnings for U.S. zones also reach the NWS
  provider, so an entry here next to an NWS entry for the same coast shows
  each warning twice. A slot holds one bulletin, so if a second event takes
  the slot before the first one ends, the first stays `stale` until its
  `expires`, 1 to 2.5 hours. PTWC's international threat messages are not
  covered yet.

Per-field mappings and the reasoning behind each provider are in
[`docs/architecture.md`](docs/architecture.md).

## Documentation

| Document | Covers |
|---|---|
| [`docs/frontend_hints.md`](docs/frontend_hints.md) | Consumer contract: attributes, discovering entities, fetching geometry |
| [`docs/events.md`](docs/events.md) | Event payload schema and lifecycle semantics |
| [`docs/architecture.md`](docs/architecture.md) | Alert identity, provider mappings, normalization, design rationale |
| [`rfc.md`](rfc.md) | The incident model as a proposed Home Assistant Core integration; this integration is its reference implementation |
| [`docs/rfc-summary.md`](docs/rfc-summary.md) | The RFC on one page: the two claims, the ten requirements, what runs today |
| [`docs/evidence/`](docs/evidence/README.md) | Evidence behind the RFC, one page per finding: the measurement, the excerpt, how to reproduce it |
| [`docs/roadmap.md`](docs/roadmap.md) | Planned work that has no issue yet |
| [`docs/provider-watch.md`](docs/provider-watch.md) | Provider drift monitoring (feed vocabulary, WMO mirror retirement) and the response playbook |
| [`CONTRIBUTING.md`](CONTRIBUTING.md) | Dev setup, test gates, adding a provider, translations |
| [`CHANGELOG.md`](CHANGELOG.md) | Generated release history |

## License

[MIT](LICENSE).
