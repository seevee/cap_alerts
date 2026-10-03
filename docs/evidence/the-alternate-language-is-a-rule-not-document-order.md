# The alternate language block is chosen by rule, not by document order

| | |
| :-- | :-- |
| Supports | RFC §2.7 |
| Source | WMO SWIC CAP bodies and MeteoAlarm per-country feeds, live. Issue [#154](https://github.com/seevee/cap_alerts/issues/154) |
| Sample | 110 SWIC sources for the report, then 314 WMO documents across 119 sources and 6,567 MeteoAlarm warnings across 38 countries for the sweep |
| Observed | 2026-08-16 (report), 2026-08-21 (sweep) |
| Reproduce | `.venv/bin/python scripts/alt_language_sweep.py` (`--providers wmo --wmo-docs 5` to narrow, `--json` for output). Rule pinned by `tests/test_wmo_provider.py::test_select_alt_info_prefers_english_on_three_language_doc`, `tests/test_meteoalarm_parser.py::test_three_language_feed_takes_english_as_alternate`, `tests/test_property_cap.py::test_alternate_info_index_picks_a_different_language_english_first` |

| Measurement | WMO SWIC | MeteoAlarm |
| :-- | :-- | :-- |
| Swept | 314 documents, 119 sources | 6,567 warnings, 38 countries (DE timed out) |
| Sources with more than one `<info>` block (report sample) | 46 of 110 | not counted |
| 3+-language combinations seen | `fi-fmi-xx` fi-FI/sv-FI/en-GB, `mo-smg-xx` zh-mo/pt-PT/en-US | CH en/de/fr/it/rm (207 docs), BE nl-BE/fr-BE/en-GB/de-DE (53), DK da-DK/kl-GL/en-GB (42), FI fi-FI/sv-SE/en-GB (22), LU en-GB/fr-FR/de-DE (8) |
| 3+-language documents carrying English | all | all |
| Non-English primary given a non-English alternate, document order | 32 | 287 (BE 159, DK 84, FI 44) |
| Same, English-first rule | 0 | 0 |
| Same-language twins that defeat "prefer an English block" | `ca-msc-xx` en-CA/fr-CA/en-CA/fr-CA, `rs-hidmet-sr` en-GB/sr/sr-Latn | none seen |

`tests/fixtures/wmo_cap_multilang.xml`, language lines only:

```xml
	<info>
		<language>en-US</language>
		…
	</info>
	<info>
		<language>zh-CN</language>
		…
	</info>
```

## Reading

Before #154 each provider took the first leftover `<info>` block. A `zh`
reader of `mo-smg-xx` got Portuguese and a Swedish reader of `fi-fmi-xx` got
Finnish. Every 3+-language document on both feeds carried English. So the
rule in `providers/cap.py::alternate_info_index` is English when the primary
is not English, else the first other language. Same-language blocks never
qualify. On a two-language document that is always the other one,
which is the committed fixture above. §2.7 states that rule.

## Caveats

- The report wrote "more than one `<info>` block" for the 46 of 110 figure.
  Notes §2.7 and RFC §2.7 render it as "more than two languages", which the
  issue text does not support.
- No committed fixture carries three languages. The three-language cases are
  built inline in the tests named above. `wmo_cap_multilang_no_en.xml` is a
  two-block zh-mo/pt-PT document shaped after `mo-smg-xx` with its English
  block removed.
- One sweep. MeteoAlarm DE timed out, and the WMO sweep took up to a few CAP
  bodies per source, not every document.
- The "none" branch of the rule, a document whose blocks all share one
  language or carry no tag, fired on no live document.
- ECCC is bilingual by construction and was not swept.
