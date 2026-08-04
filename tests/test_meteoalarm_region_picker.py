"""MeteoAlarm region-picker config-flow surface (issue #48).

The picker can only list regions named by warnings currently in the feed — no
usable regions endpoint exists — so it accepts typed-in codes and must not drop
a stored code just because the current fetch doesn't offer it. When the feed
names nothing at all, there is no form worth rendering.

Runs against a real Home Assistant test instance
(pytest-homeassistant-custom-component) so the schema under test is the one HA
actually renders. ``fetch_regions_for_country`` is patched, so no HTTP happens.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.cap_alerts.config_flow import (
    _normalize_region_selection,
    _region_label_map,
    _region_selector,
)
from custom_components.cap_alerts.const import (
    CONF_COUNTRY,
    CONF_PROVIDER,
    CONF_REGION_LABELS,
    CONF_REGIONS,
)

DOMAIN = "cap_alerts"

_FETCHED = [("FI811", "Saaristomeri"), ("FI813", "Ahvenanmeri")]

_PATCH_TARGET = "custom_components.cap_alerts.config_flow.fetch_regions_for_country"


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_region_selector_accepts_custom_values():
    config = _region_selector(_FETCHED).config
    assert config["custom_value"] is True
    assert config["multiple"] is True
    assert config["sort"] is True
    assert [opt["value"] for opt in config["options"]] == ["FI811", "FI813"]


def test_region_selector_merges_extra_options():
    config = _region_selector(_FETCHED, extra=[("FI815", "FI815")]).config
    assert [opt["value"] for opt in config["options"]] == ["FI811", "FI813", "FI815"]
    assert config["options"][-1]["label"] == "FI815"


def test_normalize_region_selection_strips_dedupes_and_drops_empties():
    assert _normalize_region_selection([" FI811 ", "FI813", "FI811", "", "   "]) == [
        "FI811",
        "FI813",
    ]
    assert _normalize_region_selection([]) == []


def test_region_label_map_prefers_fetched_then_stored_then_the_code():
    labels = _region_label_map(
        ["FI811", "FI810", "FI815"],
        {"FI811": "Saaristomeri"},
        {"FI810": "Perämeren eteläosa"},
    )
    assert labels == {
        "FI811": "Saaristomeri",
        "FI810": "Perämeren eteläosa",
        "FI815": "FI815",
    }


# ---------------------------------------------------------------------------
# Setup flow
# ---------------------------------------------------------------------------


async def _start_region_picker(hass):
    """Walk the setup flow to the region-picker form for Finland."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "meteoalarm"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "meteoalarm_country"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_COUNTRY: "FI"}
    )
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "meteoalarm_region_picker"}
    )


@pytest.mark.asyncio
async def test_setup_stores_a_typed_code_with_a_self_label(
    hass, enable_custom_integrations
):
    with patch(_PATCH_TARGET, return_value=list(_FETCHED)):
        result = await _start_region_picker(hass)
        assert result["step_id"] == "meteoalarm_region_picker"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_REGIONS: ["FI811", " FI815 "]}
        )

    assert result["type"] == "create_entry"
    data = result["data"]
    assert data[CONF_PROVIDER] == "meteoalarm"
    assert data[CONF_REGIONS] == ["FI811", "FI815"]
    # A code no live warning names still gets an entry in the label map, so
    # every selection is labeled and the device title can count them.
    assert data[CONF_REGION_LABELS] == {
        "FI811": "Saaristomeri",
        "FI815": "FI815",
    }


@pytest.mark.asyncio
async def test_setup_rejects_an_empty_selection(hass, enable_custom_integrations):
    with patch(_PATCH_TARGET, return_value=list(_FETCHED)):
        result = await _start_region_picker(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_REGIONS: ["  "]}
        )

    assert result["type"] == "form"
    assert result["errors"] == {"base": "no_regions_selected"}


@pytest.mark.asyncio
async def test_setup_aborts_when_the_feed_names_no_regions(
    hass, enable_custom_integrations
):
    # Iceland and Malta: the feed reads fine and offers nothing. The old
    # ``cannot_fetch_regions`` form invited a retry that could not help.
    with patch(_PATCH_TARGET, return_value=[]):
        result = await _start_region_picker(hass)

    assert result["type"] == "abort"
    assert result["reason"] == "no_regions_available"


@pytest.mark.asyncio
async def test_setup_still_errors_when_the_fetch_fails(
    hass, enable_custom_integrations
):
    # A genuine outage keeps the retryable error — distinct from the abort.
    with patch(_PATCH_TARGET, side_effect=UpdateFailed("boom")):
        result = await _start_region_picker(hass)

    assert result["type"] == "form"
    assert result["errors"] == {"base": "cannot_fetch_regions"}


# ---------------------------------------------------------------------------
# Reconfigure flow
# ---------------------------------------------------------------------------


def _fi_entry(hass) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="CAP Alerts METEOALARM (FI — Saaristomeri +1)",
        data={
            CONF_PROVIDER: "meteoalarm",
            CONF_COUNTRY: "FI",
            CONF_REGIONS: ["FI811", "FI815"],
            CONF_REGION_LABELS: {"FI811": "Saaristomeri", "FI815": "FI815"},
        },
    )
    entry.add_to_hass(hass)
    return entry


async def _start_reconfigure(hass, entry):
    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "reconfigure_meteoalarm"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "reconfigure_meteoalarm_country"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_COUNTRY: "FI"}
    )
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "reconfigure_meteoalarm_region_picker"}
    )


@pytest.mark.asyncio
async def test_reconfigure_keeps_a_code_the_fetch_no_longer_offers(
    hass, enable_custom_integrations
):
    entry = _fi_entry(hass)
    with patch(_PATCH_TARGET, return_value=list(_FETCHED)):
        result = await _start_reconfigure(hass, entry)

        assert result["step_id"] == "reconfigure_meteoalarm_region_picker"
        schema = result["data_schema"].schema
        key = next(k for k in schema if str(k) == CONF_REGIONS)
        # FI815 has no live warning, so it is absent from the fetched list —
        # it must still be pre-selected and offered as an option, or saving
        # the form would silently drop it.
        assert key.default() == ["FI811", "FI815"]
        options = [opt["value"] for opt in schema[key].config["options"]]
        assert options == ["FI811", "FI813", "FI815"]

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_REGIONS: ["FI811", "FI815"]}
        )

    assert result["type"] == "abort"
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_REGIONS] == ["FI811", "FI815"]
    # The stored label survives even though the fetch didn't supply one.
    assert entry.data[CONF_REGION_LABELS] == {
        "FI811": "Saaristomeri",
        "FI815": "FI815",
    }


@pytest.mark.asyncio
async def test_reconfigure_renders_stored_codes_against_an_empty_fetch(
    hass, enable_custom_integrations
):
    # A quiet feed must not abort reconfigure: the stored selections are the
    # whole point of the form, and they are still offered as options.
    entry = _fi_entry(hass)
    with patch(_PATCH_TARGET, return_value=[]):
        result = await _start_reconfigure(hass, entry)

    assert result["step_id"] == "reconfigure_meteoalarm_region_picker"
    schema = result["data_schema"].schema
    key = next(k for k in schema if str(k) == CONF_REGIONS)
    assert key.default() == ["FI811", "FI815"]
    options = [opt["value"] for opt in schema[key].config["options"]]
    assert options == ["FI811", "FI815"]


@pytest.mark.asyncio
async def test_reconfigure_aborts_with_nothing_fetched_and_nothing_stored(
    hass, enable_custom_integrations
):
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="CAP Alerts METEOALARM (FI)",
        data={CONF_PROVIDER: "meteoalarm", CONF_COUNTRY: "FI", CONF_REGIONS: []},
    )
    entry.add_to_hass(hass)
    with patch(_PATCH_TARGET, return_value=[]):
        result = await _start_reconfigure(hass, entry)

    assert result["type"] == "abort"
    assert result["reason"] == "no_regions_available"
