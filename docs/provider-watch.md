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
`.github/workflows/feed-vocab.yml` runs it Monday and Thursday and opens or
appends to a `provider-drift` issue when the feed publishes a token the
baseline has never seen. The CAM change would have tripped it on day one:
`layer:EC-MSC-SMC:DLC:1.1` was a new geocode scheme, and ten new
`Storm_*`/`MSC_*` parameter keys came with it.

Responding to a drift issue:

1. Read the diff. A new geocode scheme, parameter key, or lifecycle value
   usually means provider behavior changed; a new element path can also mean
   the envelope or signature machinery moved.
2. Check the provider's announcement channel (below) for what shipped.
3. Decide whether the integration needs to react (a parser, filter, or
   convention-table change) and file that work as its own issue.
4. Accept the vocabulary: run `scripts/feed_vocab_probe.py --update` and PR
   the baseline diff. Close the drift issue when the batch is dealt with.

The baseline only grows; a token absent from one run is never drift, so a
quiet week removes nothing. Expect some warm-up noise in the first weeks as
rare-but-routine vocabulary (an NWS event type not active at seed time, a
seasonal ECCC event) shows up once and gets folded in. The CAP 1.2 enums are
pre-seeded so spec-legal values never page.

WMO is not probed: it's per-configured-source RSS with no bounded national
endpoint. GDACS index structure is covered; its per-event GeoJSON is not.

## The human half: announcement channels

| Provider | Channel | Cadence |
| --- | --- | --- |
| ECCC / MSC | [MSC open-data announcements (dd_info)](https://eccc-msc.github.io/open-data/comms_en/) mailing list | subscribe by email |
| NAAD (Pelmorex) | [NAAD governance council summaries](https://alerts.pelmorex.com/) | skim quarterly |
| NWS | [Service Change Notices](https://www.weather.gov/notification/) and the [weather-gov/api](https://github.com/weather-gov/api) repo discussions | subscribe / GitHub watch |
| MeteoAlarm | no formal channel; [meteoalarm.org](https://meteoalarm.org/) news | probe covers it |
| WMO SWIC | no formal channel | announcement-only (no probe) |
| GDACS | no formal channel; [gdacs.org](https://www.gdacs.org/) | probe covers the indexes |

The dd_info list is the one that would have given months of lead time on CAM;
it is where MSC announces Datamart and CAP format changes. The NAADS council
summaries are low-volume but flagged both the alertready.ca migration and the
freeform-polygon rollout before they happened.
