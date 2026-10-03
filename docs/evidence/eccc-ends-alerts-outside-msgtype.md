# ECCC ends an alert in a CAP parameter while msgType stays Update

| | |
| :-- | :-- |
| Supports | RFC §2.2 |
| Source | NAAD GeoRSS index (`rss.naad-adna.pelmorex.com`) and the CAP bodies it links |
| Sample | 211 Atom entries, 100 unique CAP documents, 92 `status=Actual`, one national snapshot |
| Observed | 2026-07-22 |
| Reproduce | `tests/fixtures/eccc_cap_cam_4258489601_update.xml` (verbatim capture). Entry-to-document ratio: `naad-probe-2026-07-22.jsonl`. The status counts are a one-time capture, no snapshot committed |

| Measure | Value |
| :-- | :-- |
| Atom entries in the snapshot | 211 |
| Unique CAP documents behind them | 100 (78 documents × 2 entries, 11 × 4, 11 × 1) |
| Documents with `status=Actual` | 92 |
| `msgType=Cancel` | 1 of 92, from a non-ECCC sender |
| `Actual` documents mixed or wholly `ended` | 19 of 92 |
| `Alert_Location_Status=active`, `expires − sent` | median ~16 h |
| `Alert_Location_Status=ended`, `expires − sent` | ~1 h |
| `Alert_Location_Status=transitioned_out`, `expires − sent` | ~0 h |

The same index over the following twelve hours, 179 samples from the probe
log (`naad-probe-2026-07-22.jsonl`, `old` host):

| Measure | Min | Median | Max |
| :-- | :-- | :-- | :-- |
| Atom entries | 207 | 217 | 249 |
| Unique ids | 100 | 100 | 100 |
| Entries per document | 2.07 | 2.17 | 2.49 |

One live document, `msgType=Update`, one area group still active and one
ended, `tests/fixtures/eccc_cap_cam_4258489601_update.xml` (fetched from
`rss.alertready.ca` on 2026-08-23):

```xml
<sent>2026-08-22T20:43:49-00:00</sent>
<status>Actual</status>
<msgType>Update</msgType>
…
<info>
    <language>en-CA</language>
    …
    <urgency>Immediate</urgency>
    …
    <expires>2026-08-22T22:41:49-00:00</expires>
    …
    <headline>yellow warning - severe thunderstorm - in effect</headline>
    …
        <valueName>layer:EC-MSC-SMC:1.1:Alert_Location_Status</valueName>
        <value>active</value>
…
<info>
    <language>en-CA</language>
    …
    <responseType>AllClear</responseType>
    <urgency>Past</urgency>
    …
    <expires>2026-08-22T21:43:49-00:00</expires>
    …
    <headline>yellow warning - severe thunderstorm - ended</headline>
    …
        <valueName>layer:EC-MSC-SMC:1.1:Alert_Location_Status</valueName>
        <value>ended</value>
    …
        <areaDesc>Goderich - Bluewater - Southern Huron County</areaDesc>
```

## Reading

`msgType` does not carry ECCC endings. The one `Cancel` in 92 documents came
from another sender. The ending travels per area group in
`Alert_Location_Status`, with about an hour of `expires` still on the clock.
An integration that trusts `msgType` holds an ended warning live for that
hour. And one document can be live over one region and ended over another,
which is what 19 of 92 looked like. RFC §2.2 therefore takes a provider
termination hint and scopes selection to the configured region.

## Caveats

- The status counts are one snapshot of one index on one day. The probe log
  covers the entry-to-document ratio only, not `msgType` or the parameter.
- `expires − sent` figures are medians and approximations from that snapshot.
- The `cancelled` token was seen live later, on 2026-08-23
  ([#172](https://github.com/seevee/cap_alerts/issues/172)), not in this
  sample.
- The fixture is one document from a different day than the snapshot. It
  shows the shape, not the counts.
