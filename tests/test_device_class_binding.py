"""Home Assistant behavior the sensor device-class binding rests on (RFC §1.5).

Nothing here exercises the integration. These pin what core does with a
``sensor`` entity marked by a custom device class, so a core release that
changes it fails here instead of going unnoticed in the RFC. The record is
``docs/evidence/core-accepts-a-custom-incident-device-class.md``.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

from homeassistant.components.sensor import DOMAIN as SENSOR_DOMAIN
from homeassistant.components.sensor import SensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import selector
from homeassistant.helpers.automation import DomainSpec
from homeassistant.helpers.trigger import Trigger, make_entity_target_state_trigger
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import (
    MockModule,
    async_capture_events,
    mock_integration,
    mock_platform,
)

DEVICE_CLASS = "incident"


class _Sensor(SensorEntity):
    _attr_should_poll = False

    def __init__(
        self,
        object_id: str,
        value: str,
        *,
        device_class: str | None = DEVICE_CLASS,
        options: list[str] | None = None,
        unique_id: str | None = None,
    ) -> None:
        self.entity_id = f"{SENSOR_DOMAIN}.{object_id}"
        self._attr_name = object_id
        self._attr_native_value = value
        self._attr_unique_id = unique_id
        if device_class is not None:
            self._attr_device_class = device_class  # type: ignore[assignment]
        if options is not None:
            self._attr_options = options


async def _add(hass: HomeAssistant, *entities: _Sensor) -> None:
    assert await async_setup_component(hass, SENSOR_DOMAIN, {})
    await hass.data[SENSOR_DOMAIN].async_add_entities(list(entities))
    await hass.async_block_till_done()


async def test_sensor_accepts_a_custom_device_class(hass, caplog):
    await _add(hass, _Sensor("tornado_warning", "severe", unique_id="t1"))

    state = hass.states.get("sensor.tornado_warning")
    assert state.state == "severe"
    assert state.attributes["device_class"] == DEVICE_CLASS
    entry = er.async_get(hass).async_get("sensor.tornado_warning")
    assert entry.original_device_class == DEVICE_CLASS
    assert [
        r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING
    ] == []


async def test_options_are_refused_without_the_enum_device_class(hass, caplog):
    await _add(hass, _Sensor("flood_watch", "minor", options=["minor", "severe"]))

    assert hass.states.get("sensor.flood_watch") is None
    assert "is providing enum options, but is missing the enum device class" in (
        caplog.text
    )


async def test_a_device_class_trigger_fires_for_the_marked_sensor_only(hass):
    triggers: dict[str, type[Trigger]] = {
        "became_severe": make_entity_target_state_trigger(
            {SENSOR_DOMAIN: DomainSpec(device_class=DEVICE_CLASS)},
            {"severe", "extreme"},
        )
    }

    async def async_get_triggers(hass: HomeAssistant) -> dict[str, type[Trigger]]:
        return triggers

    mock_integration(hass, MockModule("incident_probe"))
    mock_platform(
        hass,
        "incident_probe.trigger",
        SimpleNamespace(async_get_triggers=async_get_triggers),
    )
    marked = _Sensor("wind_warning", "minor")
    plain = _Sensor("plain", "minor", device_class=None)
    await _add(hass, marked, plain)
    fired = async_capture_events(hass, "probe_fired")
    assert await async_setup_component(
        hass,
        "automation",
        {
            "automation": {
                "trigger": {
                    "trigger": "incident_probe.became_severe",
                    "target": {"entity_id": [marked.entity_id, plain.entity_id]},
                },
                "action": {
                    "event": "probe_fired",
                    "event_data": {"entity_id": "{{ trigger.entity_id }}"},
                },
            }
        },
    )

    for entity in (marked, plain):
        entity._attr_native_value = "severe"
        entity.async_write_ha_state()
    await hass.async_block_till_done()

    assert [event.data["entity_id"] for event in fired] == ["sensor.wind_warning"]


def test_the_entity_selector_takes_the_device_class_filter():
    config = {"filter": {"domain": SENSOR_DOMAIN, "device_class": DEVICE_CLASS}}

    serialized = selector.EntitySelector(config).serialize()

    assert serialized["selector"]["entity"]["filter"] == [
        {"domain": [SENSOR_DOMAIN], "device_class": [DEVICE_CLASS]}
    ]
