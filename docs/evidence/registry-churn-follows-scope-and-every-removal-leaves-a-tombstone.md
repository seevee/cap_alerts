# Registry churn follows the entry's scope, and every removal leaves a tombstone

| | |
| :-- | :-- |
| Supports | RFC §2.5, §1.5, §5 |
| Source | the dev instance's recorder database and `.storage/core.entity_registry`, Home Assistant 2026.9.3, `helpers/entity_registry.py` |
| Sample | 23 config entries, each read as one household's install. Recorder window 2026-09-22 02:36 to 2026-10-03 21:27, ten whole days. Registry tombstones back to 2026-05-08 |
| Observed | 2026-10-03 |
| Reproduce | `scripts/registry_churn.py --db <recorder db> --storage <.storage>` on any instance running the integration. The figures here are a one-time capture |

A registry write is one alert entity created or removed. Per-day and per-hour
figures count both. Totals cover the whole window, the per-day columns the ten
whole days.

| Entry, as a user would configure it | Created | Removed | Writes per day, median | Busiest day | Busiest hour | Most registered at once |
| :-- | --: | --: | --: | --: | --: | --: |
| **A point, a zone, a region or a district** | | | | | | |
| NWS, one GPS point | 0 | 0 | 0 | 0 | 0 | 0 |
| NWS, one zone | 0 | 0 | 0 | 0 | 0 | 0 |
| ECCC, one GPS point | 1 | 1 | 0 | 1 | 1 | 1 |
| ECCC, one GPS point, far north | 0 | 0 | 0 | 0 | 0 | 0 |
| MeteoAlarm, one region (FR, Cher) | 2 | 1 | 0 | 1 | 1 | 1 |
| MeteoAlarm, following a phone | 0 | 0 | 0 | 0 | 0 | 0 |
| BBK, one district (Berlin) | 0 | 0 | 0 | 0 | 0 | 0 |
| GDACS, one GPS point (three entries) | 0 | 0 | 0 | 0 | 0 | 0 |
| GDACS, following a phone | 2 | 2 | 0 | 2 | 2 | 1 |
| **A province or a state** | | | | | | |
| ECCC, British Columbia | 16 | 16 | 1 | 10 | 7 | 6 |
| ECCC, Ontario | 29 | 30 | 1.5 | 17 | 8 | 4 |
| AU, Tasmania | 6 | 6 | 0.5 | 3 | 1 | 2 |
| AU, Western Australia | 34 | 23 | 4 | 11 | 10 | 12 |
| AU, Queensland | 599 | 619 | 92 | 153 | 47 | 87 |
| AU, New South Wales | 715 | 763 | 144.5 | 165 | 63 | 93 |
| **One national authority (WMO)** | | | | | | |
| Romania | 2 | 2 | 0 | 2 | 2 | 2 |
| Thailand | 24 | 25 | 3 | 11 | 6 | 5 |
| Philippines | 82 | 90 | 12 | 31 | 18 | 17 |
| Greece | 147 | 156 | 18.5 | 53 | 32 | 49 |
| China, narrowed to one province by geocode prefix | 825 | 786 | 105 | 242 | 194 | 177 |
| **The world** | | | | | | |
| GDACS, global, Orange and above | 21 | 23 | 1 | 17 | 14 | 9 |

All 23 together: 2,505 created and 2,543 removed. That is the test rig, not an
install.

What the registry file holds on the same day:

| | Records | Bytes |
| :-- | --: | --: |
| `core.entity_registry`, whole file | | 7,650,241 |
| Live `cap_alerts` entities, all kinds | 264 | 265,795 |
| `deleted_entities` from `cap_alerts` | 10,031 | 7,330,733 |
| of which marked orphaned, so due for purge | 22 | |

A tombstone averages 729 bytes. Home Assistant purges one only after its config
entry is gone (`helpers/entity_registry.py`):

```python
ORPHANED_ENTITY_KEEP_SECONDS = 3600 * 24 * 30
…
# If the entity does not belong to a config entry, mark it as orphaned
orphaned_timestamp = None if config_entry_id else time.time()
…
for key, deleted_entity in list(self.deleted_entities.items()):
    if (orphaned_timestamp := deleted_entity.orphaned_timestamp) is None:
        continue
```

Tombstones per entry, by the month the entity was removed. This reaches back
past the recorder's ten days:

| Entry | Since | Tombstones | KB | May | Jun | Jul | Aug | Sep | Oct 1 to 3 |
| :-- | :-- | --: | --: | --: | --: | --: | --: | --: | --: |
| NWS, one GPS point | 05-12 | 250 | 178 | 137 | 38 | 50 | 25 | 0 | 0 |
| NWS, one zone | 05-30 | 81 | 58 | 4 | 43 | 13 | 9 | 12 | 0 |
| ECCC, one GPS point | 05-08 | 76 | 55 | 8 | 0 | 51 | 12 | 4 | 1 |
| MeteoAlarm, one region | 07-12 | 59 | 44 | | | 56 | 3 | 0 | 0 |
| ECCC, British Columbia | 05-08 | 547 | 399 | 4 | 3 | 194 | 314 | 26 | 6 |
| ECCC, Ontario | 08-21 | 625 | 451 | | | | 267 | 357 | 1 |
| AU, New South Wales | 09-20 | 1,165 | 827 | | | | | 962 | 203 |
| AU, Queensland | 09-20 | 868 | 606 | | | | | 725 | 143 |
| WMO, Philippines | 05-25 | 1,294 | 938 | 87 | 229 | 383 | 358 | 232 | 5 |
| WMO, China, one province | 08-04 | 3,792 | 2,669 | | | | 2,407 | 1,165 | 220 |

## Reading

Scope decides the traffic. An entry scoped to a point, a zone or a region
wrote the registry at most twice on its busiest day of the window, and the
busiest month any of them shows is 137 removals. The busiest state and
national entries run to about a hundred writes a day with 90 to 180 entities
registered at once, and the quietest stay in single digits. The Australian
feeds have no narrower scope to offer, so that row is what an NSW household
gets.

The cost that does not scale down is the tombstone. §2.5 removes the live
registry entry with the incident, and core keeps a deleted record anyway and
never purges it while the config entry lives. On this instance those records
are 96% of the registry file, and every registry save rewrites the file whole.
The NSW entry laid down 827 KB of them in two weeks. Both entity bindings in
§1.5 pay this and the static pool does not. An integration has no supported way
to drop its own tombstones, so the fix is a purge rule in core.

## Caveats

- The entries are scopes a developer picked for testing, not a sample of
  users. They are read one at a time for that reason.
- The window is late September to early October. It is quiet for the NWS and
  ECCC point entries, and the tombstone months are the only view of a busier
  season.
- Tombstones miscount in both directions. One is consumed when its unique id
  returns, and a pre-release build that changes an identity key re-mints
  entities. The Queensland September figure includes one such change
  ([#233](https://github.com/seevee/cap_alerts/pull/233)).
- The instance restarted 17 times in the window and was down from 09-28 16:56
  to 09-29 15:01. The busiest hour for China, British Columbia, Cher and
  Romania is the hour it came back.
- Saves are not counted. Home Assistant debounces registry saves by 10 s, so
  the writes of one reconciliation share one file write.
- Tombstone bytes are each record re-serialized, not its span in the file.
