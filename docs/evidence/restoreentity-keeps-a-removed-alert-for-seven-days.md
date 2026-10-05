# RestoreEntity keeps a removed alert for seven days and rewrites the file every fifteen minutes

| | |
| :-- | :-- |
| Supports | RFC §2.5, §1.4 requirement 5, §5 |
| Source | Home Assistant 2026.9.3, `helpers/restore_state.py`. The dev instance's `/api/states`, `.storage/core.entity_registry` and `.storage/core.restore_state` |
| Sample | 23 config entries, each read as one household's install. 110 live alert entities. Removals over the seven days to 2026-10-05 19:24 UTC |
| Observed | 2026-10-05 |
| Reproduce | `tests/test_restore_state_linger.py` pins the core behavior. `scripts/restore_state_size.py --storage <.storage>` sizes it on any instance running the integration. The figures here are a one-time capture |

The alert entities do not inherit `RestoreEntity`, so the table is a
projection: what they would add to `core.restore_state` if they did. A record
is the entity's whole state as compact JSON. Across the 110 live alert entities
a record has a mean of 2,600 bytes, a median of 2,244 and a maximum of 8,645.

| Entry, as a user would configure it | Live alerts | Live, KB | Removed in 7 days | Lingering, KB | File, KB | Written per day, MB |
| :-- | --: | --: | --: | --: | --: | --: |
| **A point, a zone, a region or a district** | | | | | | |
| NWS, one GPS point | 0 | 0 | 0 | 0 | 0 | 0 |
| NWS, one zone | 0 | 0 | 0 | 0 | 0 | 0 |
| ECCC, one GPS point | 0 | 0 | 1 | 2.6 | 2.6 | 0.25 |
| ECCC, one GPS point, far north | 0 | 0 | 0 | 0 | 0 | 0 |
| MeteoAlarm, one region (FR, Cher) | 0 | 0 | 1 | 2.6 | 2.6 | 0.25 |
| MeteoAlarm, following a phone | 0 | 0 | 0 | 0 | 0 | 0 |
| BBK, one district (Berlin) | 0 | 0 | 0 | 0 | 0 | 0 |
| GDACS, one GPS point (three entries) | 0 | 0 | 0 | 0 | 0 | 0 |
| GDACS, following a phone | 1 | 2.3 | 0 | 0 | 2.3 | 0.22 |
| **A province or a state** | | | | | | |
| ECCC, British Columbia | 0 | 0 | 20 | 52.0 | 52.0 | 4.99 |
| ECCC, Ontario | 4 | 21.7 | 11 | 59.7 | 81.4 | 7.81 |
| AU, Tasmania | 1 | 2.8 | 5 | 14.2 | 17.1 | 1.64 |
| AU, Western Australia | 14 | 39.0 | 26 | 72.4 | 111.4 | 10.69 |
| AU, Queensland | 56 | 131.2 | 329 | 770.6 | 901.8 | 86.57 |
| AU, New South Wales | 23 | 50.4 | 431 | 945.1 | 995.6 | 95.57 |
| **One national authority (WMO)** | | | | | | |
| Romania | 0 | 0 | 2 | 5.2 | 5.2 | 0.50 |
| Thailand | 2 | 14.0 | 9 | 62.9 | 76.9 | 7.39 |
| Philippines | 1 | 3.1 | 16 | 49.4 | 52.5 | 5.04 |
| Greece | 0 | 0 | 90 | 234.0 | 234.0 | 22.46 |
| China, narrowed to one province by geocode prefix | 4 | 13.4 | 497 | 1,665.2 | 1,678.6 | 161.15 |
| **The world** | | | | | | |
| GDACS, global, Orange and above | 4 | 8.1 | 5 | 10.1 | 18.1 | 1.74 |

All 23 together: 4,232 KB added to the file and 406 MB written a day. That is
the test rig, not an install. The file on the same instance today is 38,385
bytes in 64 records, none of them an alert entity. Its modification time moved
from 19:04:12 to 19:19:12 UTC with the size unchanged.

Home Assistant writes the file on a timer and keeps what was removed
(`helpers/restore_state.py`):

```python
# How long between periodically saving the current states to disk
STATE_DUMP_INTERVAL = timedelta(minutes=15)

# How long should a saved state be preserved if the entity no longer exists
STATE_EXPIRATION = timedelta(days=7)
…
def async_restore_entity_removed(self, entity_id, state, extra_data) -> None:
    # When an entity is being removed from hass, store its last state. This
    # allows us to support state restoration if the entity is removed, then
    # re-added while hass is still running.
    …
    if state is not None:
        self.last_states[entity_id] = StoredState(
            state, extra_data, dt_util.utcnow()
        )
…
for entity_id, stored_state in self.last_states.items():
    …
    # Don't save old states that have expired
    if stored_state.last_seen < expiration_time:
        continue

    stored_states.append(stored_state)
```

| Behavior | Pinned by |
| :-- | :-- |
| A removed entity's state and attributes are still in the set the next dump writes | `test_a_removed_entity_stays_in_the_dump_with_its_attributes` |
| It leaves that set seven days after removal | `test_a_removed_entity_leaves_the_dump_after_seven_days` |
| The dump interval is fifteen minutes | `test_the_dump_runs_every_fifteen_minutes` |

## Reading

The live alerts are cheap. No entry holds more than 131 KB of them, and an
entry scoped to a point, a zone or a region holds almost nothing. If the dump
carried only those, requirement 5 would be met at every scope measured.

The removed alerts are the cost. §2.5 removes the entity with the incident,
and core then keeps its last state for seven days and writes it out 96 times a
day. On the busiest state and national entries that is 0.9 to 1.7 MB per
write, 85% or more of it alerts that have already ended. The write happens
whether or not anything changed.

An entity cannot opt out. The removal hook is core's own
(`async_internal_will_remove_from_hass`), and the dump has no per-entity
filter. So restart survival on `RestoreEntity` as it stands does not meet
requirement 5 for a feed that churns. Two things would: core skipping the
seven-day hold for an entity whose removal is final, or the integration
persisting its live alerts itself, written on change and dropped on removal.
The first is the same shape as the tombstone purge §2.5 already asks core for
([registry churn](registry-churn-follows-scope-and-every-removal-leaves-a-tombstone.md)).

## Caveats

- This is a projection. No alert entity was a `RestoreEntity` when it was
  measured.
- A removed alert's state is gone, so each is sized at the mean live record of
  its entry, or at the 2,600-byte mean of all entries where the entry had none
  live. The ECCC point, Cher, British Columbia, Romania and Greece rows use the
  second.
- Removals are counted from registry tombstones by `modified_at`. A tombstone
  is consumed when its unique id returns, so an alert removed and re-created
  inside the week is not counted, which is right: a live entity replaces its
  own lingering record.
- A record holds the whole state, including the attributes the recorder is
  told to skip. The payload budget of §7.2 bounds what the recorder stores,
  not this.
- The entries are scopes a developer picked for testing, not a sample of
  users, and the week was quiet for the NWS and ECCC point entries. Their rows
  are a floor.
- Bytes written are not flash wear. The figure is file size times 96, before
  the filesystem or the card's controller have their say.
