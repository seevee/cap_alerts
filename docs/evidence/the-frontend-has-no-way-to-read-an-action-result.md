# The frontend has no way to read an action result

| | |
| :-- | :-- |
| Supports | RFC §1.6, §8.1 |
| Source | [home-assistant/discussions#655](https://github.com/orgs/home-assistant/discussions/655), [architecture#1357](https://github.com/home-assistant/architecture/discussions/1357), [architecture#1360](https://github.com/home-assistant/architecture/discussions/1360), `homeassistant/components/weather/websocket_api.py` in Home Assistant 2026.10.4 |
| Sample | 3 discussions and 1 source file |
| Observed | 2026-11-15 (discussions fetched with `gh api graphql`, replies included; file read from `.venv`). First read 2026-10-02 |
| Reproduce | `grep -n subscribe_forecast .venv/lib/python*/site-packages/homeassistant/components/weather/websocket_api.py` |

| Discussion | Author | Opened | Last edited | State | Comments |
| :-- | :-- | :-- | :-- | :-- | :-- |
| #655 provide a way for result variables to be displayed in a dashboard card | @hrabbach | 2025-08-14 | | open, unanswered, last activity 2026-01-22 | 2 threads |
| architecture#1357 Allow sensors to report forecast data | @jpbede | 2026-03-13 | 2026-03-15 | closed 2026-09-03, moved to #1360 | 2 threads |
| architecture#1360 Introduce forecast providers for sensor entities | @jpbede | 2026-03-19 | 2026-07-21 | open, its entity model ruled out 2026-10-14 | 6 threads |

| Date | Who | Quote | Link |
| :-- | :-- | :-- | :-- |
| 2025-08-14 | @hrabbach, #655 body | "there is no way to pass the result anywhere that can be displayed in a dashboard card" | [body](https://github.com/orgs/home-assistant/discussions/655) |
| 2025-08-14 | @karwosts, #655 | "that data needs to be attached to some entity in the state machine somehow, as that's the only real way for frontend to get the data" | [comment](https://github.com/home-assistant/feature-requests/discussions/655#discussioncomment-14107014) |
| 2025-08-14 | @karwosts, same comment | an attribute for it is "going against the current direction of development, trying to remove attributes and unnecessary data as possible from the state machine" | [comment](https://github.com/home-assistant/feature-requests/discussions/655#discussioncomment-14107014) |
| 2025-10-09 | @Petro31, #655 | "using attributes isn't a workaround, that has a much larger limit and does what you want." | [comment](https://github.com/home-assistant/feature-requests/discussions/655#discussioncomment-14638559) |
| 2026-03-13 | @jpbede, #1357 body | "Custom integrations often stuff long forecast arrays into state attributes, which is inefficient and hard to standardize." | [body](https://github.com/home-assistant/architecture/discussions/1357) |
| 2026-03-13 | @jpbede, #1357 body | "Keep forecast data out of state attributes and fetch on demand." | [body](https://github.com/home-assistant/architecture/discussions/1357) |
| 2026-03-13 | @jpbede, #1357 body | "The frontend has no unified way to retrieve and render sensor forecasts." | [body](https://github.com/home-assistant/architecture/discussions/1357) |
| 2026-03-19 | @jpbede, #1360 body | "The frontend has no unified way to retrieve and render forecasts for sensor values." | [body](https://github.com/home-assistant/architecture/discussions/1360) |
| 2026-03-19 | @jpbede, #1360 body | "Frontend and automation consumers should use `forecast.get_forecasts` or `forecast/get_forecasts` to retrieve forecast data instead of reading forecast values from attributes." | [body](https://github.com/home-assistant/architecture/discussions/1360) |
| 2026-03-19 | @MartinHjelmare, #1357 | "We've discussed this proposal in the architecture meeting." It "looks very solid" but "there is worry that this proposal is too rigid" for forecasts from a second provider | [comment](https://github.com/home-assistant/architecture/discussions/1357#discussioncomment-16209504) |
| 2026-07-18 | @gjohansson-ST, #1360 | "Similar to what we did for weather, we expose this to frontend so it can present it in `more-info`" | [comment](https://github.com/home-assistant/architecture/discussions/1360#discussioncomment-17681455) |
| 2026-07-20 | @Markus98, #1360 | "I think first party support for UI representation will be one of the most important aspects of this feature." | [comment](https://github.com/home-assistant/architecture/discussions/1360#discussioncomment-17698343) |
| 2026-07-20 | @Markus98, same comment | "Using state attributes for forecasts was a pattern that was moved away from in the `weather` integration, so I don't think this will be how this will get implemented." | [comment](https://github.com/home-assistant/architecture/discussions/1360#discussioncomment-17698343) |
| 2026-09-03 | @Markus98, #1360 | "We first need to settle the debate between this forecast entity model and other alternatives like a forecast registry." | [comment](https://github.com/home-assistant/architecture/discussions/1360#discussioncomment-18270480) |
| 2026-09-10 | @MartinHjelmare, #1360 | "A forecast isn't the same as a schedule. If we try to squeeze other features into the same box built for a specific feature, the result is rarely good." | [comment](https://github.com/home-assistant/architecture/discussions/1360#discussioncomment-18383074) |
| 2026-10-14 | @Markus98, #1360 | "The forecast entity model has been ruled out as an architecture candidate." A custom integration creating a forecast entity for every compatible sensor "could almost double the number of entities per HA instance". "Alternative approaches for the architecture are being explored." | [comment](https://github.com/home-assistant/architecture/discussions/1360#discussioncomment-18925587) |

The weather precedent, in the installed core:

```
$ ls .venv/lib/python*/site-packages/homeassistant/components/weather/websocket_api.py
.venv/lib/python3.14/site-packages/homeassistant/components/weather/websocket_api.py
$ grep -n subscribe_forecast .venv/lib/python*/site-packages/homeassistant/components/weather/websocket_api.py
25:    websocket_api.async_register_command(hass, ws_subscribe_forecast)
46:        probatio.Required("type"): "weather/subscribe_forecast",
54:async def ws_subscribe_forecast(
93:    connection.subscriptions[msg["id"]] = entity.async_subscribe_forecast(
```

## Reading

A request from August 2025 to surface an action result on a dashboard has no
answer, and the one core reply says the state machine is the only read path.
The same reply says an attribute for it runs against the direction of
development. Both halves are §1.6's problem: bodies are leaving attributes and
nothing a card can read has replaced them.

The forecast proposals name that gap in their first paragraph, and #1360 pairs
its action with a websocket command. Weather already did:
`weather/subscribe_forecast` is a subscription, not a call. Core members on
the thread treat the frontend half as part of the feature, not a follow-up.

What was ruled out on 2026-10-14 is the forecast entity, on entity count, and
not the read path. A forecast entity per compatible sensor is a permanent
multiplier on an instance's registry. An incident entity exists while its
alert does (§2.5), which is a different count, but the ruling shows where the
objection to §2 will come from. The replacement is not public yet. Whatever
it is, it has to answer the question §1.6 asks for incidents.

The schedule remark cuts against treating forecasts and incidents as one
problem with one mechanism, and for §8.1's point that a generic list is the
wrong box for a warning.

## Caveats

- Discussion bodies are editable. #1357 was last edited 2026-03-15 and #1360
  on 2026-07-21. The quotes are from the text as of 2026-11-15.
- #655 is an org-level discussion. GraphQL resolves it in the
  `home-assistant/feature-requests` repository, so permalinks carry that path.
- Both proposals went to the architecture meeting, by a member's comment on
  each thread (2026-03-19 on #1357, 2026-03-27 on #1360). The 2026-10-14
  ruling is reported on the thread by an organization member. No minutes or
  ADR are linked, and "alternatives" are not named beyond a forecast registry.
- GitHub nests discussion replies. A query for top-level comments misses
  them, and the ruling is a reply.
- The grep is against HA 2026.10.4. Line numbers move between releases.
