"""The per-entry Refresh button.

Pressing it forces an off-cycle provider fetch — for ECCC in streaming mode that
is the GeoRSS backfill which otherwise only runs on the safety-resync interval.
Runs against a real Home Assistant test instance so platform wiring, entity
registration, and press handling are all exercised. Uses a polling (non-
streaming) ECCC entry, since the button is provider- and mode-agnostic.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from homeassistant.components.button import DOMAIN as BUTTON_DOMAIN, SERVICE_PRESS
from homeassistant.const import ATTR_ENTITY_ID, EntityCategory, STATE_UNAVAILABLE
from homeassistant.helpers import entity_registry as er

DOMAIN = "cap_alerts"
FEED = "https://rss.alertready.ca/"


def _cap_xml(identifier: str, event: str) -> str:
    now = datetime.now(timezone.utc)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<alert xmlns="urn:oasis:names:tc:emergency:cap:1.2">'
        f"<identifier>{identifier}</identifier>"
        f"<sender>CWTO</sender><sent>{(now - timedelta(hours=1)).isoformat()}</sent>"
        "<status>Actual</status><msgType>Alert</msgType><scope>Public</scope>"
        "<info><language>en-CA</language><category>Met</category>"
        f"<event>{event}</event><urgency>Immediate</urgency>"
        "<severity>Moderate</severity><certainty>Likely</certainty>"
        f"<expires>{(now + timedelta(days=1)).isoformat()}</expires>"
        f"<headline>{event} in effect</headline><description>desc</description>"
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


async def _setup(hass) -> MockConfigEntry:
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


def _entity_id(hass, entry, suffix: str, platform: str) -> str:
    eid = er.async_get(hass).async_get_entity_id(
        platform, DOMAIN, f"{entry.entry_id}_{suffix}"
    )
    assert eid is not None
    return eid


@pytest.mark.asyncio
async def test_refresh_button_is_created_as_a_diagnostic_entity(
    hass, aioclient_mock, enable_custom_integrations
):
    cap_a = "https://cap.example/a.cap"
    aioclient_mock.get(FEED, text=_atom(cap_a))
    aioclient_mock.get(cap_a, text=_cap_xml("urn:oid:A", "Wind Warning"))

    entry = await _setup(hass)
    ent = er.async_get(hass).async_get(_entity_id(hass, entry, "refresh", "button"))

    assert ent.entity_category == EntityCategory.DIAGNOSTIC


@pytest.mark.asyncio
async def test_press_refetches_from_the_provider(
    hass, aioclient_mock, enable_custom_integrations
):
    cap_a = "https://cap.example/a.cap"
    cap_b = "https://cap.example/b.cap"
    aioclient_mock.get(FEED, text=_atom(cap_a))
    aioclient_mock.get(cap_a, text=_cap_xml("urn:oid:A", "Wind Warning"))

    entry = await _setup(hass)
    count_id = _entity_id(hass, entry, "count", "sensor")
    assert hass.states.get(count_id).state == "1"

    # A second alert appears upstream; the button picks it up without waiting
    # for the poll interval.
    aioclient_mock.clear_requests()
    aioclient_mock.get(FEED, text=_atom(cap_a, cap_b))
    aioclient_mock.get(cap_a, text=_cap_xml("urn:oid:A", "Wind Warning"))
    aioclient_mock.get(cap_b, text=_cap_xml("urn:oid:B", "Rainfall Warning"))

    await hass.services.async_call(
        BUTTON_DOMAIN,
        SERVICE_PRESS,
        {ATTR_ENTITY_ID: _entity_id(hass, entry, "refresh", "button")},
        blocking=True,
    )
    await hass.async_block_till_done()

    assert hass.states.get(count_id).state == "2"


@pytest.mark.asyncio
async def test_button_stays_available_when_the_update_failed(
    hass, aioclient_mock, enable_custom_integrations, freezer
):
    """The button must survive a failed update — that is when it is most useful."""
    cap_a = "https://cap.example/a.cap"
    aioclient_mock.get(FEED, text=_atom(cap_a))
    aioclient_mock.get(cap_a, text=_cap_xml("urn:oid:A", "Wind Warning"))

    entry = await _setup(hass)
    count_id = _entity_id(hass, entry, "count", "sensor")
    button_id = _entity_id(hass, entry, "refresh", "button")

    aioclient_mock.clear_requests()
    aioclient_mock.get(FEED, status=503)
    freezer.tick(timedelta(seconds=360))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    assert hass.states.get(count_id).state == STATE_UNAVAILABLE
    assert hass.states.get(button_id).state != STATE_UNAVAILABLE
