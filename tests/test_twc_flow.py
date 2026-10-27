"""U.S. Tsunami Warning Centers setup, reconfigure and options steps (issue #298).

The shared menu lists live in ``test_config_flow_setup`` and
``test_config_flow_reconfigure``; this file holds what is the TWC
provider's alone: the region dropdown and an options form with nothing but
the polling fields.
"""

from __future__ import annotations

from typing import Any

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.cap_alerts.const import (
    CONF_PROVIDER,
    CONF_PROVINCE,
    CONF_SCAN_INTERVAL,
    TWC_REGIONS,
)

DOMAIN = "cap_alerts"


async def _menu(hass, *steps: str):
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


def _entry(hass, **data: Any) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN, title="existing", data={CONF_PROVIDER: "twc", **data}
    )
    entry.add_to_hass(hass)
    return entry


async def _reconfigure(hass, entry, *steps: str):
    result = await entry.start_reconfigure_flow(hass)
    for step in steps:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": step}
        )
    return result


def _field_names(result) -> list[str]:
    return [str(k) for k in result["data_schema"].schema]


def _key(result, key_name: str):
    return next(k for k in result["data_schema"].schema if str(k) == key_name)


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_menu_offers_region_and_back(hass, enable_custom_integrations):
    result = await _menu(hass, "twc")
    assert result["type"] == "menu"
    assert result["menu_options"] == ["twc_region", "user"]


@pytest.mark.asyncio
async def test_region_form_offers_every_region_in_order(
    hass, enable_custom_integrations
):
    result = await _menu(hass, "twc", "twc_region")
    assert result["type"] == "form"
    assert _field_names(result) == [CONF_PROVINCE]
    selector = result["data_schema"].schema[_key(result, CONF_PROVINCE)]
    assert [o["value"] for o in selector.config["options"]] == list(TWC_REGIONS)


@pytest.mark.asyncio
async def test_region_creates_an_entry(hass, enable_custom_integrations):
    result = await _menu(hass, "twc", "twc_region")
    result = await _submit(hass, result, {CONF_PROVINCE: "HI"})
    assert result["type"] == "create_entry"
    assert result["data"] == {CONF_PROVIDER: "twc", CONF_PROVINCE: "HI"}
    assert result["title"] == "CAP Alerts TWC (HI)"


@pytest.mark.asyncio
async def test_same_region_twice_is_already_configured(
    hass, enable_custom_integrations
):
    _entry(hass, province="AK")
    result = await _menu(hass, "twc", "twc_region")
    result = await _submit(hass, result, {CONF_PROVINCE: "AK"})
    assert result["type"] == "abort"
    assert result["reason"] == "already_configured"


# ---------------------------------------------------------------------------
# Reconfigure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reconfigure_prefills_and_rewrites_the_region(
    hass, enable_custom_integrations
):
    entry = _entry(hass, province="GU")
    result = await _reconfigure(
        hass, entry, "reconfigure_twc", "reconfigure_twc_region"
    )
    assert result["type"] == "form"
    assert _key(result, CONF_PROVINCE).default() == "GU"

    result = await _submit(hass, result, {CONF_PROVINCE: "AS"})
    assert result["type"] == "abort"
    assert result["reason"] == "reconfigure_successful"
    assert entry.data == {CONF_PROVIDER: "twc", CONF_PROVINCE: "AS"}
    assert entry.title == "CAP Alerts TWC (AS)"


# ---------------------------------------------------------------------------
# Options
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_options_are_the_polling_fields_alone(hass, enable_custom_integrations):
    """No language, no marine toggle, no area-code narrowing (#298)."""
    entry = _entry(hass, province="PR")
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert _field_names(result) == [CONF_SCAN_INTERVAL, "timeout"]
