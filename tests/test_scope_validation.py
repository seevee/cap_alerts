"""Validating a scope before the entry exists (issue #131).

The flow used to check syntax and stop there. A well-formed typo — `OHZ999`
matches the zone pattern and does not exist — created an entry that set up
cleanly, polled forever and never produced an alert, with no signal to the
user that anything was wrong.

Each provider answers with the cheapest authoritative request it has, and
"does this scope resolve" is the question, not "is the service up".
"""

from __future__ import annotations

import json

import pytest

from custom_components.cap_alerts.const import (
    CONF_COUNTRY,
    CONF_GPS_LOC,
    CONF_PROVINCE,
    CONF_SOURCE_ID,
    CONF_TRACKER_ENTITY,
    CONF_ZONE_ID,
)
from custom_components.cap_alerts.providers import get_provider
from tests.conftest import StubSession

DOMAIN = "cap_alerts"
_ZONES = "https://api.weather.gov/zones"
_MA_FRANCE = "https://feeds.meteoalarm.org/api/v1/warnings/feeds-france"
_WMO_RSS = "https://severeweather.wmo.int/v2/cap-alerts/mx-smn-es/rss.xml"


def _zone_collection(*ids: str) -> str:
    return json.dumps(
        {
            "type": "FeatureCollection",
            "features": [{"properties": {"id": zone}} for zone in ids],
        }
    )


async def _validate(provider: str, config: dict) -> str | None:
    return await get_provider(provider).async_validate_config(
        StubSession(_RESPONSES), config, user_agent="test"
    )


_RESPONSES: dict = {}


# ---------------------------------------------------------------------------
# NWS — the zone has to be one NWS publishes
# ---------------------------------------------------------------------------


async def test_a_published_zone_validates():
    _RESPONSES.clear()
    _RESPONSES[f"{_ZONES}?id=OHZ049"] = _zone_collection("OHZ049")
    assert await _validate("nws", {CONF_ZONE_ID: "OHZ049"}) is None


async def test_a_well_formed_zone_that_does_not_exist_is_rejected():
    """The case the regex cannot catch, and the reason this issue exists."""
    _RESPONSES.clear()
    _RESPONSES[f"{_ZONES}?id=OHZ999"] = _zone_collection()
    assert await _validate("nws", {CONF_ZONE_ID: "OHZ999"}) == "unknown_zone"


async def test_one_bad_zone_in_a_list_fails_the_list():
    _RESPONSES.clear()
    # One request covers the list, and NWS answers only for what it knows.
    _RESPONSES[f"{_ZONES}?id=OHZ049,OHZ999"] = _zone_collection("OHZ049")
    assert await _validate("nws", {CONF_ZONE_ID: "OHZ049,OHZ999"}) == "unknown_zone"


async def test_nws_service_failure_is_not_a_bad_zone():
    _RESPONSES.clear()
    _RESPONSES[f"{_ZONES}?id=OHZ049"] = (503, "")
    assert await _validate("nws", {CONF_ZONE_ID: "OHZ049"}) == "cannot_connect"


@pytest.mark.parametrize(
    "config",
    [{CONF_GPS_LOC: "40.0,-74.0"}, {CONF_TRACKER_ENTITY: "device_tracker.phone"}],
)
async def test_nws_point_modes_are_not_checkable(config: dict):
    """A point query answers for any coordinate, so there is nothing to test."""
    _RESPONSES.clear()
    assert await _validate("nws", config) is None


# ---------------------------------------------------------------------------
# ECCC — local, since the feed is national
# ---------------------------------------------------------------------------


async def test_a_known_province_validates():
    assert await _validate("eccc", {CONF_PROVINCE: "AB"}) is None


async def test_an_unknown_province_is_rejected_without_a_request():
    session = StubSession({})
    result = await get_provider("eccc").async_validate_config(
        session, {CONF_PROVINCE: "ZZ"}, user_agent="test"
    )
    assert result == "invalid_province"
    assert session.requested == []


# ---------------------------------------------------------------------------
# MeteoAlarm — the country still has to have a feed
# ---------------------------------------------------------------------------


async def test_a_country_with_a_live_feed_validates():
    _RESPONSES.clear()
    _RESPONSES[_MA_FRANCE] = "{}"
    assert await _validate("meteoalarm", {CONF_COUNTRY: "FR"}) is None


async def test_a_country_whose_feed_is_gone_is_rejected():
    _RESPONSES.clear()
    _RESPONSES[_MA_FRANCE] = (404, "")
    assert await _validate("meteoalarm", {CONF_COUNTRY: "FR"}) == "unknown_country_feed"


async def test_meteoalarm_mobile_mode_has_no_country_to_check():
    """Fully-mobile resolves a country per poll, so there is none at setup."""
    _RESPONSES.clear()
    assert await _validate("meteoalarm", {}) is None


# ---------------------------------------------------------------------------
# WMO — the mirror has to serve the source
# ---------------------------------------------------------------------------


async def test_a_mirrored_source_validates():
    _RESPONSES.clear()
    _RESPONSES[_WMO_RSS] = "<rss/>"
    assert await _validate("wmo", {CONF_SOURCE_ID: "mx-smn-es"}) is None


async def test_a_source_the_mirror_does_not_carry_is_rejected():
    """Covers both a typo and a registered-but-unmirrored source."""
    _RESPONSES.clear()
    _RESPONSES[_WMO_RSS] = (404, "")
    assert await _validate("wmo", {CONF_SOURCE_ID: "mx-smn-es"}) == "unknown_wmo_source"


# ---------------------------------------------------------------------------
# GDACS — the scope is the planet
# ---------------------------------------------------------------------------


async def test_gdacs_validates_without_asking_anything():
    session = StubSession({})
    assert (
        await get_provider("gdacs").async_validate_config(
            session, {CONF_GPS_LOC: "1.0,2.0"}, user_agent="test"
        )
        is None
    )
    assert session.requested == []


# ---------------------------------------------------------------------------
# Through the flow
# ---------------------------------------------------------------------------


@pytest.mark.validate_scope
@pytest.mark.asyncio
async def test_the_form_reports_an_unknown_zone(
    hass, aioclient_mock, enable_custom_integrations
):
    aioclient_mock.get(f"{_ZONES}?id=OHZ999", text=_zone_collection())

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "nws"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "nws_zone"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ZONE_ID: "OHZ999"}
    )

    assert result["type"] == "form"
    assert result["errors"] == {"base": "unknown_zone"}
    assert not hass.config_entries.async_entries(DOMAIN)


@pytest.mark.validate_scope
@pytest.mark.asyncio
async def test_a_validated_zone_still_creates_the_entry(
    hass, aioclient_mock, enable_custom_integrations
):
    aioclient_mock.get(f"{_ZONES}?id=OHZ049", text=_zone_collection("OHZ049"))

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "nws"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "nws_zone"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ZONE_ID: "OHZ049"}
    )

    assert result["type"] == "create_entry"
    assert result["result"].unique_id == "nws:zone:OHZ049"


@pytest.mark.validate_scope
@pytest.mark.asyncio
async def test_an_unreachable_service_does_not_block_setup_permanently(
    hass, aioclient_mock, enable_custom_integrations
):
    """Failure to answer is not evidence the scope is bad — retry, don't refuse."""
    import aiohttp

    aioclient_mock.get(f"{_ZONES}?id=OHZ049", exc=aiohttp.ClientError("boom"))

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "nws"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "nws_zone"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ZONE_ID: "OHZ049"}
    )

    assert result["type"] == "form"
    assert result["errors"] == {"base": "cannot_connect"}


@pytest.mark.validate_scope
@pytest.mark.asyncio
async def test_reconfigure_reports_an_unknown_zone_too(
    hass, aioclient_mock, enable_custom_integrations
):
    """The same typo is just as wrong on an edit as on setup."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    aioclient_mock.get(f"{_ZONES}?id=OHZ999", text=_zone_collection())
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"provider": "nws", CONF_ZONE_ID: "OHZ049"},
        unique_id="nws:zone:OHZ049",
    )
    entry.add_to_hass(hass)

    result = await entry.start_reconfigure_flow(hass)
    for step in ("reconfigure_nws", "reconfigure_nws_zone"):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": step}
        )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ZONE_ID: "OHZ999"}
    )

    assert result["type"] == "form"
    assert result["errors"] == {"base": "unknown_zone"}
    # The entry keeps the scope it had.
    assert entry.data[CONF_ZONE_ID] == "OHZ049"
