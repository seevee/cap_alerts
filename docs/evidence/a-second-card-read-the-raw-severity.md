# A second card read the raw severity instead of the normalized one

| | |
| :-- | :-- |
| Supports | RFC §5, §2.1 |
| Source | [DTekNO/ha-alert-card#1](https://github.com/DTekNO/ha-alert-card/issues/1) and release [v2026.10.2](https://github.com/DTekNO/ha-alert-card/releases/tag/v2026.10.2) |
| Sample | 1 issue with 2 comments and 1 release, 2026-10-01 to 2026-10-03, dev-box check over 23 `cap_alerts` entries and about 250 alerts |
| Observed | 2026-10-03 (the dev-box check reported in the second comment, UTC) |
| Reproduce | `timeout 30 gh api repos/DTekNO/ha-alert-card/issues/1/comments` |

| Event | UTC | Elapsed | Link |
| :-- | :-- | :-- | :-- |
| Issue filed, asks for a `device:` source | 2026-10-01 17:07:36 | | [issue](https://github.com/DTekNO/ha-alert-card/issues/1) |
| v2026.10.2 published with "Device sources (`device: <id>`)" | 2026-10-02 07:30:28 | 14 h 23 min | [release](https://github.com/DTekNO/ha-alert-card/releases/tag/v2026.10.2) |
| Maintainer reply, "It's in 2026.10.2 as a `device:` source" | 2026-10-02 07:36:44 | 14 h 29 min | [comment](https://github.com/DTekNO/ha-alert-card/issues/1#issuecomment-5947491474) |
| Dev-box report, three discrepancies | 2026-10-03 01:24:28, edited 03:24:53 | 1 d 8 h | [comment](https://github.com/DTekNO/ha-alert-card/issues/1#issuecomment-5964077058) |

Discrepancies, as reported in the 2026-10-03 comment:

| Field | Card reads | `cap_alerts` publishes | Effect reported | Numbers in the comment |
| :-- | :-- | :-- | :-- | :-- |
| link | `url` before `web` | `url` is the source document (CAP XML, NWS API JSON, the ECCC Atom id), `web` is the page | "Everywhere else a tap opens raw XML." MeteoAlarm leaves `url` empty, so the maintainer's test fell through to `web` | every provider but MeteoAlarm |
| severity | `severity` | `severity` is the agency's raw value, `severity_normalized` is the tier-derived one and the entity state | wrong color and sort order on the tierless AU alerts | "22 of 108 differ, e.g. a QLD fire that's raw Moderate but normalized severe" |
| time | `time` with fallback to `onset` | QLD and WA publish no `onset` or `effective` | "their rows show no time" | QLD and WA |

```
# release note, v2026.10.2, 2026-10-02
- Unmapped `time`, `url` and `area` also try the CAP names `onset`, `web` and `area_desc`.
- A `_self` entity in state `unknown` counts as an alert when its attributes carry one.

# second comment, 2026-10-03
I put 2026.10.2 on my dev box against 23 cap_alerts entries, screenshot below.
NWS, ECCC, MeteoAlarm and BBK are quiet right now, so this covers WMO, GDACS
and the Australian states, about 250 alerts. … Three things turned up:
1. `url` wins over `web`. …
2. Severity. … 22 of 108 differ …
3. Time. QLD and WA publish no `onset` or `effective`, so their rows show no time.
```

## Reading

An implementer who did not write the contract built against the README and
tested on one provider, and shipped in under 15 hours. First contact then
tripped on the line §2.1 draws between platform-guaranteed and feed-supplied
fields. `severity_normalized` is the guaranteed one and disagreed with the raw
field on 22 of 108 live AU alerts. `web` is the guaranteed link and `url` is
not a page. The gap closed as fast as it opened, which is what §5 means by a
contract shaped by a real UI.

## Caveats

- The 22 of 108 is one dev-box snapshot on 2026-10-03 and covers the
  Australian entries only. NWS, ECCC, MeteoAlarm and BBK had no live alerts at
  the time.
- The reporter is this project's author, so the discrepancy list is not an
  independent review of `cap_alerts`. The independent part is the card.
- The issue was open with two comments when fetched. Whether the card changed
  `web` or `severity_normalized` handling afterwards is not checked here.
- The second comment admits the `url` doc on this side called it the alert page
  until 2026-10-03 ([#266](https://github.com/seevee/cap_alerts/pull/266)), so
  one of the three is partly a documentation miss.
