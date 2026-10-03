# The frontend has no way to read an action result

| | |
| :-- | :-- |
| Supports | RFC §1.6 |
| Source | [home-assistant/discussions#655](https://github.com/orgs/home-assistant/discussions/655), [architecture#1357](https://github.com/home-assistant/architecture/discussions/1357), [architecture#1360](https://github.com/home-assistant/architecture/discussions/1360), `homeassistant/components/weather/websocket_api.py` in Home Assistant 2026.9.3 |
| Sample | 3 discussions and 1 source file |
| Observed | 2026-10-02 (discussions fetched with `gh api graphql`, file read from `.venv`) |
| Reproduce | `grep -n subscribe_forecast .venv/lib/python*/site-packages/homeassistant/components/weather/websocket_api.py` |

| Discussion | Author | Opened | Last edited | State | Comments |
| :-- | :-- | :-- | :-- | :-- | :-- |
| #655 provide a way for result variables to be displayed in a dashboard card | @hrabbach | 2025-08-14 | | open, unanswered, last activity 2026-01-22 | 2 threads |
| architecture#1357 Allow sensors to report forecast data | @jpbede | 2026-03-13 | 2026-03-15 | closed | 2 |
| architecture#1360 Introduce forecast providers for sensor entities | @jpbede | 2026-03-19 | 2026-07-21 | open | 6 |

| Date | Who | Quote | Link |
| :-- | :-- | :-- | :-- |
| 2025-08-14 | @hrabbach, #655 body | "there is no way to pass the result anywhere that can be displayed in a dashboard card" | [body](https://github.com/orgs/home-assistant/discussions/655) |
| 2025-08-14 | @karwosts, #655 | "that data needs to be attached to some entity in the state machine somehow, as that's the only real way for frontend to get the data" | [comment](https://github.com/home-assistant/feature-requests/discussions/655#discussioncomment-14107014) |
| 2026-03-13 | @jpbede, #1357 body | "Custom integrations often stuff long forecast arrays into state attributes, which is inefficient and hard to standardize." | [body](https://github.com/home-assistant/architecture/discussions/1357) |
| 2026-03-13 | @jpbede, #1357 body | "Keep forecast data out of state attributes and fetch on demand." | [body](https://github.com/home-assistant/architecture/discussions/1357) |
| 2026-03-13 | @jpbede, #1357 body | "The frontend has no unified way to retrieve and render sensor forecasts." | [body](https://github.com/home-assistant/architecture/discussions/1357) |
| 2026-03-19 | @jpbede, #1360 body | "The frontend has no unified way to retrieve and render forecasts for sensor values." | [body](https://github.com/home-assistant/architecture/discussions/1360) |
| 2026-03-19 | @jpbede, #1360 body | "Frontend and automation consumers should use `forecast.get_forecasts` or `forecast/get_forecasts` to retrieve forecast data instead of reading forecast values from attributes." | [body](https://github.com/home-assistant/architecture/discussions/1360) |

The weather precedent, in the installed core:

```
$ ls .venv/lib/python*/site-packages/homeassistant/components/weather/websocket_api.py
.venv/lib/python3.14/site-packages/homeassistant/components/weather/websocket_api.py
$ grep -n subscribe_forecast .venv/lib/python*/site-packages/homeassistant/components/weather/websocket_api.py
25:    websocket_api.async_register_command(hass, ws_subscribe_forecast)
46:        vol.Required("type"): "weather/subscribe_forecast",
52:async def ws_subscribe_forecast(
91:    connection.subscriptions[msg["id"]] = entity.async_subscribe_forecast(
```

## Reading

Two years of requests to surface an action result on a dashboard have no
answer, and the one core reply says the state machine is the only read path.
The architecture proposals that move forecasts out of attributes name the same
frontend gap in their first paragraph, and #1360 pairs its action with a
websocket command rather than leaving cards on the action. Weather already did
that: `weather/subscribe_forecast` is a subscription, not a call. §1.6 asks for
the same arrangement for anything that leaves an incident entity's attributes.

## Caveats

- Discussion bodies are editable. #1357 was last edited 2026-03-15 and #1360
  on 2026-07-21. The quotes are from the text as of 2026-10-02.
- #655 is an org-level discussion. GraphQL resolves it in the
  `home-assistant/feature-requests` repository, so permalinks carry that path.
- The notes say both architecture proposals were taken to the architecture
  meeting. That is not verifiable from the threads and is not claimed here.
- The grep is against HA 2026.9.3. Line numbers move between releases.
