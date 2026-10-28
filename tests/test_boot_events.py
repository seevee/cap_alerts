"""Events from the first fetch at boot wait for Home Assistant to start (#361).

Core arms automation triggers in a startup job just before
``homeassistant_started``, and the first refresh runs inside setup, well before
that. The coordinator holds what it fires until then and releases it in order;
the sensor platform keeps a removed alert's entity registered until the release,
so a device, area or label trigger still tracks the entity the event names.

A boot here is an unload, the core state set back to ``starting``, and a fresh
setup of the same entry; the start is the state set to ``running`` and
``homeassistant_started`` fired, which is what ``async_at_started`` waits on.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CoreState, Event, HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.cap_alerts.const import (
    EVENT_INCIDENT_CREATED,
    EVENT_INCIDENT_REMOVED,
)
from tests.test_activity_context import _bare_coordinator
from tests.test_coordinator_restore import (
    EXPIRES,
    FEED,
    _alert_entity_ids,
    _atom,
    _cap_xml,
    _key,
    _record_events,
    _serve_nothing,
    _setup_with_saved_alert,
    _unload,
)
from tests.test_trigger import _arm, _device_target

NEW_CAP_URL = "https://cap.example/arrival.cap"


async def _boot(hass: HomeAssistant, entry) -> None:
    """Set the entry up the way a restart does: before Home Assistant has started."""
    hass.set_state(CoreState.starting)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def _start(hass: HomeAssistant) -> None:
    hass.set_state(CoreState.running)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done()


def _expire_record(hass_storage, entry, alert_id: str) -> None:
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    hass_storage[_key(entry)]["data"]["alerts"][alert_id]["expires"] = past


@pytest.mark.asyncio
async def test_a_boot_removal_and_arrival_fire_after_start_in_order(
    hass, aioclient_mock, enable_custom_integrations, hass_storage
):
    entry, alert_id = await _setup_with_saved_alert(hass, aioclient_mock)
    (old_entity_id,) = _alert_entity_ids(hass, entry)
    await _unload(hass, entry)
    _expire_record(hass_storage, entry, alert_id)
    events = _record_events(hass)
    # The saved alert is gone from the feed; a different one arrived.
    aioclient_mock.clear_requests()
    aioclient_mock.get(FEED, text=_atom(NEW_CAP_URL))
    aioclient_mock.get(
        NEW_CAP_URL,
        # ECCC keys an alert on ``sent`` and the polygon, not the identifier.
        text=_cap_xml(EXPIRES)
        .replace("urn:oid:RESTORE", "urn:oid:ARRIVAL")
        .replace("2026-10-27T10:00:00", "2026-10-27T11:00:00"),
    )

    # What a listener finds in the registry when the removal reaches it: a
    # device, area or label trigger reads the same registry (#361). A release
    # inside the started dispatch delivered it after the removal applied.
    registered_on_delivery: list[bool] = []

    def _check_registry(event: Event) -> None:
        entity_id = event.data["entity_id"]
        registered_on_delivery.append(
            er.async_get(hass).async_get(entity_id) is not None
        )

    hass.bus.async_listen(EVENT_INCIDENT_REMOVED, _check_registry)

    await _boot(hass, entry)

    assert events == []
    assert entry.runtime_data.holding_events
    # Held removal names an entity that has to outlive it.
    assert er.async_get(hass).async_get(old_entity_id) is not None

    await _start(hass)

    assert [t for t, _ in events] == [EVENT_INCIDENT_REMOVED, EVENT_INCIDENT_CREATED]
    removed, created = (data for _, data in events)
    assert removed["incident_id"] == alert_id
    assert removed["entity_id"] == old_entity_id
    assert removed["severity"] == "moderate"
    assert registered_on_delivery == [True]
    assert created["incident_id"] != alert_id
    assert not entry.runtime_data.holding_events
    assert er.async_get(hass).async_get(old_entity_id) is None
    assert old_entity_id not in _alert_entity_ids(hass, entry)


@pytest.mark.asyncio
async def test_a_device_trigger_fires_on_the_held_removal(
    hass, aioclient_mock, enable_custom_integrations, hass_storage
):
    """The soak case: a restored removal reaches an automation armed at start."""
    entry, alert_id = await _setup_with_saved_alert(hass, aioclient_mock)
    (entity_id,) = _alert_entity_ids(hass, entry)
    target = _device_target(hass, entry)
    await _unload(hass, entry)
    fired = await _arm(hass, "incident_ended", target, threshold="moderate")
    _expire_record(hass_storage, entry, alert_id)
    _serve_nothing(aioclient_mock)

    await _boot(hass, entry)
    assert fired == []

    await _start(hass)

    assert [e.data["entity_id"] for e in fired] == [entity_id]
    assert fired[0].data["severity"] == "moderate"


@pytest.mark.asyncio
async def test_an_unload_before_start_drops_what_was_held(
    hass, aioclient_mock, enable_custom_integrations, hass_storage
):
    entry, alert_id = await _setup_with_saved_alert(hass, aioclient_mock)
    await _unload(hass, entry)
    _expire_record(hass_storage, entry, alert_id)
    _serve_nothing(aioclient_mock)
    await _boot(hass, entry)
    events = _record_events(hass)

    await _unload(hass, entry)
    await _start(hass)

    assert events == []


@pytest.mark.asyncio
async def test_a_setup_while_running_fires_at_once(
    hass, aioclient_mock, enable_custom_integrations, hass_storage
):
    """A reload, or an entry added after start: nothing to wait for."""
    entry, alert_id = await _setup_with_saved_alert(hass, aioclient_mock)
    await _unload(hass, entry)
    _expire_record(hass_storage, entry, alert_id)
    _serve_nothing(aioclient_mock)
    events = _record_events(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert [t for t, _ in events] == [EVENT_INCIDENT_REMOVED]
    assert not entry.runtime_data.holding_events
    assert _alert_entity_ids(hass, entry) == []


def test_a_job_registered_twice_runs_once():
    coordinator = _bare_coordinator()
    coordinator.hass.state = CoreState.starting
    coordinator.config_entry = MagicMock()
    runs: list[int] = []

    def job() -> None:
        runs.append(1)

    coordinator._async_fire_event("incident_removed", {"incident_id": "a"})
    coordinator.async_after_held_events(job)
    coordinator.async_after_held_events(job)
    coordinator.hass.bus.async_fire.assert_not_called()

    coordinator._async_fire_held_events()

    coordinator.hass.bus.async_fire.assert_called_once()
    assert runs == [1]
    coordinator.async_after_held_events(job)
    assert runs == [1, 1]


def test_an_event_while_stopping_fires_at_once():
    """``started`` never follows ``stopping``, so nothing waits for it."""
    coordinator = _bare_coordinator()
    coordinator.hass.state = CoreState.stopping
    coordinator.config_entry = MagicMock()

    coordinator._async_fire_event("incident_removed", {"incident_id": "a"})

    coordinator.hass.bus.async_fire.assert_called_once()
    assert not coordinator.holding_events
