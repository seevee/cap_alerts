# Retaining an absent alert is only safe when something else can end it

| | |
| :-- | :-- |
| Supports | RFC §2.5 (the absence algorithm, last three branches) |
| Source | WMO SWIC CAP bodies across 113 authorities, and the convention table |
| Sample | 330 CAP documents, 510 `<info>` blocks ([#122](https://github.com/seevee/cap_alerts/pull/122)), re-checked live on two sources |
| Observed | 2026-08-08 or shortly before (#122), live check 2026-10-03 |
| Reproduce | `.venv/bin/python scripts/provider_probe.py wmo --source mo-smg-xx --json` and count `expires` keys |

The rule, copied from `rfc.md` §2.5:

```
if the provider signalled termination:    terminate (cancel; removal_reason where published)
elif now >= expires:                      terminate (expired)
elif the source declares absence-ends:    terminate (cancel)
elif expires is published:                retain, mark stale, record last_confirmed
elif the source can still end it:         retain, mark stale, record last_confirmed
else:                                     terminate (cancel)
```

What each shipped source can do, from
`custom_components/cap_alerts/conventions.py` (`CONVENTIONS`, lines 1224 to
1296) and the provider docstrings:

| Source | Publishes `expires` | Terminal vocabulary | Termination lookup | How an absent alert ends |
| :-- | :-- | :-- | :-- | :-- |
| NWS | yes, 0 of 629 messages missing (#122) | CAP `msgType=Cancel` via the lookup | yes, `discovers_terminations=True` (conventions.py:1224) | at `expires`, or in the cycle the cancel lookup returns it |
| ECCC | yes, 0 of 144 blocks missing (#122) | `ended`, `cancelled`, `transitioned_out` (conventions.py:115) | no | at `expires`, or on a terminal `Alert_Location_Status` |
| MeteoAlarm | yes, 0 of 11,384 blocks missing (#122) | none declared (conventions.py:1237) | no | at `expires` |
| WMO | 20 of 510 blocks missing, 100% on Macao and Curaçao (#122) | none (conventions.py:1259) | no | at `expires` when published, otherwise on absence (last branch) |
| GDACS | never (providers/gdacs.py:29) | none (conventions.py:1269) | no | on absence (last branch) |
| BBK / NINA | DWD relay yes, MoWaS none (providers/bbk.py:38) | none (conventions.py:1277) | no | DWD at `expires`, MoWaS on absence (last branch) |
| Australian states | blanked, feed value is a regeneration TTL (providers/au.py:29) | none (conventions.py:1288) | no | on absence (last branch) |

No row sets `absence_policy=ABSENCE_ENDS` (conventions.py:1114, default at
1182).

| WMO authority (#122 sample) | `<info>` blocks | No `<expires>` |
| :-- | --: | --: |
| `mo-smg-xx` (Macao) | 72 | 72 |
| `cw-meteo-en` (Curaçao) | 25 | 25 |
| `hk-hko-xx` (Hong Kong) | 50 | 22 |
| `cn-cma-xx` (China) | 50 | 8 |
| All 113 authorities | 510 | 20 |

| Live probe, 2026-10-03 05:07Z | Alerts returned | With `expires` |
| :-- | --: | --: |
| `mo-smg-xx` | 498 | 0 |
| `cw-meteo-en` | 147 | 0 |

## Reading

An alert with no `expires` cannot be ended by time. Keeping it is safe only
if a terminal token or a termination lookup can still end it, and WMO has
neither. Before #122 an expiry-less WMO alert on Macao or Curaçao would have
stayed stale until Home Assistant restarted. The last branch terminates it on
absence instead, and the rule reads the source's capabilities from the
convention table rather than naming the senders. The three sources whose
incidents genuinely end by withdrawal reach the same branch, so the declared
absence-ends branch stays unused.

## Caveats

- The 510-block sweep was run once for #122 and its documents were not
  committed. The per-authority counts are quoted from the PR body.
- The live probe reads the provider's `to_attributes()` output, which omits
  empty fields, so "0 with `expires`" means the key was absent on every alert.
- `cap:expires` in the RSS envelope was absent on 0 of 127 of those items
  (#122), so the envelope offers no fallback. Not re-measured here.
- The RSS mirror for `mo-smg-xx` returned HTTP 404 for 2 of its linked CAP
  bodies on the live run. Those two are not in the 498.
