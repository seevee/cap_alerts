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
import ssl
from datetime import datetime, timedelta, timezone

import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
    async_fire_time_changed,
)

from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE
from homeassistant.helpers import entity_registry as er

DOMAIN = "cap_alerts"
FEED = "https://rss.alertready.ca/"
RESYNC_S = 1800  # DEFAULT_STREAM_RESYNC_INTERVAL
BACKFILL_FLOOR_S = 300  # NAAD_STREAM_BACKFILL_MIN_INTERVAL_S


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
    status: str = "Actual",
    msg_type: str = "Alert",
    sent_offset_h: int = 1,
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
        f"<sent>{_iso(now - timedelta(hours=sent_offset_h))}</sent>"
        f"<status>{status}</status><msgType>{msg_type}</msgType>"
        "<scope>Public</scope>"
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
            on_connection_change=None,
            **_kwargs,
        ) -> None:
            holder["on_alert_doc"] = on_alert_doc
            holder["on_heartbeat"] = on_heartbeat
            holder["on_backfill_needed"] = on_backfill_needed
            holder["on_connection_change"] = on_connection_change
            holder["kwargs"] = _kwargs
            holder["client"] = self
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


async def _setup(hass, **options) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="ECCC: Ontario",
        data={"provider": "eccc", "province": "ON"},
        options=options or None,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


@pytest.mark.asyncio
async def test_coordinator_injects_an_ssl_context_into_the_stream_client(
    hass, aioclient_mock, enable_custom_integrations, monkeypatch
):
    """The coordinator builds the TLS context and hands it to the client.

    Without this the client would fall back to building one inside
    ``_default_connect``; HA's blocking-call detector flags loading the CA
    bundle on the event loop. Asserting the context is *injected* keeps that
    work on the coordinator's executor hop.
    """
    holder = _install_fake_stream(monkeypatch)
    aioclient_mock.get(FEED, text=_atom())

    await _setup(hass)

    assert isinstance(holder["kwargs"].get("ssl_context"), ssl.SSLContext)


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
    hass, aioclient_mock, enable_custom_integrations, monkeypatch, freezer
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
    # Past the reconnect-backfill floor, so the fetch is actually attempted
    # rather than throttled against the one setup just ran.
    freezer.tick(timedelta(seconds=BACKFILL_FLOOR_S + 1))
    await holder["on_backfill_needed"]()
    await hass.async_block_till_done()

    state = hass.states.get(count_id).state
    assert state != STATE_UNAVAILABLE
    assert state == "1"


@pytest.mark.asyncio
async def test_reconnect_backfill_is_throttled_against_a_recent_one(
    hass, aioclient_mock, enable_custom_integrations, monkeypatch, freezer
):
    """A flapping socket must not pay a ~7 MB fetch per reconnect.

    Backoff only grows for connections that delivered nothing, so an endpoint
    that sends a heartbeat and then drops reconnects at the heartbeat cadence
    with the backoff at its floor. Unthrottled, that is a full GeoRSS fetch every
    ~60 s — more expensive than the 300 s polling this feature replaced.
    """
    holder = _install_fake_stream(monkeypatch)
    cap_a = "https://cap.example/a.cap"
    aioclient_mock.get(FEED, text=_atom(cap_a))
    aioclient_mock.get(cap_a, text=_cap_xml("urn:oid:A"))

    await _setup(hass)
    calls_before = len(aioclient_mock.mock_calls)

    # Four reconnects inside the floor, spread the way a heartbeat-then-drop
    # server would produce them.
    for _ in range(4):
        freezer.tick(timedelta(seconds=60))
        await holder["on_backfill_needed"]()
        await hass.async_block_till_done()
    assert len(aioclient_mock.mock_calls) == calls_before, "unthrottled reconnect fetch"

    # Past the floor, the next reconnect does resync.
    freezer.tick(timedelta(seconds=BACKFILL_FLOOR_S + 1))
    await holder["on_backfill_needed"]()
    await hass.async_block_till_done()
    assert len(aioclient_mock.mock_calls) > calls_before


@pytest.mark.asyncio
async def test_non_actual_doc_referencing_a_tracked_alert_is_not_retained(
    hass, aioclient_mock, enable_custom_integrations, monkeypatch
):
    """The references escape must not readmit test traffic.

    ``doc_matches_region`` rejects non-``Actual`` docs, but the escape bypasses it
    entirely — and a heartbeat's ``<references>`` lists recent alert OIDs, so a
    heartbeat that ever escaped classification would land in the live set once a
    minute until the 48 h prune.
    """
    holder = _install_fake_stream(monkeypatch)
    cap_a = "https://cap.example/a.cap"
    aioclient_mock.get(FEED, text=_atom(cap_a))
    aioclient_mock.get(cap_a, text=_cap_xml("urn:oid:A"))

    entry = await _setup(hass)
    coordinator = entry.runtime_data
    assert "urn:oid:A" in coordinator._live_docs

    await holder["on_alert_doc"](
        _cap_xml(
            "urn:oid:TEST",
            status="Test",
            references="CWTO,urn:oid:A,2026-07-22T12:00:00-00:00",
        )
    )
    await hass.async_block_till_done()

    assert "urn:oid:TEST" not in coordinator._live_docs
    assert hass.states.get(_count_id(hass, entry)).state == "1"


@pytest.mark.asyncio
async def test_heartbeat_does_not_advance_last_updated(
    hass, aioclient_mock, enable_custom_integrations, monkeypatch, freezer
):
    """ "Last updated" reports the last fetch, not the last local rebuild.

    Heartbeats run the full pipeline every ~60 s with no network I/O; stamping
    the timestamp there would show a fresh time while the authoritative GeoRSS
    backfill was up to 30 minutes stale.
    """
    holder = _install_fake_stream(monkeypatch)
    cap_a = "https://cap.example/a.cap"
    aioclient_mock.get(FEED, text=_atom(cap_a))
    aioclient_mock.get(cap_a, text=_cap_xml("urn:oid:A"))

    entry = await _setup(hass)
    last_updated_id = er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"{entry.entry_id}_last_updated"
    )
    seeded = hass.states.get(last_updated_id).state

    freezer.tick(timedelta(seconds=120))
    await holder["on_heartbeat"]()
    await hass.async_block_till_done()
    assert hass.states.get(last_updated_id).state == seeded

    # A backfill — a real fetch — does advance it. Driven directly rather than
    # through the resync timer, whose refresh runs as a background task that
    # ``async_block_till_done`` does not wait for.
    freezer.tick(timedelta(seconds=RESYNC_S))
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(last_updated_id).state != seeded


@pytest.mark.asyncio
async def test_resync_backfill_fires_despite_heartbeats(
    hass, aioclient_mock, enable_custom_integrations, monkeypatch, freezer
):
    """Heartbeats must not defer the safety resync.

    Regression: pushing stream updates through ``async_set_updated_data`` reset
    the coordinator's refresh timer, so a heartbeat every ~60 s meant the
    30-minute GeoRSS resync — the only authoritative backfill, and the only
    availability signal — never ran at all.
    """
    holder = _install_fake_stream(monkeypatch)
    cap_a = "https://cap.example/a.cap"
    aioclient_mock.get(FEED, text=_atom(cap_a))
    aioclient_mock.get(cap_a, text=_cap_xml("urn:oid:A"))

    await _setup(hass)
    calls_before = len(aioclient_mock.mock_calls)

    # Two heartbeats spread over 20 minutes — each would have pushed the resync
    # a fresh 30 minutes into the future.
    for _ in range(2):
        freezer.tick(timedelta(seconds=600))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
        await holder["on_heartbeat"]()
        await hass.async_block_till_done()
    assert len(aioclient_mock.mock_calls) == calls_before, "resync fired early"

    # Past the resync interval measured from setup, the backfill must run.
    freezer.tick(timedelta(seconds=RESYNC_S - 1200 + 60))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert len(aioclient_mock.mock_calls) > calls_before


@pytest.mark.asyncio
async def test_periodic_backfill_failure_flips_unavailable_and_heartbeat_does_not_restore(
    hass, aioclient_mock, enable_custom_integrations, monkeypatch, freezer
):
    """Only a backfill drives availability — a heartbeat must not paper over it."""
    holder = _install_fake_stream(monkeypatch)
    cap_a = "https://cap.example/a.cap"
    aioclient_mock.get(FEED, text=_atom(cap_a))
    aioclient_mock.get(cap_a, text=_cap_xml("urn:oid:A"))

    entry = await _setup(hass)
    count_id = _count_id(hass, entry)
    assert hass.states.get(count_id).state == "1"

    aioclient_mock.clear_requests()
    aioclient_mock.get(FEED, status=503)
    freezer.tick(timedelta(seconds=RESYNC_S + 60))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get(count_id).state == STATE_UNAVAILABLE

    # The stream is still alive, but a heartbeat is not evidence the data is good.
    await holder["on_heartbeat"]()
    await hass.async_block_till_done()
    assert hass.states.get(count_id).state == STATE_UNAVAILABLE


@pytest.mark.asyncio
async def test_streamed_test_message_does_not_surface(
    hass, aioclient_mock, enable_custom_integrations, monkeypatch
):
    """The socket carries the whole NAAD channel, tests and exercises included."""
    holder = _install_fake_stream(monkeypatch)
    cap_a = "https://cap.example/a.cap"
    aioclient_mock.get(FEED, text=_atom(cap_a))
    aioclient_mock.get(cap_a, text=_cap_xml("urn:oid:A"))

    entry = await _setup(hass)
    count_id = _count_id(hass, entry)
    assert hass.states.get(count_id).state == "1"

    for status in ("Test", "Exercise", "Draft"):
        await holder["on_alert_doc"](
            _cap_xml(f"urn:oid:{status}", event=f"{status} Warning", status=status)
        )
        await hass.async_block_till_done()
    assert hass.states.get(count_id).state == "1"


@pytest.mark.asyncio
async def test_out_of_region_streamed_doc_is_not_retained(
    hass, aioclient_mock, enable_custom_integrations, monkeypatch
):
    """A BC alert must not enter an Ontario entry's live set."""
    holder = _install_fake_stream(monkeypatch)
    cap_a = "https://cap.example/a.cap"
    aioclient_mock.get(FEED, text=_atom(cap_a))
    aioclient_mock.get(cap_a, text=_cap_xml("urn:oid:A"))

    entry = await _setup(hass)
    coordinator = entry.runtime_data
    count_id = _count_id(hass, entry)

    await holder["on_alert_doc"](
        _cap_xml("urn:oid:BC", event="Wind Warning", sgc="5915022")
    )
    await hass.async_block_till_done()

    assert hass.states.get(count_id).state == "1"
    assert "urn:oid:BC" not in coordinator._live_docs


@pytest.mark.asyncio
async def test_superseding_doc_is_retained_even_when_out_of_region(
    hass, aioclient_mock, enable_custom_integrations, monkeypatch
):
    """A revision that references a tracked alert is kept, so it can supersede it.

    Without this escape from the region filter, an update whose geometry moved
    off the user would be dropped and the superseded alert would linger active
    until it expired.
    """
    holder = _install_fake_stream(monkeypatch)
    cap_a = "https://cap.example/a.cap"
    aioclient_mock.get(FEED, text=_atom(cap_a))
    aioclient_mock.get(cap_a, text=_cap_xml("urn:oid:A"))

    entry = await _setup(hass)
    coordinator = entry.runtime_data
    count_id = _count_id(hass, entry)
    assert hass.states.get(count_id).state == "1"

    await holder["on_alert_doc"](
        _cap_xml(
            "urn:oid:A2",
            references="CWTO,urn:oid:A,2026-07-22T12:00:00-00:00",
            sgc="5915022",  # revised geometry is no longer in Ontario
            msg_type="Update",
            sent_offset_h=0,
        )
    )
    await hass.async_block_till_done()

    assert "urn:oid:A2" in coordinator._live_docs
    assert hass.states.get(count_id).state == "0"


@pytest.mark.asyncio
async def test_streamed_revision_fires_incident_updated(
    hass, aioclient_mock, enable_custom_integrations, monkeypatch
):
    """Supersession over the stream goes through AlertStore like a poll does."""
    holder = _install_fake_stream(monkeypatch)
    cap_a = "https://cap.example/a.cap"
    aioclient_mock.get(FEED, text=_atom(cap_a))
    aioclient_mock.get(cap_a, text=_cap_xml("urn:oid:A"))

    entry = await _setup(hass)
    count_id = _count_id(hass, entry)
    events = async_capture_events(hass, "incident_updated")

    await holder["on_alert_doc"](
        _cap_xml(
            "urn:oid:A2",
            event="Wind Warning",
            references="CWTO,urn:oid:A,2026-07-22T12:00:00-00:00",
            msg_type="Update",
            sent_offset_h=0,
        )
    )
    await hass.async_block_till_done()

    assert hass.states.get(count_id).state == "1"  # superseded, not duplicated
    assert len(events) == 1


def _ended_cap_xml(
    identifier: str, *, references: str, event: str = "Wind Warning"
) -> str:
    """A revision whose only area group has ended, ECCC-style.

    Termination is never visible through ``msgType`` — it stays ``Update``, and
    ``expires`` is still an hour out. The ``Alert_Location_Status`` parameter is
    the whole signal (issue #45).
    """
    now = datetime.now(timezone.utc)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<alert xmlns="urn:oasis:names:tc:emergency:cap:1.2">'
        f"<identifier>{identifier}</identifier>"
        "<sender>CWTO</sender>"
        f"<sent>{_iso(now)}</sent>"
        "<status>Actual</status><msgType>Update</msgType><scope>Public</scope>"
        f"<references>{references}</references>"
        "<info>"
        f"<language>en-CA</language><category>Met</category><event>{event}</event>"
        "<urgency>Past</urgency><severity>Minor</severity>"
        f"<certainty>Observed</certainty><expires>{_iso(now + timedelta(hours=1))}</expires>"
        f"<headline>{event} ended</headline><description>desc</description>"
        "<parameter>"
        "<valueName>layer:EC-MSC-SMC:1.1:Alert_Location_Status</valueName>"
        "<value>ended</value>"
        "</parameter>"
        "<area><areaDesc>Ottawa</areaDesc>"
        "<polygon>45.0,-76.0 45.0,-75.5 45.5,-75.5 45.5,-76.0 45.0,-76.0</polygon>"
        "<geocode><valueName>profile:CAP-CP:Location:0.3</valueName>"
        "<value>3506008</value></geocode>"
        "</area></info></alert>"
    )


@pytest.mark.asyncio
async def test_streamed_ended_document_removes_entity(
    hass, aioclient_mock, enable_custom_integrations, monkeypatch
):
    """An ended revision retires the entity instead of leaving it to expire.

    Issue #45 on the default transport: with ``msgType`` still ``Update`` and an
    hour left on ``expires``, the alert used to stay live — headline reading
    "ended" — until the clock caught up.
    """
    holder = _install_fake_stream(monkeypatch)
    cap_a = "https://cap.example/a.cap"
    aioclient_mock.get(FEED, text=_atom(cap_a))
    aioclient_mock.get(cap_a, text=_cap_xml("urn:oid:A"))

    entry = await _setup(hass)
    count_id = _count_id(hass, entry)
    assert hass.states.get(count_id).state == "1"
    removed = async_capture_events(hass, "incident_removed")

    await holder["on_alert_doc"](
        _ended_cap_xml(
            "urn:oid:A2", references="CWTO,urn:oid:A,2026-07-22T12:00:00-00:00"
        )
    )
    await hass.async_block_till_done()

    assert hass.states.get(count_id).state == "0"
    assert len(removed) == 1
    assert removed[0].data["phase"] == "expired"


def _stream_id(hass, entry) -> str | None:
    return er.async_get(hass).async_get_entity_id(
        "binary_sensor", DOMAIN, f"{entry.entry_id}_stream_connected"
    )


@pytest.mark.asyncio
async def test_connectivity_entity_tracks_the_socket(
    hass, aioclient_mock, enable_custom_integrations, monkeypatch
):
    """Socket up/down is surfaced as a connectivity binary_sensor.

    A "last stream event" timestamp cannot answer this: Canada is often quiet for
    hours, so an idle healthy socket and a dead one produce the same reading.
    """
    holder = _install_fake_stream(monkeypatch)
    cap_a = "https://cap.example/a.cap"
    aioclient_mock.get(FEED, text=_atom(cap_a))
    aioclient_mock.get(cap_a, text=_cap_xml("urn:oid:A"))

    entry = await _setup(hass)
    stream_id = _stream_id(hass, entry)
    assert stream_id is not None
    # Nothing has connected yet — the client is a stub that never dials.
    assert hass.states.get(stream_id).state == STATE_OFF

    holder["on_connection_change"](True)
    await hass.async_block_till_done()
    assert hass.states.get(stream_id).state == STATE_ON

    holder["on_connection_change"](False)
    await hass.async_block_till_done()
    assert hass.states.get(stream_id).state == STATE_OFF


@pytest.mark.asyncio
async def test_connectivity_entity_survives_a_failed_backfill(
    hass, aioclient_mock, enable_custom_integrations, monkeypatch, freezer
):
    """The socket's state must stay readable when the GeoRSS backfill is failing.

    That is the exact moment a user is trying to work out *which* half is broken,
    so this entity is deliberately not a CoordinatorEntity — that base would tie
    its availability to ``last_update_success``.
    """
    holder = _install_fake_stream(monkeypatch)
    cap_a = "https://cap.example/a.cap"
    aioclient_mock.get(FEED, text=_atom(cap_a))
    aioclient_mock.get(cap_a, text=_cap_xml("urn:oid:A"))

    entry = await _setup(hass)
    stream_id = _stream_id(hass, entry)
    count_id = _count_id(hass, entry)
    holder["on_connection_change"](True)
    await hass.async_block_till_done()

    aioclient_mock.clear_requests()
    aioclient_mock.get(FEED, status=503)
    freezer.tick(timedelta(seconds=RESYNC_S + 60))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    assert hass.states.get(count_id).state == STATE_UNAVAILABLE
    assert hass.states.get(stream_id).state == STATE_ON


@pytest.mark.asyncio
async def test_polling_entry_has_no_connectivity_entity(
    hass, aioclient_mock, enable_custom_integrations, monkeypatch
):
    """With streaming off there is no socket to report on."""
    _install_fake_stream(monkeypatch)
    cap_a = "https://cap.example/a.cap"
    aioclient_mock.get(FEED, text=_atom(cap_a))
    aioclient_mock.get(cap_a, text=_cap_xml("urn:oid:A"))

    entry = await _setup(hass, streaming=False)

    assert _stream_id(hass, entry) is None


@pytest.mark.asyncio
async def test_disabling_streaming_removes_a_stale_connectivity_entity(
    hass, aioclient_mock, enable_custom_integrations, monkeypatch
):
    """Turning streaming off cleans up the registry entry from the streaming run.

    Otherwise it would linger as a permanently unavailable orphan on the device
    page, with no socket behind it.
    """
    _install_fake_stream(monkeypatch)
    cap_a = "https://cap.example/a.cap"
    aioclient_mock.get(FEED, text=_atom(cap_a))
    aioclient_mock.get(cap_a, text=_cap_xml("urn:oid:A"))

    entry = await _setup(hass)
    assert _stream_id(hass, entry) is not None

    hass.config_entries.async_update_entry(entry, options={"streaming": False})
    await hass.async_block_till_done()

    assert _stream_id(hass, entry) is None


@pytest.mark.asyncio
async def test_unload_stops_the_stream_task(
    hass, aioclient_mock, enable_custom_integrations, monkeypatch
):
    """Unloading the entry must stop the client and leave no stream task behind."""
    holder = _install_fake_stream(monkeypatch)
    cap_a = "https://cap.example/a.cap"
    aioclient_mock.get(FEED, text=_atom(cap_a))
    aioclient_mock.get(cap_a, text=_cap_xml("urn:oid:A"))

    entry = await _setup(hass)
    coordinator = entry.runtime_data
    assert coordinator._stream_task is not None

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert holder["client"]._stopped.is_set()
    assert coordinator._stream_task is None
