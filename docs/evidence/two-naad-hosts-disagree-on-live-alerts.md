# Two sanctioned NAAD hosts disagree on which alerts are live

| | |
| :-- | :-- |
| Supports | RFC §2.5 (absence is not termination), §1.4 requirement 8 |
| Source | `rss.alertready.ca` and `rss.naad-adna.pelmorex.com`, fetched together |
| Sample | 179 paired fetches, 2026-07-22 17:45Z to 2026-07-23 05:18Z, about every 3 min |
| Observed | 2026-07-22 |
| Reproduce | `scripts/naad_feed_diff.py --summary naad-probe-2026-07-22.jsonl`, and the snippet below |

| Measure | Value |
| :-- | --: |
| Samples | 179 |
| Samples with at least one `Actual`/`Public` alert on the legacy host and absent from alertready | 179 of 179 |
| Mean share of legacy-host ids present on alertready | 87.5% (min 79%, max 94%) |
| Most `Actual`/`Public` alerts missing in one sample | 18 |
| Distinct `Actual`/`Public` alerts missing at least once | 55 |
| Samples with an `Extreme` alert missing | 41 of 179 |
| Distinct `Extreme` alerts missing | 2 (a wildfire evacuation and a 911 outage, both `Immediate`/`Observed`) |
| Consecutive samples whose missing set changed | 101 of 178 pairs |

```python
import json, statistics
rows = [json.loads(l) for l in open("naad-probe-2026-07-22.jsonl")]
gaps = [r["missing_from_new"] for r in rows]            # Actual/Public only
cover = [1 - r["missing_from_new_total"] / r["hosts"]["old"]["unique_ids"] for r in rows]
print("samples", len(rows), rows[0]["sampled_at"], rows[-1]["sampled_at"])
print("samples with an Actual/Public alert missing", sum(1 for g in gaps if g))
print("mean coverage of legacy ids", round(statistics.mean(cover), 3), "min", min(cover), "max", max(cover))
print("max Actual/Public missing in one sample", max(len(g) for g in gaps))
print("distinct Actual/Public alerts missing at least once", len({a["id"] for g in gaps for a in g}))
print("samples with an Extreme alert missing", sum(1 for r in rows if r["extreme_missing"]))
print("distinct Extreme alerts missing", len({i for r in rows for i in r["extreme_missing"]}))
sets = [frozenset(a["id"] for a in g) for g in gaps]
print("consecutive samples whose missing set changed", sum(a != b for a, b in zip(sets, sets[1:])))
```

The longer run reported on [#38](https://github.com/seevee/cap_alerts/issues/38)
(comment of 2026-08-14) extends the same probe:

| Measure, 1422 samples every 15 min, 2026-07-30 to 2026-08-13 | Value |
| :-- | --: |
| Mean share of legacy-host ids present on alertready | 84.6% (median 86%, min 39%, max 99%, never 100%) |
| Samples under 90% coverage | 967 |
| Distinct alerts missing at least once | 1134 |
| Of those, `Extreme` | 26 (all `Immediate`/`Observed`) |
| Samples with an `Extreme` alert missing | 373 (26%) |
| Missing alerts fetched by id from `cap.alertready.ca` | 6 of 6, HTTP 200, byte-identical to the legacy copy |

## Reading

Both hosts are sanctioned endpoints for the same national system, yet no
paired fetch agreed. The set of absent alerts churned between most consecutive
samples, so this is not a fixed omission. An integration that terminated on
one missed observation would have cleared an `Extreme` wildfire evacuation
from a dashboard while it was in effect. Requirement 8 and the retain-on-absence
rule in §2.5 exist because of this measurement.

## Caveats

- The committed log covers 11.5 hours. The 1422-sample run is quoted from the
  issue comment, its raw log was not committed (one-time capture).
- Coverage is computed over all ids on the legacy host, which also carries
  `Test` messages. The gap list itself is `Actual`/`Public` only.
- The legacy host serves at most 100 unique ids per fetch (every sample), so
  its retention, not the alert population, bounds the denominator.
- Alert identities are not reproduced here. The set self-falsifies within
  minutes. The log file carries them.
- The notes record byte-identical back-to-back fetches and well-formed bodies
  on every sample. The log records retries on 4 host fetches out of 358, all
  resolved on a later attempt.
