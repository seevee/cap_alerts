# CAP Alerts

A Home Assistant custom integration that creates **one entity per active weather alert**, solving the 16 KB attribute limit that affects single-entity alert integrations.

Alert data is modeled using [CAP (Common Alerting Protocol) 1.2](https://docs.oasis-open.org/emergency/cap/v1.2/CAP-v1.2.html) field names via a `CAPAlert` frozen dataclass. Ships with providers for:

- **NWS** — U.S. National Weather Service (GeoJSON API)
- **ECCC** — Environment and Climate Change Canada (NAAD real-time streaming feed, GeoRSS as backfill)
- **MeteoAlarm** — EUMETNET European aggregator (per-country CAP JSON, ~37 member services)
- **WMO** — World Meteorological Organization Severe Weather Information Centre (per-source RSS → CAP XML), covering ~100 national services without a dedicated provider
- **GDACS** — Global Disaster Alert and Coordination System (two worldwide RSS indexes, unioned): earthquakes, volcanoes and tsunamis alongside cyclones, floods, droughts and wildfires. The one non-weather source, and the reason the model is a CAP model rather than a weather model.

Additional providers (BoM, DWD, …) can be added behind the same `AlertProvider` protocol.

A companion Lovelace card lives at [`weather_alerts_card`](https://github.com/seevee/weather_alerts_card); its `cap.ts` adapter is a thin passthrough because normalization happens here.

---

## Installation

### HACS (custom repository)

1. HACS → Integrations → ⋮ → Custom repositories
2. Add this repo, category "Integration"
3. Install **CAP Alerts**, restart Home Assistant

### Manual

Copy `custom_components/cap_alerts/` into your HA config's `custom_components/` directory and restart.

### Removal

1. Settings → Devices & Services → **CAP Alerts** → ⋮ → Delete, once per
   configured entry. This removes the entry's device, its diagnostic entities,
   and any alert entities currently active.
2. Uninstall via HACS, or delete `custom_components/cap_alerts/` for a manual
   install.
3. Restart Home Assistant.

The integration writes nothing to `.storage/` of its own, so deleting the
entries and the directory leaves nothing behind. Recorder history for past
alert entities survives deletion and ages out under your usual `recorder`
purge policy.

---

## Configuration

Settings → Devices & Services → **Add Integration** → *CAP Alerts*.

Pick a provider, then a location mode:

| Provider | Modes |
|---|---|
| NWS         | Zone ID (e.g. `ILZ014`, or comma-separated), GPS (`lat,lon`), `device_tracker` entity |
| ECCC        | Province code (`AB`, `BC`, `ON`, …), GPS (`lat,lon`), `device_tracker` entity |
| MeteoAlarm  | Country (ISO 3166-1 alpha-2, e.g. `DE`), with optional GPS polygon filter or region multi-select (`EMMA_ID` for most countries, `NUTS3`/`NUTS2` for some, area names where a feed publishes no geocodes) |
| WMO         | Source ID picked from the live SWIC registry (e.g. `mx-smn-es`; custom IDs accepted), country-wide or with optional GPS polygon filter |
| GDACS       | Worldwide (every event in the index), GPS (`lat,lon`) or `device_tracker` entity, keeping only events whose affected area contains the point |

### Options (per entry)

- **Scan interval** — 60–3600 s, default 300
- **Timeout** — 5–120 s, default 30
- **Language** — ECCC: `auto` / `en-CA` / `fr-CA`. MeteoAlarm: 2-letter prefix (`en`, `de`, `fr`, …) used to pick the primary `<cap:info>` block. WMO: `auto` or any BCP 47 tag the source publishes (e.g. `zh-Hans`), matched against each `<info>` block's language. NWS has no language option (English-only).
- **Real-time streaming** (ECCC) — ingest alerts the moment they are issued, over the NAAD TCP streaming feed; the GeoRSS feed becomes a startup/reconnect backfill plus periodic resync. Default on. Turning it off falls back to GeoRSS polling on the scan interval. A diagnostic binary sensor reports the socket state (see Entities). Turning it off also raises a repair (Settings → Repairs): the legacy GeoRSS host retires in late September 2026 and the surviving one omits some live alerts, which a polling entry reads as ended. Submit turns streaming back on; ignore the repair if polling is deliberate. Alerts issued while the socket is down are recovered from the NAAD 48-hour repository: every heartbeat lists the last ten alerts published, and any the entry has not seen is fetched by reference (issue #164), so a reconnect gap no longer depends on the GeoRSS index carrying the alert.
- **Feed source** (ECCC) — which NAAD GeoRSS host serves polling/backfill: `auto` (default; fetches both hosts and unions their entries, since neither alone carries every live alert), or pin `alertready` / `pelmorex` as an escape hatch. Pinning `pelmorex` raises a repair, since that host retires in late September 2026; its Submit sets the source back to `auto`.
- **Event types** (GDACS) — which hazards to track (Earthquake, Tropical Cyclone, Flood, Volcano, Drought, Wildfire, Tsunami). Every type by default. Applied to the RSS indexes before any geometry is fetched, so narrowing it also cuts the fetch cost.
- **Minimum alert level** (GDACS) — `Green` (everything), `Orange` (default) or `Red`. This is GDACS's own impact scale, and with no per-event CAP body to read a `<severity>` from it is also what the entity state derives from (Green → minor, Orange → severe, Red → extreme). Lower it to `Green` if you want the full worldwide tail of minor wildfires and earthquakes.
- **Exclude marine alerts** (NWS, ECCC) — opt-in filter that drops alerts carrying a marine zone code (NWS marine UGC area prefixes, ECCC CLC codes starting `00`). Default off.
- **Area codes (prefix match)** (all providers except GDACS, which publishes no area codes) — opt-in narrowing on top of the location filter chosen at setup. Comma-separated prefixes (e.g. `13,37`); an alert is kept when any area code it publishes starts with one of them. Codes are hierarchical, so a shorter prefix covers a wider area (`13` = Hebei, `1307` = Zhangjiakou). Mainly for sources with no per-alert geometry and no region picker — notably WMO's `cn-cma-xx`, where it cuts a country-wide entry from ~234 alerts to ~28 for Hebei. Code lengths vary within a scheme, so prefer the leading digits over pasting a full code. Empty by default.

Polygons are **never** emitted in entity attributes — instead, each alert
carries a `geometry_ref` handle plus a `bbox`. Fetch the full GeoJSON via:

- REST: `GET /api/cap_alerts/geometry/{geometry_ref}` (HA auth required)
- Websocket: `{type: "cap_alerts/geometry", geometry_ref: "<ref>"}`

Both return a GeoJSON `FeatureCollection`. See
[`docs/frontend_hints.md`](docs/frontend_hints.md) for a card-side snippet.

Both **reconfigure** (identity/location/provider) and **options** (behavior) flows are supported.

---

## Entities

Every config entry produces one **device** (named `CAP Alerts <PROVIDER>`, e.g. `CAP Alerts ECCC`) that groups these entities:

| Entity | Purpose | State |
|---|---|---|
| `sensor.cap_alerts_<provider>_alert_count` | Diagnostic. Number of alerts. Attributes `active` and `upcoming` break that total down by whether the alert's `onset` has passed (no `onset` counts as active). | integer |
| `sensor.cap_alerts_<provider>_last_updated` | Diagnostic. Last successful poll. | ISO timestamp |
| `sensor.cap_alerts_<provider>_cap_alert_<event_slug>_<hash>` | One per active alert; created/removed dynamically each poll. | normalized severity (`minor` \| `moderate` \| `severe` \| `extreme` \| `unknown`) |
| `button.cap_alerts_<provider>_refresh` | Diagnostic. Fetches from the provider now, without waiting for the next poll. | — |
| `binary_sensor.cap_alerts_eccc_real_time_stream` | Diagnostic. Whether the NAAD real-time socket is connected. ECCC with streaming on only. | `on` (connected) \| `off` |

The device name is intentionally stable across reconfigures so entity_ids don't drift when you change GPS, zone, or region. The per-entry friendly label (with location detail) remains visible in the integrations list as the entry title; users running multiple entries of the same provider can set `name_by_user` on the device for a personalized label.

Alert entity `extra_state_attributes` is a sparse dict of CAP fields — only populated fields are included. See `model.py::CAPAlert` for the full schema.

### Integration domain vs. entity IDs

This trips up new HA users, so worth stating explicitly:

- **Integration domain** (`cap_alerts`) — identifies the integration itself, used in `hass.data`, config entries, device identifiers, fired event types (`incident_created`, etc.).
- **Entity platform domain** (`sensor`) — every entity this integration produces is a *sensor*, so its `entity_id` starts with `sensor.`, never `cap_alerts.`.

So the integration is `cap_alerts`, but you refer to its entities as `sensor.cap_alerts_<provider>_cap_alert_<event_slug>_<hash>`, `sensor.cap_alerts_<provider>_alert_count`, `sensor.cap_alerts_<provider>_last_updated` in automations, templates, and the frontend.

Per-alert entity IDs are the device name, then the alert's `event` text, then an 8-character hash:

```
sensor.cap_alerts_nws_cap_alert_tornado_warning_1f0c6a62
       └── device ──┘ └──── event slug ───────┘ └─ hash ─┘
```

The device prefix comes from Home Assistant, not from us. These entities set `has_entity_name`, and HA prefixes the device name onto the object ID an integration suggests. So renaming the device changes the shape: a device renamed to `CAP Alerts METEOALARM Cher` yields `sensor.cap_alerts_meteoalarm_cher_cap_alert_…`.

The hash is `sha1(unique_id)[:8]` and disambiguates two concurrent alerts sharing an event name, so HA's `_2` / `_3` numeric fallback is not normally reached. Unique IDs are stable across restarts (`{entry_id}_{provider}_{alert_id}`), so the registry keeps identity even when the entity_id changes.

**Don't pattern-match on the entity ID** — users can rename it, and the event slug follows the alert language. Discover alert entities by device, or by testing for the `incident_platform_version` attribute, which the diagnostic sensors don't carry.

---

## Diagnostics

Each config entry supports Home Assistant's diagnostics download: **Settings →
Devices & Services → CAP Alerts → ⋮ on the entry → Download diagnostics**.

The download is the fastest way to get a bug report answered — attach it to the
issue instead of a debug log. It reports what the entry is configured for and
what the last update actually did:

- provider, scope mode, and the upstream endpoints the next fetch will use
  (for ECCC, the feed source and both union hosts; the NAAD stream endpoint
  when streaming is on)
- when the last update succeeded, when one last failed, and with what error —
  the failure is kept after a recovery, since that is usually what is being
  asked about
- alert counts (active / upcoming) plus a per-alert lifecycle row: entity_id,
  phase, timestamps, sender, area geocodes. Sparse, and capped at 25 rows —
  the counts above it stay exact whatever the cap drops
- active filters: marine exclusion, area-code prefixes, configured *and*
  resolved language
- which per-source convention row is in effect, and which senders landed on it

**What it leaves out.** GPS coordinates, the tracker entity, and the MeteoAlarm
country-source entity are redacted everywhere they appear, including inside a
provider URL built from them, so the file is safe to paste into a public issue.
Credential keys are redacted too, though no provider needs one today. Alert
body text and geometry are omitted — they are large, and neither helps.

---

## Events

For automation use, the integration fires three event types on the HA bus:

| Event | When |
|---|---|
| `incident_created` | A new alert ID appears. |
| `incident_updated` | An existing alert's lifecycle **phase** or other tracked fields changed. |
| `incident_removed` | An alert moved to a terminal phase (`cancel` / `expired`) or disappeared from the feed. |

Full payload schema and semantics are documented in [`docs/events.md`](docs/events.md).
`incident_removed` payloads carry the terminal `phase` (`cancel` or `expired`)
so automations can distinguish an upstream cancel from a natural expiry
without re-deriving it from timestamps. When the provider says *why* the alert
went away, they also carry `removal_reason` (`superseded` or `ended`) — an
automation with a message budget can skip a `superseded` removal, since the
alert replacing it fires its own `incident_created`.

### History UI tradeoff

Once an alert ends, its entity is removed from the entity registry. This
means Home Assistant's **History** dashboard renders past alerts with only
a slugified `entity_id` rather than a friendly name. Recorder rows are
preserved at the database level, but the UI has no friendly-name context
to paint. Wire up an automation that listens for `incident_removed` and
forwards the payload to your archival store of choice (InfluxDB,
Postgres, a notify service) — see
[`blueprints/cap_alerts_archive_incident_removed.yaml`](blueprints/cap_alerts_archive_incident_removed.yaml)
for a reference blueprint.

---

## Architecture

Data flow per poll:

```
Weather API → Provider.async_fetch() → list[CAPAlert]
                ↑ (NWS: GeoJSON, ECCC: Atom→CAP XML, MeteoAlarm: JSON,
                   WMO: RSS→CAP XML, GDACS: RSS + GeoJSON)
  Coordinator._async_update_data()
    normalize_alerts() → sets severity_normalized, phase
    store.process()    → diffs vs previous, sets phase_changed, fires HA events
    ├─ CountSensor (state = len)
    └─ coordinator listener → diffs alert IDs vs tracked entities
         → async_add_entities / registry remove
           └─ AlertEntity (finds own CAPAlert by ID in coordinator.data)
```

### Files

```
custom_components/cap_alerts/
  __init__.py       # entry setup, coordinator wiring, platform forwarding
  const.py          # domain, defaults, user-agent format
  config_flow.py    # setup + reconfigure + options flows (handler + dispatch)
  flows/            # per-provider flow steps, mixed into the handler
  coordinator.py    # orchestrates provider, feeds list[CAPAlert] to entities
  diagnostics.py    # support dump: scope, endpoints, update health, filters
  sensor.py         # CountSensor, LastUpdatedSensor, AlertEntity, dynamic lifecycle
  model.py          # CAPAlert dataclass + to_attributes()
  normalize.py      # shared normalization: severity, phase, Buddhist-Era year fix, state truncation
  payload.py        # attribute-payload budget: keeps a state under the recorder's ceiling
  store.py          # inter-poll diffing, transition detection, HA event firing
  providers/
    __init__.py             # AlertProvider protocol + get_provider() factory
    cap.py                  # shared, provider-neutral CAP 1.2 XML parsing (used by eccc + wmo)
    cap_content_cache.py    # LRU cache for immutable CAP XML bodies
    nws.py                  # NWS GeoJSON API — zone / GPS / tracker
    eccc.py                 # Environment Canada NAAD Atom feed
    meteoalarm.py           # EUMETNET per-country CAP JSON + region listing
    wmo.py                  # WMO SWIC per-source RSS → CAP XML + source registry
    gdacs.py                # GDACS global RSS indexes → CAPAlert + episode GeoJSON
```

Deeper reference: [`docs/architecture.md`](docs/architecture.md) (alert identity hashing, field mappings, provider rationale, future providers). Planned work: [`docs/roadmap.md`](docs/roadmap.md).

### Provider notes

- **ECCC** fetches the linked CAP XML body (not just the Atom envelope), so alerts carry full `headline`/`description`/`instruction`, accurate timestamps, and one card per alert series — revision chains (NEW → UPDATE → CANCEL) collapse to the current leaf via CAP `<references>`.
- **WMO** reuses the same RSS-index → per-item CAP two-step (and the shared CAP parser), populates its source dropdown from the live SWIC registry, and pre-filters already-expired RSS items so high-volume feeds don't blow the poll timeout. EU/US users are better served by the dedicated MeteoAlarm/NWS providers.

- **GDACS** is the one provider that fetches no CAP document, because GDACS has no per-event CAP endpoint that works: `cap.aspx` ignores its `eventid` parameter and returns the newest event of the requested type, and the per-event path the feed advertises exists only for cyclones. The RSS item is the record instead, with per-event geometry pulled from the GeoJSON its episode names. Three things follow:
  - **Both indexes are polled and unioned.** `rss.xml` lists current events for as long as they run (a drought for over a year), but gates earthquakes on magnitude; `rss_24h.xml` carries everything recent regardless of significance. Neither contains the other.
  - **Alert identity is `sha256("{eventtype}:{eventid}")`.** GDACS writes its own identifier as `GDACS_<type>_<eventid>_<episodeid>` and bumps the episode on every re-issue, so keying on that would mint a new entity per update.
  - **No alert ever carries an `expires`.** `todate` is the last observation time, not an expiry — it is in the past for every live event — so withdrawal from the feed is what ends an alert. Retention therefore scales with significance: roughly four days for a major earthquake, a day for a small one, a year for a drought.

See [`docs/architecture.md`](docs/architecture.md) for the full ECCC and WMO sections — CAP body fetch, concurrency and caching, the expiry pre-filter, and per-field mappings.

### Key design decisions

- `CAPAlert` has all fields optional except `id` — tolerates providers with varying completeness.
- `to_attributes()` emits only non-empty fields (sparse attributes).
- The attribute payload is bounded, not the individual fields: an alert is serialized, measured against what the recorder measures, and trimmed in priority order only if it doesn't fit (alternate-language text first, primary text last, then redundant keys).
- Dynamic entity lifecycle via `_sync_alert_entities()` in `sensor.py`: add on new ID, remove from entity registry on disappearance.
- Severity, zones, and phase are normalized at the integration level, not in the card.
- `entry.runtime_data` (typed `CAPAlertsConfigEntry`) is used instead of the legacy `hass.data[DOMAIN]` dict.
- `async_config_entry_first_refresh()` gates setup so startup surfaces connection errors properly.
- No `CONF_NAME` — entry title is derived programmatically from provider + location.

---

## Development

This is a standard Home Assistant custom integration. It lives entirely under `custom_components/cap_alerts/` and follows [HA custom component conventions](https://developers.home-assistant.io/docs/creating_integration_manifest).

```bash
pytest                             # run all tests
pytest tests/test_coordinator.py   # single file
pytest -k test_parse_alerts        # pattern

mypy custom_components/cap_alerts/
ruff check custom_components/cap_alerts/
ruff format custom_components/cap_alerts/
```

### Workflow

- `main` is protected; all changes go through PRs.
- Branches: `feat/<slug>`, `fix/<slug>`, `chore/<slug>`.
- Commits: `type(scope): description` (`feat`, `fix`, `docs`, `refactor`, `test`, `chore`).
- Dependency order when modifying code: **model → providers → coordinator → sensor → config_flow → `__init__`**.

### Adding a provider

1. Implement the `AlertProvider` protocol in `providers/<name>.py` — an `async_fetch()` returning `list[CAPAlert]`.
2. Register it in `providers/__init__.py::get_provider()`.
3. Add a flow module in `flows/<name>.py` (a menu step plus one form per location mode), and mix it into the handler in `config_flow.py`.
4. Add translations under `translations/` and matching keys in `strings.json`.
5. Normalization lives in `normalize.py`; extend severity mapping there rather than in the provider.

---

## License

See repository for license details.
