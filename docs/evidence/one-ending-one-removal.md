# An ending fires one incident_removed however long the feed republishes it

| | |
| :-- | :-- |
| Supports | RFC §2.3, §7.3 |
| Source | ECCC NAAD (polling and streaming), `AlertStore` replays, issues [#145](https://github.com/seevee/cap_alerts/issues/145) and [#185](https://github.com/seevee/cap_alerts/issues/185) |
| Sample | one ended document replayed five times (#145). Ten archived documents of one CAM chain replayed in `sent` order (#185). NAAD host-gap probe, 1340 samples at 15 min from 2026-07-30 |
| Observed | 2026-08-13 (#145), 2026-09-02 (#185) |
| Reproduce | `tests/test_store_tombstones.py`, `tests/test_store_reissued_endings.py` |

Before the fix, one live poll then five polls of the same `ended` document
(#145, against 0.4.0-alpha.5):

```
EVENTS:  ['incident_created', 'incident_removed' x 5]
REASONS: [None, 'ended', 'ended', 'ended', 'ended', 'ended']
```

| Path | Duplicate rate before the fix | Bounded by |
| :-- | :-- | :-- |
| Polling | 1 per scan interval | feed drops the document |
| ECCC streaming | ~1 per 60 s heartbeat | 48 h `_live_docs` window |

Same ending, new revision 24 s later (#185, London CAM chain, 2026-09-02):

| `sent` (UTC) | identifier | bilingual key of the ended group | fired before the fix |
| :-- | :-- | :-- | :-- |
| 18:16:29 | `urn:oid:2.49.0.1.124.2538483898.2026` | `995cac20cf96` | `incident_removed` |
| 18:16:53 | `urn:oid:2.49.0.1.124.0371172464.2026` | `90f0bef5056f` | `incident_removed` |

The second revision's `<references>` named the first. Every CAM update on that
chain was published twice, English-first then bilingual, 30 to 90 s apart.

What fires now, per case:

| Case | Fires | Pinned by |
| :-- | :-- | :-- |
| Same id, same ended record republished | one `incident_removed`, then nothing | `test_republished_ended_document_fires_one_removal` |
| First sighting already terminal, republished | one `incident_removed` | `test_terminal_on_first_sight_fires_one_removal` |
| Same ending, new revision whose `<references>` name the announced one | nothing | `test_reissued_ended_group_fires_one_removal` |
| Different group of the same chain ends later | its own `incident_removed` | `test_a_different_group_of_the_same_chain_fires_its_own_removal` |
| Id comes back live after an ending | `incident_created`, and a later ending fires again | `test_an_id_that_comes_back_live_is_created_again`, `test_group_revived_live_then_ended_again_fires_both_events` |
| Terminal record returns after more than 48 idle hours | one more `incident_removed` | `test_a_tombstone_ages_out_after_the_idle_ttl` |
| Suppressed duplicate arrives | the memory is refreshed, not spent | `test_suppressing_a_duplicate_refreshes_the_tombstone` |

The memory's TTL, `store.py`:

```python
# The NAAD host-gap probe has sampled
# ``rss.alertready.ca`` every 15 minutes since 2026-07-30; across 1340 samples
# individual records went missing and came back after as much as ~21 h, ended
# alerts among them, with the persistence to rule out propagation lag. 48 h
# clears the measured worst case with headroom.
…
TOMBSTONE_IDLE_TTL = timedelta(hours=48)
```

## Reading

ECCC keeps an ended record in the feed for up to 48 h, and streaming rebuilds
the active set on every heartbeat. A store with no memory of what it ended
reads each rebuild as a fresh terminal sighting, about sixty removals an hour.
The store now remembers announced ids and the CAP identifiers behind them, so
a re-issued ending is silent while a live return still creates. RFC §2.3
states the contract as one ending, one removal, and §7.3 skips step 1 for an
announced id.

## Caveats

- The #145 counts are a local replay, not the reporter's live event log. The
  reporter's volume fit the heartbeat cadence but was not counted.
- The #185 duplicate was shown by replaying ten archived documents. It is a
  streaming-only shape, since polling collapses both revisions in one scan.
- The 48 h TTL is sized to one probe's worst gap (~21 h). A longer gap fires
  one more removal, so consumers stay idempotent.
