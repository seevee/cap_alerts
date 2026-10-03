# NWS publishes cancellations where the active endpoint cannot see them

| | |
| :-- | :-- |
| Supports | RFC §2.5 (absence is not termination) |
| Source | `api.weather.gov/alerts/active` against `api.weather.gov/alerts?message_type=cancel` |
| Sample | 629 messages in one six-hour national window, plus a live spot check |
| Observed | 2026-08-08 or shortly before (reported in [#121](https://github.com/seevee/cap_alerts/pull/121)), live check 2026-10-03 |
| Reproduce | the snippet below |

| Six-hour national window (#121) | Count |
| :-- | --: |
| Messages | 629 |
| VTEC `CAN` products | 101 |
| CAP `messageType=Cancel` | 101 |
| VTEC `EXP` products | 73 |
| Terminal products appearing in `/alerts/active` | 0 of 174 |
| Terminal products whose VTEC tracking key was still in the active set | 1 of 174 |

| Live check, 2026-10-03 05:05Z | Count |
| :-- | --: |
| Features on `/alerts/active` | 352 (227 `Alert`, 125 `Update`) |
| Most recent `message_type=cancel` products | 50 |
| Cancel ids present in the active set | 0 |

```python
import json, urllib.request
UA = {"User-Agent": "HomeAssistant-CAPAlerts/probe", "Accept": "application/geo+json"}
def get(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=60) as r:
        return json.load(r)
active = get("https://api.weather.gov/alerts/active")
cancel = get("https://api.weather.gov/alerts?message_type=cancel&limit=50")
active_ids = {f["properties"]["id"] for f in active["features"]}
cancel_ids = [f["properties"]["id"] for f in cancel["features"]]
print("active", len(active_ids), "cancel", len(cancel_ids))
print("cancel ids present in active", sum(1 for i in cancel_ids if i in active_ids))
```

From `custom_components/cap_alerts/providers/nws.py`, `_fetch_cancellations`:

```
NWS publishes cancellations as first-class products carrying VTEC action
``CAN`` and ``messageType=Cancel``, but **never on the active endpoint**:
measured over a six-hour national window, 101 of 101 cancellations were
absent from ``/alerts/active``. Polling only that endpoint therefore
makes a genuine cancellation indistinguishable from a dropped record,
…
And an id still in the active set is skipped: NWS can
cancel a warning over part of its area while it runs on elsewhere (1 of
174 terminal products in the sample), and there the active record is the
truthful one.
```

## Reading

An integration polling only `/alerts/active` sees a cancelled warning and a
dropped record the same way, as an id that is gone. Expiry-gated retention
would then hold every cancelled warning to its published expiry. The provider
queries the all-messages endpoint for `message_type=cancel`, scoped to the
configured zone or point and a six-hour lookback (`CANCEL_LOOKBACK`), and the
convention row sets `discovers_terminations=True` so the store knows an exit
exists. The platform's absence rule is the fallback when that lookup fails.

## Caveats

- The six-hour window was measured once and its raw messages were not
  committed. The counts are quoted from the PR body and the provider
  docstring.
- The live check compares 50 recent cancel products against one snapshot of
  the active set. It confirms the direction, not the rate.
- `/alerts/active` was fetched without `limit`. The API returns the full
  active set by default (352 features here).
- The point-scoped cancel query is unverified against the live API (#121
  body). Zone scoping is confirmed.
