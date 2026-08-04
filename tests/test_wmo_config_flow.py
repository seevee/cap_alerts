"""WMO config-flow surface: source-ID validation and the language option.

Runs against a real Home Assistant test instance
(pytest-homeassistant-custom-component) so the options schema is the one HA
actually renders. The source-ID validator is a pure function and is called
directly.
"""

from __future__ import annotations

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.cap_alerts.config_flow import (
    _validate_wmo_source,
    _wmo_language_selector,
)
from custom_components.cap_alerts.const import CONF_LANGUAGE, WMO_LANGUAGES

DOMAIN = "cap_alerts"


# ---------------------------------------------------------------------------
# Source-ID validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "source_id",
    [
        "mx-smn-es",
        "cn-cma-xx",
        "tl-dnmg-tet",
        # Real registry IDs the pre-#59 {country}-{agency}-{lang} regex rejected.
        "lu-ana-meteo-fr",
        "us-noaa-nws-en",
        "us-noaa-nws-en-marine",
    ],
)
def test_valid_wmo_source_ids_accepted(source_id: str):
    cleaned, err = _validate_wmo_source(source_id)
    assert err is None
    assert cleaned == source_id


def test_wmo_source_id_is_normalized():
    assert _validate_wmo_source("  MX-SMN-ES  ") == ("mx-smn-es", None)


@pytest.mark.parametrize("source_id", ["", "   ", "mx", "mx-smn", "not_a_source"])
def test_invalid_wmo_source_ids_rejected(source_id: str):
    _cleaned, err = _validate_wmo_source(source_id)
    assert err == "invalid_wmo_source"


# ---------------------------------------------------------------------------
# Language option (issue #59)
# ---------------------------------------------------------------------------


def test_language_selector_accepts_custom_values_and_leads_with_auto():
    config = _wmo_language_selector().config
    assert config["custom_value"] is True
    assert config["sort"] is False
    assert [opt["value"] for opt in config["options"]] == list(WMO_LANGUAGES)


def test_language_catalog_leads_with_auto_then_sorted_unique_subtags():
    """``sort=False`` keeps ``auto`` first, so the const carries the ordering."""
    assert WMO_LANGUAGES[0] == "auto"
    subtags = list(WMO_LANGUAGES[1:])
    assert subtags == sorted(subtags)
    assert len(subtags) == len(set(subtags))


def _entry(hass, provider: str, **data) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN, title=provider, data={"provider": provider, **data}
    )
    entry.add_to_hass(hass)
    return entry


@pytest.mark.asyncio
async def test_wmo_options_flow_offers_language(hass, enable_custom_integrations):
    entry = _entry(hass, "wmo", source_id="cn-cma-xx")
    result = await hass.config_entries.options.async_init(entry.entry_id)
    schema = result["data_schema"].schema
    key = next(k for k in schema if str(k) == CONF_LANGUAGE)
    assert key.default() == "auto"


@pytest.mark.asyncio
async def test_wmo_options_flow_keeps_a_configured_language(
    hass, enable_custom_integrations
):
    entry = _entry(hass, "wmo", source_id="cn-cma-xx")
    hass.config_entries.async_update_entry(entry, options={CONF_LANGUAGE: "zh-Hans"})
    result = await hass.config_entries.options.async_init(entry.entry_id)
    key = next(k for k in result["data_schema"].schema if str(k) == CONF_LANGUAGE)
    assert key.default() == "zh-Hans"


@pytest.mark.asyncio
async def test_nws_options_flow_has_no_language(hass, enable_custom_integrations):
    entry = _entry(hass, "nws", zone_id="OHC049")
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert not any(str(k) == CONF_LANGUAGE for k in result["data_schema"].schema)
