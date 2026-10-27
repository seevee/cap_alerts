# Keeping up with provider changes

Providers change their feeds, and not all of them announce it where we'd see
it. Issue #172 is the motivating case: ECCC launched Convective Alert
Modernization (freeform threat-area polygons on severe thunderstorm and
tornado warnings) on 2026-08-11, flagged it months earlier in a NAADS
governance summary, and the integration learned about it twelve days later
from a user report. Two watch mechanisms now exist, one automated and one
human.

## The automated half: the feed probe

`scripts/feed_vocab_probe.py` fetches each provider's live feed, extracts its
structural vocabulary (element/key paths, geocode schemes, parameter keys,
and the small closed value sets like severity, lifecycle tokens, and the ECCC
DLC values), and diffs it against `scripts/feed_vocab_baseline.json`.
`.github/workflows/provider-drift.yml` runs it daily and opens or appends to a
`provider-drift` issue when the feed publishes a token the baseline has never
seen. Daily because each feed shows only a window (NAAD keeps 48 hours, the
others show what is live now), and the earlier Monday-and-Thursday cadence
left a 48-hour hole every weekend in which a rare product was never sampled
(#184). NWS is the one feed with a historical query, so the probe reads the
last 48 hours of NWS alerts as well as the active set, and a product that
came and went between runs is still sampled there. A token stays drift until
its baseline PR merges, so a run whose report only repeats what is already on
the open issue stays quiet; a probe failure always posts. The CAM change
would have tripped it on day one: `layer:EC-MSC-SMC:DLC:1.1` was a new
geocode scheme, and ten new
`Storm_*`/`MSC_*` parameter keys came with it.

Responding to a drift issue:

1. Read the diff. A new geocode scheme, parameter key, or lifecycle value
   usually means provider behavior changed; a new element path can also mean
   the envelope or signature machinery moved. Each token lists the first few
   alert ids that carried it: open those documents while the feed still has
   them (NAAD keeps 48 hours; the NWS API answers historical queries).
2. Check the provider's announcement channel (below) for what shipped.
3. Decide whether the integration needs to react (a parser, filter, or
   convention-table change) and file that work as its own issue.
4. Accept the vocabulary: run `scripts/feed_vocab_probe.py --update` and PR
   the baseline diff. A token that has already aged out of the feed (a
   tornado warning's wireless-alerting parameters, #184) is invisible to
   `--update`; add it to the baseline by hand, the file is sorted JSON.
   Close the drift issue when the batch is dealt with.

The baseline only grows; a token absent from one run is never drift, so a
quiet week removes nothing.

Because every finding opens an issue, the probe is deliberately strict about
*which* value sets it watches. A set is tracked only when its full membership
is known up front (the CAP 1.2 enums, NWS's `/alerts/types` product catalog,
GDACS's seven hazard codes, MeteoAlarm's awareness codes) or when it is a
lifecycle vocabulary whose new token would change how alerts retire (ECCC
`Alert_Location_Status`, the DLC values). Sets that would fill in as weather
happens are not tracked at all: the first seeded baseline learned NWS event
names from one afternoon's live feed, and the first scheduled run filed #175
because a tornado warning had appeared. Structural vocabulary (element and
key paths, geocode schemes, ECCC parameter keys) is closed by nature and is
the main signal. Parameter keys are read from the provider's own alerts only:
the NAAD channel carries provincial EMOs and Amber alerts beside ECCC, and the
NWS active feed relays IPAWS traffic from local originators who name their
parameters however they like.

WMO vocabulary is not probed: it's per-configured-source RSS with no bounded
national endpoint. GDACS index structure is covered; its per-event GeoJSON is
not.

### WMO mirror retirement

The same probe watches WMO for a different thing. SWIC retired the per-source
RSS feeds on its mirror on 2026-10-08 and confirmed it on 2026-10-22: 138 of
140 ids answer 404, and only `cn-cma-xx` and `au-bom-en` still serve. The
integration reads the authority's own feed first under `auto` and keeps the
mirror in the union for those two (#333). The day SWIC retires one of them is
the day the mirror leg and the `mirror` feed source can come out, so the
probe asks both every run and reports a `mirror_retired` token once one
answers 404. Any other failure is a probe failure, not a retirement.

Responding to a `mirror_retired` token:

1. Confirm it stays down: fetch
   `https://severeweather.wmo.int/v2/cap-alerts/<id>/rss.xml` again later.
   A single 404 behind CloudFront has happened before during an origin outage.
2. Do #333's last step: remove the mirror leg and the `mirror` feed source,
   with a repairs issue for entries pinned to it.
3. Accept the token with `--update` as for vocabulary, and close the issue.

**History: mirror lag, 2026-09-19 to 2026-10-07.** Before the retirement the
probe compared each mirror with its source's `cap-sources` bucket, because a
mirror could silently stop following a source: three Timor-Leste alerts
issued over 2024-2025 never reached it, and a user on `tl-dnmg-en` saw an
empty feed, which looks exactly like no warnings in force (#210). About 120
language feeds lived on the bucket, one file per alert and publicly
listable, so the probe compared each mirror's newest item with the newest
`Actual` alert its bucket received at least a week ago, and reported a mirror
still missing it while the authority had published in the last 90 days. The
week was the mirror's grace: judged against the bucket's newest file instead,
the probe paged an hour after Senegal's `sn-anacim-fr` published its first
alert in four months (#238). It filed Senegal's real stall a week later
(#248), and lossy rather than stalled mirrors for Cameroon (#240), India
(#282) and Nigeria (#290); the measurements are under the WMO section of
`docs/architecture.md`. The lag tokens left the baseline when the check was
retired, since there is no mirror left to fall behind.

## The human half: announcement channels

| Provider | Channel | Cadence |
| --- | --- | --- |
| ECCC / MSC | [MSC open-data announcements (dd_info)](https://eccc-msc.github.io/open-data/comms_en/) mailing list | subscribe by email |
| NAAD (Pelmorex) | [NAAD governance council summaries](https://alerts.pelmorex.com/) | skim quarterly |
| NWS | [Service Change Notices](https://www.weather.gov/notification/) and the [weather-gov/api](https://github.com/weather-gov/api) repo discussions | subscribe / GitHub watch |
| MeteoAlarm | no formal channel; [meteoalarm.org](https://meteoalarm.org/) news | probe covers it |
| WMO SWIC | no formal channel; the [registry record](https://severeweather.wmo.int/v2/json/sources.json) names each authority's contact | probe watches the two mirror feeds SWIC still serves; vocabulary is announcement-only |
| GDACS | no formal channel; [gdacs.org](https://www.gdacs.org/) | probe covers the indexes |
| Australia (NSW RFS, QFD, DFES, TasALERT) | no formal channel; each agency's own site, and the [CAP-AU profile page](https://www.bom.gov.au/metadata/CAP-AU/About.shtml) for the national standard | probe covers all four feeds: envelope and alert paths, `AlertLevel` values, event codes, geocode schemes, marker-circle radii (`IncidentType` is tracked as a key only, its values are dispatch dictionaries) |
| GeoSphere Austria | no formal channel; the [Warn API spec](https://openapi.hub.geosphere.at/warnapi/v1/openapi.json) (version in `info.version`, 1.1.0 on 2026-10-27) | probe covers the national `getWarnstatus` index: paths, `wtype` / `wlevel` codes, the `warnid` shape; an empty index is a quiet day, not a failure |
| U.S. Tsunami Warning Centers | NWS [Service Change Notices](https://www.weather.gov/notification/) (tsunami product changes go out as SCNs) and [tsunami.gov](https://www.tsunami.gov/) | probe covers the twelve domestic product slots: which are issued, alert paths, event names, VTEC actions, parameter keys; the slots are undocumented, so a slot that stops answering is the change to watch for |
| BBK / NINA | no formal channel; [BBK NINA pages](https://www.bbk.bund.de/DE/Warnung-Vorsorge/Warn-App-NINA/warn-app-nina_node.html) and the [bund.dev API listing](https://bund.dev/apis) | probe covers the channel indexes, documents, `GROUP` codes, id prefixes |

The dd_info list is the one that would have given months of lead time on CAM;
it is where MSC announces Datamart and CAP format changes. The NAADS council
summaries are low-volume but flagged both the alertready.ca migration and the
freeform-polygon rollout before they happened.
