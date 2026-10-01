"""The entity_id an alert entity actually gets registered under.

`sensor._alert_object_id` is what the integration *suggests*; Home Assistant
prefixes the device name onto it because these entities set `has_entity_name`.
Nothing pinned the composed result, and the docs drifted for months describing
the unprefixed form (`sensor.cap_alert_<slug>_<hash>`), which no install has
ever had. This test is the pin: it asserts both halves, so a change to either
the suggestion or the `has_entity_name` decision shows up as a rename here
rather than in a bug report about a broken dashboard.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from homeassistant.core import State, callback
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.cap_alerts import sensor

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
        "<geocode><valueName>profile:CAP-CP:Location:0.3</valueName>"
        "<value>3506008</value></geocode>"
        "</area></info></alert>"
    )


def _atom(cap_url: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<feed xmlns="http://www.w3.org/2005/Atom" '
        'xmlns:georss="http://www.georss.org/georss">'
        "<entry><id>atom-0</id><title>Warning</title>"
        '<category term="status=Actual"/>'
        "<georss:polygon>45.0 -76.0 45.0 -75.5 45.5 -75.5 45.5 -76.0 "
        "45.0 -76.0</georss:polygon>"
        f'<link type="application/cap+xml" href="{cap_url}"/>'
        "</entry></feed>"
    )


@pytest.mark.asyncio
async def test_alert_entity_id_carries_the_device_prefix(
    hass, aioclient_mock, enable_custom_integrations
):
    cap_url = "https://cap.example/a.cap"
    aioclient_mock.get(FEED, text=_atom(cap_url))
    aioclient_mock.get(cap_url, text=_cap_xml("urn:oid:A", "Wind Warning"))

    entry = MockConfigEntry(
        domain=DOMAIN,
        title="ECCC: Ontario",
        data={"provider": "eccc", "province": "ON"},
        options={"streaming": False, "scan_interval": 300},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    registry = er.async_get(hass)
    alert_prefix = f"{entry.entry_id}_eccc_"
    registered = [
        e
        for e in er.async_entries_for_config_entry(registry, entry.entry_id)
        if e.unique_id.startswith(alert_prefix)
    ]
    assert len(registered) == 1
    entity = registered[0]

    # The device name ("CAP Alerts ECCC") slugified, then what we suggested.
    suggested = sensor._alert_object_id(entity.unique_id, entity.original_name)
    assert entity.entity_id == f"sensor.cap_alerts_eccc_{suggested}"
    # And the suggestion itself is still the documented shape.
    assert suggested.startswith("cap_alert_")
    assert len(suggested.rsplit("_", 1)[1]) == 8


@pytest.mark.asyncio
async def test_incident_created_names_a_registered_entity_with_state(
    hass, aioclient_mock, enable_custom_integrations
):
    """The first sighting's event fires once its entity exists (issue #249).

    The store diffs inside the first refresh, before the sensor platform has
    run, so firing from there left ``entity_id`` off every creation. The event
    now waits for the entity, and fires after its first state write: a listener
    reading ``states.get(entity_id)`` as the event lands finds the attributes.
    """
    cap_url = "https://cap.example/a.cap"
    aioclient_mock.get(FEED, text=_atom(cap_url))
    aioclient_mock.get(cap_url, text=_cap_xml("urn:oid:A", "Wind Warning"))

    # Snapshot the state *as the event fires*, not after everything settles;
    # a check made later would pass whatever the ordering.
    seen: list[tuple[dict, State | None]] = []

    @callback
    def _on_created(event) -> None:
        seen.append((event.data, hass.states.get(event.data.get("entity_id", ""))))

    hass.bus.async_listen("incident_created", _on_created)

    entry = MockConfigEntry(
        domain=DOMAIN,
        title="ECCC: Ontario",
        data={"provider": "eccc", "province": "ON"},
        options={"streaming": False, "scan_interval": 300},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    registry = er.async_get(hass)
    registered = [
        e
        for e in er.async_entries_for_config_entry(registry, entry.entry_id)
        if e.unique_id.startswith(f"{entry.entry_id}_eccc_")
    ]
    assert len(registered) == 1

    assert len(seen) == 1
    data, state = seen[0]
    assert data["entity_id"] == registered[0].entity_id
    assert state is not None
    assert state.attributes["headline"] == "Wind Warning in effect"
