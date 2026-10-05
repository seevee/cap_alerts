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
from homeassistant.config_entries import ConfigFlow
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import selector
from homeassistant.helpers.automation import DomainSpec
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_component import EntityComponent
from homeassistant.helpers.trigger import Trigger, make_entity_target_state_trigger
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    MockModule,
    MockPlatform,
    async_capture_events,
    mock_config_flow,
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
                    # Required on the 2026.4 floor, defaulted on later releases.
                    "options": {},
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


async def test_a_device_class_trigger_does_not_fire_for_a_sensor_added_severe(hass):
    """The trigger needs a previous state, and a new entity has none.

    So the device class alone gives escalation and not arrival: an incident
    that is created severe fires nothing.
    """
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
    registry = er.async_get(hass)
    label = "incidents"
    # A label, so the target can name an entity before it has a state.
    existing = _Sensor("wind_warning", "minor", unique_id="w1")
    arriving = _Sensor("tornado_warning", "severe", unique_id="t1")
    for entity in (existing, arriving):
        entry = registry.async_get_or_create(
            SENSOR_DOMAIN,
            SENSOR_DOMAIN,
            entity.unique_id,
            suggested_object_id=entity.name,
        )
        registry.async_update_entity(entry.entity_id, labels={label})
    await _add(hass, existing)
    fired = async_capture_events(hass, "probe_fired")
    assert await async_setup_component(
        hass,
        "automation",
        {
            "automation": {
                "trigger": {
                    "trigger": "incident_probe.became_severe",
                    "target": {"label_id": label},
                    "options": {},
                },
                "action": {
                    "event": "probe_fired",
                    "event_data": {"entity_id": "{{ trigger.entity_id }}"},
                },
            }
        },
    )

    await hass.data[SENSOR_DOMAIN].async_add_entities([arriving])
    await hass.async_block_till_done()
    assert hass.states.get("sensor.tornado_warning").state == "severe"
    assert fired == []

    # The same automation does fire for the labeled sensor that escalates.
    existing._attr_native_value = "severe"
    existing.async_write_ha_state()
    await hass.async_block_till_done()
    assert [event.data["entity_id"] for event in fired] == ["sensor.wind_warning"]


def test_the_entity_selector_takes_the_device_class_filter():
    config = {"filter": {"domain": SENSOR_DOMAIN, "device_class": DEVICE_CLASS}}

    serialized = selector.EntitySelector(config).serialize()

    assert serialized["selector"]["entity"]["filter"] == [
        {"domain": [SENSOR_DOMAIN], "device_class": [DEVICE_CLASS]}
    ]


async def test_an_integration_outside_core_can_host_an_entity_domain(hass, caplog):
    """A hub can own ``incident.*`` and let other integrations forward to it."""
    domain = "incident"

    class _Incident(Entity):
        _attr_should_poll = False
        _attr_has_entity_name = True
        _attr_name = "Tornado warning"
        _attr_unique_id = "t1"

        @property
        def state(self) -> str:
            return "severe"

    async def hub_setup(hass: HomeAssistant, config: dict) -> bool:
        hass.data[domain] = EntityComponent(logging.getLogger(domain), domain, hass)
        return True

    async def hub_setup_entry(hass: HomeAssistant, entry: MockConfigEntry) -> bool:
        return await hass.data[domain].async_setup_entry(entry)

    async def provider_setup_entry(hass: HomeAssistant, entry: MockConfigEntry) -> bool:
        await hass.config_entries.async_forward_entry_setups(entry, [domain])
        return True

    async def platform_setup_entry(hass, entry, async_add_entities) -> None:
        async_add_entities([_Incident()])

    mock_integration(
        hass,
        MockModule(domain, async_setup=hub_setup, async_setup_entry=hub_setup_entry),
    )
    mock_integration(
        hass, MockModule("incident_provider", async_setup_entry=provider_setup_entry)
    )
    mock_platform(
        hass,
        f"incident_provider.{domain}",
        MockPlatform(async_setup_entry=platform_setup_entry),
    )
    mock_platform(hass, "incident_provider.config_flow", None)

    class _Flow(ConfigFlow):
        pass

    with mock_config_flow("incident_provider", _Flow):
        entry = MockConfigEntry(domain="incident_provider")
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert hass.states.get("incident.tornado_warning").state == "severe"
    assert [
        r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING
    ] == []
