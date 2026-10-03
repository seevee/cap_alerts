# A per-field text cap shreds alerts that fit and cannot rescue the one that overflows

| | |
| :-- | :-- |
| Supports | RFC §7.2, §2.4 |
| Source | Raw provider output before normalization: NWS national active set, all 13 ECCC provinces, all 38 MeteoAlarm countries, 119 WMO SWIC sources, GDACS global (172 scopes) |
| Sample | 9,604 CAP messages for text sizes, 425 of them serialized through `to_attributes()`. UTF-8 bytes throughout |
| Observed | 2026-08-16 |
| Reproduce | `.venv/bin/python scripts/text_size_sweep.py` ([#150](https://github.com/seevee/cap_alerts/issues/150)) (`--json` for machine output, `--providers nws,eccc` to narrow, 5 to 10 minutes for a full run) |

| Field | Values measured | Over 4,096 bytes | Longest | Note |
| :-- | --: | --: | --: | :-- |
| `description` | 9,381 non-empty | 13 (0.14%), all NWS tropical | 8,783 (Tropical Cyclone Local Statement) | 13 of 47 tropical NWS messages (28%). Largest non-tropical anywhere: 3,757 |
| `instruction` | 6,158 | 0 | 1,835 | |
| `description_alt` | 7,907 alerts with both languages | 6, three ECCC BC air-quality warnings each seen twice via WMO `ca-msc-xx` | 5,081 | English 2,976 to 3,757 vs French 4,115 to 5,081 on the same three alerts |
| localized vs primary ratio | 7,907 pairs | | | alternate longer 69% of the time, median ratio 1.08, mean 1.28 |

| Alert | Long-form text | Serialized payload | Under a 4 KB per-field cap | Under the 15,800-byte payload bound |
| :-- | --: | --: | :-- | :-- |
| NWS Tropical Cyclone Local Statement | 8,871 | 14,290 | description cut by more than half | fits untouched, 2,094 bytes spare |
| ECCC BC air-quality warning | 9,535 | 19,084 | still over the 16,384 ceiling | fits after geocode de-duplication alone |

## Reading

The old 4 KB cap was dormant almost everywhere and bit only on NWS tropical
products, where it destroyed text that would have fit. The only other over-cap
values in the sample were French alternates, which the cap never covered, so
the entire overrun landed on the unbounded field. The overflow was already in
production: nine recorder rows on the reference instance held a real state and
empty attributes, all ECCC air-quality warnings. Set a text cap low enough to
bound the schema and it destroys incidents that fit. Set it high enough to
spare them and it bounds nothing. That is why §7.2 measures the payload.

## Caveats

- Single snapshot, and it caught live Atlantic tropical activity, which favors
  finding long text. The localized-length finding does not depend on that.
- Text was measured on raw provider output in UTF-8 bytes. JSON escaping and
  the recorder's own overhead are only in the serialized column.
- The 19,084-byte figure is from the notes. The `payload.py` docstring quotes
  19,080 for the same alert on a later fetch.
- The serialized column is a floor: `geometry_ref`, `bbox`,
  `incident_platform_version` and `friendly_name` are added after the script
  measures (see the script docstring).
- The 425-alert serialization pass is one draw, not a census of the 9,604.
