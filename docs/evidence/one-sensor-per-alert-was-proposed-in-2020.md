# One sensor per alert was proposed in core in 2020

| | |
| :-- | :-- |
| Supports | RFC §3.3, §8.1 |
| Source | [home-assistant/core#37415](https://github.com/home-assistant/core/pull/37415), [#100009](https://github.com/home-assistant/core/pull/100009), [#103352](https://github.com/home-assistant/core/issues/103352), [#150737](https://github.com/home-assistant/core/issues/150737), [#163096](https://github.com/home-assistant/core/pull/163096) |
| Sample | 2 PRs, 2 issues and 1 fix PR, 2020-07-03 to 2026-02-25 |
| Observed | 2026-10-02 (threads fetched with `gh api`, thread dates as GitHub records them) |
| Reproduce | `timeout 30 gh api repos/home-assistant/core/issues/37415/comments --paginate` and the same for `pulls/37415/comments` |

| Thread | Author | Opened | Closed | Outcome |
| :-- | :-- | :-- | :-- | :-- |
| #37415 Add alert sensor platform to NWS | @MatthewFlamm | 2020-07-03 | 2020-07-24 | closed unmerged, `geo_location` conversion judged too complex |
| #100009 Add support for OpenWeatherMap national weather alerts | @IceBotYT | 2023-09-09 | 2023-12-15 | closed unmerged, stale after `CHANGES_REQUESTED` |
| #103352 DWD Weather warning status doesn't reset after warning end | @jckoester | 2023-11-04 | 2023-11-13 | closed, "root cause is the DWD server", resolved upstream |
| #150737 DWD Weather warning status doesn't reset after warning end (again) | @tribut | 2025-08-16 | 2026-02-25 | closed by PR #163096, "Filter expired warnings", merged 2026-02-25 |

| Date | Who | Quote | Link |
| :-- | :-- | :-- | :-- |
| 2020-07-09 | @cgarwood, #37415 | "All other integrations have stored them in the attributes of a single sensor entity like this PR does, but that's always felt like an ugly hack or workaround." | [comment](https://github.com/home-assistant/core/pull/37415#issuecomment-655828991) |
| 2020-07-09 | @cgarwood, #37415 | "Only other thought I have had would be making each alert be its own sensor? `sensor.nws_alert_NWS-IDP-PROD-4306023-3615559` etc." | [comment](https://github.com/home-assistant/core/pull/37415#issuecomment-655828991) |
| 2020-07-10 | @MatthewFlamm, #37415 | "For example it is common that one alert will replace another." | [comment](https://github.com/home-assistant/core/pull/37415#issuecomment-656661327) |
| 2020-07-10 | @MatthewFlamm, #37415 | "Some entries like `references` might be useful when an alert updates a previous alert." | [comment](https://github.com/home-assistant/core/pull/37415#issuecomment-656661327) |
| 2020-07-22 | @MartinHjelmare, #37415 | "Could we make a geo_location platform that creates an entity for each geocode in the alert and calculates the distance from the user to that geocode?" | [review comment](https://github.com/home-assistant/core/pull/37415#discussion_r458706001) |
| 2020-07-24 | @MatthewFlamm, #37415 | "Closing this, as converting to geo_location platform is complex." | [comment](https://github.com/home-assistant/core/pull/37415#issuecomment-663530453) |
| 2023-09-12 | @edenhaus, #100009 | "Storing all the alerts with their data in the attributes will blow up the state machine. We can leave the number of alerts as sensor value, but we should get the alert data via a service call, similar to the weather forecast." | [review comment](https://github.com/home-assistant/core/pull/100009#discussion_r1322946406) |
| 2023-09-18 | @MatthewFlamm, `nws` code owner, #100009 | "The geo_location platform was suggested as was a custom event. But I wasn't that satisfied in either case with the solution at that time, so decided not to pursue it." | [comment](https://github.com/home-assistant/core/pull/100009#issuecomment-1724541839) |
| 2023-11-04 | @jckoester, #103352 body | "This way I have a weather warning active although it was over two days ago." | [body](https://github.com/home-assistant/core/issues/103352) |
| 2023-11-04 | @stephan192, #103352 | "Neither the weather warnings integration nor the used dwdwfsapi do have any plausibility checking implemented, so the warnings are processed and reported like all other warnings." | [comment](https://github.com/home-assistant/core/issues/103352#issuecomment-1793440167) |
| 2025-08-16 | @tribut, #150737 body | "the returned data clearly includes `"EXPIRES":"2025-08-15T17:00:00Z"` which both is easily machine parseable and expresses an explicit intent." | [body](https://github.com/home-assistant/core/issues/150737) |

## Reading

The conclusions in §2 were reached in a core thread six years earlier. A core
contributor called the packed sensor a hack and floated one sensor per alert
in the same comment. The `nws` author named replacement as the common case and
`references` as the lifecycle link. The thread ended on `geo_location`, the
only per-item platform core had, and the author found neither it nor a custom
event satisfactory in 2023. The DWD pair shows requirement 6: an upstream
`EXPIRES` was published, exposed as an attribute, and not used to end the
warning until 2026.

## Caveats

- The "ugly hack" line is @cgarwood's, not the PR author's. The notes say "a
  reviewer", which holds.
- #103352 was closed as resolved on the DWD side in 2023. The same symptom
  returned in 2025 and was fixed in core. Both are quoted, the first as the
  symptom and the second as the cause.
- #163096's description first promised an opt-in default and then struck that
  through, shipping as a breaking change. The option's final shape was not
  checked in the diff.
- #100009 went stale and was closed by the bot. The `CHANGES_REQUESTED` review
  (@gjohansson-ST, 2023-09-10) was about code structure, the attribute remark
  came from a second reviewer.
