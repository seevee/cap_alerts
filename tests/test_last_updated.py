"""The last-updated sensor stamps the last change to the alert set (issue #323).

Not the last poll: a poll that changes nothing is the absence of a cause to
Home Assistant, and a timestamp that moved on every one wrote a row per poll
per entry saying so. The store counts the events a reconciliation fired or
parked, the coordinator stamps when that count is non-zero, and the sensor
restores its value across a restart so a quiet entry is not ``unknown`` until
something happens.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
from homeassistant.core import State
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
    mock_restore_cache_with_extra_data,
)

from custom_components.cap_alerts.store import AlertStore

DOMAIN = "cap_alerts"
FEED = "https://rss.alertready.ca/"
LAST_UPDATED = "sensor.cap_alerts_eccc_last_updated"
SAVED = "2026-10-19T10:00:00+00:00"


# --- the store's count -----------------------------------------------------


@pytest.fixture
def bus():
    h = MagicMock()
    h.bus.async_fire = MagicMock()
    h.entity_registry.async_get_entity_id.return_value = None
    return h


def _store(bus) -> AlertStore:
    return AlertStore(
        "entry1",
        "nws",
        fire=bus.bus.async_fire,
        lookup_entity_id=lambda alert_id: bus.entity_registry.async_get_entity_id(
            "sensor", DOMAIN, f"entry1_nws_{alert_id}"
        ),
        defer_until_registered=True,
    )


def test_a_parked_creation_counts_for_the_cycle_that_saw_it(bus, alert_factory):
    """Parked is still this cycle's news; the release later is not."""
    store = _store(bus)

    store.process([alert_factory(id="a")])
    assert store.transitions == 1
    assert bus.bus.async_fire.call_count == 0

    # The next cycle releases it and changes nothing of its own.
    store.process([alert_factory(id="a")])
    assert bus.bus.async_fire.call_count == 1
    assert store.transitions == 0


def test_an_update_and_an_ending_count(bus, alert_factory):
    store = _store(bus)
    store.process([alert_factory(id="a")])

    store.process([alert_factory(id="a", headline="revised")])
    assert store.transitions == 1

    store.process([alert_factory(id="a", phase="cancel")])
    assert store.transitions == 1

    store.process([])
    assert store.transitions == 0


# --- the sensor, over Home Assistant -----------------------------------------


def _cap_xml(identifier: str) -> str:
    expires = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<alert xmlns="urn:oasis:names:tc:emergency:cap:1.2">'
        f"<identifier>{identifier}</identifier>"
        "<sender>CWTO</sender><sent>2026-10-20T12:00:00-00:00</sent>"
        "<status>Actual</status><msgType>Alert</msgType><scope>Public</scope>"
        "<info><language>en-CA</language><category>Met</category>"
        "<event>Wind Warning</event><urgency>Immediate</urgency>"
        "<severity>Moderate</severity><certainty>Likely</certainty>"
        f"<expires>{expires}</expires>"
        "<headline>Wind Warning in effect</headline><description>desc</description>"
        "<area><areaDesc>Ottawa</areaDesc>"
        "<polygon>45.0,-76.0 45.0,-75.5 45.5,-75.5 45.5,-76.0 45.0,-76.0</polygon>"
        "<geocode><valueName>profile:CAP-CP:Location:0.3</valueName>"
        "<value>3506008</value></geocode>"
        "</area></info></alert>"
    )


def _atom(*cap_urls: str) -> str:
    entries = "".join(
        (
            "<entry>"
            f"<id>atom-{i}</id><title>Warning</title>"
            '<category term="status=Actual"/>'
            "<georss:polygon>45.0 -76.0 45.0 -75.5 45.5 -75.5 45.5 -76.0 "
            "45.0 -76.0</georss:polygon>"
            f'<link type="application/cap+xml" href="{url}"/>'
            "</entry>"
        )
        for i, url in enumerate(cap_urls)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<feed xmlns="http://www.w3.org/2005/Atom" '
        'xmlns:georss="http://www.georss.org/georss">'
        f"{entries}</feed>"
    )


def _saved(hass) -> None:
    """What RestoreEntity kept of the sensor from before the restart."""
    mock_restore_cache_with_extra_data(
        hass,
        (
            (
                State(LAST_UPDATED, SAVED),
                {
                    "native_value": {
                        "__type": "<class 'datetime.datetime'>",
                        "isoformat": SAVED,
                    },
                    "native_unit_of_measurement": None,
                },
            ),
        ),
    )


async def _setup(hass) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"provider": "eccc", "province": "ON"},
        options={"streaming": False, "scan_interval": 300},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def _poll(hass, freezer) -> None:
    freezer.tick(timedelta(seconds=360))
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)


@pytest.mark.asyncio
async def test_unknown_until_something_happens_on_a_fresh_install(
    hass, aioclient_mock, enable_custom_integrations
):
    aioclient_mock.get(FEED, text=_atom())
    await _setup(hass)

    assert hass.states.get(LAST_UPDATED).state == "unknown"


@pytest.mark.asyncio
async def test_restored_across_a_restart_until_the_set_changes(
    hass, aioclient_mock, enable_custom_integrations, freezer
):
    _saved(hass)
    aioclient_mock.get(FEED, text=_atom())
    entry = await _setup(hass)
    assert hass.states.get(LAST_UPDATED).state == SAVED

    # A quiet poll keeps it; the diagnostics' poll time moves on its own.
    await _poll(hass, freezer)
    assert hass.states.get(LAST_UPDATED).state == SAVED
    assert entry.runtime_data.last_update_success_time > datetime.fromisoformat(SAVED)

    cap_a = "https://cap.example/a.cap"
    aioclient_mock.clear_requests()
    aioclient_mock.get(FEED, text=_atom(cap_a))
    aioclient_mock.get(cap_a, text=_cap_xml("urn:oid:A"))
    await _poll(hass, freezer)

    assert hass.states.get(LAST_UPDATED).state != SAVED
    assert entry.runtime_data.last_change_time > datetime.fromisoformat(SAVED)


@pytest.mark.asyncio
async def test_a_change_since_boot_outranks_the_saved_value(
    hass, aioclient_mock, enable_custom_integrations
):
    """The first refresh runs before the platform restores; its stamp is newer."""
    _saved(hass)
    cap_a = "https://cap.example/a.cap"
    aioclient_mock.get(FEED, text=_atom(cap_a))
    aioclient_mock.get(cap_a, text=_cap_xml("urn:oid:A"))
    entry = await _setup(hass)

    state = hass.states.get(LAST_UPDATED).state
    assert state != SAVED
    # The state is the stamp to the second.
    assert datetime.fromisoformat(state) == (
        entry.runtime_data.last_change_time.replace(microsecond=0)
    )
