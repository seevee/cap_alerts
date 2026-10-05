"""Home Assistant behavior the cost of ``RestoreEntity`` rests on (RFC §2.5).

Nothing here exercises the integration. These pin what core's restore-state
helper does with an entity that has been removed, so a core release that
changes it fails here instead of going unnoticed in the RFC. The record is
``docs/evidence/restoreentity-keeps-a-removed-alert-for-seven-days.md``.
"""

from __future__ import annotations

from datetime import timedelta

from homeassistant.components.sensor import DOMAIN as SENSOR_DOMAIN
from homeassistant.components.sensor import SensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers import restore_state
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.setup import async_setup_component

ENTITY_ID = f"{SENSOR_DOMAIN}.tornado_warning"


class _RestoredSensor(SensorEntity, RestoreEntity):
    _attr_should_poll = False

    def __init__(self) -> None:
        self.entity_id = ENTITY_ID
        self._attr_name = "tornado_warning"
        self._attr_native_value = "severe"
        self._attr_extra_state_attributes = {"description": "Take cover now."}


def _stored(hass: HomeAssistant) -> dict[str, restore_state.StoredState]:
    data = restore_state.async_get(hass)
    return {s.state.entity_id: s for s in data.async_get_stored_states()}


async def _add_then_remove(hass: HomeAssistant) -> None:
    assert await async_setup_component(hass, SENSOR_DOMAIN, {})
    entity = _RestoredSensor()
    await hass.data[SENSOR_DOMAIN].async_add_entities([entity])
    await hass.async_block_till_done()
    assert ENTITY_ID in _stored(hass)
    await entity.async_remove()
    await hass.async_block_till_done()
    assert hass.states.get(ENTITY_ID) is None


async def test_a_removed_entity_stays_in_the_dump_with_its_attributes(hass):
    await _add_then_remove(hass)

    kept = _stored(hass)[ENTITY_ID]
    assert kept.state.state == "severe"
    assert kept.state.attributes["description"] == "Take cover now."


async def test_a_removed_entity_leaves_the_dump_after_seven_days(hass, freezer):
    assert timedelta(days=7) == restore_state.STATE_EXPIRATION
    await _add_then_remove(hass)

    freezer.tick(restore_state.STATE_EXPIRATION - timedelta(minutes=1))
    assert ENTITY_ID in _stored(hass)
    freezer.tick(timedelta(minutes=2))
    assert ENTITY_ID not in _stored(hass)


def test_the_dump_runs_every_fifteen_minutes():
    assert timedelta(minutes=15) == restore_state.STATE_DUMP_INTERVAL
