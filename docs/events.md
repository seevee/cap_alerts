# HA Bus Events

The integration fires three event types on the Home Assistant event bus.
Event names match the RFC §2.3 `incident_*` contract.

| Event | Fires when |
| :-- | :-- |
| `incident_created` | The store sees an alert ID for the first time. |
| `incident_updated` | An existing alert's allowlisted fields (see `changed_fields` below) differ from the previous poll. |
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
