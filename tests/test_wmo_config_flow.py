"""WMO config-flow surface: source-ID validation, the language option, and
the feed-source option (issue #210).

Runs against a real Home Assistant test instance
(pytest-homeassistant-custom-component) so the options schema is the one HA
actually renders. The source-ID validator is a pure function and is called
directly.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.cap_alerts.const import (
    CONF_FEED_SOURCE,
    CONF_LANGUAGE,
    WMO_FEED_SOURCES,
    WMO_LANGUAGES,
)
from custom_components.cap_alerts.flows.wmo import (
    _validate_wmo_source,
    _wmo_language_selector,
)

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


# ---------------------------------------------------------------------------
# Feed source option (issue #210)
# ---------------------------------------------------------------------------

_AVAILABLE = "custom_components.cap_alerts.flows.wmo.async_authority_feed_available"


def _schema_key(result, name: str):
    return next(k for k in result["data_schema"].schema if str(k) == name)


async def _submit(hass, entry, feed_source: str):
    result = await hass.config_entries.options.async_init(entry.entry_id)
    return await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            "scan_interval": 300,
            "timeout": 30,
            CONF_LANGUAGE: "auto",
            CONF_FEED_SOURCE: feed_source,
            "geocode_prefixes": "",
        },
    )


@pytest.mark.asyncio
async def test_wmo_options_flow_offers_feed_source_default_auto(
    hass, enable_custom_integrations
):
    entry = _entry(hass, "wmo", source_id="th-tmd-en")
    result = await hass.config_entries.options.async_init(entry.entry_id)
    key = _schema_key(result, CONF_FEED_SOURCE)
    assert key.default() == "auto"
    assert list(result["data_schema"].schema[key].container) == list(WMO_FEED_SOURCES)
    # Rendered after the language field, before the shared prefix field.
    names = [str(k) for k in result["data_schema"].schema]
    assert names.index(CONF_LANGUAGE) < names.index(CONF_FEED_SOURCE)
    assert names.index(CONF_FEED_SOURCE) < names.index("geocode_prefixes")


@pytest.mark.asyncio
async def test_wmo_options_flow_keeps_configured_feed_source(
    hass, enable_custom_integrations
):
    entry = _entry(hass, "wmo", source_id="th-tmd-en")
    hass.config_entries.async_update_entry(entry, options={CONF_FEED_SOURCE: "mirror"})
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert _schema_key(result, CONF_FEED_SOURCE).default() == "mirror"


@pytest.mark.asyncio
async def test_wmo_options_flow_auto_saves_without_asking_the_network(
    hass, enable_custom_integrations
):
    entry = _entry(hass, "wmo", source_id="th-tmd-en")
    with patch(_AVAILABLE, new_callable=AsyncMock) as available:
        result = await _submit(hass, entry, "auto")
    assert result["type"] == "create_entry"
    assert result["data"][CONF_FEED_SOURCE] == "auto"
    available.assert_not_awaited()


@pytest.mark.asyncio
async def test_wmo_options_flow_authority_on_dead_feed_errors(
    hass, enable_custom_integrations
):
    """A pin that cannot fetch is refused here, not discovered as an unavailable entry."""
    entry = _entry(hass, "wmo", source_id="ph-pagasa-en")
    with patch(_AVAILABLE, new_callable=AsyncMock, return_value=False):
        result = await _submit(hass, entry, "authority")
    assert result["type"] == "form"
    assert result["errors"] == {"base": "authority_feed_unavailable"}
    assert entry.options.get(CONF_FEED_SOURCE) is None


@pytest.mark.asyncio
async def test_wmo_options_flow_authority_on_live_feed_saves(
    hass, enable_custom_integrations
):
    entry = _entry(hass, "wmo", source_id="tl-dnmg-en")
    with patch(_AVAILABLE, new_callable=AsyncMock, return_value=True) as available:
        result = await _submit(hass, entry, "authority")
    assert result["type"] == "create_entry"
    assert result["data"][CONF_FEED_SOURCE] == "authority"
    assert available.await_args.args[1] == "tl-dnmg-en"


@pytest.mark.asyncio
async def test_wmo_options_flow_authority_check_that_times_out_errors(
    hass, enable_custom_integrations
):
    entry = _entry(hass, "wmo", source_id="tl-dnmg-en")
    with patch(_AVAILABLE, new_callable=AsyncMock, side_effect=TimeoutError):
        result = await _submit(hass, entry, "authority")
    assert result["type"] == "form"
    assert result["errors"] == {"base": "authority_feed_unavailable"}


@pytest.mark.asyncio
async def test_eccc_options_flow_feed_source_values_unchanged(
    hass, enable_custom_integrations
):
    """The key is shared with ECCC; its value set is not."""
    entry = _entry(hass, "eccc", province="ON")
    result = await hass.config_entries.options.async_init(entry.entry_id)
    key = _schema_key(result, CONF_FEED_SOURCE)
    assert key.default() == "auto"
    assert list(result["data_schema"].schema[key].container) == [
        "auto",
        "alertready",
        "pelmorex",
    ]
