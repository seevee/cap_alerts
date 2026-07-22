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

**ECCC** — Stable key is `sha256(sender + sent + primary_CAP-CP_eventCode + polygon_hash)[:12]`. Fields are language-independent: `<sender>` is the issuing office, `<sent>` is the second-precision issue timestamp, the CAP-CP `<eventCode>` value (e.g. `freezing-drizzle`) is a language-independent profile code, and `<area>/<polygon>` is geometric. Urgency is deliberately excluded — urgency shifts between revisions (`Likely` → `Observed`) and would produce different IDs on each update. Two language siblings of the same revision share identical values for all four inputs and therefore hash to the same ID, enabling bilingual merge. Revision chains (NEW → UPDATE → CANCEL) are resolved to the leaf via CAP `<references>` before the key is computed, so only the current revision's key is active.

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
- **ECCC and other CAP-native providers** — CAP `<severity>` is trustworthy; lowercase it. Values outside the canonical set clamp to `unknown`.
- **MeteoAlarm** — when the `awareness_level` parameter (`"N; color; Label"`) is present, the color token drives the canonical severity (`yellow` → moderate, `orange` → severe, `red` → extreme, `green` → unknown). EUMETNET members publish color consistently while CAP `<severity>` is sometimes blank or off-axis, so color is the authoritative signal. Falls back to lower-cased CAP `<severity>` when the parameter is missing or malformed.
- **Future non-CAP providers** (DWD level codes, BoM title inference) — register a new branch in `_normalize_severity` keyed on `provider`.

### Lifecycle filtering is centralized

Providers used to filter `msgType=Cancel` themselves. That's a semantic decision, not a fetch decision — it belongs in the normalization layer. The pipeline is:

```
fetch → normalize (sets phase) → filter_active_alerts (drops Cancel/expired) → store.process
```

Different providers express cancellation differently (NWS: VTEC `CAN` + `msgType=Cancel`; ECCC and WMO: `msgType=Cancel` plus revision-chain resolution via CAP `<references>`; MeteoAlarm: status/absence from the country feed). Normalization maps these to `phase="Cancel"`, and filtering happens once — not N times in each provider.

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

**API**: `https://rss.alertready.ca/` Atom feed (national, client-side filtered). Migrated April 2026 from `rss.naad-adna.pelmorex.com` per the NAAD System Governance Council — an intentional domain rebrand off the Pelmorex name, both feeds maintained concurrently for ~6 months (legacy host sunsets ~late Sept 2026). Since 0.2.0 the GeoRSS feed is the **backfill** source: ECCC ingests in real time from the NAAD streaming feed by default (see *NAAD streaming* below), and the GeoRSS poll seeds the set on startup/reconnect + a periodic safety resync.

**Feed shape**: each Atom `<entry>` links to a per-entry CAP XML document via `<atom:link rel="alternate">`. The link may carry `type="application/cap+xml"` (legacy) or no `type` at all (alertready.ca), so `_pick_cap_link` resolves it by MIME type when present and falls back to the `.cap`/`.xml` href extension otherwise. The CAP file is the authoritative source for all `CAPAlert` fields. The Atom envelope is used for: (1) pre-fetch filtering — the `status` category and the `<georss:polygon>` are evaluated before the CAP file is fetched, avoiding bandwidth waste (GPS mode point-in-polygon; province mode a coarse polygon-bbox vs province-bbox test); (2) the Atom `<id>` element supplies `CAPAlert.url`. All CAP-1.2 fields (`identifier`, `sent`, `effective`, `onset`, `expires`, `headline`, `description`, `instruction`, `references`, etc.) come from the CAP body. Note: the alertready.ca envelope no longer carries the `geocode`/`areaDesc` categories the legacy feed had, so province cannot be *confirmed* pre-fetch — the envelope bbox is a coarse gate only, authoritatively confirmed from the CAP body afterwards — see Location matching.

Bilingual — entries appear twice (`en-CA` and `fr-CA`). The coordinator resolves the preferred language before calling the provider; the provider merges language siblings using a language-independent bilingual key and stores the alternate-language content in `headline_alt` / `description_alt` / `instruction_alt` / `language_alt`.

**Lifecycle**: CAP `<references>` is parsed to build a revision chain. Within a poll, `resolve_chain_leaves` (in the shared `cap.py` module) drops superseded revisions (alerts whose `<identifier>` appears in another alert's `<references>` list). Only the leaf revision (the current UPDATE) is exposed. Across polls, the `AlertStore` detects cross-poll supersession when an incoming alert's `<references>` contains the CAP `<identifier>` of a disappearing previous-poll alert — it fires `incident_updated` instead of `incident_removed`.

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
| `<info>/<area>/<geocode>` SAME values | `geocode_same` |
| `<info>/<area>/<geocode>` `layer:EC-MSC-SMC:1.0:CLC` values | `geocode_clc` |

Note: `event_code_same` and `event_code_nws` remain empty for ECCC. CAP-CP profile codes (e.g. `profile:CAP-CP:Event:0.4 → freezing-drizzle`) flow through `parameters` under their `valueName` keys. `geocode_clc` carries the Canadian Location Code (province-numbered for land, `00…` for marine/water zones); other ECCC area geocode schemes (`profile:CAP-CP:Location:0.3`) are not surfaced.

## ECCC — NAAD streaming

**Default since 0.2.0.** The NAADS 2.0 LMD User Guide documents the TCP streaming feed — not the auxiliary GeoRSS feed — as the correct channel for 24/7 automated systems, and it removes the ~7 MB-per-poll GeoRSS transfer. ECCC streams by default; the `CONF_STREAMING` option (ECCC options flow, default on) is an operational escape hatch back to GeoRSS polling if the endpoint degrades. Toggling it reloads the entry.

**Endpoint**: `streaming.alertready.ca:8443` (TLS 1.3, no client cert, no subscribe handshake). The deprecated pelmorex `streaming1/2:8080` plain-TCP hosts are gated to registered LMDs and sunset ~Sept 2026 — not targeted.

**Client** (`providers/naad_stream.py`, `NAADStreamClient`): a standalone TLS client owning only the transport. The wire is a continuous byte stream of concatenated CAP-CP documents (XML declaration + `<alert>…</alert>`); the client reassembles complete `<alert>…</alert>` frames (bounded buffer guards a missing close tag), classifies heartbeats (`<sender>` starts `NAADS-Heartbeat`, `<status>System</status>`, emitted ≥ every 60 s), and hands raw alert-doc strings to the coordinator — it parses no alert semantics. A read that returns no bytes within the heartbeat timeout doubles as the liveness watchdog and forces a reconnect; reconnects use bounded exponential backoff with jitter. The TLS connection is created through an injectable `connect` callable so it is unit-testable with a scripted reader. On each *reconnect* (not the first connect) it requests a GeoRSS backfill to recover alerts issued while disconnected.

**Coordinator integration**: the coordinator stays the single source of truth for `data`, entity sync, store diffing, and availability. It holds a live `dict[identifier → CAPDoc]` (`_live_docs`) guarded by an `_ingest_lock`, and every ingest rebuilds the `CAPAlert` list from that set through the shared `build_alerts_from_cap_docs` (extracted from `eccc.py`) → `_apply` (normalize → marine filter → geometry → `AlertStore.process`) → `async_set_updated_data`, so stream and poll converge on one pipeline (identical revision-chain resolution, bilingual merge, transition events). Three mutators — a streamed alert doc, a heartbeat (docs `[]`; the rebuild ages out expired alerts with no network I/O), and a backfill — are serialized by the lock. Docs older than 48 h are pruned from the live set (the NAAD feeds carry a rolling 48 h window). The GeoRSS `async_fetch_docs` is the backfill source, so the `_fetch_feed_root` truncation guard still protects it.

**Availability (issue #16)**: only a *backfill* drives `last_update_success`. The periodic `_async_update_data` backfill raising `UpdateFailed` flips entities `unavailable`; a transient socket disconnect, or a stream-triggered reconnect backfill that fails, does **not** — the last-known active set is retained while the client reconnects, avoiding availability flapping.

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

**Location matching** (mutually exclusive, picked in the config flow):
- **Country-wide** — return every `Actual` warning for the country.
- **GPS polygon** — parses each warning's `area.polygon` (CAP whitespace-
  separated `lat,lon` pairs) into a GeoJSON ring and keeps warnings whose
  ring contains the configured point. Fails loud with `UpdateFailed` when
  the page has warnings but none carry polygons (the country does not
  publish per-warning geometry); matches the ECCC GPS-mode contract.
- **Region picker** — multi-select of region codes. Feeds carry a mix of
  area-geocode schemes across countries (`EMMA_ID` for most, `NUTS3` for
  FR/BG/RO/MK, `NUTS2` for HU; sub-region cell schemes `WARNCELLID`/`CISORP`
  co-occur with these). A single scheme-priority resolver
  (`METEOALARM_REGION_SCHEMES = ("EMMA_ID", "NUTS3", "NUTS2")`, `areaDesc` as
  last resort) drives **both** picker population and the per-warning filter, so
  the value stored in `CONF_REGIONS` and the value matched at fetch time are
  always the same scheme for a given feed. The config flow populates the picker
  by calling the regions endpoint, falling back to deriving the list from the
  warnings feed itself when the regions endpoint is unavailable (the only path
  for NUTS3 countries like France, which have no regions endpoint). Selected
  `code → label` pairs are persisted as `CONF_REGION_LABELS` so the device
  title can show readable region names (e.g. `MeteoAlarm DE — Bavaria +2`)
  without re-fetching.

**Severity**: when an `info` block carries an `awareness_level` parameter
(format `"N; color; Label"`, e.g. `"3; orange; Severe"`), the color token
is mapped to canonical severity (`yellow` → moderate, `orange` → severe,
`red` → extreme, `green` → unknown). EUMETNET members publish color
reliably while CAP `<severity>` is often blank or inconsistent, so color
is the authoritative signal here. When `awareness_level` is missing or
malformed, falls back to lower-cased CAP `severity` via the standard
non-NWS branch. The full `awareness_level` string is preserved verbatim
in `parameters` for cards that want the numeric tier or label.

**Identity**: dispatched per sender. Every authority **except MeteoFrance**
uses `sha256(cap.identifier)[:12]` (falling back to the warning `uuid` when the
identifier is missing) — there, identifier collisions across a poll are
genuinely-distinct concurrent warnings (e.g. Italy/Austria publish one
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
  `sent`) — the forecast day, stable across a day's re-issues, distinct across
  the J/J+1/J+2/J+3 outlook.

Severity/color (`awareness_level`) is deliberately excluded so an orange→red
escalation updates the existing entity rather than spawning a new one. Existing
MeteoFrance entities recompute once on upgrade (stale ones are safe to delete);
all other authorities are byte-for-byte unchanged.

**Field mapping**:

| MeteoAlarm JSON path | CAPAlert field |
|---|---|
| `warnings[].uuid` | identifier-fallback source for `id` (non-MeteoFrance senders) |
| `warnings[].alert.identifier` | `identifier`; primary source for `id` (non-MeteoFrance senders) |
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
| `alert.info[].area[].geocode[]` (all schemes, keyed by `valueName`) | `geocodes` — scheme-keyed container (`{"EMMA_ID": (...), "NUTS3": (...)}`); drives the region-picker filter. MeteoAlarm leaves `geocode_same` empty (EMMA_ID is not a SAME code), unlike NWS/ECCC/WMO |
| `alert.info[].area[].polygon` | `geometry` (GeoJSON Polygon or MultiPolygon, lon/lat) |
| `sha256(identifier)[:12]` (or `sha256(uuid)[:12]` fallback) | `id` |

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
- **GPS polygon** — parses each alert's CAP `<polygon>` into a GeoJSON ring and
  keeps alerts whose ring contains the configured point. Fails loud with
  `UpdateFailed` when the feed has alerts but none carry polygons (the source
  does not publish per-alert geometry); matches the ECCC/MeteoAlarm GPS-mode
  contract. WMO CAP has no standardized sub-country region code, so there is no
  area-code filter and no GPS-tracker mode.

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
| `<info>/<area>/<geocode>` SAME values | `geocode_same` |
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

Split into two concerns, both wired in `config_flow.py`:

- **Reconfigure flow** — identity (provider, zone / GPS / tracker / province / country / regions). Triggers full reload via `async_update_reload_and_abort`. Shows the same top-level provider menu as initial setup, so NWS / ECCC / MeteoAlarm switches work without remove/re-add.
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

### DWD — Deutscher Wetterdienst, Germany

- **API**: `https://www.dwd.de/DWD/warnungen/warnapp/json/warnings.json` — JSONP (strip `warnWetter.loadWarnings(…);` wrapper).
- Warnings keyed by warncell ID.
- `level` 0–4 maps to severity: 4=Extreme, 3=Severe, 2=Moderate, 1=Minor, 0=None. Color hex as fallback.
- No CAP urgency/certainty; event names are in German.
- Config flow: warncell ID or region name.

---

## RFC Schema Alignment (platform v1.0)

The integration implements the `IncidentEntity` contract from `rfc.md` §2.2, §2.2.2, §2.4, §2.6, §2.7.

### Phase vocabulary

`phase` attribute values are **lowercase**: `new`, `update`, `cancel`, `expired`. `expired` is computed in `normalize.py` by comparing the `expires` timestamp against the current time; cancelled and expired alerts are dropped by `filter_active_alerts`. Automations that string-matched the previous title-case (`"New"` / `"Update"` / `"Cancel"`) must be updated.

### Icon policy

Every alert entity exposes `icon: mdi:…` derived from the event type. The taxonomy lives in `icons.py` — NWS entries match full event names; ECCC and MeteoAlarm entries match substrings against their respective hazard vocabularies. Unknown events fall back to `mdi:alert`. Severity still drives entity state; the icon indicates hazard.

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
