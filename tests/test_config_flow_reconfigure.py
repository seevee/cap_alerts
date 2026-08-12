"""Reconfigure config flow: every provider's menu, defaults and update.

Reconfigure is the setup flow's mirror image with two extra jobs: it renders
the entry's current values as form defaults, and it rewrites the entry in place
(``async_update_and_abort``) instead of creating one. Both are tested per
provider here, against a real Home Assistant test instance so the schema and
the stored entry are the ones HA actually produces.

The MeteoAlarm region picker lives in ``test_meteoalarm_region_picker``, which
covers the one reconfigure step with a fetch behind it.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
import voluptuous as vol
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.cap_alerts.const import (
    CONF_COUNTRY,
    CONF_COUNTRY_ATTRIBUTE,
    CONF_COUNTRY_ENTITY,
    CONF_GEOCODE_PREFIXES,
    CONF_GPS_LOC,
    CONF_LANGUAGE,
    CONF_PROVIDER,
    CONF_PROVINCE,
    CONF_SCAN_INTERVAL,
    CONF_SOURCE_ID,
    CONF_TRACKER_ENTITY,
    CONF_ZONE_ID,
    WMO_SOURCE_NAMES,
)

DOMAIN = "cap_alerts"

_WMO_FETCH = "custom_components.cap_alerts.flows.wmo.fetch_wmo_sources"
_WMO_OPTIONS = [("mx-smn-es", "Mexico — SMN"), ("cn-cma-xx", "China — CMA")]
_TRACKER = "device_tracker.phone"
_NEW_TRACKER = "device_tracker.tablet"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _entry(hass, **data: Any) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, title="existing", data=data)
    entry.add_to_hass(hass)
    return entry


async def _reconfigure(hass, entry, *steps: str):
    """Start the reconfigure flow and walk it through ``steps``."""
    result = await entry.start_reconfigure_flow(hass)
    for step in steps:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": step}
        )
    return result


async def _submit(hass, result, user_input):
    return await hass.config_entries.flow.async_configure(result["flow_id"], user_input)


def _default(result, key_name: str):
    """The rendered default for ``key_name`` in the form's schema."""
    schema = result["data_schema"].schema
    key = next(k for k in schema if str(k) == key_name)
    return key.default()


# ---------------------------------------------------------------------------
# Menus
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reconfigure_lists_every_provider(hass, enable_custom_integrations):
    entry = _entry(hass, provider="nws", zone_id="OHZ049")
    result = await entry.start_reconfigure_flow(hass)
    assert result["type"] == "menu"
    assert result["menu_options"] == [
        "reconfigure_nws",
        "reconfigure_eccc",
        "reconfigure_meteoalarm",
        "reconfigure_wmo",
        "reconfigure_gdacs",
    ]


@pytest.mark.parametrize(
    ("step", "options"),
    [
        (
            "reconfigure_nws",
            [
                "reconfigure_nws_zone",
                "reconfigure_nws_gps_loc",
                "reconfigure_nws_gps_tracker",
            ],
        ),
        (
            "reconfigure_eccc",
            [
                "reconfigure_eccc_province",
                "reconfigure_eccc_gps_loc",
                "reconfigure_eccc_gps_tracker",
            ],
        ),
        (
            "reconfigure_meteoalarm",
            [
                "reconfigure_meteoalarm_country",
                "reconfigure_meteoalarm_country_source",
            ],
        ),
        (
            "reconfigure_gdacs",
            ["reconfigure_gdacs_global", "reconfigure_gdacs_gps_loc"],
        ),
    ],
)
@pytest.mark.asyncio
async def test_reconfigure_provider_menus(
    hass, enable_custom_integrations, step: str, options: list[str]
):
    entry = _entry(hass, provider="nws", zone_id="OHZ049")
    result = await _reconfigure(hass, entry, step)
    assert result["type"] == "menu"
    assert result["menu_options"] == options


# ---------------------------------------------------------------------------
# NWS
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reconfigure_nws_zone_rewrites_the_entry(
    hass, enable_custom_integrations
):
    entry = _entry(hass, provider="nws", zone_id="OHZ049")
    result = await _reconfigure(hass, entry, "reconfigure_nws", "reconfigure_nws_zone")
    assert _default(result, CONF_ZONE_ID) == "OHZ049"

    result = await _submit(hass, result, {CONF_ZONE_ID: "txz100"})
    assert result["type"] == "abort"
    assert result["reason"] == "reconfigure_successful"
    assert entry.data == {CONF_PROVIDER: "nws", CONF_ZONE_ID: "TXZ100"}
    assert entry.title == "CAP Alerts NWS (TXZ100)"


@pytest.mark.asyncio
async def test_reconfigure_nws_zone_rejects_a_malformed_id(
    hass, enable_custom_integrations
):
    entry = _entry(hass, provider="nws", zone_id="OHZ049")
    result = await _reconfigure(hass, entry, "reconfigure_nws", "reconfigure_nws_zone")
    result = await _submit(hass, result, {CONF_ZONE_ID: "nonsense"})
    assert result["type"] == "form"
    assert result["errors"] == {"base": "invalid_zone"}
    assert entry.data[CONF_ZONE_ID] == "OHZ049"


@pytest.mark.asyncio
async def test_reconfigure_nws_switches_zone_to_gps(hass, enable_custom_integrations):
    """A mode switch replaces entry data rather than merging: the old
    ``zone_id`` must not survive alongside the new ``gps_loc``."""
    entry = _entry(hass, provider="nws", zone_id="OHZ049")
    result = await _reconfigure(
        hass, entry, "reconfigure_nws", "reconfigure_nws_gps_loc"
    )
    assert _default(result, CONF_GPS_LOC) == ""

    result = await _submit(hass, result, {CONF_GPS_LOC: "39.96,-82.99"})
    assert result["type"] == "abort"
    assert entry.data == {CONF_PROVIDER: "nws", CONF_GPS_LOC: "39.96,-82.99"}


@pytest.mark.asyncio
async def test_reconfigure_nws_gps_rejects_bad_coordinates(
    hass, enable_custom_integrations
):
    entry = _entry(hass, provider="nws", gps_loc="39.96,-82.99")
    result = await _reconfigure(
        hass, entry, "reconfigure_nws", "reconfigure_nws_gps_loc"
    )
    result = await _submit(hass, result, {CONF_GPS_LOC: "39.96"})
    assert result["type"] == "form"
    assert result["errors"] == {"base": "invalid_gps"}


@pytest.mark.asyncio
async def test_reconfigure_nws_tracker_carries_the_current_entity(
    hass, enable_custom_integrations
):
    entry = _entry(hass, provider="nws", tracker_entity=_TRACKER)
    result = await _reconfigure(
        hass, entry, "reconfigure_nws", "reconfigure_nws_gps_tracker"
    )
    assert _default(result, CONF_TRACKER_ENTITY) == _TRACKER

    result = await _submit(hass, result, {CONF_TRACKER_ENTITY: _NEW_TRACKER})
    assert result["type"] == "abort"
    assert entry.data == {CONF_PROVIDER: "nws", CONF_TRACKER_ENTITY: _NEW_TRACKER}
    assert entry.title == "CAP Alerts NWS (tablet)"


# ---------------------------------------------------------------------------
# ECCC
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reconfigure_eccc_province_rewrites_the_entry(
    hass, enable_custom_integrations
):
    entry = _entry(hass, provider="eccc", province="ON")
    result = await _reconfigure(
        hass, entry, "reconfigure_eccc", "reconfigure_eccc_province"
    )
    assert _default(result, CONF_PROVINCE) == "ON"

    result = await _submit(hass, result, {CONF_PROVINCE: "bc"})
    assert result["type"] == "abort"
    assert entry.data == {CONF_PROVIDER: "eccc", CONF_PROVINCE: "BC"}
    assert entry.title == "CAP Alerts ECCC (BC)"


@pytest.mark.asyncio
async def test_reconfigure_eccc_province_rejects_an_unknown_code(
    hass, enable_custom_integrations
):
    entry = _entry(hass, provider="eccc", province="ON")
    result = await _reconfigure(
        hass, entry, "reconfigure_eccc", "reconfigure_eccc_province"
    )
    result = await _submit(hass, result, {CONF_PROVINCE: "ZZ"})
    assert result["type"] == "form"
    assert result["errors"] == {"base": "invalid_province"}


@pytest.mark.asyncio
async def test_reconfigure_eccc_gps_rewrites_the_entry(
    hass, enable_custom_integrations
):
    entry = _entry(hass, provider="eccc", gps_loc="45.42,-75.69")
    result = await _reconfigure(
        hass, entry, "reconfigure_eccc", "reconfigure_eccc_gps_loc"
    )
    assert _default(result, CONF_GPS_LOC) == "45.42,-75.69"

    result = await _submit(hass, result, {CONF_GPS_LOC: "49.28,-123.12"})
    assert result["type"] == "abort"
    assert entry.data == {CONF_PROVIDER: "eccc", CONF_GPS_LOC: "49.28,-123.12"}


@pytest.mark.asyncio
async def test_reconfigure_eccc_gps_rejects_bad_coordinates(
    hass, enable_custom_integrations
):
    entry = _entry(hass, provider="eccc", gps_loc="45.42,-75.69")
    result = await _reconfigure(
        hass, entry, "reconfigure_eccc", "reconfigure_eccc_gps_loc"
    )
    result = await _submit(hass, result, {CONF_GPS_LOC: "somewhere"})
    assert result["type"] == "form"
    assert result["errors"] == {"base": "invalid_gps"}


@pytest.mark.asyncio
async def test_reconfigure_eccc_tracker_carries_the_current_entity(
    hass, enable_custom_integrations
):
    entry = _entry(hass, provider="eccc", tracker_entity=_TRACKER)
    result = await _reconfigure(
        hass, entry, "reconfigure_eccc", "reconfigure_eccc_gps_tracker"
    )
    assert _default(result, CONF_TRACKER_ENTITY) == _TRACKER

    result = await _submit(hass, result, {CONF_TRACKER_ENTITY: _NEW_TRACKER})
    assert result["type"] == "abort"
    assert entry.data == {CONF_PROVIDER: "eccc", CONF_TRACKER_ENTITY: _NEW_TRACKER}


# ---------------------------------------------------------------------------
# MeteoAlarm
# ---------------------------------------------------------------------------


async def _meteoalarm_filter(hass, entry, country: str = "FI"):
    """Walk reconfigure to the MeteoAlarm filter menu."""
    result = await _reconfigure(
        hass, entry, "reconfigure_meteoalarm", "reconfigure_meteoalarm_country"
    )
    return await _submit(hass, result, {CONF_COUNTRY: country})


@pytest.mark.asyncio
async def test_reconfigure_meteoalarm_country_defaults_to_the_stored_one(
    hass, enable_custom_integrations
):
    entry = _entry(hass, provider="meteoalarm", country="FI")
    result = await _reconfigure(
        hass, entry, "reconfigure_meteoalarm", "reconfigure_meteoalarm_country"
    )
    assert _default(result, CONF_COUNTRY) == "FI"

    result = await _submit(hass, result, {CONF_COUNTRY: "AT"})
    assert result["type"] == "menu"
    assert result["menu_options"] == [
        "reconfigure_meteoalarm_country_only",
        "reconfigure_meteoalarm_gps_polygon",
        "reconfigure_meteoalarm_gps_tracker",
        "reconfigure_meteoalarm_region_picker",
    ]


@pytest.mark.asyncio
async def test_reconfigure_meteoalarm_country_offers_no_default_when_unset(
    hass, enable_custom_integrations
):
    """A fully-mobile entry has no stored country, and neither does an entry
    reconfigured over from another provider — the key must carry no default
    rather than an empty string the selector would reject."""
    entry = _entry(hass, provider="meteoalarm", tracker_entity=_TRACKER)
    result = await _reconfigure(
        hass, entry, "reconfigure_meteoalarm", "reconfigure_meteoalarm_country"
    )
    schema = result["data_schema"].schema
    key = next(k for k in schema if str(k) == CONF_COUNTRY)
    assert key.default is vol.UNDEFINED


@pytest.mark.asyncio
async def test_reconfigure_meteoalarm_country_step_reports_a_bad_code(
    hass, enable_custom_integrations
):
    """Same guard as setup: unreachable while the selector has no
    ``custom_value``, so the selector is stubbed to free text to reach it."""
    entry = _entry(hass, provider="meteoalarm", country="FI")
    with patch(
        "custom_components.cap_alerts.flows.meteoalarm._country_selector",
        return_value=str,
    ):
        result = await _reconfigure(
            hass, entry, "reconfigure_meteoalarm", "reconfigure_meteoalarm_country"
        )
        result = await _submit(hass, result, {CONF_COUNTRY: "ZZ"})
    assert result["type"] == "form"
    assert result["errors"] == {"base": "invalid_country"}
    assert entry.data[CONF_COUNTRY] == "FI"


@pytest.mark.asyncio
async def test_reconfigure_meteoalarm_country_source_keeps_an_attribute(
    hass, enable_custom_integrations
):
    entry = _entry(
        hass,
        provider="meteoalarm",
        tracker_entity=_TRACKER,
        country_entity="sensor.home_country",
    )
    result = await _reconfigure(
        hass, entry, "reconfigure_meteoalarm", "reconfigure_meteoalarm_country_source"
    )
    # Nothing stored, so the optional field renders empty rather than absent.
    assert _default(result, CONF_COUNTRY_ATTRIBUTE) == ""

    result = await _submit(
        hass,
        result,
        {
            CONF_TRACKER_ENTITY: _TRACKER,
            CONF_COUNTRY_ENTITY: "sensor.home_country",
            CONF_COUNTRY_ATTRIBUTE: " country_code ",
        },
    )
    assert result["type"] == "abort"
    assert entry.data[CONF_COUNTRY_ATTRIBUTE] == "country_code"


@pytest.mark.asyncio
async def test_reconfigure_meteoalarm_country_only_rewrites_the_entry(
    hass, enable_custom_integrations
):
    entry = _entry(hass, provider="meteoalarm", country="FI", gps_loc="60.17,24.94")
    result = await _meteoalarm_filter(hass, entry, "AT")
    result = await _submit(
        hass, result, {"next_step_id": "reconfigure_meteoalarm_country_only"}
    )
    assert result["type"] == "abort"
    # Dropping to country-wide must drop the GPS narrowing with it.
    assert entry.data == {CONF_PROVIDER: "meteoalarm", CONF_COUNTRY: "AT"}
    assert entry.title == "CAP Alerts METEOALARM (Austria)"


@pytest.mark.asyncio
async def test_reconfigure_meteoalarm_gps_polygon_rewrites_the_entry(
    hass, enable_custom_integrations
):
    entry = _entry(hass, provider="meteoalarm", country="FI", gps_loc="60.17,24.94")
    result = await _meteoalarm_filter(hass, entry)
    result = await _submit(
        hass, result, {"next_step_id": "reconfigure_meteoalarm_gps_polygon"}
    )
    assert _default(result, CONF_GPS_LOC) == "60.17,24.94"

    result = await _submit(hass, result, {CONF_GPS_LOC: "61.5,23.79"})
    assert result["type"] == "abort"
    assert entry.data == {
        CONF_PROVIDER: "meteoalarm",
        CONF_COUNTRY: "FI",
        CONF_GPS_LOC: "61.5,23.79",
    }


@pytest.mark.asyncio
async def test_reconfigure_meteoalarm_gps_polygon_rejects_bad_coordinates(
    hass, enable_custom_integrations
):
    entry = _entry(hass, provider="meteoalarm", country="FI", gps_loc="60.17,24.94")
    result = await _meteoalarm_filter(hass, entry)
    result = await _submit(
        hass, result, {"next_step_id": "reconfigure_meteoalarm_gps_polygon"}
    )
    result = await _submit(hass, result, {CONF_GPS_LOC: "60.17/24.94"})
    assert result["type"] == "form"
    assert result["errors"] == {"base": "invalid_gps"}


@pytest.mark.asyncio
async def test_reconfigure_meteoalarm_tracker_rewrites_the_entry(
    hass, enable_custom_integrations
):
    entry = _entry(hass, provider="meteoalarm", country="FI", tracker_entity=_TRACKER)
    result = await _meteoalarm_filter(hass, entry)
    result = await _submit(
        hass, result, {"next_step_id": "reconfigure_meteoalarm_gps_tracker"}
    )
    assert _default(result, CONF_TRACKER_ENTITY) == _TRACKER

    result = await _submit(hass, result, {CONF_TRACKER_ENTITY: _NEW_TRACKER})
    assert result["type"] == "abort"
    assert entry.data == {
        CONF_PROVIDER: "meteoalarm",
        CONF_COUNTRY: "FI",
        CONF_TRACKER_ENTITY: _NEW_TRACKER,
    }


@pytest.mark.asyncio
async def test_reconfigure_meteoalarm_country_source_carries_all_three_fields(
    hass, enable_custom_integrations
):
    entry = _entry(
        hass,
        provider="meteoalarm",
        tracker_entity=_TRACKER,
        country_entity="sensor.home_country",
        country_attribute="country_code",
    )
    result = await _reconfigure(
        hass, entry, "reconfigure_meteoalarm", "reconfigure_meteoalarm_country_source"
    )
    assert _default(result, CONF_TRACKER_ENTITY) == _TRACKER
    assert _default(result, CONF_COUNTRY_ENTITY) == "sensor.home_country"
    assert _default(result, CONF_COUNTRY_ATTRIBUTE) == "country_code"

    result = await _submit(
        hass,
        result,
        {
            CONF_TRACKER_ENTITY: _NEW_TRACKER,
            CONF_COUNTRY_ENTITY: "sensor.travel_country",
            CONF_COUNTRY_ATTRIBUTE: "",
        },
    )
    assert result["type"] == "abort"
    # Clearing the attribute removes the key rather than storing "".
    assert entry.data == {
        CONF_PROVIDER: "meteoalarm",
        CONF_TRACKER_ENTITY: _NEW_TRACKER,
        CONF_COUNTRY_ENTITY: "sensor.travel_country",
    }
    assert entry.title == "CAP Alerts METEOALARM (auto: tablet)"


# ---------------------------------------------------------------------------
# WMO
# ---------------------------------------------------------------------------


async def _wmo_filter(hass, entry, source_id: str = "mx-smn-es"):
    """Walk reconfigure to the WMO filter menu with the registry patched out."""
    with patch(_WMO_FETCH, return_value=list(_WMO_OPTIONS)):
        result = await _reconfigure(hass, entry, "reconfigure_wmo")
        return await _submit(hass, result, {CONF_SOURCE_ID: source_id})


@pytest.mark.asyncio
async def test_reconfigure_wmo_source_defaults_to_the_stored_id(
    hass, enable_custom_integrations
):
    entry = _entry(hass, provider="wmo", source_id="cn-cma-xx")
    with patch(_WMO_FETCH, return_value=list(_WMO_OPTIONS)):
        result = await _reconfigure(hass, entry, "reconfigure_wmo")
    assert result["step_id"] == "reconfigure_wmo_source"
    assert _default(result, CONF_SOURCE_ID) == "cn-cma-xx"


@pytest.mark.asyncio
async def test_reconfigure_wmo_source_rejects_a_bad_id(
    hass, enable_custom_integrations
):
    entry = _entry(hass, provider="wmo", source_id="cn-cma-xx")
    with patch(_WMO_FETCH, return_value=list(_WMO_OPTIONS)):
        result = await _reconfigure(hass, entry, "reconfigure_wmo")
        result = await _submit(hass, result, {CONF_SOURCE_ID: "nope"})
    assert result["type"] == "form"
    assert result["errors"] == {"base": "invalid_wmo_source"}


@pytest.mark.asyncio
async def test_reconfigure_wmo_country_wide_rewrites_the_entry(
    hass, enable_custom_integrations
):
    entry = _entry(hass, provider="wmo", source_id="cn-cma-xx", gps_loc="19.43,-99.13")
    result = await _wmo_filter(hass, entry)
    assert result["menu_options"] == [
        "reconfigure_wmo_country_wide",
        "reconfigure_wmo_gps_loc",
        "reconfigure_wmo_gps_tracker",
        "reconfigure_wmo_geocode",
    ]

    result = await _submit(
        hass, result, {"next_step_id": "reconfigure_wmo_country_wide"}
    )
    assert result["type"] == "abort"
    assert entry.data == {CONF_PROVIDER: "wmo", CONF_SOURCE_ID: "mx-smn-es"}
    assert entry.title == f"CAP Alerts WMO ({WMO_SOURCE_NAMES['mx-smn-es']})"


@pytest.mark.asyncio
async def test_reconfigure_wmo_gps_rewrites_the_entry(hass, enable_custom_integrations):
    entry = _entry(hass, provider="wmo", source_id="mx-smn-es", gps_loc="19.43,-99.13")
    result = await _wmo_filter(hass, entry)
    result = await _submit(hass, result, {"next_step_id": "reconfigure_wmo_gps_loc"})
    assert _default(result, CONF_GPS_LOC) == "19.43,-99.13"

    result = await _submit(hass, result, {CONF_GPS_LOC: "20.67,-103.35"})
    assert result["type"] == "abort"
    assert entry.data == {
        CONF_PROVIDER: "wmo",
        CONF_SOURCE_ID: "mx-smn-es",
        CONF_GPS_LOC: "20.67,-103.35",
    }


@pytest.mark.asyncio
async def test_reconfigure_wmo_gps_rejects_bad_coordinates(
    hass, enable_custom_integrations
):
    entry = _entry(hass, provider="wmo", source_id="mx-smn-es")
    result = await _wmo_filter(hass, entry)
    result = await _submit(hass, result, {"next_step_id": "reconfigure_wmo_gps_loc"})
    result = await _submit(hass, result, {CONF_GPS_LOC: "20.67"})
    assert result["type"] == "form"
    assert result["errors"] == {"base": "invalid_gps"}


@pytest.mark.asyncio
async def test_reconfigure_wmo_tracker_rewrites_the_entry(
    hass, enable_custom_integrations
):
    entry = _entry(hass, provider="wmo", source_id="mx-smn-es", tracker_entity=_TRACKER)
    result = await _wmo_filter(hass, entry)
    result = await _submit(
        hass, result, {"next_step_id": "reconfigure_wmo_gps_tracker"}
    )
    assert _default(result, CONF_TRACKER_ENTITY) == _TRACKER

    result = await _submit(hass, result, {CONF_TRACKER_ENTITY: _NEW_TRACKER})
    assert result["type"] == "abort"
    assert entry.data == {
        CONF_PROVIDER: "wmo",
        CONF_SOURCE_ID: "mx-smn-es",
        CONF_TRACKER_ENTITY: _NEW_TRACKER,
    }


@pytest.mark.asyncio
async def test_reconfigure_wmo_geocode_merges_into_existing_options(
    hass, enable_custom_integrations
):
    """``options=`` replaces the whole mapping, so the step has to merge — a
    plain assignment would silently drop scan_interval and the language."""
    entry = _entry(hass, provider="wmo", source_id="cn-cma-xx")
    hass.config_entries.async_update_entry(
        entry,
        options={
            CONF_SCAN_INTERVAL: 600,
            CONF_LANGUAGE: "zh-Hans",
            CONF_GEOCODE_PREFIXES: ["13"],
        },
    )
    result = await _wmo_filter(hass, entry, "cn-cma-xx")
    result = await _submit(hass, result, {"next_step_id": "reconfigure_wmo_geocode"})
    # The stored list renders as the comma-joined string the field takes.
    assert _default(result, CONF_GEOCODE_PREFIXES) == "13"

    result = await _submit(hass, result, {CONF_GEOCODE_PREFIXES: "13,37"})
    assert result["type"] == "abort"
    assert entry.data == {CONF_PROVIDER: "wmo", CONF_SOURCE_ID: "cn-cma-xx"}
    assert entry.options == {
        CONF_SCAN_INTERVAL: 600,
        CONF_LANGUAGE: "zh-Hans",
        CONF_GEOCODE_PREFIXES: ["13", "37"],
    }


@pytest.mark.parametrize("raw", ["", "13,@@"])
@pytest.mark.asyncio
async def test_reconfigure_wmo_geocode_rejects_an_unusable_list(
    hass, enable_custom_integrations, raw: str
):
    entry = _entry(hass, provider="wmo", source_id="cn-cma-xx")
    result = await _wmo_filter(hass, entry, "cn-cma-xx")
    result = await _submit(hass, result, {"next_step_id": "reconfigure_wmo_geocode"})
    result = await _submit(hass, result, {CONF_GEOCODE_PREFIXES: raw})
    assert result["type"] == "form"
    assert result["errors"] == {"base": "invalid_geocode_prefix"}
    assert entry.options == {}


# ---------------------------------------------------------------------------
# GDACS
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reconfigure_gdacs_global_drops_the_gps_filter(
    hass, enable_custom_integrations
):
    entry = _entry(hass, provider="gdacs", gps_loc="-33.87,151.21")
    result = await _reconfigure(
        hass, entry, "reconfigure_gdacs", "reconfigure_gdacs_global"
    )
    assert result["type"] == "abort"
    assert entry.data == {CONF_PROVIDER: "gdacs"}
    assert entry.title == "CAP Alerts GDACS (Global)"


@pytest.mark.asyncio
async def test_reconfigure_gdacs_gps_rewrites_the_entry(
    hass, enable_custom_integrations
):
    entry = _entry(hass, provider="gdacs")
    result = await _reconfigure(
        hass, entry, "reconfigure_gdacs", "reconfigure_gdacs_gps_loc"
    )
    # A global entry has no stored point, so the field starts empty.
    assert _default(result, CONF_GPS_LOC) == ""

    result = await _submit(hass, result, {CONF_GPS_LOC: "-33.87,151.21"})
    assert result["type"] == "abort"
    assert entry.data == {CONF_PROVIDER: "gdacs", CONF_GPS_LOC: "-33.87,151.21"}


@pytest.mark.asyncio
async def test_reconfigure_gdacs_gps_rejects_bad_coordinates(
    hass, enable_custom_integrations
):
    entry = _entry(hass, provider="gdacs", gps_loc="-33.87,151.21")
    result = await _reconfigure(
        hass, entry, "reconfigure_gdacs", "reconfigure_gdacs_gps_loc"
    )
    result = await _submit(hass, result, {CONF_GPS_LOC: "-33.87,181.0"})
    assert result["type"] == "form"
    assert result["errors"] == {"base": "invalid_gps"}
