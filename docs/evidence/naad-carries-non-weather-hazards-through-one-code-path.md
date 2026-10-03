# NAAD and BBK carry non-weather hazards through the weather code path

| | |
| :-- | :-- |
| Supports | RFC §4.1 (what belongs on `incident`) |
| Source | NAAD GeoRSS (ECCC provider) and BBK / NINA `warnings/{id}.json` |
| Sample | one live NAAD feed sample, one MoWaS document captured 2026-09-04 |
| Observed | 2026-07-22 (NAAD), 2026-09-04 (BBK fixture) |
| Reproduce | `tests/fixtures/bbk_warning_mowas.json`. The NAAD 911 record is in `naad-probe-2026-07-22.jsonl`, the rest of the sample is a one-time capture |

One NAAD sample, ingested through the ECCC provider with no hazard-specific
code (notes §4.1):

| Sender | `category` | `event` | `severity` |
| :-- | :-- | :-- | :-- |
| ECCC storm prediction centres | `Met` | Wind Warning, Squall Warning, air quality, … | Moderate to Severe |
| Manitoba Emergency Management Organization | `Infra` | `911 Service Inoperative` | Extreme |
| RCMP "E" Division | `Other` | `AMBER Alert` | Moderate |
| Calgary Police Service | `Other` | `AMBER Alert` (Alberta Emergency Alert) | Moderate |

All `status=Actual`, `scope=Public`. The 911 record also appears in the
committed probe log, where the legacy host carried it and alertready did not:

| Field in `naad-probe-2026-07-22.jsonl` | Value |
| :-- | :-- |
| `title` | `911 Service Disruption` |
| `event` | `911 Service Inoperative` |
| `severity` / `urgency` / `certainty` | `Extreme` / `Immediate` / `Observed` |
| First sample | 2026-07-22T19:58:37+00:00 |

BBK's channels, from `custom_components/cap_alerts/providers/bbk.py` lines 3
to 9: MoWaS, KATWARN, BIWAPP and the LHP flood portal, carrying evacuations,
hazmat, drinking-water contamination and utility outages, plus a DWD weather
relay. The wire format is CAP 1.2 serialized as JSON. A MoWaS drinking-water
notice, from `tests/fixtures/bbk_warning_mowas.json`:

```json
{
  "identifier": "mow.DE-SL-SLS-W038-20260904-000",
  "sender": "DE-SL-SLS-W038",
  "sent": "2026-09-04T13:05:28+02:00",
  "status": "Actual",
  "msgType": "Update",
  "scope": "Public",
  …
  "info": [
    {
      "language": "de",
      "category": [
        "Health"
      ],
      "event": "Gefahreninformation",
      "urgency": "Immediate",
      "severity": "Minor",
      "certainty": "Observed",
      …
      "headline": "1. AKTUALISIERUNG: Bakteriologische Beeinträchtigung des Trinkwassers - Chlorung - Biringen, Fürweiler, Gerlfangen und Oberesch - Gde. ReSi",
```

## Reading

The NAAD rows are CAP messages from a provincial emergency office and two
police services. They differ from a thunderstorm warning only in `category`
and `event`, and the same provider normalized all of them. The BBK document
is a municipal water notice, `category=Health`, parsed by the shared CAP
machinery after a JSON-to-`CAPDoc` conversion. §4.1's claim that the domain
is not a weather abstraction rests on these running paths.

## Caveats

- The NAAD sender attribution for the 911 and AMBER rows comes from the notes.
  The probe log records title, event and severity for the 911 record but no
  sender, and the AMBER alerts are not in it.
- One NAAD sample. How often non-weather traffic appears is not measured
  here. The [#38](https://github.com/seevee/cap_alerts/issues/38) comment of
  2026-08-14 lists further civil-emergency messages seen in the gap set.
- The BBK fixture is one document from one channel (MoWaS). KATWARN, BIWAPP
  and LHP documents are not captured in `tests/fixtures/`.
