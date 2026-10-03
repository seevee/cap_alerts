# Core accepts a custom incident device class on a sensor and triggers on it

| | |
| :-- | :-- |
| Supports | RFC §1.5 |
| Source | Home Assistant 2026.9.3: the `sensor` component, `helpers/automation.py`, `helpers/trigger.py`, the `battery` system integration |
| Sample | four checks in a test harness. One dev-box run on v0.6.1-rc.1 with 23 config entries, 113 alert entities loaded when queried |
| Observed | 2026-10-03 |
| Reproduce | `tests/test_device_class_binding.py`. The dev-box run is a one-time capture |

| Check | Where | Result | Pinned by |
| :-- | :-- | :-- | :-- |
| A sensor with `device_class: incident` and a severity token as state | harness | added, attribute and registry both carry the class, nothing logged at warning or above | `test_sensor_accepts_a_custom_device_class` |
| The same with `options` declared | harness | not added, `ValueError` | `test_options_are_refused_without_the_enum_device_class` |
| A target-state trigger on `sensor` + `incident`, targeting a marked and an unmarked sensor, both set to `severe` | harness | fired once, for the marked sensor | `test_a_device_class_trigger_fires_for_the_marked_sensor_only` |
| Entity selector with `domain: sensor, device_class: incident` | harness | schema accepts it | `test_the_entity_selector_takes_the_device_class_filter` |
| The class set on the integration's real alert entities | dev box | 113 of 113 loaded alert entities carried it, no `cap_alerts` warning or error in the log | one-time capture |
| A trigger shipped from the integration's own `trigger.py` | dev box | validated and subscribed against live alert entities | one-time capture |

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

## Reading

The per-incident entities of §2 can stay under `sensor` and still be told
apart: a device class is enough for a selector filter and for purpose-specific
triggers, and a system integration can own those triggers without owning a
domain. A custom integration can do this today, which the domain binding can't
offer. The price is the second row. A fixed severity vocabulary can't be
declared on the entity until sensor's validation learns the class.

## Caveats

- The frontend was not looked at. The selector check is the backend schema
  only, and filtering happens in the browser. The more-info dialog and card
  discovery with the class present are unchecked.
- The trigger fired in the harness only. On the dev box it was validated and
  subscribed, and no severity change was forced on live alerts.
- The dev box was queried while entries were still loading. It held 260 alert
  entities once every entry was up.
- A custom class is tolerated, not sanctioned. `SensorEntity.device_class` is
  annotated `SensorDeviceClass | None`, and the comment above names
  customization and legacy translations as the reason for the tolerance.
- Two core versions: 2026.9.3 locally and on the dev box, and the 2026.4.3
  floor in CI, where the trigger config needs `options` passed explicitly. The
  test fails if a later release changes any of the four harness results.
