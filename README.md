# CAP Alerts

A Home Assistant custom integration that creates **one entity per active weather alert**, solving the 16 KB attribute limit that affects single-entity alert integrations.

Alert data is modeled using [CAP (Common Alerting Protocol) 1.2](https://docs.oasis-open.org/emergency/cap/v1.2/CAP-v1.2.html) field names via a `CAPAlert` frozen dataclass. Ships with providers for:

- **NWS** — U.S. National Weather Service (GeoJSON API)
- **ECCC** — Environment and Climate Change Canada (NAAD Atom feed)

Additional providers (BoM, MeteoAlarm, DWD, WMO CAP, …) can be added behind the same `AlertProvider` protocol.

A companion Lovelace card lives at [`weather_alerts_card`](../weather_alerts_card); its `cap.ts` adapter is a thin passthrough because normalization happens here.

---

## Installation

### HACS (custom repository)

1. HACS → Integrations → ⋮ → Custom repositories
2. Add this repo, category "Integration"
3. Install **CAP Alerts**, restart Home Assistant

### Manual

Copy `custom_components/cap_alerts/` into your HA config's `custom_components/` directory and restart.

---

## Configuration

Settings → Devices & Services → **Add Integration** → *CAP Alerts*.

Pick a provider, then a location mode:

| Provider | Modes |
|---|---|
| NWS   | Zone ID (e.g. `ILZ014`, or comma-separated), GPS (`lat,lon`), `device_tracker` entity |
| ECCC  | Province code (`AB`, `BC`, `ON`, …), GPS (`lat,lon`) |

### Options (per entry)

- **Scan interval** — 60–3600 s, default 300
- **Timeout** — 5–120 s, default 30
- **Language** (ECCC only) — `auto` / `en-CA` / `fr-CA`

Polygons are **never** emitted in entity attributes — instead, each alert
carries a `geometry_ref` handle plus a `bbox`. Fetch the full GeoJSON via:

- REST: `GET /api/cap_alerts/geometry/{geometry_ref}` (HA auth required)
- Websocket: `{type: "cap_alerts/geometry", geometry_ref: "<ref>"}`

Both return a GeoJSON `FeatureCollection`. See
[`docs/frontend_hints.md`](docs/frontend_hints.md) for a card-side snippet.

Both **reconfigure** (identity/location/provider) and **options** (behavior) flows are supported.

---

## Entities

Every config entry produces one **device** that groups these entities:

| Entity | Purpose | State |
|---|---|---|
| `sensor.cap_alerts_count` | Diagnostic. Number of active alerts. | integer |
| `sensor.cap_alerts_last_updated` | Diagnostic. Last successful poll. | ISO timestamp |
| `sensor.cap_alert_<event_slug>` | One per active alert; created/removed dynamically each poll. | normalized severity (`minor` \| `moderate` \| `severe` \| `extreme` \| `unknown`) |

Alert entity `extra_state_attributes` is a sparse dict of CAP fields — only populated fields are included. See `model.py::CAPAlert` for the full schema.

### Integration domain vs. entity IDs

This trips up new HA users, so worth stating explicitly:

- **Integration domain** (`cap_alerts`) — identifies the integration itself, used in `hass.data`, config entries, device identifiers, fired event types (`cap_alert_created`, etc.).
- **Entity platform domain** (`sensor`) — every entity this integration produces is a *sensor*, so its `entity_id` starts with `sensor.`, never `cap_alerts.`.

So the integration is `cap_alerts`, but you refer to its entities as `sensor.cap_alert_<slug>`, `sensor.cap_alerts_count`, `sensor.cap_alerts_last_updated` in automations, templates, and the frontend.

Per-alert entity IDs are derived from the alert's `event` text (e.g. `sensor.cap_alert_tornado_warning`). If multiple active alerts share an event name, HA appends `_2`, `_3`, … Unique IDs are stable across restarts (`{entry_id}_{provider}_{alert_id}`), so the registry keeps identity even when the entity_id suffix shifts.

---

## Events

For automation use, the integration fires three event types on the HA bus:

| Event | When |
|---|---|
| `cap_alert_created` | A new alert ID appears. |
| `cap_alert_updated` | An existing alert's lifecycle **phase** changed. |
| `cap_alert_removed` | An alert disappears from the feed. |

Event payload includes `entry_id`, `alert_id`, `event`, and phase fields. Subscribe in automations via the `event` trigger platform.

---

## Architecture

Data flow per poll:

```
Weather API → Provider.async_fetch() → list[CAPAlert]
                ↑ (NWS: GeoJSON, ECCC: Atom XML, future: varies)
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
  config_flow.py    # setup + reconfigure + options flows
  coordinator.py    # orchestrates provider, feeds list[CAPAlert] to entities
  sensor.py         # CountSensor, LastUpdatedSensor, AlertEntity, dynamic lifecycle
  model.py          # CAPAlert dataclass + to_attributes()
  normalize.py      # shared normalization: severity, phase, state truncation
  store.py          # inter-poll diffing, transition detection, HA event firing
  providers/
    __init__.py     # AlertProvider protocol + get_provider() factory
    nws.py          # NWS GeoJSON API — zone / GPS / tracker
    eccc.py         # Environment Canada NAAD Atom feed
```

Deeper reference: [`docs/architecture.md`](docs/architecture.md) (alert identity hashing, field mappings, provider rationale, future providers). Planned work: [`docs/roadmap.md`](docs/roadmap.md).

### Key design decisions

- `CAPAlert` has all fields optional except `id` — tolerates providers with varying completeness.
- `to_attributes()` emits only non-empty fields (sparse attributes).
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
3. Add a config-flow branch in `config_flow.py` (a menu step plus one form per location mode).
4. Add translations under `translations/` and matching keys in `strings.json`.
5. Normalization lives in `normalize.py`; extend severity mapping there rather than in the provider.

---

## License

See repository for license details.
