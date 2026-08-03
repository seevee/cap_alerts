# HA Bus Events

The integration fires three event types on the Home Assistant event bus.
Event names match the RFC §2.3 `incident_*` contract.

| Event | Fires when |
| :-- | :-- |
| `incident_created` | The store sees an alert ID for the first time. |
| `incident_updated` | An existing alert's allowlisted fields (see `changed_fields` below) differ from the previous poll, **or** a new alert's `<references>` points to a previous-poll alert (cross-poll supersession — the new alert is considered an update even though its `id` is new). |
| `incident_removed` | An alert moves to a terminal phase (`cancel` / `expired`) or disappears from the feed between polls. |

## Payload schema

| Key | Type | RFC | Notes |
| :-- | :-- | :-- | :-- |
| `incident_id` | `str` | §2.3 | Stable lifecycle-aware alert id. |
| `event` | `str` | §2.3 | Human-readable event name (e.g. `"Severe Thunderstorm Warning"`). |
| `severity` | `str` | §2.3 | Normalized: `extreme` / `severe` / `moderate` / `minor` / `unknown`. |
| `phase` | `str` | §2.3 | Current phase: `new` / `update` / `cancel` / `expired`. On `incident_removed`, carries the terminal phase. |
| `phase_changed` | `bool` | §2.3 | `True` on first sighting, or when `phase` differs from the previous poll. |
| `changed_fields` | `list[str]` | §2.3 | Allowlisted fields that changed since the previous poll: `headline`, `description`, `instruction`, `severity_normalized`, `phase`, `expires`, `area_desc`. Empty on creation and on silent-disappearance removal. |
| `entity_id` | `str` | §2.3 | Omitted on the very first sighting (the entity has not yet been registered). |
| `entry_id` | `str` | extension | Config entry id. Useful when a Home Assistant install has multiple CAP Alerts entries (e.g. two NWS zones). Not in the RFC. |
| `area_desc` | `str` | extension | Human-readable area description, denormalized onto the event for convenience. Not in the RFC. |

`previous_phase` is **not** on the event payload. Consumers can reconstruct
it when `phase` appears in `changed_fields`: the previous phase was whatever
`phase` is now minus the transition.

### MeteoFrance episodes

The payload schema is unchanged, but the *cadence* differs for MeteoFrance
alerts, whose forecast days are merged into one episode entity (see
*architecture.md → MeteoAlarm → Identity*). An episode gaining a day surfaces as
`incident_updated` with `expires` in `changed_fields` — where the previous
behaviour was an `incident_created` for a whole new entity each day. An episode
losing its earliest finished day keeps its id and usually also fires
`incident_updated`: the dominant-day tie-break prefers the earliest day, so
when that day finishes the content flips to the next day's
`headline`/`description` (and `severity_normalized` falls if the finished day
was the more severe one). The roll-off passes without an event only when the
surviving day already supplied the content — `onset` moves, but it is not an
allowlisted field. Once the *whole* episode has finished, the provider drops
it, so it reaches the store as a silent disappearance and fires
`incident_removed` with the inferred terminal phase `expired`.

## Terminal-phase semantics on `incident_removed`

The removal event always carries a terminal `phase`:

- `cancel` — the provider explicitly cancelled (e.g. NWS VTEC `CAN`,
  ECCC `msgType=Cancel`), **or** the alert disappeared from the feed
  before its `expires` timestamp. In the second case the integration
  infers cancel because "the provider dropped it" is functionally the
  same as an explicit cancel for automation purposes.
- `expired` — the alert's `expires` timestamp is in the past. Reached
  either by a live alert aging past its end time, or by silent
  disappearance after the timestamp passed.

This is a departure from earlier builds, which emitted the
*previous* phase (typically `new` or `update`) on removal. Automations
that keyed off `phase` on removal to distinguish cancel from expired
now get that information directly on the payload.

## Cross-poll supersession (ECCC)

For ECCC, an `incident_removed` event is **not** fired when a disappearing alert's CAP `<identifier>` appears in any incoming alert's `references` list. In that case, `incident_updated` is fired for the incoming alert (carrying `previous_phase` from the superseded alert), and `incident_removed` is suppressed. This correctly models a revision chain (NEW → UPDATE) across poll boundaries without spurious removal events.

The CAP `<identifier>` used for this lookup is distinct from `incident_id` (the bilingual lifecycle hash). When a revision shifts the bilingual key inputs (e.g. the alert polygon expands), the UPDATE gets a new `incident_id`; the `<references>` link is the signal that connects it back to the previous poll's alert.

## ECCC — CAP-body fields now populated

Every CAP-1.2 field is provider-supplied for ECCC alerts: `identifier`, `sender`, `sender_name`, `sent`, `effective`, `onset`, `expires`, `headline`, `description`, `instruction`, `references`, `category`, `scope`. The `event` field uses the title-case form from the CAP body (e.g. `"Freezing Drizzle Advisory"` instead of the lowercase Atom category term). ECCC CAP-CP `<eventCode>` blocks flow through `parameters` under their `valueName` key (e.g. `parameters["profile:CAP-CP:Event:0.4"] == "freezing-drizzle"`); `event_code_same` and `event_code_nws` remain empty for ECCC.

## `unique_id` vs. RFC §2.2

RFC §2.2 specifies `unique_id` as the provider's stable lifecycle hash
(raw VTEC, raw composite). The integration uses:

```
unique_id = f"{entry_id}_{provider}_{alert_id}"
```

This is an **intentional deviation**. Home Assistant requires
`unique_id` values to be globally unique across all config entries for
a given platform. Two config entries against the same provider —
say, two overlapping NWS zone groups in the same install — would
produce identical raw lifecycle hashes for alerts that cover both
zones. Prefixing with `entry_id` keeps them distinct.

The lifecycle hash itself (what the RFC calls `unique_id`) is exposed
as `incident_id` on both entity attributes and event payloads, so
consumers cross-referencing the RFC can use that field directly.

## Archival pattern (RFC §6.4)

Because registry-removed entities lose their friendly names in the
History dashboard, long-term retention of past alerts is a separate
concern from the live-data model. A reference blueprint ships at
[`blueprints/cap_alerts_archive_incident_removed.yaml`](../blueprints/cap_alerts_archive_incident_removed.yaml)
— listen for `incident_removed`, forward the payload to any notify
service.
