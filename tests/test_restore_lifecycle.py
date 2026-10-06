"""Restart restore on a real Home Assistant instance (issue #281).

``test_store_restore.py`` pins the store's seeding rules against a mocked hass.
These pin the wiring: setup restores before the first refresh, the coordinator
writes the live set when it changes and once more at unload, the file goes
with the entry, and a failed first fetch still retries setup without firing
anything. A "restart" here is an unload followed by a fresh setup of the same
entry; ``hass_storage`` survives it the way ``.storage/`` survives a reboot.
"""

from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import Event, HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.cap_alerts.const import (
    DOMAIN,
    EVENT_INCIDENT_CREATED,
    EVENT_INCIDENT_REMOVED,
    EVENT_INCIDENT_UPDATED,
    RESTORE_SAVE_DELAY,
)
from custom_components.cap_alerts.coordinator import AlertsDataUpdateCoordinator

FEED = "https://rss.alertready.ca/"
CAP_URL = "https://cap.example/restore.cap"
INCIDENT_EVENTS = (
    EVENT_INCIDENT_CREATED,
    EVENT_INCIDENT_UPDATED,
    EVENT_INCIDENT_REMOVED,
)


def _cap_xml(expires: datetime) -> str:
    """One Ontario warning with a polygon, expiring at ``expires``."""
    now = datetime.now(timezone.utc)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<alert xmlns="urn:oasis:names:tc:emergency:cap:1.2">'
        "<identifier>urn:oid:RESTORE</identifier>"
        f"<sender>CWTO</sender><sent>{(now - timedelta(hours=1)).isoformat()}</sent>"
        "<status>Actual</status><msgType>Alert</msgType><scope>Public</scope>"
        "<info><language>en-CA</language><category>Met</category>"
        "<event>Wind Warning</event><urgency>Immediate</urgency>"
        "<severity>Moderate</severity><certainty>Likely</certainty>"
        f"<expires>{expires.isoformat()}</expires>"
        "<headline>Wind Warning in effect</headline><description>desc</description>"
        "<area><areaDesc>Ottawa</areaDesc>"
        "<polygon>45.0,-76.0 45.0,-75.5 45.5,-75.5 45.5,-76.0 45.0,-76.0</polygon>"
        "<geocode><valueName>profile:CAP-CP:Location:0.3</valueName>"
        "<value>3506008</value></geocode>"
        "</area></info></alert>"
    )


def _atom(*cap_urls: str) -> str:
    entries = "".join(
        f"<entry><id>atom-{i}</id><title>Warning</title>"
        '<category term="status=Actual"/>'
        f'<link type="application/cap+xml" href="{url}"/></entry>'
        for i, url in enumerate(cap_urls)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<feed xmlns="http://www.w3.org/2005/Atom">{entries}</feed>'
    )


def _serve_alert(aioclient_mock, *, expires: datetime | None = None) -> None:
    aioclient_mock.clear_requests()
    aioclient_mock.get(FEED, text=_atom(CAP_URL))
    expires = expires or datetime.now(timezone.utc) + timedelta(days=1)
    aioclient_mock.get(CAP_URL, text=_cap_xml(expires))


def _serve_nothing(aioclient_mock) -> None:
    aioclient_mock.clear_requests()
    aioclient_mock.get(FEED, text=_atom())


def _serve_failure(aioclient_mock) -> None:
    aioclient_mock.clear_requests()
    aioclient_mock.get(FEED, status=500)


def _key(entry: MockConfigEntry) -> str:
    return f"{DOMAIN}.{entry.entry_id}"


def _record_events(hass: HomeAssistant) -> list[tuple[str, dict[str, Any]]]:
    seen: list[tuple[str, dict[str, Any]]] = []
    for event_type in INCIDENT_EVENTS:

        def _note(event: Event, event_type: str = event_type) -> None:
            seen.append((event_type, dict(event.data)))

        hass.bus.async_listen(event_type, _note)
    return seen


async def _setup(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="ECCC: Ontario",
        data={"provider": "eccc", "province": "ON"},
        options={
            "streaming": False,
            "scan_interval": 300,
            "feed_source": "alertready",
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def _flush(hass: HomeAssistant) -> None:
    async_fire_time_changed(
        hass, dt_util.utcnow() + timedelta(seconds=RESTORE_SAVE_DELAY + 1)
    )
    await hass.async_block_till_done()


async def _unload(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def _restart(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    await _unload(hass, entry)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


def _alert_entity_ids(hass: HomeAssistant, entry: MockConfigEntry) -> list[str]:
    prefix = f"{entry.entry_id}_eccc_"
    return [
        registered.entity_id
        for registered in er.async_entries_for_config_entry(
            er.async_get(hass), entry.entry_id
        )
        if registered.unique_id.startswith(prefix)
    ]


async def _setup_with_saved_alert(
    hass: HomeAssistant, aioclient_mock
) -> tuple[MockConfigEntry, str]:
    """An entry with one live alert, its restore file written. Returns the id."""
    _serve_alert(aioclient_mock)
    entry = await _setup(hass)
    await _flush(hass)
    (alert_id,) = entry.runtime_data.data
    return entry, alert_id


@pytest.mark.asyncio
async def test_a_live_alert_is_written_without_geometry(
    hass, aioclient_mock, enable_custom_integrations, hass_storage
):
    entry, alert_id = await _setup_with_saved_alert(hass, aioclient_mock)

    data = hass_storage[_key(entry)]["data"]
    (record,) = data["alerts"]
    assert record["id"] == alert_id
    assert "geometry" not in record
    assert record["geometry_ref"]
    assert data["scope_key"]
    assert data["confirmed_at"]


@pytest.mark.asyncio
async def test_reload_restores_the_alert_stale_then_the_fetch_clears_it(
    hass, aioclient_mock, enable_custom_integrations, hass_storage
):
    entry, _alert_id = await _setup_with_saved_alert(hass, aioclient_mock)
    events = _record_events(hass)

    await _restart(hass, entry)

    # The restored record was in the store before the first refresh, so the
    # refresh saw a known alert, not a new one.
    assert entry.runtime_data.restored_at_boot == 1
    assert [t for t, _ in events if t == EVENT_INCIDENT_CREATED] == []
    (entity_id,) = _alert_entity_ids(hass, entry)
    state = hass.states.get(entity_id)
    assert state.state == "moderate"
    assert "stale" not in state.attributes


@pytest.mark.asyncio
async def test_reload_into_an_absent_alert_retains_it_to_its_expiry(
    hass, aioclient_mock, enable_custom_integrations, hass_storage
):
    entry, _alert_id = await _setup_with_saved_alert(hass, aioclient_mock)
    events = _record_events(hass)
    _serve_nothing(aioclient_mock)

    await _restart(hass, entry)

    (entity_id,) = _alert_entity_ids(hass, entry)
    state = hass.states.get(entity_id)
    assert state is not None, "a restart into a feed gap removed a live alert"
    assert state.state == "moderate"
    assert state.attributes["stale"] is True
    assert state.attributes["last_confirmed"]
    assert [t for t, _ in events if t == EVENT_INCIDENT_REMOVED] == [], (
        "a restart into a feed gap fired a false all-clear; the restored "
        "alert should be retained to its expiry (issue #281)"
    )


@pytest.mark.asyncio
async def test_an_alert_expired_in_storage_is_removed_on_the_first_fetch(
    hass, aioclient_mock, enable_custom_integrations, hass_storage
):
    entry, alert_id = await _setup_with_saved_alert(hass, aioclient_mock)
    await _unload(hass, entry)
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    hass_storage[_key(entry)]["data"]["alerts"][0]["expires"] = past
    events = _record_events(hass)
    _serve_nothing(aioclient_mock)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.runtime_data.expired_at_boot == 1
    removed = [data for t, data in events if t == EVENT_INCIDENT_REMOVED]
    assert len(removed) == 1
    assert removed[0]["incident_id"] == alert_id
    assert removed[0]["phase"] == "expired"
    assert removed[0]["area_desc"] == "Ottawa"
    assert _alert_entity_ids(hass, entry) == []


@pytest.mark.asyncio
async def test_a_quiet_entry_writes_no_file(
    hass, aioclient_mock, enable_custom_integrations, hass_storage
):
    _serve_nothing(aioclient_mock)
    entry = await _setup(hass)
    await _flush(hass)

    assert _key(entry) not in hass_storage

    await _unload(hass, entry)

    assert _key(entry) not in hass_storage


@pytest.mark.asyncio
async def test_unload_saves_a_fresh_confirmed_at(
    hass, aioclient_mock, enable_custom_integrations, hass_storage
):
    entry, _alert_id = await _setup_with_saved_alert(hass, aioclient_mock)
    first = hass_storage[_key(entry)]["data"]["confirmed_at"]
    # An unchanged set schedules no write, so the file still says ``first``
    # after this refresh; only the unload brings it up to date.
    await entry.runtime_data.async_refresh()
    await _flush(hass)
    assert hass_storage[_key(entry)]["data"]["confirmed_at"] == first

    await _unload(hass, entry)

    assert hass_storage[_key(entry)]["data"]["confirmed_at"] > first


@pytest.mark.asyncio
async def test_home_assistant_stop_saves_a_fresh_confirmed_at(
    hass, aioclient_mock, enable_custom_integrations, hass_storage
):
    """Core does not unload entries at shutdown, so the stop event is the save."""
    entry, _alert_id = await _setup_with_saved_alert(hass, aioclient_mock)
    first = hass_storage[_key(entry)]["data"]["confirmed_at"]
    await entry.runtime_data.async_refresh()

    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await hass.async_block_till_done()

    assert hass_storage[_key(entry)]["data"]["confirmed_at"] > first


@pytest.mark.asyncio
async def test_removing_the_entry_removes_the_file(
    hass, aioclient_mock, enable_custom_integrations, hass_storage
):
    entry, _alert_id = await _setup_with_saved_alert(hass, aioclient_mock)
    assert _key(entry) in hass_storage

    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()

    assert _key(entry) not in hass_storage


@pytest.mark.asyncio
async def test_a_failed_first_fetch_still_retries_setup_and_fires_nothing(
    hass, aioclient_mock, enable_custom_integrations, hass_storage
):
    entry, _alert_id = await _setup_with_saved_alert(hass, aioclient_mock)
    await _unload(hass, entry)
    saved = copy.deepcopy(hass_storage[_key(entry)])
    events = _record_events(hass)
    _serve_failure(aioclient_mock)

    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert events == []
    assert hass_storage[_key(entry)] == saved


@pytest.mark.asyncio
async def test_a_scope_key_mismatch_suspends_retention_on_the_first_cycle(
    hass, aioclient_mock, enable_custom_integrations, hass_storage
):
    entry, alert_id = await _setup_with_saved_alert(hass, aioclient_mock)
    await _unload(hass, entry)
    hass_storage[_key(entry)]["data"]["scope_key"] = "a scope this entry never had"
    events = _record_events(hass)
    _serve_nothing(aioclient_mock)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert _alert_entity_ids(hass, entry) == []
    removed = [data for t, data in events if t == EVENT_INCIDENT_REMOVED]
    assert len(removed) == 1
    assert removed[0]["incident_id"] == alert_id
    assert removed[0]["phase"] == "cancel"
    assert removed[0]["area_desc"] == "Ottawa"


@pytest.mark.asyncio
async def test_a_coordinator_without_a_restore_store_persists_nothing():
    """The constructor default, which every directly built coordinator gets."""
    coordinator = object.__new__(AlertsDataUpdateCoordinator)
    coordinator._restore_store = None
    coordinator._last_reconciled_at = datetime.now(timezone.utc)
    coordinator.data = {}

    await coordinator.async_restore()
    await coordinator.async_save_restore_state()

    assert coordinator.data == {}
    assert coordinator.last_saved is None
