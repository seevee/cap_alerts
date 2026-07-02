"""End-to-end lifecycle for tracker/country-source location (issue #16).

Unlike the rest of the suite, these tests run against a real Home Assistant
test instance (pytest-homeassistant-custom-component): real config entry,
integration setup, coordinator, and sensor platform — only HTTP is mocked.
They pin the availability contract from issue #16: a failed poll (offline
tracker or unresolvable country) makes every entity report ``unavailable``
without removing alert entities, and the next successful poll resumes.

The trip simulated: set up in Slovenia, drive into France (country source
flips, feed switches, polygon-less warning kept), lose connectivity in the
mountains (country source unavailable), recover.
"""

from __future__ import annotations

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.helpers import entity_registry as er

DOMAIN = "cap_alerts"
SI_FEED = "https://feeds.meteoalarm.org/api/v1/warnings/feeds-slovenia"
FR_FEED = "https://feeds.meteoalarm.org/api/v1/warnings/feeds-france"

TRACKER = "device_tracker.van"
COUNTRY = "sensor.geolocator_country"

LJUBLJANA = {"latitude": 46.05, "longitude": 14.51}
LYON = {"latitude": 45.76, "longitude": 4.84}


def _warning(identifier: str, event: str, *, polygon: str | None = None) -> dict:
    area: dict = {
        "areaDesc": "Somewhere",
        "geocode": [{"valueName": "EMMA_ID", "value": "XX000"}],
    }
    if polygon:
        area["polygon"] = polygon
    return {
        "uuid": f"uuid-{identifier}",
        "alert": {
            "identifier": identifier,
            "sender": "tests@example.com",
            "sent": "2026-07-01T08:00:00Z",
            "status": "Actual",
            "msgType": "Alert",
            "scope": "Public",
            "info": [
                {
                    "language": "en",
                    "category": ["Met"],
                    "event": event,
                    "severity": "Moderate",
                    "urgency": "Immediate",
                    "certainty": "Likely",
                    "onset": "2026-07-01T08:00:00Z",
                    "expires": "2099-01-01T00:00:00Z",
                    "headline": f"{event} headline",
                    "description": "desc",
                    "area": [area],
                }
            ],
        },
    }


# Triangle containing Ljubljana (46.05N 14.51E).
SI_PAYLOAD = {
    "warnings": [
        _warning("si-1", "Wind Ljubljana", polygon="45.5,14.0 46.5,14.0 46.0,15.5"),
    ]
}

# France: one warning with a polygon that does NOT contain Lyon, one without
# any geometry. Mobile mode must keep the polygon-less warning and drop the
# non-matching polygon.
FR_PAYLOAD = {
    "warnings": [
        _warning("fr-1", "Paris Wind", polygon="48.5,2.0 49.0,2.0 48.8,2.8"),
        _warning("fr-2", "Orange Thunderstorms"),
    ]
}


def _alert_entity_ids(hass, entry) -> list[str]:
    ent_reg = er.async_get(hass)
    prefix = f"{entry.entry_id}_meteoalarm_"
    return [
        e.entity_id
        for e in er.async_entries_for_config_entry(ent_reg, entry.entry_id)
        if e.unique_id.startswith(prefix)
    ]


async def _setup_entry(hass) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="METEOALARM auto: van",
        data={
            "provider": "meteoalarm",
            "tracker_entity": TRACKER,
            "country_entity": COUNTRY,
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


@pytest.mark.asyncio
async def test_camper_trip_lifecycle(hass, aioclient_mock, enable_custom_integrations):
    aioclient_mock.get(SI_FEED, json=SI_PAYLOAD)
    aioclient_mock.get(FR_FEED, json=FR_PAYLOAD)

    hass.states.async_set(TRACKER, "not_home", LJUBLJANA)
    hass.states.async_set(COUNTRY, "Slovenia")
    entry = await _setup_entry(hass)

    ent_reg = er.async_get(hass)
    count_id = ent_reg.async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_count")
    assert count_id is not None

    # Parked in Ljubljana: the polygon warning matches the tracker position.
    assert hass.states.get(count_id).state == "1"
    si_ids = _alert_entity_ids(hass, entry)
    assert len(si_ids) == 1
    assert hass.states.get(si_ids[0]).attributes["event"] == "Wind Ljubljana"

    # Drive into France: tracker and country source both move; the feed
    # switches, the Slovenian alert entity is removed, and the polygon-less
    # French warning is kept while the non-matching Paris polygon is dropped.
    hass.states.async_set(TRACKER, "not_home", LYON)
    hass.states.async_set(COUNTRY, "France")
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()

    assert hass.states.get(count_id).state == "1"
    fr_ids = _alert_entity_ids(hass, entry)
    assert len(fr_ids) == 1
    assert fr_ids != si_ids
    assert hass.states.get(si_ids[0]) is None
    fr_state = hass.states.get(fr_ids[0])
    assert fr_state.attributes["event"] == "Orange Thunderstorms"

    # Parked in the mountains, no connectivity: the country source goes
    # unavailable → poll fails → everything reports unavailable, but the
    # alert entity is retained (issue #16: "as long as 'unavailable' is
    # handled correctly").
    hass.states.async_set(COUNTRY, STATE_UNAVAILABLE)
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()

    assert hass.states.get(count_id).state == STATE_UNAVAILABLE
    assert _alert_entity_ids(hass, entry) == fr_ids
    assert hass.states.get(fr_ids[0]).state == STATE_UNAVAILABLE

    # Signal returns: same alert resumes on the next successful poll.
    hass.states.async_set(COUNTRY, "France")
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()

    assert hass.states.get(count_id).state == "1"
    assert hass.states.get(fr_ids[0]).state != STATE_UNAVAILABLE
    assert hass.states.get(fr_ids[0]).attributes["event"] == "Orange Thunderstorms"


@pytest.mark.asyncio
async def test_tracker_loss_goes_unavailable_and_recovers(
    hass, aioclient_mock, enable_custom_integrations
):
    aioclient_mock.get(SI_FEED, json=SI_PAYLOAD)

    hass.states.async_set(TRACKER, "not_home", LJUBLJANA)
    hass.states.async_set(COUNTRY, "Slovenia")
    entry = await _setup_entry(hass)

    ent_reg = er.async_get(hass)
    count_id = ent_reg.async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_count")
    assert hass.states.get(count_id).state == "1"
    (alert_id,) = _alert_entity_ids(hass, entry)

    # Tracker loses its GPS fix (no coordinates in attributes).
    hass.states.async_set(TRACKER, "not_home", {})
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()

    assert hass.states.get(count_id).state == STATE_UNAVAILABLE
    assert hass.states.get(alert_id).state == STATE_UNAVAILABLE

    # Fix restored.
    hass.states.async_set(TRACKER, "not_home", LJUBLJANA)
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()

    assert hass.states.get(count_id).state == "1"
    assert hass.states.get(alert_id).state != STATE_UNAVAILABLE
