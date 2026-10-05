"""Alert entities carry the ``incident`` sensor device class (issue #277).

``tests/test_device_class_binding.py`` pins that core accepts the class. These
pin that the integration sets it, on the alert entities and nowhere else.
"""

from __future__ import annotations

import logging

import pytest
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.json import json_bytes
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.cap_alerts.const import DOMAIN, INCIDENT_DEVICE_CLASS
from custom_components.cap_alerts.payload import PAYLOAD_RESERVE
from tests.test_attribute_budget import FEED, _atom, _cap_xml

CAP_URL = "https://cap.example/alert.cap"
ALERT_PREFIX = "sensor.cap_alerts_eccc_cap_alert_"


async def _setup(hass: HomeAssistant, aioclient_mock, cap_xml: str) -> MockConfigEntry:
    aioclient_mock.get(FEED, text=_atom(CAP_URL))
    aioclient_mock.get(CAP_URL, text=cap_xml)
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="ECCC: Ontario",
        data={"provider": "eccc", "province": "ON"},
        options={"streaming": False, "scan_interval": 300},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _alert_state(hass: HomeAssistant) -> State:
    (state,) = [
        state
        for state in hass.states.async_all("sensor")
        if state.entity_id.startswith(ALERT_PREFIX)
    ]
    return state


@pytest.mark.asyncio
async def test_alert_entities_carry_the_incident_device_class(
    hass, aioclient_mock, enable_custom_integrations, caplog
):
    await _setup(hass, aioclient_mock, _cap_xml("Smoke.", "Fumee."))

    state = _alert_state(hass)
    assert state.state == "moderate"
    assert state.attributes["device_class"] == INCIDENT_DEVICE_CLASS
    registered = er.async_get(hass).async_get(state.entity_id)
    assert registered.original_device_class == INCIDENT_DEVICE_CLASS
    # The fixture feed draws provider warnings of its own. What matters is
    # that core says nothing about the entity.
    assert [
        r.getMessage()
        for r in caplog.records
        if r.levelno >= logging.WARNING
        and r.name.startswith(("homeassistant.components.", "homeassistant.helpers."))
    ] == []


@pytest.mark.asyncio
async def test_diagnostic_sensors_do_not_carry_it(
    hass, aioclient_mock, enable_custom_integrations
):
    await _setup(hass, aioclient_mock, _cap_xml("Smoke.", "Fumee."))

    marked = [
        state.entity_id
        for state in hass.states.async_all()
        if state.attributes.get("device_class") == INCIDENT_DEVICE_CLASS
    ]
    assert marked == [_alert_state(hass).entity_id]


@pytest.mark.asyncio
async def test_the_reserve_covers_what_home_assistant_appends(
    hass, aioclient_mock, enable_custom_integrations
):
    """The longest event normalization lets through, in ASCII."""
    cap_xml = _cap_xml("Smoke.", "Fumee.").replace(
        "<event>Air Quality Warning</event>", f"<event>{'E' * 255}</event>"
    )
    entry = await _setup(hass, aioclient_mock, cap_xml)

    state = _alert_state(hass)
    (alert,) = entry.runtime_data.data.values()
    published = set(alert.to_attributes()) | {"incident_platform_version"}
    appended = {k: v for k, v in state.attributes.items() if k not in published}
    assert set(appended) == {"friendly_name", "device_class"}
    assert len(json_bytes(appended)) <= PAYLOAD_RESERVE
