"""Options-flow surface for the geocode-prefix filter (issue #73).

The validator is a pure function and is called directly. The flow itself runs
against a real Home Assistant test instance so the rendered schema and the
stored options are the ones HA actually produces.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.cap_alerts.config_flow import _validate_geocode_prefixes
from custom_components.cap_alerts.const import CONF_GEOCODE_PREFIXES

DOMAIN = "cap_alerts"


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("13", ["13"]),
        (" 13 , 37 ", ["13", "37"]),
        ("13,13", ["13"]),  # deduped, order preserved
        ("130709000000", ["130709000000"]),
        ("OHZ049", ["OHZ049"]),  # alphabetic schemes are in scope
        ("profile:CAP-CP", ["profile:CAP-CP"]),
        ("", []),  # clearing the field turns the filter off
        ("   ", []),
        (",,", []),
    ],
)
def test_accepted_prefix_lists(raw: str, expected: list[str]):
    prefixes, err = _validate_geocode_prefixes(raw)
    assert err is None
    assert prefixes == expected


@pytest.mark.parametrize("raw", ["11,@@", "13 37", "he/bei", "x" * 33, "13!"])
def test_rejected_prefix_lists(raw: str):
    prefixes, err = _validate_geocode_prefixes(raw)
    assert err == "invalid_geocode_prefix"
    assert prefixes == []


def test_input_is_stored_verbatim_not_upper_cased():
    """Casefolding happens at match time, so the user's input stays readable."""
    assert _validate_geocode_prefixes("ohz049") == (["ohz049"], None)


# ---------------------------------------------------------------------------
# Options flow
# ---------------------------------------------------------------------------


def _entry(hass, provider: str, **data) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN, title=provider, data={"provider": provider, **data}
    )
    entry.add_to_hass(hass)
    return entry


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider", "data"),
    [
        ("wmo", {"source_id": "cn-cma-xx"}),
        ("nws", {"zone_id": "OHC049"}),
        ("eccc", {"province": "ON"}),
        ("meteoalarm", {"country": "DE"}),
    ],
)
async def test_every_provider_is_offered_the_field(
    hass, enable_custom_integrations, provider: str, data: dict
):
    """Provider-neutral by design — every provider populates ``geocodes``."""
    entry = _entry(hass, provider, **data)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    key = next(
        k for k in result["data_schema"].schema if str(k) == CONF_GEOCODE_PREFIXES
    )
    assert key.default() == ""


@pytest.mark.asyncio
async def test_configured_prefixes_render_as_a_comma_list(
    hass, enable_custom_integrations
):
    entry = _entry(hass, "wmo", source_id="cn-cma-xx")
    hass.config_entries.async_update_entry(
        entry, options={CONF_GEOCODE_PREFIXES: ["13", "37"]}
    )
    result = await hass.config_entries.options.async_init(entry.entry_id)
    key = next(
        k for k in result["data_schema"].schema if str(k) == CONF_GEOCODE_PREFIXES
    )
    assert key.default() == "13,37"


@pytest.mark.asyncio
async def test_submitting_prefixes_stores_a_list(hass, enable_custom_integrations):
    entry = _entry(hass, "wmo", source_id="cn-cma-xx")
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            "scan_interval": 300,
            "timeout": 30,
            "language": "auto",
            CONF_GEOCODE_PREFIXES: " 13 , 37 ",
        },
    )
    assert result["type"] == "create_entry"
    assert result["data"][CONF_GEOCODE_PREFIXES] == ["13", "37"]


@pytest.mark.asyncio
async def test_clearing_the_field_removes_the_key(hass, enable_custom_integrations):
    """Absent rather than empty, so the coordinator short-circuits the filter."""
    entry = _entry(hass, "wmo", source_id="cn-cma-xx")
    hass.config_entries.async_update_entry(
        entry, options={CONF_GEOCODE_PREFIXES: ["13"]}
    )
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            "scan_interval": 300,
            "timeout": 30,
            "language": "auto",
            CONF_GEOCODE_PREFIXES: "",
        },
    )
    assert result["type"] == "create_entry"
    assert CONF_GEOCODE_PREFIXES not in result["data"]


@pytest.mark.asyncio
async def test_invalid_prefix_re_renders_the_form_with_the_bad_input(
    hass, enable_custom_integrations
):
    entry = _entry(hass, "wmo", source_id="cn-cma-xx")
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            "scan_interval": 300,
            "timeout": 30,
            "language": "auto",
            CONF_GEOCODE_PREFIXES: "13,@@",
        },
    )
    assert result["type"] == "form"
    assert result["errors"] == {"base": "invalid_geocode_prefix"}
    key = next(
        k for k in result["data_schema"].schema if str(k) == CONF_GEOCODE_PREFIXES
    )
    assert key.default() == "13,@@"


# ---------------------------------------------------------------------------
# WMO setup step — sets the option *before* the first refresh, so a
# high-volume source never creates the entities it is about to remove.
# ---------------------------------------------------------------------------


@pytest.fixture
def offline_wmo_registry():
    """Stub the live SWIC registry fetch the source step performs.

    The flow falls back to the static ``WMO_SOURCE_NAMES`` catalog on an empty
    result, and the step accepts custom IDs regardless, so this keeps the test
    off the network without changing what the flow does.
    """
    with patch(
        "custom_components.cap_alerts.config_flow.fetch_wmo_sources",
        new=AsyncMock(return_value=[]),
    ):
        yield


async def _reach_wmo_filter_menu(hass):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "wmo"}
    )
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {"source_id": "cn-cma-xx"}
    )


@pytest.mark.asyncio
async def test_setup_filter_menu_offers_the_geocode_mode(
    hass, enable_custom_integrations, offline_wmo_registry
):
    result = await _reach_wmo_filter_menu(hass)
    assert result["type"] == "menu"
    assert "wmo_geocode" in result["menu_options"]


@pytest.mark.asyncio
async def test_setup_geocode_step_stores_prefixes_in_options(
    hass, enable_custom_integrations, offline_wmo_registry
):
    result = await _reach_wmo_filter_menu(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "wmo_geocode"}
    )
    assert result["step_id"] == "wmo_geocode"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_GEOCODE_PREFIXES: "13, 37"}
    )
    assert result["type"] == "create_entry"
    # Options, not data — the filter is provider-neutral and lives in options.
    assert result["options"][CONF_GEOCODE_PREFIXES] == ["13", "37"]
    assert CONF_GEOCODE_PREFIXES not in result["data"]
    # Country-wide location mode: no GPS or tracker key.
    assert result["data"] == {"provider": "wmo", "source_id": "cn-cma-xx"}


@pytest.mark.asyncio
async def test_setup_geocode_step_rejects_an_empty_value(
    hass, enable_custom_integrations, offline_wmo_registry
):
    """Empty means 'off' in the options flow, but is a mistake in this step."""
    result = await _reach_wmo_filter_menu(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "wmo_geocode"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_GEOCODE_PREFIXES: "  "}
    )
    assert result["type"] == "form"
    assert result["errors"] == {"base": "invalid_geocode_prefix"}


@pytest.mark.asyncio
async def test_reconfigure_to_geocode_keeps_other_options_and_drops_gps(
    hass, enable_custom_integrations, offline_wmo_registry
):
    entry = _entry(hass, "wmo", source_id="cn-cma-xx", gps_loc="39.9,116.4")
    hass.config_entries.async_update_entry(
        entry, options={"scan_interval": 600, "language": "zh-Hans"}
    )
    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "reconfigure_wmo"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"source_id": "cn-cma-xx"}
    )
    assert "reconfigure_wmo_geocode" in result["menu_options"]
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "reconfigure_wmo_geocode"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_GEOCODE_PREFIXES: "13"}
    )
    assert result["type"] == "abort"
    assert entry.options[CONF_GEOCODE_PREFIXES] == ["13"]
    # `options=` replaces the whole mapping, so the merge is what keeps these.
    assert entry.options["scan_interval"] == 600
    assert entry.options["language"] == "zh-Hans"
    assert "gps_loc" not in entry.data
