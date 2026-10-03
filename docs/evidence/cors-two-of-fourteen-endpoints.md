# Two of fourteen CAP endpoints send Access-Control-Allow-Origin

| | |
| :-- | :-- |
| Supports | RFC §3.8 |
| Source | Every endpoint the reference implementation ingests, fetched with an `Origin: https://example.org` request header |
| Sample | 14 endpoints across 11 authorities, one GET each |
| Observed | 2026-08-08 (first seven rows), 2026-10-03 (GDACS, BBK and Australian rows) |
| Reproduce | `for u in <urls>; do echo "$u"; curl -s -o /dev/null -m 20 -H "Origin: https://example.org" -D - "$u" \| grep -i -E "^HTTP/\|access-control-allow-origin"; done` |

| Endpoint | Authority | HTTP | `Access-Control-Allow-Origin` |
| :-- | :-- | :-- | :-- |
| `api.weather.gov/alerts/active` | NWS (US) | 200 | `*` |
| `feeds.meteoalarm.org/api/v1/warnings/feeds-{country}` | MeteoAlarm / EUMETNET | 200 | *(none)* |
| `severeweather.wmo.int/v2/json/sources.json` | WMO SWIC | 200 | *(none)* |
| `severeweather.wmo.int/v2/cap-alerts/{source}/rss.xml` | WMO SWIC | 200 | *(none)* |
| `rss.alertready.ca` | Pelmorex / NAAD (CA) | 200 | *(none)* |
| `rss.naad-adna.pelmorex.com` | Pelmorex / NAAD (CA) | 200 | *(none)* |
| `cap.alertready.ca/{date}/{id}.xml` | Pelmorex / NAAD (CA) | 200 | *(none)* |
| `gdacs.org/xml/rss.xml`, `rss_24h.xml` | GDACS (JRC) | 200 | *(none)* |
| `warnung.bund.de/api31/dashboard/{ars}.json` | BBK / NINA (DE) | 200 | *(none)* |
| `rfs.nsw.gov.au/feeds/majorIncidentsCAP.xml` | NSW Rural Fire Service (AU) | 200 | `*` |
| `…s3.amazonaws.com/…/bushfireAlert_capau.xml` | Queensland Fire Department (AU) | 200 | *(none)* |
| `api.emergency.wa.gov.au/v1/capau` | DFES (AU) | 200 | *(none)* |
| `alert.tas.gov.au/data/cap-au.xml` | TasALERT (AU) | 200 | *(none)* |

The 2026-10-03 run, as printed:

```
https://www.gdacs.org/xml/rss.xml
   HTTP/1.1 200 OK
https://www.rfs.nsw.gov.au/feeds/majorIncidentsCAP.xml
   HTTP/2 200
   access-control-allow-origin: *
https://alert.tas.gov.au/data/cap-au.xml
   HTTP/2 200
https://warnung.bund.de/api31/dashboard/110000000000.json
   HTTP/2 200
https://publiccontent-gis-psba-qld-gov-au.s3.amazonaws.com/content/Feeds/BushfireCurrentIncidents/bushfireAlert_capau.xml
   HTTP/1.1 200 OK
https://api.emergency.wa.gov.au/v1/capau
   HTTP/2 200
```

## Reading

Every endpoint answers 200, and only NWS and NSW RFS opt in to cross-origin
reads. A page cannot read the other twelve without a server-side
intermediary, and in a Home Assistant deployment that intermediary is an
integration. The `cap.alertready.ca` row is where the CAP bodies live, so a
card willing to parse CAP 1.2 itself still cannot reach the documents. This
is the narrow claim §3.8 makes. The broader one, that a card-local fetch
reaches no recorder, state machine or automation, holds even where the header
is present.

## Caveats

- One GET per endpoint, on two dates two months apart. A CORS policy can
  change without notice.
- The GDACS row covers two files on one host, which is how 13 rows make 14
  endpoints.
- Only the 2026-10-03 transcript is reproduced. The 2026-08-08 probe left no
  committed output.
- Browsers cannot set `User-Agent`, a forbidden header under the Fetch spec, so
  a card could not send the contact identification NWS asks of API consumers
  even on the two readable endpoints. NWS serves the request either way.
- Responses were not inspected for `Vary` or preflight behavior. The probe
  checks the one header that decides a simple GET.
