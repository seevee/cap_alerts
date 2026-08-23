"""Setup config flow: every provider's menu, form, validation and entry.

Runs against a real Home Assistant test instance
(pytest-homeassistant-custom-component) so the menus, schemas and created
entries are the ones HA actually produces. Nothing here touches the network:
the two steps that fetch (MeteoAlarm's region picker, WMO's source list) are
patched, and the region picker has its own file — ``test_meteoalarm_region_picker``.

Each provider gets the same three checks per mode: the entry a valid input
creates (data *and* title, since the title is derived rather than asked for),
the error a bad input re-renders, and the schema the form offers.
"""

from __future__ import annotations

import re
from unittest.mock import patch

import pytest
import voluptuous as vol

from custom_components.cap_alerts.flows.common import _validate_gps
from custom_components.cap_alerts.flows.meteoalarm import _validate_country
from custom_components.cap_alerts.const import (
    CONF_COUNTRY,
    CONF_COUNTRY_ATTRIBUTE,
    CONF_COUNTRY_ENTITY,
    CONF_GEOCODE_PREFIXES,
    CONF_GPS_LOC,
    CONF_PROVIDER,
    CONF_PROVINCE,
    CONF_SOURCE_ID,
    CONF_TRACKER_ENTITY,
    CONF_ZONE_ID,
    WMO_SOURCE_NAMES,
)

DOMAIN = "cap_alerts"

_WMO_FETCH = "custom_components.cap_alerts.flows.wmo.fetch_wmo_sources"
_TRACKER = "device_tracker.phone"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _menu(hass, *steps: str):
    """Start the user flow and walk it through ``steps`` menu selections."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    for step in steps:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": step}
        )
    return result


async def _submit(hass, result, user_input):
    return await hass.config_entries.flow.async_configure(result["flow_id"], user_input)


# ---------------------------------------------------------------------------
# Provider menu
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_user_step_lists_every_provider(hass, enable_custom_integrations):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    assert result["type"] == "menu"
    assert result["menu_options"] == ["nws", "eccc", "meteoalarm", "wmo", "gdacs"]


@pytest.mark.parametrize(
    ("provider", "options"),
    [
        ("nws", ["nws_zone", "nws_gps_loc", "nws_gps_tracker", "user"]),
        ("eccc", ["eccc_province", "eccc_gps_loc", "eccc_gps_tracker", "user"]),
        ("meteoalarm", ["meteoalarm_country", "meteoalarm_country_source", "user"]),
        ("gdacs", ["gdacs_global", "gdacs_gps_loc", "gdacs_gps_tracker", "user"]),
    ],
)
@pytest.mark.asyncio
async def test_provider_menus(
    hass, enable_custom_integrations, provider: str, options: list[str]
):
    result = await _menu(hass, provider)
    assert result["type"] == "menu"
    assert result["menu_options"] == options


@pytest.mark.parametrize("provider", ["nws", "eccc", "meteoalarm", "gdacs"])
@pytest.mark.asyncio
async def test_provider_menu_backs_to_the_provider_list(
    hass, enable_custom_integrations, provider: str
):
    """The trailing "user" option is a back edge to the provider menu (#140)."""
    result = await _menu(hass, provider, "user")
    assert result["type"] == "menu"
    assert result["step_id"] == "user"


# ---------------------------------------------------------------------------
# NWS
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_nws_zone_creates_an_entry(hass, enable_custom_integrations):
    result = await _menu(hass, "nws", "nws_zone")
    assert result["step_id"] == "nws_zone"

    result = await _submit(hass, result, {CONF_ZONE_ID: " ohz049,ohc049 "})
    assert result["type"] == "create_entry"
    # Upper-cased and stripped by the validator, so the title reads back clean.
    assert result["data"] == {CONF_PROVIDER: "nws", CONF_ZONE_ID: "OHZ049,OHC049"}
    assert result["title"] == "CAP Alerts NWS (OHZ049,OHC049)"


@pytest.mark.parametrize("zone", ["", "OH049", "OHX049", "OHZ49", "OHZ049,"])
@pytest.mark.asyncio
async def test_nws_zone_rejects_malformed_ids(
    hass, enable_custom_integrations, zone: str
):
    result = await _menu(hass, "nws", "nws_zone")
    result = await _submit(hass, result, {CONF_ZONE_ID: zone})
    assert result["type"] == "form"
    assert result["errors"] == {"base": "invalid_zone"}


@pytest.mark.asyncio
async def test_nws_gps_creates_an_entry(hass, enable_custom_integrations):
    result = await _menu(hass, "nws", "nws_gps_loc")
    result = await _submit(hass, result, {CONF_GPS_LOC: "39.96 , -82.99"})
    assert result["type"] == "create_entry"
    assert result["data"] == {CONF_PROVIDER: "nws", CONF_GPS_LOC: "39.96,-82.99"}
    assert result["title"] == "CAP Alerts NWS (39.96,-82.99)"


@pytest.mark.parametrize(
    "gps",
    [
        "",
        "39.96",
        "39.96,-82.99,10",
        "north,west",
        "91.0,0.0",  # latitude out of range
        "0.0,-181.0",  # longitude out of range
    ],
)
@pytest.mark.asyncio
async def test_nws_gps_rejects_bad_coordinates(
    hass, enable_custom_integrations, gps: str
):
    result = await _menu(hass, "nws", "nws_gps_loc")
    result = await _submit(hass, result, {CONF_GPS_LOC: gps})
    assert result["type"] == "form"
    assert result["errors"] == {"base": "invalid_gps"}


@pytest.mark.asyncio
async def test_nws_tracker_creates_an_entry(hass, enable_custom_integrations):
    result = await _menu(hass, "nws", "nws_gps_tracker")
    assert result["step_id"] == "nws_gps_tracker"
    # Setup has nothing to carry forward, so the key holds no default.
    key = next(k for k in result["data_schema"].schema if str(k) == CONF_TRACKER_ENTITY)
    assert key.default is vol.UNDEFINED

    result = await _submit(hass, result, {CONF_TRACKER_ENTITY: _TRACKER})
    assert result["type"] == "create_entry"
    assert result["data"] == {CONF_PROVIDER: "nws", CONF_TRACKER_ENTITY: _TRACKER}
    # Title takes the object id, not the full entity id.
    assert result["title"] == "CAP Alerts NWS (phone)"


# ---------------------------------------------------------------------------
# ECCC
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_eccc_province_creates_an_entry(hass, enable_custom_integrations):
    result = await _menu(hass, "eccc", "eccc_province")
    result = await _submit(hass, result, {CONF_PROVINCE: " on "})
    assert result["type"] == "create_entry"
    assert result["data"] == {CONF_PROVIDER: "eccc", CONF_PROVINCE: "ON"}
    assert result["title"] == "CAP Alerts ECCC (ON)"


@pytest.mark.parametrize("province", ["", "XX", "ONT", "Ontario"])
@pytest.mark.asyncio
async def test_eccc_province_rejects_unknown_codes(
    hass, enable_custom_integrations, province: str
):
    result = await _menu(hass, "eccc", "eccc_province")
    result = await _submit(hass, result, {CONF_PROVINCE: province})
    assert result["type"] == "form"
    assert result["errors"] == {"base": "invalid_province"}


@pytest.mark.asyncio
async def test_eccc_gps_creates_an_entry(hass, enable_custom_integrations):
    result = await _menu(hass, "eccc", "eccc_gps_loc")
    result = await _submit(hass, result, {CONF_GPS_LOC: "45.42,-75.69"})
    assert result["type"] == "create_entry"
    assert result["data"] == {CONF_PROVIDER: "eccc", CONF_GPS_LOC: "45.42,-75.69"}
    assert result["title"] == "CAP Alerts ECCC (45.42,-75.69)"


@pytest.mark.asyncio
async def test_eccc_gps_rejects_bad_coordinates(hass, enable_custom_integrations):
    result = await _menu(hass, "eccc", "eccc_gps_loc")
    result = await _submit(hass, result, {CONF_GPS_LOC: "not-a-point"})
    assert result["type"] == "form"
    assert result["errors"] == {"base": "invalid_gps"}


@pytest.mark.asyncio
async def test_eccc_tracker_creates_an_entry(hass, enable_custom_integrations):
    result = await _menu(hass, "eccc", "eccc_gps_tracker")
    assert result["step_id"] == "eccc_gps_tracker"
    result = await _submit(hass, result, {CONF_TRACKER_ENTITY: _TRACKER})
    assert result["type"] == "create_entry"
    assert result["data"] == {CONF_PROVIDER: "eccc", CONF_TRACKER_ENTITY: _TRACKER}
    assert result["title"] == "CAP Alerts ECCC (phone)"


# ---------------------------------------------------------------------------
# MeteoAlarm
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_meteoalarm_country_leads_to_the_filter_menu(
    hass, enable_custom_integrations
):
    result = await _menu(hass, "meteoalarm", "meteoalarm_country")
    result = await _submit(hass, result, {CONF_COUNTRY: "FI"})
    assert result["type"] == "menu"
    assert result["menu_options"] == [
        "meteoalarm_country_only",
        "meteoalarm_gps_polygon",
        "meteoalarm_gps_tracker",
        "meteoalarm_region_picker",
        "meteoalarm",
    ]


@pytest.mark.asyncio
async def test_meteoalarm_filter_backs_to_a_prefilled_country_form(
    hass, enable_custom_integrations
):
    """Backing out of the filter menu must not cost the pick already made:
    the country form re-renders with the in-flow pick as its default."""
    result = await _menu(hass, "meteoalarm", "meteoalarm_country")
    result = await _submit(hass, result, {CONF_COUNTRY: "FI"})
    result = await _submit(hass, result, {"next_step_id": "meteoalarm"})
    assert result["type"] == "menu"
    assert result["step_id"] == "meteoalarm"

    result = await _submit(hass, result, {"next_step_id": "meteoalarm_country"})
    assert result["type"] == "form"
    key = next(k for k in result["data_schema"].schema if str(k) == CONF_COUNTRY)
    assert key.default() == "FI"


@pytest.mark.asyncio
async def test_meteoalarm_country_offers_no_default_before_a_pick(
    hass, enable_custom_integrations
):
    result = await _menu(hass, "meteoalarm", "meteoalarm_country")
    key = next(k for k in result["data_schema"].schema if str(k) == CONF_COUNTRY)
    assert key.default is vol.UNDEFINED


@pytest.mark.parametrize("raw", ["", "   ", "ZZ", "Finland"])
def test_country_validator_rejects_anything_off_the_catalog(raw: str):
    cleaned, err = _validate_country(raw)
    assert err == "invalid_country"
    assert cleaned == raw


def test_country_validator_normalizes_case_and_whitespace():
    assert _validate_country(" fi ") == ("FI", None)


@pytest.mark.asyncio
async def test_meteoalarm_country_step_reports_a_bad_code(
    hass, enable_custom_integrations
):
    """The selector has no ``custom_value``, so HA rejects an off-catalog code
    before the step ever runs — stubbing it to free text is the only way to
    reach the step's own guard, which is what would catch a selector and a
    catalog that drift apart."""
    with patch(
        "custom_components.cap_alerts.flows.meteoalarm._country_selector",
        return_value=str,
    ):
        result = await _menu(hass, "meteoalarm", "meteoalarm_country")
        result = await _submit(hass, result, {CONF_COUNTRY: "ZZ"})
    assert result["type"] == "form"
    assert result["errors"] == {"base": "invalid_country"}


def test_gps_validator_guards_the_float_conversion():
    """The regex is the only thing keeping ``float()`` from seeing junk, so the
    guard behind it is pinned with the regex stubbed out."""
    with patch("custom_components.cap_alerts.flows.common._GPS_RE", re.compile(".*")):
        assert _validate_gps("north,west") == ("north,west", "invalid_gps")


@pytest.mark.asyncio
async def test_meteoalarm_country_only_creates_an_entry(
    hass, enable_custom_integrations
):
    result = await _menu(hass, "meteoalarm", "meteoalarm_country")
    result = await _submit(hass, result, {CONF_COUNTRY: "FI"})
    result = await _submit(hass, result, {"next_step_id": "meteoalarm_country_only"})
    assert result["type"] == "create_entry"
    assert result["data"] == {CONF_PROVIDER: "meteoalarm", CONF_COUNTRY: "FI"}
    assert result["title"] == "CAP Alerts METEOALARM (Finland)"


@pytest.mark.asyncio
async def test_meteoalarm_gps_polygon_creates_an_entry(
    hass, enable_custom_integrations
):
    result = await _menu(hass, "meteoalarm", "meteoalarm_country")
    result = await _submit(hass, result, {CONF_COUNTRY: "FI"})
    result = await _submit(hass, result, {"next_step_id": "meteoalarm_gps_polygon"})
    result = await _submit(hass, result, {CONF_GPS_LOC: "60.17,24.94"})
    assert result["type"] == "create_entry"
    assert result["data"] == {
        CONF_PROVIDER: "meteoalarm",
        CONF_COUNTRY: "FI",
        CONF_GPS_LOC: "60.17,24.94",
    }
    # GPS wins over the country in the title, matching NWS/ECCC.
    assert result["title"] == "CAP Alerts METEOALARM (60.17,24.94)"


@pytest.mark.asyncio
async def test_meteoalarm_gps_polygon_rejects_bad_coordinates(
    hass, enable_custom_integrations
):
    result = await _menu(hass, "meteoalarm", "meteoalarm_country")
    result = await _submit(hass, result, {CONF_COUNTRY: "FI"})
    result = await _submit(hass, result, {"next_step_id": "meteoalarm_gps_polygon"})
    result = await _submit(hass, result, {CONF_GPS_LOC: "60.17"})
    assert result["type"] == "form"
    assert result["errors"] == {"base": "invalid_gps"}


@pytest.mark.asyncio
async def test_meteoalarm_tracker_keeps_the_chosen_country(
    hass, enable_custom_integrations
):
    result = await _menu(hass, "meteoalarm", "meteoalarm_country")
    result = await _submit(hass, result, {CONF_COUNTRY: "FI"})
    result = await _submit(hass, result, {"next_step_id": "meteoalarm_gps_tracker"})
    assert result["step_id"] == "meteoalarm_gps_tracker"
    result = await _submit(hass, result, {CONF_TRACKER_ENTITY: _TRACKER})
    assert result["type"] == "create_entry"
    assert result["data"] == {
        CONF_PROVIDER: "meteoalarm",
        CONF_COUNTRY: "FI",
        CONF_TRACKER_ENTITY: _TRACKER,
    }
    assert result["title"] == "CAP Alerts METEOALARM (phone)"


@pytest.mark.asyncio
async def test_meteoalarm_country_source_stores_the_attribute(
    hass, enable_custom_integrations
):
    result = await _menu(hass, "meteoalarm", "meteoalarm_country_source")
    assert result["step_id"] == "meteoalarm_country_source"
    result = await _submit(
        hass,
        result,
        {
            CONF_TRACKER_ENTITY: _TRACKER,
            CONF_COUNTRY_ENTITY: "sensor.home_country",
            CONF_COUNTRY_ATTRIBUTE: " country_code ",
        },
    )
    assert result["type"] == "create_entry"
    assert result["data"] == {
        CONF_PROVIDER: "meteoalarm",
        CONF_TRACKER_ENTITY: _TRACKER,
        CONF_COUNTRY_ENTITY: "sensor.home_country",
        CONF_COUNTRY_ATTRIBUTE: "country_code",
    }
    assert result["title"] == "CAP Alerts METEOALARM (auto: phone)"


@pytest.mark.asyncio
async def test_meteoalarm_country_source_omits_a_blank_attribute(
    hass, enable_custom_integrations
):
    """Blank means "read the state", so the key must be absent, not empty."""
    result = await _menu(hass, "meteoalarm", "meteoalarm_country_source")
    result = await _submit(
        hass,
        result,
        {
            CONF_TRACKER_ENTITY: _TRACKER,
            CONF_COUNTRY_ENTITY: "sensor.home_country",
            CONF_COUNTRY_ATTRIBUTE: "   ",
        },
    )
    assert result["type"] == "create_entry"
    assert CONF_COUNTRY_ATTRIBUTE not in result["data"]


# ---------------------------------------------------------------------------
# WMO
# ---------------------------------------------------------------------------

_WMO_OPTIONS = [("mx-smn-es", "Mexico — SMN"), ("cn-cma-xx", "China — CMA")]


@pytest.mark.asyncio
async def test_wmo_source_step_offers_the_live_registry(
    hass, enable_custom_integrations
):
    with patch(_WMO_FETCH, return_value=list(_WMO_OPTIONS)) as fetch:
        result = await _menu(hass, "wmo")
        assert result["step_id"] == "wmo_source"
        key = next(k for k in result["data_schema"].schema if str(k) == CONF_SOURCE_ID)
        options = result["data_schema"].schema[key].config["options"]
        assert [opt["value"] for opt in options] == ["mx-smn-es", "cn-cma-xx"]

        # A re-render (here: a rejected source) must not re-fetch the registry.
        result = await _submit(hass, result, {CONF_SOURCE_ID: "nope"})
        assert result["errors"] == {"base": "invalid_wmo_source"}
        assert fetch.call_count == 1


@pytest.mark.asyncio
async def test_wmo_source_step_falls_back_to_the_static_catalog(
    hass, enable_custom_integrations
):
    with patch(_WMO_FETCH, return_value=[]):
        result = await _menu(hass, "wmo")
    key = next(k for k in result["data_schema"].schema if str(k) == CONF_SOURCE_ID)
    options = result["data_schema"].schema[key].config["options"]
    assert {opt["value"] for opt in options} == set(WMO_SOURCE_NAMES)


async def _wmo_filter(hass, source_id: str = "mx-smn-es"):
    """Walk to the WMO filter menu with the registry patched out."""
    with patch(_WMO_FETCH, return_value=list(_WMO_OPTIONS)):
        result = await _menu(hass, "wmo")
        return await _submit(hass, result, {CONF_SOURCE_ID: source_id})


@pytest.mark.asyncio
async def test_wmo_filter_menu(hass, enable_custom_integrations):
    result = await _wmo_filter(hass)
    assert result["type"] == "menu"
    assert result["menu_options"] == [
        "wmo_country_wide",
        "wmo_gps_loc",
        "wmo_gps_tracker",
        "wmo_geocode",
        "wmo_source",
    ]


@pytest.mark.asyncio
async def test_wmo_filter_backs_to_a_prefilled_source_form(
    hass, enable_custom_integrations
):
    result = await _wmo_filter(hass)
    # The back re-entry runs outside the helper's patch, so only the
    # handler's per-flow cache keeps this render off the network.
    result = await _submit(hass, result, {"next_step_id": "wmo_source"})
    assert result["type"] == "form"
    assert result["step_id"] == "wmo_source"
    key = next(k for k in result["data_schema"].schema if str(k) == CONF_SOURCE_ID)
    assert key.default() == "mx-smn-es"


@pytest.mark.asyncio
async def test_wmo_country_wide_titles_from_the_catalog_name(
    hass, enable_custom_integrations
):
    result = await _wmo_filter(hass)
    result = await _submit(hass, result, {"next_step_id": "wmo_country_wide"})
    assert result["type"] == "create_entry"
    assert result["data"] == {CONF_PROVIDER: "wmo", CONF_SOURCE_ID: "mx-smn-es"}
    assert result["title"] == f"CAP Alerts WMO ({WMO_SOURCE_NAMES['mx-smn-es']})"


@pytest.mark.asyncio
async def test_wmo_gps_creates_an_entry(hass, enable_custom_integrations):
    result = await _wmo_filter(hass)
    result = await _submit(hass, result, {"next_step_id": "wmo_gps_loc"})
    result = await _submit(hass, result, {CONF_GPS_LOC: "19.43,-99.13"})
    assert result["type"] == "create_entry"
    assert result["data"] == {
        CONF_PROVIDER: "wmo",
        CONF_SOURCE_ID: "mx-smn-es",
        CONF_GPS_LOC: "19.43,-99.13",
    }
    name = WMO_SOURCE_NAMES["mx-smn-es"]
    assert result["title"] == f"CAP Alerts WMO ({name} (19.43,-99.13))"


@pytest.mark.asyncio
async def test_wmo_gps_rejects_bad_coordinates(hass, enable_custom_integrations):
    result = await _wmo_filter(hass)
    result = await _submit(hass, result, {"next_step_id": "wmo_gps_loc"})
    result = await _submit(hass, result, {CONF_GPS_LOC: "19.43;-99.13"})
    assert result["type"] == "form"
    assert result["errors"] == {"base": "invalid_gps"}


@pytest.mark.asyncio
async def test_wmo_tracker_creates_an_entry(hass, enable_custom_integrations):
    result = await _wmo_filter(hass)
    result = await _submit(hass, result, {"next_step_id": "wmo_gps_tracker"})
    assert result["step_id"] == "wmo_gps_tracker"
    result = await _submit(hass, result, {CONF_TRACKER_ENTITY: _TRACKER})
    assert result["type"] == "create_entry"
    assert result["data"] == {
        CONF_PROVIDER: "wmo",
        CONF_SOURCE_ID: "mx-smn-es",
        CONF_TRACKER_ENTITY: _TRACKER,
    }
    name = WMO_SOURCE_NAMES["mx-smn-es"]
    assert result["title"] == f"CAP Alerts WMO ({name} (phone))"


@pytest.mark.asyncio
async def test_wmo_geocode_stores_prefixes_as_options(hass, enable_custom_integrations):
    result = await _wmo_filter(hass, "cn-cma-xx")
    result = await _submit(hass, result, {"next_step_id": "wmo_geocode"})
    result = await _submit(hass, result, {CONF_GEOCODE_PREFIXES: "13, 37, 13"})
    assert result["type"] == "create_entry"
    # Entry data stays location-mode only; the narrowing is an option so the
    # options flow can edit it later.
    assert result["data"] == {CONF_PROVIDER: "wmo", CONF_SOURCE_ID: "cn-cma-xx"}
    assert result["options"] == {CONF_GEOCODE_PREFIXES: ["13", "37"]}


@pytest.mark.parametrize(
    ("raw", "error"),
    [
        ("", "invalid_geocode_prefix"),  # picked the mode, gave nothing
        ("   ", "invalid_geocode_prefix"),
        ("13,@@", "invalid_geocode_prefix"),
    ],
)
@pytest.mark.asyncio
async def test_wmo_geocode_rejects_an_unusable_list(
    hass, enable_custom_integrations, raw: str, error: str
):
    result = await _wmo_filter(hass, "cn-cma-xx")
    result = await _submit(hass, result, {"next_step_id": "wmo_geocode"})
    result = await _submit(hass, result, {CONF_GEOCODE_PREFIXES: raw})
    assert result["type"] == "form"
    assert result["errors"] == {"base": error}


# ---------------------------------------------------------------------------
# GDACS
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gdacs_global_creates_an_entry(hass, enable_custom_integrations):
    result = await _menu(hass, "gdacs", "gdacs_global")
    assert result["type"] == "create_entry"
    assert result["data"] == {CONF_PROVIDER: "gdacs"}
    # "Global" is a real scope, not a missing one.
    assert result["title"] == "CAP Alerts GDACS (Global)"


@pytest.mark.asyncio
async def test_gdacs_gps_creates_an_entry(hass, enable_custom_integrations):
    result = await _menu(hass, "gdacs", "gdacs_gps_loc")
    result = await _submit(hass, result, {CONF_GPS_LOC: "-33.87,151.21"})
    assert result["type"] == "create_entry"
    assert result["data"] == {CONF_PROVIDER: "gdacs", CONF_GPS_LOC: "-33.87,151.21"}
    assert result["title"] == "CAP Alerts GDACS (-33.87,151.21)"


@pytest.mark.asyncio
async def test_gdacs_gps_rejects_bad_coordinates(hass, enable_custom_integrations):
    result = await _menu(hass, "gdacs", "gdacs_gps_loc")
    result = await _submit(hass, result, {CONF_GPS_LOC: "-33.87"})
    assert result["type"] == "form"
    assert result["errors"] == {"base": "invalid_gps"}


@pytest.mark.asyncio
async def test_gdacs_tracker_creates_an_entry(hass, enable_custom_integrations):
    result = await _menu(hass, "gdacs", "gdacs_gps_tracker")
    assert result["step_id"] == "gdacs_gps_tracker"
    result = await _submit(hass, result, {CONF_TRACKER_ENTITY: _TRACKER})
    assert result["type"] == "create_entry"
    assert result["data"] == {CONF_PROVIDER: "gdacs", CONF_TRACKER_ENTITY: _TRACKER}
    # The tracker name, not "Global": the entry follows the device (#171).
    assert result["title"] == "CAP Alerts GDACS (phone)"
