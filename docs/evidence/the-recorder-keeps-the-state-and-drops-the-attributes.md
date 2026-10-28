# The recorder keeps the state and drops every attribute on overflow

| | |
| :-- | :-- |
| Supports | RFC §1.1 |
| Source | `homeassistant/components/recorder/db_schema.py`, Home Assistant 2026.9.3; [home-assistant/core#87105](https://github.com/home-assistant/core/pull/87105), which set the ceiling; a Home Assistant Discord search for `16384` |
| Sample | the code path, not a feed; 67 Discord search hits, of which 18 report the warning, 2023-02-25 to 2026-06-11 |
| Observed | 2026-10-03 (first checked against 2026.7.3 on 2026-08-07); #87105 and the Discord search 2026-10-27 |
| Reproduce | `grep -n "MAX_STATE_ATTRS_BYTES" .venv/lib/python*/site-packages/homeassistant/components/recorder/db_schema.py` |

```python
MAX_STATE_ATTRS_BYTES = 16384
…
    def shared_attrs_bytes_from_event(
        event: Event[EventStateChangedData],
        dialect: SupportedDialect | None,
    ) -> bytes:
        """Create shared_attrs from a state_changed event."""
        …
        else:
            exclude_attrs = ALL_DOMAIN_EXCLUDE_ATTRS
        encoder = json_bytes_strip_null if dialect == PSQL_DIALECT else json_bytes
        bytes_result = encoder(
            {k: v for k, v in state.attributes.items() if k not in exclude_attrs}
        )
        if len(bytes_result) > MAX_STATE_ATTRS_BYTES:
            _LOGGER.warning(
                "State attributes for %s exceed maximum size of %s bytes. "
                "This can cause database performance issues; Attributes "
                "will not be stored",
                state.entity_id,
                MAX_STATE_ATTRS_BYTES,
            )
            return b"{}"
        return bytes_result
```

| Step | What happens |
| :-- | :-- |
| Measure | `state.attributes` minus `ALL_DOMAIN_EXCLUDE_ATTRS` and the entity's own `unrecorded_attributes`, serialized with the recorder's JSON encoder |
| Over 16,384 bytes | one warning per state write, `{}` returned in place of the payload |
| State row | committed as normal, with the state value intact |
| History | keeps the state, loses every attribute, with nothing marking the row |

The ceiling was set by @bdraco in
[core#87105](https://github.com/home-assistant/core/pull/87105), merged
2023-02-02 and shipped as a breaking change. The PR says why, and why that
number:

> Some integrations (file download, torrent, etc) were storing 100000s of
> bytes of data in the attributes which would get the recorder in a
> overload failure state or run the system out of disk space.

> 16KiB was chosen because the maximum legitimate payload size was 1760
> bytes in all of the test data I have

The one review remark, from @balloob, was that the warning should not print
the attributes, "they are very big".

What reaches the ceiling in practice, from the Discord search. Each row is
one user's report of the warning in a log; where several users reported the
same source it is one row with a count:

| Source | Reports | First (UTC) | Last (UTC) | What the attribute held |
| :-- | --: | :-- | :-- | :-- |
| Android app `*_active_notification_count` | 7 | 2023-02-25 | 2025-08-10 | the phone's notification list |
| `watchman_missing_entities` | 2 | 2023-03-29 | 2024-03-30 | a list of entity ids |
| Octopus Energy `*_current_rate` | 2 | 2023-08-06 | 2023-08-10 | a tariff's rate table |
| `fedex_delivery` | 1 | 2023-04-28 | | package tracking |
| a REST dump, "the results of an REST call" | 1 | 2023-07-17 | | an upstream response |
| `camera.xiaomi_cloud_map_extractor` | 1 | 2023-08-06 | | a vacuum map |
| `sensor.1_bundesliga` | 1 | 2024-01-10 | | a league table |
| `select.zigbee2mqtt_bridge_log_level` | 1 | 2024-03-06 | | a bridge log |
| `predbat.load_power_best` | 1 | 2024-05-24 | | a battery plan's forecast series |
| EPEX Spot, 192 quarter-hour prices (20,160 chars) | 1 | 2025-12-09 | | a price series, and a template sensor copying it |
| ESPN sensors | 1 | 2026-06-11 | | fixtures |

The only remedy offered in the results, by a core member on 2024-01-10, was
"reducing the amount of data included in the state of the sensor below 16384
bytes". The EPEX report shows the two paths side by side: the
integration's own sensor carried the same 20,160 characters without a
warning, the template sensor copying them was dropped. An integration can
exclude attributes from recording (`unrecorded_attributes` in the Measure
row); a template sensor cannot, so the copy measures in full.

## Reading

The ceiling is not a write failure, which is why it goes unnoticed. A
packed-attribute alert sensor keeps its count in history and loses the alerts
the count was about.

The number was a safety stop, not a design limit: nine times the largest
legitimate payload its author had seen, aimed at integrations writing
hundreds of kilobytes. Three years on, what trips it is lists. Every source
in the table above packs a collection into one entity's attributes, which
is the shape §1.1 describes and the shape the entity-per-item binding
replaces. None of the reports is a weather alert, because the alert
integrations that would trip it are outside core and were not asked about
in the captured results. The measured set is smaller than `to_attributes()`
returns, which is what lets `payload.py` declare `parameters` and `geocodes`
unrecorded and take them out of the bound (§7.2).

## Caveats

- Line numbers move between releases. The constant and the warning text have
  been stable from 2026.7.3 to 2026.9.3.
- The PostgreSQL dialect strips nulls before measuring, so the same state can
  measure a few bytes smaller there.
- The Discord rows were copied by hand from a client search on 2026-10-27,
  with no permalinks. Dates are UTC, converted from the reader's display
  zone. The 49 hits not listed are other uses of the number: context
  windows, kernel logs, bit flags, a Thread partition limit.
- The table counts reports, not users or installs. The same user reported
  the Android app twice, a year apart.
- "Shipped as a breaking change" is the PR's own checkbox. The behavior is a
  warning plus a dropped payload, not an error.
