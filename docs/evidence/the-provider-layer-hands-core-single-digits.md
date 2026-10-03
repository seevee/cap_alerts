# The ECCC provider reduces a national feed to single-digit entities

| | |
| :-- | :-- |
| Supports | RFC §5 (provider quirks stay in the integration) |
| Source | NAAD GeoRSS hosts, province mode, British Columbia |
| Sample | one July 2026 live configuration, re-run 2026-10-03 |
| Observed | 2026-07-23 (figures first committed in `fa44e5a`), 2026-10-03 |
| Reproduce | `.venv/bin/python scripts/provider_probe.py eccc --province BC` |

| Stage | July 2026 | 2026-10-03 |
| :-- | :-- | :-- |
| National feed, one host | ~220 entries / 1.4 MB (legacy) and 970 entries / 4.7 MB (alertready), earlier samples ~1,800 `Actual` entries / ~7 MB | not re-measured |
| Envelope dedup (entries are per language × area group) | 211 entries → 100 CAP documents | not re-measured |
| Bounding-box pre-filter before any body fetch (Ontario) | ~1,800 candidate bodies → ~7 | not re-measured |
| Area-group selection and domain filters | region-matching `<info>` block, marine exclusion | same code |
| Alert entities handed to the platform (BC) | 9 | 2 |
| Coordinator timeout the stages must fit (`const.py:63`) | 30 s | 30 s, probe finished in 2.0 s |

The dedup ratio is visible in the committed probe log: across all 179 samples
in `naad-probe-2026-07-22.jsonl` the legacy host served 207 to 249 entries and
100 unique CAP ids every time.

The pre-filter's rationale, from
`custom_components/cap_alerts/providers/eccc.py` lines 139 to 150:

```
# Coarse province/territory bounding boxes for the province-mode envelope
# pre-filter, … Since the alertready.ca migration the
# envelope carries no geographic category, so province mode would otherwise have
# to fetch the CAP body of every national Actual entry (~1800) just to read its
# SGC code … unfeasible inside the poll timeout. Instead we reject entries whose
# georss-polygon bbox does not intersect the (padded) province box before the
# fetch.
```

GDACS is the other extreme, a source that absorbs nothing itself
(`providers/gdacs.py` lines 29 to 31 and 109 to 110):

| GDACS stage | Measured |
| :-- | :-- |
| Items in a sampled current-events index | 315 |
| Of those, green wildfires | 280 |
| Live cyclones | 3 |
| `<gdacs:todate>` in the past | 315 of 315, so it is not an expiry |
| Forecast shapes excluded per cyclone | track, cone, 3 wind-radii rings per 12 h step out to 4 days, some 40 rings |

## Reading

Four reduction stages run inside the provider before any entity exists, and
the platform receives single digits. The 16 KB ceiling, the registry churn
and the fan-out arguments in §2.5 are sized against 9 entities, not against
the ~1,800-entry feed. The pre-filter was a correctness fix, not a tuning:
without it province mode could not finish inside the 30 s timeout.

## Caveats

- The July stage counts (211 → 100, ~1,800 → ~7, 9 entities) were recorded in
  the notes and code comments, not in a committed log. The 2026-10-03 run
  re-measures only the final count.
- Entity count tracks the weather. 2 fog advisories on 2026-10-03 against 9
  alerts in late July says nothing about the reduction ratio.
- The Ontario pre-filter figure and the BC entity figure are from different
  configurations in the same period.
- The GDACS counts are from the provider docstring, one sampled index.
