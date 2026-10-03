# MeteoAlarm shows one alert when there are several

| | |
| :-- | :-- |
| Supports | RFC §3.3, §1.4 requirement 4 |
| Source | [home-assistant/core#108908](https://github.com/home-assistant/core/issues/108908), [#131045](https://github.com/home-assistant/core/issues/131045), [#156838](https://github.com/home-assistant/core/issues/156838), [#103132](https://github.com/home-assistant/core/issues/103132), community topics [393707](https://community.home-assistant.io/t/meteoalarm-multiple-alerts/393707) and [120069](https://community.home-assistant.io/t/meteoalarm-integration-not-working/120069) |
| Sample | 4 core issues and 2 forum topics (19 posts), 2019-06-04 to 2026-07-01 |
| Observed | 2026-10-02 (issues fetched with `gh api`, topics fetched as `.json`) |
| Reproduce | `timeout 30 gh api repos/home-assistant/core/issues/156838` and `timeout 60 curl -sL -A HomeAssistant-CAPAlerts/probe https://community.home-assistant.io/t/meteoalarm-multiple-alerts/393707.json` |

| Issue | Opened | State | Closed | Locked | Symptom |
| :-- | :-- | :-- | :-- | :-- | :-- |
| #108908 Meteoalarm integration showing only one alert, however should be several | 2024-01-26 | closed, not planned | 2024-05-02, stale bot | yes | one alert shown while feeds.meteoalarm.org reports several |
| #131045 MeteoAlarm integration does not display all active warnings (e.g., Wind Warning Missing) | 2024-11-20 | closed, not planned | 2025-02-25, stale bot | yes | Noord-Holland snow-ice warning shown, wind warning missing |
| #156838 MeteoAlarm integration does not display all active warnings (e.g., Wind Warning Missing) | 2025-11-18 | closed, not planned | 2026-07-01, stale bot | yes | re-filed #131045, "Only one of the three active alerts is displayed", "Same issue in the UK" |
| #103132 Missing Unique ID in Meteoallarm enities | 2023-10-31 | open | | no | no `unique_id`, so no registry entry, groups or tags, 40 comments |

| Topic | Created | Last post | Posts | Participants | Countries named in posts |
| :-- | :-- | :-- | :-- | :-- | :-- |
| 120069 Meteoalarm integration not working | 2019-06-04 | 2023-09-21 | 14 | 12 | France, Denmark, UK, Belgium, Slovakia, Switzerland, Austria, Italy |
| 393707 Meteoalarm - multiple alerts | 2022-02-17 | 2026-04-23 | 5 | 5 | none |

| Date | Who | Quote | Link |
| :-- | :-- | :-- | :-- |
| 2020-01-20 | Tomahawk, topic 120069 | "the sensor only deliver one alarm for a location, when there are several alarms for that location. Is this a bug, or has Meteoalarm made changes and the sensor has not kept up? Sometimes the sensor pics up the last, other days, the middle one." | [post](https://community.home-assistant.io/t/meteoalarm-integration-not-working/120069) |
| 2022-02-17 | stShark, topic 393707 | "It often happens that there are several alerts for a set area at one time, alternatively, it is the same warning in two stages." | [post](https://community.home-assistant.io/t/meteoalarm-multiple-alerts/393707) |
| 2024-01-26 | @andrejgorin, #108908 | "I noticed it does show only one alert; however, I saw several situations when https://feeds.meteoalarm.org reported several alerts." | [body](https://github.com/home-assistant/core/issues/108908) |
| 2025-10-08 | tinwetari, topic 393707 | "it is extremely common that there will be more than one and that they will have different starting and ending times" | [post](https://community.home-assistant.io/t/meteoalarm-multiple-alerts/393707) |
| 2026-03-26 | @jankomb, #156838 | "Only one of the three active alerts is displayed. As a result, the integration fails to serve its full purpose." | [comment](https://github.com/home-assistant/core/issues/156838#issuecomment-4132359024) |

## Reading

The same symptom was filed in core three times in three winters and closed by
the stale bot each time, with no maintainer comment on any of them. The forum
reported it in 2020 and again in 2022. Nine countries appear across the six
threads: the eight in topic 120069 plus the Netherlands in #131045 and
#156838. A single `binary_sensor` holding one alert cannot satisfy requirement
4, and the missing `unique_id` keeps it outside the entity registry as well.

## Caveats

- Topic 120069 mixes two complaints, feed outages in 2019 and the one-alert
  limit from 2020 on. Only the latter is quoted here.
- Country attribution is from the YAML snippets and self-descriptions in the
  posts, not from profile data.
- Discourse post permalinks are not stable across edits. The topic URL is given
  and the post is identified by date and username.
- #103132 is a registry defect, not a dropout. It is in the table because the
  RFC cites it alongside the dropouts in §8.2.
