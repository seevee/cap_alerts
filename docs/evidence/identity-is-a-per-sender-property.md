# Identity is a per-sender property, not a per-provider one

| | |
| :-- | :-- |
| Supports | RFC §1.2 |
| Source | NWS `api.weather.gov/alerts/active`, MeteoAlarm FR and FI feeds, GDACS `rss.xml`, BBK NINA warning bodies, the four Australian EDXL feeds |
| Sample | NWS 65 active non-VTEC alerts on one national poll. FMI 23 warnings. QLD 74 warning ids over 46 h (186 polls). WA 10 live alerts. TAS 6 alerts |
| Observed | 2026-08-06 (NWS, FMI), 2026-08-08 (GDACS), 2026-09-19 to 09-24 (BBK, AU) |
| Reproduce | `tests/fixtures/meteoalarm_fr_reissue.json`, `tests/fixtures/au_nsw.xml`, `tests/fixtures/au_tas.xml`, `tests/fixtures/bbk_warning_mowas.json`. NWS and QLD counts are one-time captures, no snapshot committed |

| Provider | Key | Measured reason | Sample |
| :-- | :-- | :-- | :-- |
| NWS, VTEC | `office.phenomena.significance.tracking.year` | VTEC is a supersession protocol | [#115](https://github.com/seevee/cap_alerts/pull/115) |
| NWS, non-VTEC | sender + AWIPS id + event + sorted UGC set | 23 of 65 active non-VTEC alerts were surplus re-issues (35%), six offices, deepest cluster six messages of one Air Quality Alert, `<references>` empty on all 65 | national feed, 2026-08-06 |
| ECCC | `sha256(sender + sent + eventCode + polygon_hash)` | en/fr siblings share those bytes, urgency excluded so revision churn does not split them | `tests/fixtures/eccc_cap_en_new_1.xml`, `eccc_cap_fr_new_1.xml` (synthetic pair) |
| MeteoAlarm, most senders | `sha256(identifier)` | identifier collisions are distinct concurrent warnings | [#116](https://github.com/seevee/cap_alerts/issues/116) table |
| MeteoAlarm, MeteoFrance | sender + awareness type + region set + forecast window | identifier embeds the issue timestamp, so each re-issue mints a new one | fixture below, [#37](https://github.com/seevee/cap_alerts/issues/37) |
| MeteoAlarm, FMI | same content key via a contiguous-window run rule | splits one warning at the window edge. All 23 sampled warnings `2; yellow`, 12 of 23 share one `sent` to the second | `conventions.py`, [#98](https://github.com/seevee/cap_alerts/issues/98) |
| WMO SWIC | `sha256(identifier)` | identifier stable across Update and Cancel | `tests/fixtures/wmo_cap_1.xml` |
| GDACS | `sha256("{eventtype}:{eventid}")` | identifier is `GDACS_<type>_<eventid>_<episodeid>` and the episode segment increments per re-issue (`GDACS_EQ_1556861_1723786`, then a new episode) | [#116](https://github.com/seevee/cap_alerts/issues/116), 2026-08-08 |
| BBK / NINA | `sha256(identifier)`, one per revision | each revision names its predecessor in `references`, so chains collapse to the leaf | fixture below |
| AU: NSW, TAS | `sha256("{state}:{incidents}:{eventCode}")` | identifier re-mints per update (NSW `{sent}:{incident}`, TAS a counter: `IDT21037-83466` then `-83572`), `<incidents>` constant. TAS publishes two products per incident | fixtures below, [#218](https://github.com/seevee/cap_alerts/issues/218) |
| AU: QLD `WARN-n` | `sha256("{state}:{areaDesc}:{eventCode}")` | 36 re-mints across 74 ids in 46 h, no predecessor named. One fire carried two warnings for different street sets at once | 2026-09-20 to 09-22, [#116](https://github.com/seevee/cap_alerts/issues/116), [#233](https://github.com/seevee/cap_alerts/pull/233) |
| AU: WA | `sha256("{state}:{identifier}")` | ObjectId identifiers, updates keep the id (4 of 10 live alerts carried `sent` days after the id's creation time) | 2026-09-24, [#116](https://github.com/seevee/cap_alerts/issues/116) |

MeteoFrance, two messages for one warning,
`tests/fixtures/meteoalarm_fr_reissue.json`:

```json
"identifier": "2.49.0.0.250.0.FR.20260715060108.004033",
"sent": "2026-07-15T06:01:08+02:00",
"msgType": "Alert",
…
  {"valueName": "awareness_level", "value": "2; yellow; Moderate"},
  {"valueName": "awareness_type", "value": "5; Extreme high temperature"}
…
"identifier": "2.49.0.0.250.0.FR.20260715130426.047036",
"sent": "2026-07-15T13:04:26+02:00",
"msgType": "Update",
…
  {"valueName": "awareness_level", "value": "3; orange; Severe"},
```

NSW and TAS, `tests/fixtures/au_nsw.xml` and `tests/fixtures/au_tas.xml`:

```xml
<cap:identifier>2026-09-20T09:05:00.0000000:678279</cap:identifier>
<cap:sent>2026-09-20T09:05:00+10:00</cap:sent>
…
<cap:incidents>678279</cap:incidents>
…
<cap:identifier>IDT21037-83466</cap:identifier>
<cap:sent>2026-09-20T09:44:40+10:00</cap:sent>
…
<cap:incidents>SES:IDT21037</cap:incidents>
```

BBK MoWaS, one revision naming the one before it,
`tests/fixtures/bbk_warning_mowas.json`:

```json
"identifier": "mow.DE-SL-SLS-W038-20260904-000",
…
"msgType": "Update",
…
"references": "DE-SL-SLS-W038,mow.DE-SL-SLS-W038-20260901-000,2026-09-01T14:57:23-00:00",
```

## Reading

No one CAP field is the identity across these feeds. WMO's identifier is
stable, GDACS's changes every episode, and MeteoAlarm's holds for most of
Europe but not for France or Finland. Four Australian agencies on one national
profile need three keys between them. The key that holds an entity together in
NSW would mint one per update in Queensland. RFC §1.2 asks the integration for
some stable hash and leaves the derivation to the source.

## Caveats

- The NWS count is one national poll on 2026-08-06. The re-issue rate on other
  days is unmeasured.
- The MeteoFrance fixture is a reduced two-message pair built for the #37 test,
  not a verbatim feed capture. The identifier shape is the published one.
- The QLD figure comes from a 46 h sampler that has since been retired. WA was
  checked once, on 10 live alerts.
- The FMI figure is 23 warnings on one day. Finland has published no
  green marker in any sample, which is why its row carries no `keep` rule.
