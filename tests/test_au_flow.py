"""Australian setup, reconfigure and options steps (issue #127).

The shared menu lists live in ``test_config_flow_setup`` and
``test_config_flow_reconfigure``; this file holds what is AU's alone — the
state dropdown and the alert-level-only options form.
"""

from __future__ import annotations

from typing import Any

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.cap_alerts.const import (
    AU_ALERT_LEVEL_ALL,
    AU_ALERT_LEVELS,
    AU_STATE_LABELS,
    CONF_ALERT_LEVEL,
    CONF_EXCLUDE_MARINE,
    CONF_GEOCODE_PREFIXES,
    CONF_LANGUAGE,
    CONF_PROVIDER,
    CONF_PROVINCE,
    CONF_SCAN_INTERVAL,
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
        domain=DOMAIN, title="existing", data={CONF_PROVIDER: "au", **data}
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


def _default(result, key_name: str):
    schema = result["data_schema"].schema
    key = next(k for k in schema if str(k) == key_name)
    return key.default()


def _field_names(result) -> list[str]:
    return [str(k) for k in result["data_schema"].schema]


def _selector(result, key_name: str):
    schema = result["data_schema"].schema
    key = next(k for k in schema if str(k) == key_name)
    return schema[key]


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_state_form_offers_every_feed_in_order(hass, enable_custom_integrations):
    result = await _menu(hass, "au", "au_state")
    assert result["type"] == "form"
    assert _field_names(result) == [CONF_PROVINCE]
    offered = [
        opt["value"] for opt in _selector(result, CONF_PROVINCE).config["options"]
    ]
    assert offered == list(AU_STATE_LABELS)
    assert _selector(result, CONF_PROVINCE).config.get("custom_value", False) is False


@pytest.mark.asyncio
async def test_state_creates_an_entry(hass, enable_custom_integrations):
    result = await _menu(hass, "au", "au_state")
    result = await _submit(hass, result, {CONF_PROVINCE: "QLD"})
    assert result["type"] == "create_entry"
    assert result["data"] == {CONF_PROVIDER: "au", CONF_PROVINCE: "QLD"}
    assert result["title"] == "CAP Alerts AU (QLD)"


@pytest.mark.asyncio
async def test_same_state_twice_is_already_configured(hass, enable_custom_integrations):
    _entry(hass, province="NSW")
    result = await _menu(hass, "au", "au_state")
    result = await _submit(hass, result, {CONF_PROVINCE: "NSW"})
    assert result["type"] == "abort"
    assert result["reason"] == "already_configured"


# ---------------------------------------------------------------------------
# Reconfigure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reconfigure_prefills_the_current_state(hass, enable_custom_integrations):
    entry = _entry(hass, province="WA")
    result = await _reconfigure(hass, entry, "reconfigure_au", "reconfigure_au_state")
    assert result["type"] == "form"
    assert _default(result, CONF_PROVINCE) == "WA"


@pytest.mark.asyncio
async def test_reconfigure_rewrites_the_state(hass, enable_custom_integrations):
    entry = _entry(hass, province="WA")
    result = await _reconfigure(hass, entry, "reconfigure_au", "reconfigure_au_state")
    result = await _submit(hass, result, {CONF_PROVINCE: "TAS"})
    assert result["type"] == "abort"
    assert result["reason"] == "reconfigure_successful"
    assert entry.data == {CONF_PROVIDER: "au", CONF_PROVINCE: "TAS"}
    assert entry.title == "CAP Alerts AU (TAS)"


# ---------------------------------------------------------------------------
# Options
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_options_offer_the_alert_level_floor_and_nothing_provider_foreign(
    hass, enable_custom_integrations
):
    entry = _entry(hass, province="NSW")
    result = await hass.config_entries.options.async_init(entry.entry_id)
    names = _field_names(result)
    assert names == [CONF_SCAN_INTERVAL, "timeout", CONF_ALERT_LEVEL]
    assert CONF_LANGUAGE not in names
    assert CONF_EXCLUDE_MARINE not in names
    assert CONF_GEOCODE_PREFIXES not in names
    assert _default(result, CONF_ALERT_LEVEL) == AU_ALERT_LEVEL_ALL
    assert list(_selector(result, CONF_ALERT_LEVEL).container) == [
        AU_ALERT_LEVEL_ALL,
        *AU_ALERT_LEVELS,
    ]


@pytest.mark.asyncio
async def test_options_store_the_chosen_floor(hass, enable_custom_integrations):
    entry = _entry(hass, province="NSW")
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_SCAN_INTERVAL: 300, "timeout": 30, CONF_ALERT_LEVEL: "Watch and Act"},
    )
    assert result["type"] == "create_entry"
    assert entry.options[CONF_ALERT_LEVEL] == "Watch and Act"
