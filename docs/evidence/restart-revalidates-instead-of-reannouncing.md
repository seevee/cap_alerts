# A restart re-validates known alerts instead of re-announcing them

| | |
| :-- | :-- |
| Supports | RFC §2.5, §2.3 |
| Source | `AlertStore` boot path, issues [#249](https://github.com/seevee/cap_alerts/issues/249), [#250](https://github.com/seevee/cap_alerts/issues/250), [#252](https://github.com/seevee/cap_alerts/issues/252), [#257](https://github.com/seevee/cap_alerts/issues/257) |
| Sample | code reading plus local replays against `AlertStore`, confirmed on main `ba3f02c` (#257) |
| Observed | 2026-10-01 (#249, #250, #252), 2026-10-02 (#257) |
| Reproduce | `tests/test_store_restart.py` |

The table from `docs/events.md`, "A restart re-validates, it does not
re-announce":

| Id at boot | Upstream | Fires |
| :-- | :-- | :-- |
| Not in registry | Live | `incident_created` |
| In registry | Live | Nothing. The entity picks up the fresh content |
| In registry | Still absent on the second reconciliation | `incident_removed` |
| In registry | Terminal | `incident_removed` |
| Not in registry | Terminal on the first fetch | Nothing |

Before, from the issue bodies:

> Every alert that was live before the restart fires `incident_created`
> again. HA restarts on every core update, so a notification automation
> re-announces every live alert each time. (#250)

> An alert that ended while HA was down never fires `incident_removed`.
> (#250)

> A restart during an index gap therefore drops the entity a minute later and
> fires `incident_removed` with `cancel` for an alert still in force. (#252)

The #257 replay, `AlertStore` with the `test_store_restart.py` fixtures:

```python
before = AlertStore(hass, "entry1", "nws")
before.process([alert_factory(id="a", phase="new")])
before.process([alert_factory(id="a", phase="cancel")])
before.process([alert_factory(id="a", phase="cancel")])
after = AlertStore(hass, "entry1", "nws")   # restart, "a" no longer in registry
after.process([alert_factory(id="a", phase="cancel")])
# fired: created a, removed a, removed a
```

Each row of the table, pinned:

| Row | Test |
| :-- | :-- |
| Not in registry, live | `test_alert_issued_during_downtime_is_created` |
| In registry, live | `test_known_live_alert_is_revalidated_silently`, `test_known_alert_back_within_grace_is_not_news` |
| In registry, absent twice | `test_alert_ended_during_downtime_is_removed_after_grace` |
| In registry, terminal | `test_known_alert_terminal_at_boot_is_removed_once` |
| Not in registry, terminal on first fetch | `test_announced_ending_is_not_re_announced_after_restart`, `test_boot_suppression_tombstones_the_lineage` |
| Stream rebuilds do not count as fetches | `test_stream_rebuilds_do_not_spend_the_grace`, `test_stream_rebuilds_before_the_first_fetch_stay_in_boot` |
| After the boot fetch, terminal is news again | `test_terminal_after_the_boot_fetch_is_announced` |

## Reading

The store is in memory and the entity registry is not. Seeding the known set
from the registry at construction turns the first reconciliation into a
re-validation, which RFC §2.5 describes and §2.3 relies on. The grace counts
fetch-backed reconciliations only, because a stream rebuild a minute after
boot cannot recover what a lossy GeoRSS seed missed. A record already
terminal on that first fetch is old news and fires nothing.

## Caveats

- The before behavior is documented from code reading and local replays, not
  from a captured live event log. The rc carrying the fixes was handed to the
  ECCC streaming reporter for a restart soak on 2026-10-03.
- The in-registry removal carries only what the registry kept: the id and the
  registered name. `area_desc` and `description` are missing there.
- Tombstones do not survive a restart. A record that went live and ended
  entirely while HA was down fires nothing, by choice (#257).
