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

**NWS (non-VTEC alerts)** — Some alerts (Special Weather Statements, certain advisories) lack VTEC. Fall back to `sha256(url)[:12]`; these alerts are short-lived and rarely updated, so message-level identity is acceptable.

**ECCC** — Stable key is `sha256(event + areaDesc + issued_date)[:12]`. An event type for a given area on a given day maps to one logical event. `issued_date` (date portion of `<updated>`) ensures a morning thunderstorm warning and an evening reissuance produce distinct IDs, preventing cross-day collisions. Within a day, updates to the same event preserve the same hash.

### Why not just hash the URL?

Message-level identity, not event-level. NWS alerts go through `NEW → CON → CAN` with a different URI at each stage — hashing the URL would churn entities through every phase change. VTEC exists precisely to express event identity across messages.

### Scope of the problem

The `/alerts/active` endpoint only returns current alerts. The failure mode is: entity `_abc123` disappears and entity `_def456` appears, and HA sees them as unrelated. State history for the warning splits across two entity IDs. Lifecycle-aware hashing prevents this.

---

## Entity Identity & Registry Discipline

Implements RFC §2.2.1 (stable entity_id derivation) and §2.5 (registry cleanup).

### entity_id shape

```
sensor.cap_alert_<slug(event)>_<8-hex>
```

where `<8-hex>` is `sha1(unique_id)[:8]`. The hash disambiguates alerts that share an event name (e.g. two concurrent "Severe Thunderstorm Warning" entries from different offices) without relying on Home Assistant's `_2`/`_3` numeric-suffix fallback, which can outlive its source and break history when the originally-suffixed entity is removed.

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
- **ECCC / other CAP-native providers** — CAP `<severity>` is trustworthy; lowercase it.
- **Future non-CAP providers** (DWD level codes, BoM title inference) — register a new branch in `_normalize_severity` keyed on `provider`.

### Lifecycle filtering is centralized

Providers used to filter `msgType=Cancel` themselves. That's a semantic decision, not a fetch decision — it belongs in the normalization layer. The pipeline is:

```
fetch → normalize (sets phase) → filter_active_alerts (drops Cancel/expired) → store.process
```

Different providers express cancellation differently (NWS: VTEC `CAN` + `msgType=Cancel`; ECCC: `msgType=Cancel` category; future WMO: implicit by absence). Normalization maps these to `phase="Cancel"`, and filtering happens once — not N times in each provider.

### State truncation

The `event` field becomes the entity's `native_value` (state), which HA caps at 255 characters. `normalize.py` truncates with an ellipsis. Relevant for international CAP providers that sometimes put full descriptions in `<event>`.

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
| `properties.geocode.UGC` / `SAME` | `geocode_ugc` / `geocode_same` |
| `properties.eventCode.NationalWeatherService[0]` | `event_code_nws` |
| `properties.eventCode.SAME[0]` | `event_code_same` |
| `properties.parameters.VTEC` | `vtec` → parsed → `vtec_{office,phenomena,significance,action,tracking}` |
| `properties.sender` / `senderName` | `sender` / `sender_name` |
| `properties.references` / `replacedBy` / `replacedAt` | same-named fields |
| `properties.parameters` | `parameters` (full dict) |

---

## ECCC — NAAD Atom mapping

**API**: `https://rss.naad-adna.pelmorex.com/` Atom feed (national, client-side filtered).

**Feed shape**: each `<entry>` carries `<category term="key=value"/>` pairs for most CAP fields, plus `<georss:polygon>` for geometry and `<summary>` with `Area: …` text. Bilingual — entries appear twice (`en-CA` and `fr-CA`). The coordinator resolves the preferred language before calling the provider; the provider picks the preferred-language entry as primary and stores the alternate-language content in `headline_alt` / `description_alt` / `instruction_alt` / `language_alt`.

**Location matching**:
- Province mode — match `areaDesc` / geocode prefix.
- GPS mode — point-in-polygon against `<georss:polygon>` using a pure-Python ray-caster (no `shapely`; not in HA core).

**XML parsing**: `defusedxml.ElementTree` — already an HA core dependency.

**Field mapping**:

| NAAD Atom field | CAPAlert field |
|---|---|
| `<id>` (entry) | `url` |
| `<category term="event=…">` | `event` |
| `<category term="msgType=…">` | `msg_type` |
| `<category term="status=…">` | `status` |
| `<category term="severity=…">` | `severity` |
| `<category term="urgency=…">` | `urgency` |
| `<category term="certainty=…">` | `certainty` |
| `<updated>` (entry) | `sent` |
| `<category term="expires=…">` (if present) | `expires` |
| `<summary>` Area text | `area_desc` |
| `<georss:polygon>` | `geometry` (converted to GeoJSON Polygon) |
| `<link>` | `web` |
| `sha256(event + areaDesc + issued_date)[:12]` | `id` |

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

Split into two concerns, both wired in `config_flow.py`:

- **Reconfigure flow** — identity (provider, zone / GPS / tracker / province). Triggers full reload via `async_update_reload_and_abort`. Shows the same top-level provider menu as initial setup, so NWS ↔ ECCC switch works without remove/re-add.
- **Options flow** — behavior (scan interval, timeout, language). Applied live via an update listener: updates `coordinator.update_interval` and timeout in place and calls `async_request_refresh()`. No reload, no coordinator teardown.

Entry title is derived programmatically from config data (`_compute_device_title`) — no `CONF_NAME` field. Shared by initial setup and reconfigure so the device name stays in sync.

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

### MeteoAlarm — EUMETNET, Europe

- **API**: Per-country Atom feeds at `https://feeds.meteoalarm.org/feeds/meteoalarm-legacy-atom-{country}`.
- ~35 countries, one feed each. CAP extensions (`<cap:severity>`, `<cap:urgency>`, …).
- `awareness_level` (e.g. `"2; yellow; Moderate"`) more granular than CAP severity.
- No zone codes — `<cap:areaDesc>` is free text per country.
- Config flow: country selector → province selector (populated from feed).

### DWD — Deutscher Wetterdienst, Germany

- **API**: `https://www.dwd.de/DWD/warnungen/warnapp/json/warnings.json` — JSONP (strip `warnWetter.loadWarnings(…);` wrapper).
- Warnings keyed by warncell ID.
- `level` 0–4 maps to severity: 4=Extreme, 3=Severe, 2=Moderate, 1=Minor, 0=None. Color hex as fallback.
- No CAP urgency/certainty; event names are in German.
- Config flow: warncell ID or region name.

### WMO CAP — Severe Weather Information Centre

- **API**: Per-source RSS feeds at `https://severeweather.wmo.int/v2/cap-alerts/{source-id}/rss.xml`.
- Generic CAP format. Covers countries without dedicated providers (Mexico, Brazil, …).
- Two-step fetch: RSS list → individual CAP XML documents for full details and polygon geometry.
- Source IDs follow `{country}-{agency}-{lang}` (e.g. `ca-msc-xx`, `mx-smn-es`).
- Config flow: source selector → GPS or area filter.

---

## RFC Schema Alignment (platform v1.0)

The integration implements the `IncidentEntity` contract from `rfc.md` §2.2, §2.2.2, §2.4, §2.6, §2.7.

### Phase vocabulary

`phase` attribute values are **lowercase**: `new`, `update`, `cancel`, `expired`. `expired` is computed in `normalize.py` by comparing the `expires` timestamp against the current time; cancelled and expired alerts are dropped by `filter_active_alerts`. Automations that string-matched the previous title-case (`"New"` / `"Update"` / `"Cancel"`) must be updated.

### Icon policy

Every alert entity exposes `icon: mdi:…` derived from the event type. The taxonomy lives in `icons.py` — NWS entries match full event names; ECCC entries match substrings. Unknown events fall back to `mdi:alert`. Severity still drives entity state; the icon indicates hazard.

### Platform version

`PLATFORM_VERSION = "1.0"` is exposed on every alert entity as the `incident_platform_version` attribute. Card consumers can branch on this when the contract evolves.

### bbox

When alert geometry is present, every alert entity exposes a 4-element `bbox: [min_lon, min_lat, max_lon, max_lat]` attribute (derived from Point / LineString / Polygon / MultiPolygon).

### Geometry externalization (§2.4)

Full GeoJSON polygons are **not** entity attributes. The coordinator writes them
to `.storage/cap_alerts_geometry` (an LRU-bounded `Store`, soft cap 5 MB, keyed
by `geometry_ref = "{provider}:{alert_id}"`) and entities expose only the opaque
`geometry_ref` handle. Consumers fetch polygons out-of-band:

- REST: `GET /api/cap_alerts/geometry/{geometry_ref}` → `FeatureCollection`
- Websocket: `{type: "cap_alerts/geometry", geometry_ref}` → `FeatureCollection`

Both require HA auth. The coordinator purges refs for expired/cancelled alerts
in the same cycle that drops the entity — storage reflects live state. The old
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
