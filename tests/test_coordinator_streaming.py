"""End-to-end streaming ingestion for the ECCC provider.

Like ``test_mobile_lifecycle.py`` these run against a real Home Assistant test
instance (pytest-homeassistant-custom-component): real config entry, coordinator,
and sensor platform. The NAAD socket is replaced with a fake stream client that
captures the coordinator's callbacks, so the test drives live alert docs,
heartbeats, and reconnect backfills directly — no socket, HTTP mocked. This
pins the coordinator wiring: GeoRSS backfill seeds the set, a streamed doc
surfaces as an entity, a heartbeat rebuilds locally without a network fetch, and
a failed reconnect backfill does not flip availability (issue #16).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.helpers import entity_registry as er

DOMAIN = "cap_alerts"
FEED = "https://rss.alertready.ca/"


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _cap_xml(
    identifier: str,
    *,
    event: str = "Wind Warning",
    active: bool = True,
    references: str | None = None,
    sgc: str = "3506008",  # Ontario
    polygon: str = "45.0,-76.0 45.0,-75.5 45.5,-75.5 45.5,-76.0 45.0,-76.0",
    lang: str = "en-CA",
) -> str:
    """A minimal ECCC-style CAP 1.2 alert with clock-relative timestamps."""
    now = datetime.now(timezone.utc)
    expires = now + timedelta(days=1) if active else now - timedelta(hours=1)
    refs = f"<references>{references}</references>" if references else ""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<alert xmlns="urn:oasis:names:tc:emergency:cap:1.2">'
        f"<identifier>{identifier}</identifier>"
        "<sender>CWTO</sender>"
        f"<sent>{_iso(now - timedelta(hours=1))}</sent>"
        "<status>Actual</status><msgType>Alert</msgType><scope>Public</scope>"
        f"{refs}"
        "<info>"
        f"<language>{lang}</language><category>Met</category><event>{event}</event>"
        "<urgency>Immediate</urgency><severity>Moderate</severity>"
        f"<certainty>Likely</certainty><expires>{_iso(expires)}</expires>"
        f"<headline>{event} in effect</headline><description>desc</description>"
        "<area><areaDesc>Ottawa</areaDesc>"
        f"<polygon>{polygon}</polygon>"
        "<geocode><valueName>profile:CAP-CP:Location:0.3</valueName>"
        f"<value>{sgc}</value></geocode>"
        "</area></info></alert>"
    )


def _atom(*cap_urls: str) -> str:
    """A GeoRSS envelope with one Actual entry per CAP url (Ottawa polygon)."""
    entries = "".join(
        (
            "<entry>"
            f"<id>atom-{i}</id><title>Wind Warning</title>"
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


def _install_fake_stream(monkeypatch) -> dict:
    """Replace NAADStreamClient with a fake that captures the coordinator callbacks."""
    holder: dict = {}

    class _FakeStreamClient:
        def __init__(
            self,
            host,
            port,
            *,
            on_alert_doc,
            on_heartbeat,
            on_backfill_needed,
            **_kwargs,
        ) -> None:
            holder["on_alert_doc"] = on_alert_doc
            holder["on_heartbeat"] = on_heartbeat
            holder["on_backfill_needed"] = on_backfill_needed
            self._stopped = asyncio.Event()

        async def run(self) -> None:
            await self._stopped.wait()

        def stop(self) -> None:
            self._stopped.set()

    monkeypatch.setattr(
        "custom_components.cap_alerts.coordinator.NAADStreamClient",
        _FakeStreamClient,
    )
    return holder


def _count_id(hass, entry) -> str:
    ent_reg = er.async_get(hass)
    cid = ent_reg.async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_count")
    assert cid is not None
    return cid


async def _setup(hass) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="ECCC: Ontario",
        data={"provider": "eccc", "province": "ON"},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


@pytest.mark.asyncio
async def test_streaming_seeds_from_backfill_then_ingests_live(
    hass, aioclient_mock, enable_custom_integrations, monkeypatch
):
    holder = _install_fake_stream(monkeypatch)
    cap_a = "https://cap.example/a.cap"
    aioclient_mock.get(FEED, text=_atom(cap_a))
    aioclient_mock.get(cap_a, text=_cap_xml("urn:oid:A", event="Wind Warning"))

    entry = await _setup(hass)
    count_id = _count_id(hass, entry)

    # First refresh seeded the active set from the GeoRSS backfill.
    assert hass.states.get(count_id).state == "1"

    # A live streamed alert doc surfaces as a second entity within the poll.
    await holder["on_alert_doc"](_cap_xml("urn:oid:B", event="Rainfall Warning"))
    await hass.async_block_till_done()
    assert hass.states.get(count_id).state == "2"


@pytest.mark.asyncio
async def test_heartbeat_rebuilds_locally_without_fetch(
    hass, aioclient_mock, enable_custom_integrations, monkeypatch
):
    holder = _install_fake_stream(monkeypatch)
    cap_a = "https://cap.example/a.cap"
    aioclient_mock.get(FEED, text=_atom(cap_a))
    aioclient_mock.get(cap_a, text=_cap_xml("urn:oid:A"))

    entry = await _setup(hass)
    count_id = _count_id(hass, entry)
    assert hass.states.get(count_id).state == "1"

    calls_before = len(aioclient_mock.mock_calls)
    await holder["on_heartbeat"]()
    await hass.async_block_till_done()

    # Heartbeat is a local rebuild: active alert retained, no GeoRSS/CAP fetch.
    assert hass.states.get(count_id).state == "1"
    assert len(aioclient_mock.mock_calls) == calls_before


@pytest.mark.asyncio
async def test_reconnect_backfill_failure_keeps_entities_available(
    hass, aioclient_mock, enable_custom_integrations, monkeypatch
):
    holder = _install_fake_stream(monkeypatch)
    cap_a = "https://cap.example/a.cap"
    aioclient_mock.get(FEED, text=_atom(cap_a))
    aioclient_mock.get(cap_a, text=_cap_xml("urn:oid:A"))

    entry = await _setup(hass)
    count_id = _count_id(hass, entry)
    assert hass.states.get(count_id).state == "1"

    # A stream-triggered reconnect backfill that fails must not flip the entry
    # unavailable (issue #16: only the authoritative periodic backfill does).
    aioclient_mock.clear_requests()
    aioclient_mock.get(FEED, status=503)
    await holder["on_backfill_needed"]()
    await hass.async_block_till_done()

    state = hass.states.get(count_id).state
    assert state != STATE_UNAVAILABLE
    assert state == "1"
