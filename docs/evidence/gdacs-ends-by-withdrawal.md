# GDACS publishes no end time, so an event ends when the feed withdraws it

| | |
| :-- | :-- |
| Supports | RFC §2.2 |
| Source | `https://www.gdacs.org/xml/rss.xml` (the current-events index) |
| Sample | 192 items on one live fetch. Earlier: 315 items on one fetch |
| Observed | 2026-10-03 (live table). 2026-08-08 (315-item sample, retention figures) |
| Reproduce | the snippet below, `tests/fixtures/gdacs_rss.xml` |

Live fetch, 2026-10-03T05:06Z, 192 items, 591,343 bytes:

| Event type | Items | `iscurrent=true` | `iscurrent=false` | `todate < now` |
| :-- | --: | --: | --: | --: |
| DR drought | 12 | 1 | 11 | 12 |
| EQ earthquake | 4 | 4 | 0 | 4 |
| FL flood | 16 | 8 | 8 | 14 |
| TC tropical cyclone | 8 | 8 | 0 | 8 |
| WF wildfire | 152 | 125 | 27 | 152 |
| Total | 192 | 146 | 46 | 190 |

Retention on the same index, probed 2026-08-08 (`const.py`): a cyclone stayed
12.9 d, a flood 81.9 d, a drought 403.9 d. Earthquakes left after about four
days (M5.6 present at 90.4 h, M5.7 gone at 98.4 h). `todate` was in the past
for all 315 items on that fetch, including all three live cyclones and all 280
live wildfires.

One item, `tests/fixtures/gdacs_rss.xml`:

```xml
<gdacs:eventtype>EQ</gdacs:eventtype>
<gdacs:alertlevel>Green</gdacs:alertlevel>
<gdacs:eventid>1556861</gdacs:eventid>
<gdacs:episodeid>1723786</gdacs:episodeid>
<gdacs:iscurrent>true</gdacs:iscurrent>
<gdacs:fromdate>Sat, 08 Aug 2026 05:39:00 GMT</gdacs:fromdate>
<gdacs:todate>Sat, 08 Aug 2026 05:39:00 GMT</gdacs:todate>
```

The feed carries no CAP body and no urgency. The provider stamps every alert
`urgency=Past` (`providers/gdacs.py`, `_URGENCY`) and carries `todate` in
`parameters`, never in `expires`.

The counting snippet (stdlib only):

```python
import collections, urllib.request, xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

URL = "https://www.gdacs.org/xml/rss.xml"
req = urllib.request.Request(URL, headers={"User-Agent": "HomeAssistant-CAPAlerts/probe"})
with urllib.request.urlopen(req, timeout=60) as resp:
    body = resp.read()
now = datetime.now(timezone.utc)
G = "{http://www.gdacs.org}"
rows = collections.defaultdict(collections.Counter)
for item in ET.fromstring(body).iter("item"):
    etype = item.findtext(G + "eventtype") or "?"
    cur = (item.findtext(G + "iscurrent") or "?").lower()
    todate = item.findtext(G + "todate")
    rows[etype]["items"] += 1
    rows[etype]["iscurrent=" + cur] += 1
    rows[etype]["todate<now"] += bool(todate and parsedate_to_datetime(todate) < now)
for etype in sorted(rows):
    print(etype, dict(rows[etype]))
```

## Reading

`todate` is a last-observation time, not an expiry. Mapping it to `expires`
would mark 190 of 192 items terminal on arrival. With no expiry and no
termination lookup, withdrawal from the index is the only exit, and the §2.5
absence rule ends the incident on its last branch. Retention then tracks
significance: days for a quake, a season for a drought.

## Caveats

- `iscurrent=false` is not drought-only on the 2026-10-03 fetch. 27 wildfires
  and 8 floods carried it too, every one with `todate` at 2026-09-29 00:00 or
  01:00 UTC and about 100 h old. The 2026-08-08 sample saw it only on
  droughts. The provider ignores the flag either way.
- Two flood items carried a `todate` in the future (2026-10-07, `fromdate`
  2026-10-05). Those are forecast floods, the first future `todate` seen.
- Both figures are single fetches. The 24 h index (`rss_24h.xml`) is unioned
  with this one by the provider and is not counted here.
