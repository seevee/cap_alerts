"""The first fetch runs in the background at boot (#404).

Core leaves ``starting`` only once every entry's setup returns, so at boot an
entry restores, loads its platforms and returns, and fetches afterwards. The
entities stay unavailable until that fetch lands, a failure retries on a short
backoff instead of a failed setup's, and an ECCC stream starts only once a
fetch has seeded the active set. On a running instance the first fetch still
blocks setup (``test_coordinator_restore`` covers its failure).
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMockResponse,
)

from custom_components.cap_alerts.const import DOMAIN
from tests.test_activity_context import _bare_coordinator
from tests.test_coordinator_restore import FEED, _atom
from tests.test_coordinator_streaming import _count_id, _install_fake_stream


def _entry(hass: HomeAssistant, **options) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="ECCC: Ontario",
        data={"provider": "eccc", "province": "ON"},
        options={"scan_interval": 300, "feed_source": "alertready", **options},
    )
    entry.add_to_hass(hass)
    return entry


def _gate_feed(aioclient_mock) -> asyncio.Event:
    """Serve an empty feed, but only once the returned event is set."""
    gate = asyncio.Event()

    async def _held(method, url, data):
        await gate.wait()
        return AiohttpClientMockResponse(method, url, text=_atom())

    aioclient_mock.clear_requests()
    aioclient_mock.get(FEED, side_effect=_held)
    return gate


async def _boot(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    hass.set_state(CoreState.starting)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


@pytest.mark.asyncio
async def test_boot_setup_returns_before_the_first_fetch_lands(
    hass, aioclient_mock, enable_custom_integrations
):
    gate = _gate_feed(aioclient_mock)
    entry = _entry(hass, streaming=False)

    await _boot(hass, entry)

    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data.data is None
    count_id = _count_id(hass, entry)
    assert hass.states.get(count_id).state == STATE_UNAVAILABLE

    gate.set()
    await hass.async_block_till_done(wait_background_tasks=True)

    assert entry.runtime_data.data == {}
    assert hass.states.get(count_id).state == "0"


@pytest.mark.asyncio
async def test_a_failed_boot_fetch_loads_the_entry_and_retries_soon(
    hass, aioclient_mock, enable_custom_integrations
):
    aioclient_mock.get(FEED, status=500)
    entry = _entry(hass, streaming=False)

    await _boot(hass, entry)
    await hass.async_block_till_done(wait_background_tasks=True)

    # Loaded rather than SETUP_RETRY: there is no failed setup at boot.
    assert entry.state is ConfigEntryState.LOADED
    assert hass.states.get(_count_id(hass, entry)).state == STATE_UNAVAILABLE

    aioclient_mock.clear_requests()
    aioclient_mock.get(FEED, text=_atom())
    # The first retry is 5 s out, well inside the 300 s poll interval.
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=6))
    await hass.async_block_till_done(wait_background_tasks=True)

    assert hass.states.get(_count_id(hass, entry)).state == "0"


@pytest.mark.asyncio
async def test_the_stream_starts_once_a_boot_fetch_has_seeded_it(
    hass, aioclient_mock, enable_custom_integrations, monkeypatch
):
    holder = _install_fake_stream(monkeypatch)
    gate = _gate_feed(aioclient_mock)
    entry = _entry(hass, streaming=True)

    await _boot(hass, entry)
    assert "client" not in holder

    gate.set()
    # Not a background-task wait: the stream's run loop never ends.
    async with asyncio.timeout(5):
        while "client" not in holder:
            await asyncio.sleep(0.01)


@pytest.mark.parametrize(
    ("failures", "expected"), [(0, 5.0), (1, 10.0), (3, 40.0), (4, 80.0), (9, 80.0)]
)
def test_the_first_fetch_backoff_doubles_to_80_s(failures, expected):
    coordinator = _bare_coordinator()
    coordinator.data = None
    coordinator.update_interval = timedelta(seconds=300)
    coordinator._first_fetch_failures = failures

    assert coordinator._first_fetch_retry_after() == expected


def test_the_backoff_is_capped_at_the_poll_interval_and_ends_with_data():
    coordinator = _bare_coordinator()
    coordinator.data = None
    coordinator.update_interval = timedelta(seconds=30)
    coordinator._first_fetch_failures = 4
    assert coordinator._first_fetch_retry_after() == 30.0

    coordinator.data = {}
    assert coordinator._first_fetch_retry_after() is None
