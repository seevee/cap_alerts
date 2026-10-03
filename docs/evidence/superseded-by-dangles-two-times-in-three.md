# superseded_by points at a successor that never reached NAAD two times in three

| | |
| :-- | :-- |
| Supports | RFC §2.3 |
| Source | `alertsarchive.pelmorex.com`, four days of ECCC documents, plus `cap.alertready.ca` per-alert lookups |
| Sample | 671 documents, 27 `<info>` blocks with `Alert_Location_Status=transitioned_out`, all from `cap-pac@canada.ca` |
| Observed | 2026-09-02 |
| Reproduce | one-time capture, no snapshot committed. Parsing: `tests/test_conventions.py`, `tests/test_store_payload.py` |

| Measure | Value |
| :-- | :-- |
| `transitioned_out` groups measured | 27 |
| Carrying `layer:EC-MSC-SMC:1.1:Transitioned_Out_CAP_Reference` | 27 of 27 |
| Target found in the source's own `<references>` | 0 of 27 |
| Target referencing the source back | 0 of 27 |
| Target found anywhere on NAAD (archive or constructed CAP URL) | 9 of 27 |
| Target never appeared on NAAD | 18 of 27 |
| CLC suffix matching one of the block's own area codes | 21 of 27 |
| Hazard mix | marine squall → squall, one thunderstorm, one heat |
| Watch → warning upgrades | 0 of 27 |

Parameter value shape, from
[#190](https://github.com/seevee/cap_alerts/issues/190):

```
cap-pac@canada.ca,<successor identifier>,<sent>;<CLC>
```

No committed fixture carries `Transitioned_Out_CAP_Reference`, and none
carries a `transitioned_out` group. The nearest captured lifecycle parameter
is the `ended` block of the same layer,
`tests/fixtures/eccc_cap_cam_0425565047_ended.xml` (verbatim from
`rss.alertready.ca`, 2026-08-23):

```xml
<valueName>layer:EC-MSC-SMC:1.1:Alert_Location_Status</valueName>
<value>ended</value>
```

The parser's own cases, `tests/test_conventions.py`:

| Case | Test |
| :-- | :-- |
| Only ECCC declares the hook | `test_only_eccc_declares_a_superseded_by_hook` |
| Reference parses to the successor identifier | `test_eccc_superseded_by_parses_the_reference` |
| Identifier containing a comma | `test_eccc_superseded_by_tolerates_a_comma_bearing_identifier` |
| Absent, empty or malformed parameter | `test_eccc_superseded_by_none_when_absent_or_malformed` |
| Key omitted for a plain `ended`, for non-ECCC, and when absent | `test_removed_omits_superseded_by_*` in `tests/test_store_payload.py` |

## Reading

CAP `<references>` never links a watch to the warning that replaces it at
ECCC. The link lives in a CAP-CP parameter that names the successor's own
`<identifier>`, and it was present on every `transitioned_out` group
measured. Two targets in three never appeared on the feed. RFC §2.3 therefore
ships `superseded_by` as a hint and omits it when the parameter is absent or
does not parse.

## Caveats

- Four days of one archive host, 27 groups. Whether ECCC publishes the missing
  targets elsewhere or drops them before publication is unknown.
- All 27 were marine or convective transitions. The field has never been
  observed on a watch → warning upgrade, which the reporter says essentially
  does not happen at ECCC.
- The archive host is a Pelmorex property. Its survival past the NAAD host
  sunset is unknown, so the sample may not be re-drawable there.
