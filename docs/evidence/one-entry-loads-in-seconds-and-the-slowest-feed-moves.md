# One entry loads in seconds, and across 27 the slowest feed changes every boot

| | |
| :-- | :-- |
| Supports | RFC §2.5 |
| Source | the dev instance's `home-assistant.log` at three restarts, with `homeassistant.setup: info` and `custom_components.cap_alerts.coordinator: debug`; `integration/setup_info` over the WebSocket; Home Assistant 2026.10.4, cap_alerts 0.8.0-rc.2 and rc.3 |
| Sample | 27 config entries across all ten providers, each read as one household's install. Three boots: 2026-11-01 17:53Z, 2026-11-02 07:04Z and 17:01Z |
| Observed | 2026-11-01 to 2026-11-02 |
| Reproduce | `scripts/startup_sample.py <home-assistant.log>` for any boot logged at those levels; `setup_info` from the WebSocket. The figures here are one-time captures |

What a user waits for is one entry: the time from core starting the
integration's setup to that entry's first successful fetch. Entries load
concurrently, so `setup_info`, the config-entry phase's wall clock, equals the
slowest entry's first fetch, not a sum.

| Entry, as one install | Boot 2 first fetch | Boot 3 first fetch | Alerts at boot 3 |
| :-- | --: | --: | --: |
| NWS, zone | 0.6 s | 0.8 s | 0 |
| NWS, GPS point | 0.7 s | 0.6 s | 0 |
| AU, NSW / WA / QLD / TAS | 0.8 to 1.4 s | 0.8 to 2.2 s | 19 / 5 / 25 / 0 |
| BBK, district | 0.8 s | 1.2 s | 0 |
| GeoSphere, GPS point | 1.2 s | 1.3 s | 0 |
| TWC, Hawaii | 1.0 s | 1.3 s | 0 |
| MeteoAlarm, French region | 1.6 s | 2.1 s | 0 |
| ECCC, province (ON / BC) | 8.3 / 10.4 s | 14.4 / 15.1 s | 2 / 4 |
| ECCC, GPS point (two) | 5.7 / 9.8 s | 4.7 / 13.0 s | 0 |
| GDACS, global and three GPS points | 17.8 to 18.0 s | 13.8 to 14.2 s | 4 (global) |
| WMO, eight authorities | 3.8 to 18.4 s | 9.2 to 32.4 s | 0 to 58 |
| GDACS / MeteoAlarm, device tracker | ready 26.7 / 30.6 s | ready 37.2 / 39.3 s | 1 / 0 |

| Boot | `setup_info` | Median first fetch | Slowest entry |
| :-- | --: | --: | :-- |
| 1 (rc.2) | 23.2 s | not kept | WMO Philippines (PAGASA), 23.2 s |
| 2 (rc.3) | 18.5 s | 5.7 s | WMO Philippines (PAGASA), 18.4 s |
| 3 (rc.3) | 32.5 s | 9.2 s | WMO China (CMA), 32.4 s |

## Reading

Ten of the 27 entries are ready within 2.5 s on every boot, and every
provider with one direct feed is among them. The middle is GDACS and ECCC.
The four GDACS entries finish together because they share one global
index, and ECCC reads two hosts' national feeds and filters them. The tail is
always WMO, but never the same authority: PAGASA links its CAP bodies on a raw
IP whose certificate fails, and CMA is the one source still read through the
SWIC mirror. An install with one entry waits for that entry, so the per-entry
rows are the user-shaped figure. The 18 to 33 s for all 27 is an upper bound
no real install reaches.

Tracker entries are beside `setup_info`, not in it. Their first fetch fails in
0.000 s while the tracker has no location yet, and they are ready only after
the retry, 27 to 39 s in, with fetches of 3.9 to 11.5 s once they run.

Core's own `Setup of domain cap_alerts took 0.00 seconds` is the line a user
would quote, and it measures the domain's `async_setup` only, before any entry.

## Caveats

- One box, one network, one laptop that sleeps. A feed's time is mostly its
  host's latency from here, so the order of the tail varies with the hosts,
  not the code.
- Boot 1's log has rotated away (Home Assistant keeps one previous log), so
  only its `setup_info` and slowest entries, noted at the time, survive.
- Every ECCC poll parsed its feeds and pre-filtered their envelopes on the event
  loop in these releases (#373), which can hold other entries' setup back by up
  to about a second here. The per-entry times include it.
- "Alerts at boot 3" is the count a few seconds after the boot, read as the
  fan-out each entry created, not a measured first-reconciliation figure.
