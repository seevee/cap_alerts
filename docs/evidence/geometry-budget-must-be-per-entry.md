# One GDACS entry held 87% of a global geometry budget and evicted a sibling entry's polygons

| | |
| :-- | :-- |
| Supports | RFC §2.4 |
| Source | GDACS worldwide scope on the development instance, per-episode GeoJSON from `gdacs.org`, issue [#197](https://github.com/seevee/cap_alerts/issues/197) |
| Sample | 7 events at the default Orange floor, one instance, one day. Timing over four runs of the same files |
| Observed | 2026-09-06 |
| Reproduce | `timeout 30 gh issue view 197`. Comment block at `custom_components/cap_alerts/providers/gdacs.py:128-146`. `tests/test_geometry_store.py::test_eviction_never_crosses_an_entry_boundary`, `::test_eviction_logs_once_per_overflow`, `::test_a_polygon_over_the_whole_budget_is_still_stored` |

Bytes as `GeometryStore.put` would store them, copied from #197:

| Event | 2026-09-06 | After [#195](https://github.com/seevee/cap_alerts/pull/195) (footprint preferred to country outline) |
| :-- | --: | --: |
| FL 1104081 | 2,412,815 | 319,539 |
| DR 1018332 | 878,041 | 878,041 |
| FL 1104121 | 611,366 | 4,968 |
| DR 1027449 | 356,272 | 356,272 |
| TC 1001305 | 59,213 | 59,213 |
| DR 1018431 | 26,163 | 26,163 |
| VO 1000148 | 3,094 | 3,094 |
| Total | 4,346,964 | 1,647,290 |

Fetch behavior of the same files, from the `gdacs.py` comment block:

| Measurement | Value | Where |
| :-- | :-- | :-- |
| Seven events, default Orange floor | 4.8 MB | `gdacs.py:130` |
| Largest single file | 2.4 MB | `gdacs.py:131` |
| Same file, two fetches a minute apart | 5 s, then 16 s | `gdacs.py:131-132` |
| Fetches lost at the 10 s content-cache default | 7 of 28 across four runs | `gdacs.py:132` |

```python
# custom_components/cap_alerts/geometry_store.py:32
MAX_BYTES = 5_000_000

# custom_components/cap_alerts/providers/gdacs.py:136
_GEOMETRY_FETCH_TIMEOUT = 25  # seconds

# custom_components/cap_alerts/providers/gdacs.py:146
_GEOMETRY_PHASE_MARGIN = 3  # seconds
```

## Reading

A 5 MB global cap left every other entry on the instance 650 KB to share.
The store evicted in global LRU order and logged nothing. The first sign was
a WMO alert with six good polygons drawing an empty frame off a 404. The
budget is now per entry (`geometry_store.py:9-15`), an entry evicts only its
own refs, and the first eviction logs at warning (`geometry_store.py:140-150`).
The timing rows are why §2.4 calls separately fetched geometry the less
reliable half. A 25 s per-file ceiling and a phase deadline 3 s inside the
poll budget ship a slow polygon's alert without a shape. The entry no longer
fails the cycle. Issues [#196](https://github.com/seevee/cap_alerts/issues/196),
[#201](https://github.com/seevee/cap_alerts/issues/201),
[#202](https://github.com/seevee/cap_alerts/issues/202) and
[#204](https://github.com/seevee/cap_alerts/issues/204) record the
three failures.

## Caveats

- One instance, one day. The 4,346,964-byte total is the issue's measurement
  of stored bytes. The 4.8 MB in `gdacs.py` is the same seven events the same
  day, and the comment does not say how it was measured, so the two are not
  reconciled here.
- Content-hash dedupe across entries was measured and rejected in #197: three
  GPS-scoped GDACS entries overlapped by 0, 0 and 59 KB.
- `MAX_BYTES` is sized against the 1.65 MB post-#195 figure (fits three
  times over, `geometry_store.py:27-31`). The comment also notes parsed dicts
  cost several times their JSON length in RAM, which was not measured.
- No GDACS geometry fixture in `tests/fixtures/` is at the sizes above. The
  tests exercise the eviction rule with synthetic polygons and an injected
  budget.
