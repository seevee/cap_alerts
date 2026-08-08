# AGENTS.md

This file provides guidance to AI agents working with code in this repository.

## Project Overview

A Home Assistant custom integration (`cap_alerts`) that creates **one entity per active weather alert**, solving the 16KB attribute limit in `nws_alerts`. Alert data is modeled using CAP (Common Alerting Protocol) field names via a `CAPAlert` frozen dataclass. Ships with NWS and ECCC (Environment Canada) providers; designed for future providers (BoM, MeteoAlarm, DWD, WMO CAP, etc.).

Companion frontend: [weather_alerts_card](../weather_alerts_card) — the card's `cap.ts` adapter is a thin passthrough since this integration handles all normalization.

## Architecture

See `docs/architecture.md` for design rationale (alert identity, field mappings, provider layer) and `docs/roadmap.md` for planned work. `plans/` is gitignored scratch for `/plan` output — not reference material.

### Entity Model

- **Device**: groups all entities for a configured location
- **Count sensor** (`sensor.cap_alerts_<provider>_alert_count`): `state` = number of
  active alerts, attributes `active`/`upcoming` split it on `onset`,
  `EntityCategory.DIAGNOSTIC`
- **Last updated sensor** (`sensor.cap_alerts_<provider>_last_updated`): `state` = ISO timestamp, `EntityCategory.DIAGNOSTIC`
- **Alert entities** (`sensor.cap_alert_<slug>`): one per active alert, dynamically created/removed each poll cycle
- **Refresh button** (`button.cap_alerts_<provider>_refresh`): forces an off-cycle fetch, `EntityCategory.DIAGNOSTIC`
- **Stream connectivity** (`binary_sensor.cap_alerts_eccc_real_time_stream`): NAAD socket state, `EntityCategory.DIAGNOSTIC`, ECCC-with-streaming only

### Data Flow

```
Weather API → Provider.async_fetch() → list[CAPAlert]
                ↑ (NWS: GeoJSON, ECCC: Atom XML, future: varies)
  Coordinator._async_update_data() calls provider
    normalize_alerts() → sets severity_normalized, phase
    store.process() → diffs vs previous, sets phase_changed, fires HA events
    ├─ CountSensor (state = len)
    └─ coordinator listener → diffs alert IDs vs tracked entities
         → async_add_entities / registry remove
           └─ AlertEntity (finds own CAPAlert by ID in coordinator.data)
```

### File Structure

```
custom_components/cap_alerts/
  __init__.py       # entry setup, coordinator wiring, platform forwarding; owns the shared GeometryStore and registers the REST view + WS command once per HA instance
  const.py          # domain, defaults, user-agent format
  config_flow.py    # setup flow + reconfigure flow + options flow
  coordinator.py    # orchestrates provider, feeds list[CAPAlert] to entities; owns device_info + NAAD stream lifecycle; provider-neutral post-fetch filters (marine, geocode-prefix); writes/purges geometry refs
  sensor.py         # CountSensor, LastUpdatedSensor, AlertEntity, dynamic lifecycle
  button.py         # RefreshButton: on-demand provider fetch (all providers)
  binary_sensor.py  # StreamConnectivitySensor: NAAD socket state (ECCC streaming only)
  model.py          # CAPAlert dataclass + to_attributes()
  conventions.py    # per-source convention table: marine prefixes, terminal lifecycle tokens, severity derivations, per-sender dialects (identity/keep hooks + explode/merge pipeline stages); an episode dialect declares its own run rule — MeteoFrance merges consecutive forecast days, FMI contiguous windows — over one shared pipeline
  normalize.py      # shared normalization: severity, phase, Buddhist-Era year fix, state truncation
  store.py          # alert store: inter-poll diffing, transition detection, HA event firing (incl. removal_reason)
  icons.py          # event-type → mdi dispatch; MeteoAlarm classifies on awareness_type, others on event tables
  geometry_store.py # in-memory LRU cache of full GeoJSON polygons, keyed by geometry_ref (RFC §2.4); never persisted
  views.py          # GET /api/cap_alerts/geometry/{ref} → FeatureCollection
  websocket.py      # cap_alerts/geometry WS command, same payload as the REST view
  providers/
    __init__.py           # AlertProvider protocol + get_provider() factory
    cap.py                # shared, provider-neutral CAP 1.2 XML parsing (CAPDoc/CAPInfoDoc, parse_cap_alert, resolve_chain_leaves)
    cap_content_cache.py  # LRU cache for fetched CAP XML bodies (shared: eccc + wmo)
    geometry.py           # shared CAP shapes → GeoJSON; polygon/point selection, zero-radius circles
    gps.py                # shared GPS-mode helpers: lat,lon parsing, ray-cast point-in-polygon, rings off a CAPAlert geometry
    nws.py                # NWS GeoJSON API — zone/GPS/tracker
    eccc.py               # Environment Canada NAAD Atom feed (GeoRSS host union + CAP bodies)
    naad_stream.py        # NAAD TLS streaming transport: frame reassembly, heartbeats, watchdog, reconnect/backoff — no alert semantics
    meteoalarm.py         # MeteoAlarm (EUMETNET) per-country CAP JSON
    wmo.py                # WMO SWIC per-source RSS → CAP XML; per-language <info> selection
  manifest.json
  translations/
```

### Key Design Decisions

- `CAPAlert` dataclass has all fields optional except `id` — accommodates providers with varying completeness
- `to_attributes()` serializes only non-empty fields (sparse attributes)
- Dynamic entity lifecycle: alert entities are created/removed per coordinator update via `_sync_alert_entities()` callback
- Reconfigure flow for identity/location, options flow for behavior (polling interval, timeout, language, area-code narrowing)
- No `CONF_NAME` — entry title derived programmatically from config data
- `entry.runtime_data` (typed as `CAPAlertsConfigEntry`) instead of `hass.data[DOMAIN]` dict
- `async_config_entry_first_refresh()` for proper startup error handling
- Normalization happens at the integration level (severity, zones, phase), not in the card

## Build & Test Commands

Test/lint/typecheck deps are pinned in `requirements_test.txt` and used by
`.github/workflows/ci.yml`. Mirror CI locally with a venv:

```bash
# One-time setup (system Python on Arch is PEP 668-locked)
python3 -m venv .venv
.venv/bin/pip install -r requirements_test.txt

# Tests (same invocation CI runs)
.venv/bin/python -m pytest tests -q
.venv/bin/python -m pytest tests/test_store_payload.py   # single file
.venv/bin/python -m pytest -k normalize                  # pattern

# Lint + format (CI checks custom_components/, tests/, and scripts/)
.venv/bin/ruff check custom_components/ tests/ scripts/
.venv/bin/ruff format --diff custom_components/ tests/ scripts/

# Type checking (the integration only — scripts/ is standalone dev tooling)
.venv/bin/mypy custom_components/cap_alerts
```

The changelog is generated by `git-cliff` (pinned in `requirements_test.txt`);
regenerate with `git cliff --config cliff.toml --output CHANGELOG.md`. See
`CONTRIBUTING.md`.

## Development Environment

This is a Home Assistant custom integration. It lives in `custom_components/cap_alerts/` and follows [HA custom component conventions](https://developers.home-assistant.io/docs/creating_integration_manifest).

## Agent Rules

### Before editing
- Read every file before referencing or modifying it
- Read `AGENTS.md` and `docs/architecture.md` for project context
- Do not invent architecture that doesn't exist in the repository

### Modifying code
- Only modify files identified as in-scope for the task
- Never introduce unrelated refactors or fix pre-existing issues outside changed files
- Do not change public interfaces without user confirmation
- Follow dependency order: model → providers → coordinator → sensor → config_flow → __init__

### Verification
- Run tests before presenting results; fix any new failures introduced

### Git discipline
- Never auto-commit, push, or open PRs — defer to the user or `/commit`
- Commit format: `type(scope): description` (types: feat, fix, docs, refactor, test, chore)
- Branch format: `feat/<slug>`, `fix/<slug>`, `chore/<slug>`

### Prose line wrapping

Wrap by surface, not by habit:

- **Commit messages** — hard-wrap the body at ~72–78 columns. `git log` is a
  fixed-width surface, and `cliff.toml` only interpolates the *subject* into
  `CHANGELOG.md`, so body length is otherwise free.
- **Markdown in the repo** (`AGENTS.md`, `docs/`, `README.md`) — hard-wrap at
  ~80. These are read in editors and reviewed as diffs, where wrapping keeps
  changes line-granular.
- **GitHub issue bodies, PR bodies, and comments** — do *not* hard-wrap. One
  line per paragraph. Markdown reflows the rendered output either way, so
  wrapping buys nothing there; meanwhile tables and long URLs can't be wrapped
  (leaving one document in two styles), and editing a wrapped body in GitHub's
  soft-wrapping web editor re-wraps it raggedly.

### Skills Reference
Slash-command skills (`/explore`, `/plan`, `/implement`, `/fix`, `/review`,
`/commit`, …) come from the developer's own environment, not this repo —
`.claude/` is gitignored and intentionally empty of commands. `/plan` output
goes to `plans/<slug>.md` (gitignored scratch).

## Workflow

- `main` is protected: all changes go through PRs
- Feature branches: `feat/<name>`, `fix/<name>`, `chore/<name>`, etc.
