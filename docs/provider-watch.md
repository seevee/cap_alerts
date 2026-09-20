# Keeping up with provider changes

Providers change their feeds, and not all of them announce it where we'd see
it. Issue #172 is the motivating case: ECCC launched Convective Alert
Modernization (freeform threat-area polygons on severe thunderstorm and
tornado warnings) on 2026-08-11, flagged it months earlier in a NAADS
governance summary, and the integration learned about it twelve days later
from a user report. Two watch mechanisms now exist, one automated and one
human.

## The automated half: the vocabulary probe

`scripts/feed_vocab_probe.py` fetches each provider's live feed, extracts its
structural vocabulary (element/key paths, geocode schemes, parameter keys,
and the small closed value sets like severity, lifecycle tokens, and the ECCC
DLC values), and diffs it against `scripts/feed_vocab_baseline.json`.
`.github/workflows/feed-vocab.yml` runs it daily and opens or appends to a
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

### WMO mirror lag

The same probe watches WMO for a different failure. The integration polls the
SWIC mirror, not the authority's own feed, and the mirror can silently stop
following a source: three Timor-Leste alerts issued over 2024-2025 never
reached it, and a user on `tl-dnmg-en` saw an empty feed, which looks exactly
like no warnings in force (#210). About 120 of the registry's language feeds
live on WMO's own `cap-sources` S3 bucket, one file per alert and publicly
listable, so the probe compares each mirror's newest item with the newest
`Actual` alert in its bucket and reports a mirror more than a week behind,
provided the authority has published in the last 90 days. A stall behind a
dormant source costs no one anything and would only be noise; it files the
day the source publishes again, since the token was never accepted. On
2026-09-19, the day it was written, 14 of the 93 mirrored feeds were behind
and one of them, Egypt's `eg-ema-en`, had a live authority; that one is the
seeded baseline, the rest are listed under the WMO section of
`docs/architecture.md`. The `mirror_lag` token carries the
date the mirror stopped (`tl-dnmg-en@2023-12-20`), so a mirror that recovers
and stalls again is new drift, and the witness is the newest alert the mirror
is missing.

Responding to a mirror-lag token is different from vocabulary drift, since
nothing in the integration can fix it:

1. Confirm it against the witness: the alert exists in the bucket and the
   mirror's feed for that source doesn't list it.
2. Report the source to SWIC (their contact is on the registry page each
   record links to). There is no formal channel; #210's reporter went
   through the SWIC site.
3. Add the source to the mirror-lag list under the WMO section of
   `docs/architecture.md`, so a user picking it can find out why it's empty.
4. Accept the token with `--update` as for vocabulary, and close the issue.

## The human half: announcement channels

| Provider | Channel | Cadence |
| --- | --- | --- |
| ECCC / MSC | [MSC open-data announcements (dd_info)](https://eccc-msc.github.io/open-data/comms_en/) mailing list | subscribe by email |
| NAAD (Pelmorex) | [NAAD governance council summaries](https://alerts.pelmorex.com/) | skim quarterly |
| NWS | [Service Change Notices](https://www.weather.gov/notification/) and the [weather-gov/api](https://github.com/weather-gov/api) repo discussions | subscribe / GitHub watch |
| MeteoAlarm | no formal channel; [meteoalarm.org](https://meteoalarm.org/) news | probe covers it |
| WMO SWIC | no formal channel; the [registry record](https://severeweather.wmo.int/v2/json/sources.json) names each authority's contact | probe covers mirror lag on the cap-sources feeds; vocabulary is announcement-only |
| GDACS | no formal channel; [gdacs.org](https://www.gdacs.org/) | probe covers the indexes |
| Australia (NSW RFS, QFD, DFES, TasALERT) | no formal channel; each agency's own site, and the [CAP-AU profile page](https://www.bom.gov.au/metadata/CAP-AU/About.shtml) for the national standard | probe covers all four feeds: envelope and alert paths, `AlertLevel` / `IncidentType` values, event codes, geocode schemes, marker-circle radii |
| BBK / NINA | no formal channel; [BBK NINA pages](https://www.bbk.bund.de/DE/Warnung-Vorsorge/Warn-App-NINA/warn-app-nina_node.html) and the [bund.dev API listing](https://bund.dev/apis) | probe covers the channel indexes, documents, `GROUP` and `DE-BBK-EVENTCODE` codes, id prefixes |

The dd_info list is the one that would have given months of lead time on CAM;
it is where MSC announces Datamart and CAP format changes. The NAADS council
summaries are low-volume but flagged both the alertready.ca migration and the
freeform-polygon rollout before they happened.
