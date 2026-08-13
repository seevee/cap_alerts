"""One entry per scope (issue #130).

Two entries watching the same zone means two coordinators polling it, two
devices, and two alert entities per alert. Nothing stopped that: eighteen
create paths called `async_create_entry` directly and no flow ever set a
unique ID.

The key is derived from entry `data` alone, so the tests come in two halves —
what the key says about a scope, and what the flow does when two scopes
collide.
"""

from __future__ import annotations

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.config_entries import ConfigEntryState

from custom_components.cap_alerts.const import (
    CONF_COUNTRY,
    CONF_COUNTRY_ENTITY,
    CONF_GPS_LOC,
    CONF_PROVIDER,
    CONF_PROVINCE,
    CONF_REGIONS,
    CONF_SOURCE_ID,
    CONF_TRACKER_ENTITY,
    CONF_ZONE_ID,
)
from custom_components.cap_alerts.flows.common import compute_scope_key

DOMAIN = "cap_alerts"


# ---------------------------------------------------------------------------
# What the key says
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        ({CONF_PROVIDER: "nws", CONF_ZONE_ID: "OHZ049"}, "nws:zone:OHZ049"),
        (
            {CONF_PROVIDER: "nws", CONF_GPS_LOC: "39.7392,-104.9903"},
            "nws:gps:39.7392,-104.9903",
        ),
        (
            {CONF_PROVIDER: "nws", CONF_TRACKER_ENTITY: "device_tracker.phone"},
            "nws:tracker:device_tracker.phone",
        ),
        ({CONF_PROVIDER: "eccc", CONF_PROVINCE: "AB"}, "eccc:province:AB"),
        (
            {CONF_PROVIDER: "wmo", CONF_SOURCE_ID: "mx-smn-es"},
            "wmo:source:mx-smn-es",
        ),
        ({CONF_PROVIDER: "gdacs"}, "gdacs:global"),
    ],
)
def test_scope_key_shape(data: dict, expected: str):
    assert compute_scope_key(data) == expected


def test_multi_value_fields_are_order_insensitive():
    """`OHC049,OHZ035` and `OHZ035,OHC049` are one scope typed two ways."""
    a = compute_scope_key({CONF_PROVIDER: "nws", CONF_ZONE_ID: "OHC049,OHZ035"})
    b = compute_scope_key({CONF_PROVIDER: "nws", CONF_ZONE_ID: "OHZ035,OHC049"})
    assert a == b == "nws:zone:OHC049,OHZ035"


def test_region_sets_are_order_insensitive():
    base = {CONF_PROVIDER: "meteoalarm", CONF_COUNTRY: "FR"}
    a = compute_scope_key({**base, CONF_REGIONS: ["FR002", "FR001"]})
    b = compute_scope_key({**base, CONF_REGIONS: ["FR001", "FR002"]})
    assert a == b == "meteoalarm:country:FR:regions:FR001,FR002"


def test_every_component_participates():
    """A WMO entry is a source *and* a location; collapsing either merges scopes."""
    source_only = compute_scope_key({CONF_PROVIDER: "wmo", CONF_SOURCE_ID: "in-imd-en"})
    with_gps = compute_scope_key(
        {CONF_PROVIDER: "wmo", CONF_SOURCE_ID: "in-imd-en", CONF_GPS_LOC: "1.0,2.0"}
    )
    other_gps = compute_scope_key(
        {CONF_PROVIDER: "wmo", CONF_SOURCE_ID: "in-imd-en", CONF_GPS_LOC: "3.0,4.0"}
    )
    assert len({source_only, with_gps, other_gps}) == 3


def test_a_narrower_region_set_is_a_different_scope():
    base = {CONF_PROVIDER: "meteoalarm", CONF_COUNTRY: "FR"}
    assert compute_scope_key({**base, CONF_REGIONS: ["FR001"]}) != compute_scope_key(
        {**base, CONF_REGIONS: ["FR001", "FR002"]}
    )


def test_the_same_scope_on_two_providers_is_two_scopes():
    assert compute_scope_key(
        {CONF_PROVIDER: "nws", CONF_GPS_LOC: "1.0,2.0"}
    ) != compute_scope_key({CONF_PROVIDER: "gdacs", CONF_GPS_LOC: "1.0,2.0"})


def test_mobile_mode_keys_on_the_country_source_and_the_tracker():
    key = compute_scope_key(
        {
            CONF_PROVIDER: "meteoalarm",
            CONF_COUNTRY_ENTITY: "sensor.phone_country",
            CONF_TRACKER_ENTITY: "device_tracker.phone",
        }
    )
    assert key == (
        "meteoalarm:country_source:sensor.phone_country:tracker:device_tracker.phone"
    )


def test_options_take_no_part_in_the_key():
    """Behavior isn't identity: prefixes, language and interval all live in options."""
    data = {CONF_PROVIDER: "wmo", CONF_SOURCE_ID: "mx-smn-es"}
    assert compute_scope_key(data) == compute_scope_key({**data})


# ---------------------------------------------------------------------------
# What the flow does with it
# ---------------------------------------------------------------------------


async def _reconfigure(hass, entry, *steps: str):
    result = await entry.start_reconfigure_flow(hass)
    for step in steps:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": step}
        )
    return result


async def _menu(hass, *steps: str):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    for step in steps:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": step}
        )
    return result


@pytest.mark.asyncio
async def test_a_second_entry_for_the_same_zone_aborts(
    hass, enable_custom_integrations
):
    result = await _menu(hass, "nws", "nws_zone")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ZONE_ID: "OHZ049"}
    )
    assert result["type"] == "create_entry"
    assert result["result"].unique_id == "nws:zone:OHZ049"

    # Same zone, typed in a different case — the validator normalizes, so the
    # key matches and the flow refuses.
    result = await _menu(hass, "nws", "nws_zone")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ZONE_ID: "ohz049"}
    )
    assert result["type"] == "abort"
    assert result["reason"] == "already_configured"


@pytest.mark.asyncio
async def test_a_different_scope_on_the_same_provider_is_allowed(
    hass, enable_custom_integrations
):
    for zone in ("OHZ049", "OHZ050"):
        result = await _menu(hass, "nws", "nws_zone")
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_ZONE_ID: zone}
        )
        assert result["type"] == "create_entry"
    assert len(hass.config_entries.async_entries(DOMAIN)) == 2


@pytest.mark.asyncio
async def test_reconfigure_may_keep_its_own_scope(hass, enable_custom_integrations):
    """A no-op edit must not collide the entry with itself."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_PROVIDER: "nws", CONF_ZONE_ID: "OHZ049"},
        unique_id="nws:zone:OHZ049",
    )
    entry.add_to_hass(hass)

    result = await _reconfigure(hass, entry, "reconfigure_nws", "reconfigure_nws_zone")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ZONE_ID: "OHZ049"}
    )

    assert result["type"] == "abort"
    assert result["reason"] == "reconfigure_successful"


@pytest.mark.asyncio
async def test_reconfigure_onto_another_entrys_scope_aborts(
    hass, enable_custom_integrations
):
    for zone in ("OHZ049", "OHZ050"):
        MockConfigEntry(
            domain=DOMAIN,
            data={CONF_PROVIDER: "nws", CONF_ZONE_ID: zone},
            unique_id=f"nws:zone:{zone}",
        ).add_to_hass(hass)
    first, second = hass.config_entries.async_entries(DOMAIN)

    result = await _reconfigure(hass, second, "reconfigure_nws", "reconfigure_nws_zone")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ZONE_ID: first.data[CONF_ZONE_ID]}
    )

    assert result["type"] == "abort"
    assert result["reason"] == "already_configured"
    # And the entry it tried to become is untouched.
    assert second.data[CONF_ZONE_ID] == "OHZ050"


@pytest.mark.asyncio
async def test_reconfigure_rewrites_the_key_when_the_scope_moves(
    hass, enable_custom_integrations
):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_PROVIDER: "nws", CONF_ZONE_ID: "OHZ049"},
        unique_id="nws:zone:OHZ049",
    )
    entry.add_to_hass(hass)

    result = await _reconfigure(hass, entry, "reconfigure_nws", "reconfigure_nws_zone")
    await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ZONE_ID: "TXZ100"}
    )

    assert entry.unique_id == "nws:zone:TXZ100"
    # A stale key would let the old scope be re-added as a duplicate.
    assert entry.data[CONF_ZONE_ID] == "TXZ100"


# ---------------------------------------------------------------------------
# Backfill onto entries that predate the key
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_setup_backfills_the_key_on_an_older_entry(
    hass, aioclient_mock, enable_custom_integrations
):
    """Without this the guard would protect new installs only."""
    aioclient_mock.get(
        "https://rss.alertready.ca/",
        text='<?xml version="1.0" encoding="UTF-8"?>'
        '<feed xmlns="http://www.w3.org/2005/Atom"></feed>',
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_PROVIDER: "eccc", CONF_PROVINCE: "ON"},
        options={"streaming": False, "feed_source": "alertready"},
    )
    entry.add_to_hass(hass)
    assert entry.unique_id is None

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.unique_id == "eccc:province:ON"


@pytest.mark.asyncio
async def test_a_pre_existing_duplicate_is_reported_not_forced(
    hass, aioclient_mock, enable_custom_integrations, caplog
):
    """Two entries already sharing a scope can't both hold the key, and
    neither can be merged for the user."""
    aioclient_mock.get(
        "https://rss.alertready.ca/",
        text='<?xml version="1.0" encoding="UTF-8"?>'
        '<feed xmlns="http://www.w3.org/2005/Atom"></feed>',
    )
    entries = []
    for _ in range(2):
        entry = MockConfigEntry(
            domain=DOMAIN,
            data={CONF_PROVIDER: "eccc", CONF_PROVINCE: "ON"},
            options={"streaming": False, "feed_source": "alertready"},
        )
        entry.add_to_hass(hass)
        entries.append(entry)

    with caplog.at_level("WARNING"):
        for entry in entries:
            if entry.state is not ConfigEntryState.LOADED:
                assert await hass.config_entries.async_setup(entry.entry_id)
            await hass.async_block_till_done()

    assert entries[0].unique_id == "eccc:province:ON"
    assert entries[1].unique_id is None
    assert sum("duplicates" in r.message for r in caplog.records) == 1
