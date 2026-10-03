# A 291-area advisory overflowed the payload after the ladder had spent every text field

| | |
| :-- | :-- |
| Supports | RFC §7.2 |
| Source | ECCC `urn:oid:2.49.0.1.124.2872749462.2026`, "Yellow Advisory - Frost" for Saskatchewan, sent 2026-09-30T21:14Z, rebuilt from the NAAD archive for a `fr-CA` GPS scope. Issue [#245](https://github.com/seevee/cap_alerts/issues/245) |
| Sample | one document, the only one over budget among 76 NAAD documents from 2026-09-29 and 09-30 across every province in both languages (46 scope and alert combinations) |
| Observed | 2026-09-30 |
| Reproduce | `timeout 30 gh issue view 245`. `tests/test_attribute_budget.py::test_the_245_advisory_fits_without_losing_its_text`, `::test_the_alternates_pay_before_the_area_list`, `::test_the_area_list_pays_before_the_primary_text`, `::test_a_cut_area_list_ends_on_a_whole_name`, `::test_unrecorded_geocodes_do_not_count_toward_the_budget` |

| Attribute | Bytes | What it held |
| :-- | --: | :-- |
| `area_desc` | 14,072 | 291 municipality names joined with commas |
| `geocodes` | 12,245 | 291 CLC codes plus 847 SGC codes, 1,138 in all |
| `parameters` | 3,159 | unrecorded, outside the measurement |
| `description`, `instruction`, both alternates | 1,837 | the text, in two languages |
| everything else | ~500 | |
| Raw payload | 28,119 | |
| After `fit_to_budget`, before the fix | 26,141 | 10 KB over the 15,800 budget, every text field deleted |

```python
# custom_components/cap_alerts/payload.py:78
UNRECORDED_ATTRIBUTES = frozenset({"parameters", "geocodes"})

# custom_components/cap_alerts/payload.py:88-94
TRIM_PRIORITY: tuple[str, ...] = (
    "description_alt",
    "instruction_alt",
    "area_desc",
    "description",
    "instruction",
)
```

## Reading

The ladder as first shipped assumed long-form text was the only term that
grows without limit. Here the text was 1.8 KB of a 12 KB excess. Every text
field fell under the 160-byte keep floor and was deleted, and the recorder
still refused the row. The two keys that grow with the warned area were
untouchable. The fix in
[#246](https://github.com/seevee/cap_alerts/pull/246) made `geocodes`
unrecorded like `parameters`. It put `area_desc` on the ladder between the
alternate and primary text, cut at a name boundary. §7.2 draws the rule from
this: any attribute that grows with the area is a ladder or unrecorded
candidate, not only the ones that grow with the text.

## Caveats

- One document. The [#150](https://github.com/seevee/cap_alerts/issues/150)
  sweep found 0 of 443 live alerts needing any trim, and this was the only
  overflow in 76 archived NAAD documents from two days.
- The archived body is not committed as a fixture. The test rebuilds the shape
  with synthetic names and codes of the measured counts.
- Frost and heat advisories are drawn over half a province, so the issue
  expects a recurrence each shoulder season. That is a prediction, not a
  measurement.
- The recorder's warning fired twice, 36 s apart. The give-up path logged at
  debug then, and now warns once per alert id (`payload.py:116-120`).
