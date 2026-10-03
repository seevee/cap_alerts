# Core accepts a custom incident device class on a sensor and triggers on it

| | |
| :-- | :-- |
| Supports | RFC §1.5 |
| Source | Home Assistant 2026.9.3: the `sensor` component, `helpers/automation.py`, `helpers/trigger.py`, the `battery` system integration |
| Sample | five checks in a test harness. One dev-box run on v0.6.1-rc.1 with 23 config entries, 113 alert entities loaded when queried. One throwaway instance from a copy of that config, driven through a browser, 192 alert entities |
| Observed | 2026-10-03 |
| Reproduce | `tests/test_device_class_binding.py`. The dev-box and browser runs are one-time captures |

| Check | Where | Result | Pinned by |
| :-- | :-- | :-- | :-- |
| A sensor with `device_class: incident` and a severity token as state | harness | added, attribute and registry both carry the class, nothing logged at warning or above | `test_sensor_accepts_a_custom_device_class` |
| The same with `options` declared | harness | not added, `ValueError` | `test_options_are_refused_without_the_enum_device_class` |
| A target-state trigger on `sensor` + `incident`, targeting a marked and an unmarked sensor, both set to `severe` | harness | fired once, for the marked sensor | `test_a_device_class_trigger_fires_for_the_marked_sensor_only` |
| Entity selector with `domain: sensor, device_class: incident` | harness | schema accepts it | `test_the_entity_selector_takes_the_device_class_filter` |
| An integration outside core hosts an `incident` entity domain, and a second one forwards a platform to it | harness | `incident.tornado_warning` in state `severe`, nothing logged at warning or above | `test_an_integration_outside_core_can_host_an_entity_domain` |
| The class set on the integration's real alert entities | dev box | 113 of 113 loaded alert entities carried it, no `cap_alerts` warning or error in the log | one-time capture |
| A trigger shipped from the integration's own `trigger.py` | dev box | validated and subscribed against live alert entities | one-time capture |
| The class as the frontend sees it | browser | 192 sensors carried `device_class: incident` in the browser's state | one-time capture |
| The trigger in the automation editor | browser | listed in the Add trigger dialog by name and description, adds, renders its form | one-time capture |
| The trigger's target picker | browser | offered alert sensors only. "count", "last updated" and "temperature" each returned "No target found" | one-time capture |
| An entity selector on a script field, `domain: sensor, device_class: incident` | browser, by hand | offered alert sensors from four providers. "alert cou" returned "No entities found" | one-time capture |
| A saved automation on that trigger, its target forced from `minor` to `severe` | throwaway instance | `last_triggered` went from none to 21:16:40Z | one-time capture |
| More-info on a marked sensor | browser | renders, state shown as the raw token `minor` | one-time capture |
| `weather_alerts_card` with the class present | browser | 34 cards on the same dashboard, rendered text identical in length to the dev box without the class | one-time capture |

The sensor component sets custom classes aside before it validates
(`components/sensor/__init__.py`):

```python
# For the sake of validation, we can ignore custom device classes
# (customization and legacy style translations)
device_class = try_parse_enum(SensorDeviceClass, self.device_class)
```

and refuses `options` on anything but its own `enum` class:

```
ValueError: Sensor sensor.flood_watch is providing enum options, but is missing the enum device class
```

Core already ships triggers keyed on a sensor device class from an integration
with no entity domain (`components/battery/trigger.py`, `integration_type:
system`):

```python
BATTERY_PERCENTAGE_DOMAIN_SPECS: dict[str, DomainSpec] = {
    SENSOR_DOMAIN: DomainSpec(device_class=SensorDeviceClass.BATTERY),
}
```

`DomainSpec.device_class` is typed `str | AnyDeviceClassType | None`, so the
probe's trigger is the same shape with a string:

```python
make_entity_target_state_trigger(
    {SENSOR_DOMAIN: DomainSpec(device_class="incident")}, {"severe", "extreme"}
)
```

The dev-box run, with one line added to `AlertEntity` and that trigger in a
`trigger.py` beside it:

```
states 581 alert entities 113 with device_class=incident 113
incident-class entities that are not alert entities: []
states of incident sensors: Counter({'minor': 70, 'moderate': 38, 'severe': 4, 'extreme': 1})
validate {'success': True, 'result': {'triggers': {'valid': True, 'error': None}}}
subscribe {'success': True, 'result': None}
registry sensor.cap_alerts_au_cap_alert_bushfire_77067869 original_device_class= incident
```

The browser rows ran against a second container on Home Assistant 2026.9.3,
started from a copy of the dev-box config without the recorder database. It
carried the same one-line class, the `trigger.py`, a `triggers.yaml` and
`triggers` strings so the editor would list the trigger, a script with one
entity-selector field, and one saved automation. Headless Chromium drove it,
except the script-field row.

## Reading

The per-incident entities of §2 can stay under `sensor` and still be told
apart: a device class is enough for a selector filter and for purpose-specific
triggers, and a system integration can own those triggers without owning a
domain. A custom integration can do this today under its own name. It could
also host an `incident` domain (the fifth row), but that would shadow a core
integration of that name. The price is the second row. A fixed severity vocabulary can't be
declared on the entity until sensor's validation learns the class.

## Caveats

- The browser rows are one run on one frontend build, the one 2026.9.3 ships.
  No test pins them.
- Outside the harness the trigger fired once, on a state forced through the
  REST API. No feed-driven severity change was observed firing it.
- The frontend shows the state as the raw token. Sensor ships no state
  translations for a class it doesn't know.
- The dev box was queried while entries were still loading. It held 260 alert
  entities once every entry was up.
- A custom class is tolerated, not sanctioned. `SensorEntity.device_class` is
  annotated `SensorDeviceClass | None`, and the comment above names
  customization and legacy translations as the reason for the tolerance.
- Two core versions: 2026.9.3 locally and on the dev box, and the 2026.4.3
  floor in CI, where the trigger config needs `options` passed explicitly. The
  test fails if a later release changes any of the five harness results.
