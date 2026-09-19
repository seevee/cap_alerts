"""BBK setup, reconfigure and options steps (issue #66).

The shared menu lists live in ``test_config_flow_setup`` and
``test_config_flow_reconfigure``; this file holds what is BBK's alone — the
Regionalschlüssel form (normalized, then checked against the dashboard) and
the language-only options form.
"""

from __future__ import annotations

from typing import Any

import pytest
import voluptuous as vol
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.cap_alerts.const import (
    BBK_LANGUAGES,
    CONF_EXCLUDE_MARINE,
    CONF_GEOCODE_PREFIXES,
    CONF_GPS_LOC,
    CONF_LANGUAGE,
    CONF_PROVIDER,
    CONF_SCAN_INTERVAL,
    CONF_TRACKER_ENTITY,
    CONF_ZONE_ID,
)
from custom_components.cap_alerts.flows.common import ScopedEntryFlowMixin

DOMAIN = "cap_alerts"
_TRACKER = "device_tracker.phone"
_ARS = "095640000000"


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
        domain=DOMAIN, title="existing", data={CONF_PROVIDER: "bbk", **data}
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


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_region_creates_an_entry_at_district_granularity(
    hass, enable_custom_integrations
):
    result = await _menu(hass, "bbk", "bbk_region")
    assert result["type"] == "form"
    assert _field_names(result) == [CONF_ZONE_ID]
    # Five digits typed, twelve stored: the dashboard answers at Kreis level.
    result = await _submit(hass, result, {CONF_ZONE_ID: "09564"})
    assert result["type"] == "create_entry"
    assert result["data"] == {CONF_PROVIDER: "bbk", CONF_ZONE_ID: _ARS}
    assert result["title"] == f"CAP Alerts BBK ({_ARS})"


@pytest.mark.asyncio
async def test_region_rejects_a_malformed_code(hass, enable_custom_integrations):
    result = await _menu(hass, "bbk", "bbk_region")
    result = await _submit(hass, result, {CONF_ZONE_ID: "Nürnberg"})
    assert result["type"] == "form"
    assert result["errors"] == {"base": "invalid_bbk_region"}


@pytest.mark.asyncio
async def test_region_reports_a_district_the_dashboard_does_not_know(
    hass, enable_custom_integrations, monkeypatch
):
    """The scope check's answer lands on the form that collected the code."""

    async def _unknown(self, data):
        assert data == {CONF_PROVIDER: "bbk", CONF_ZONE_ID: _ARS}
        return "unknown_bbk_region"

    monkeypatch.setattr(ScopedEntryFlowMixin, "_async_validate_scope", _unknown)
    result = await _menu(hass, "bbk", "bbk_region")
    result = await _submit(hass, result, {CONF_ZONE_ID: _ARS})
    assert result["type"] == "form"
    assert result["errors"] == {"base": "unknown_bbk_region"}


@pytest.mark.asyncio
async def test_gps_creates_an_entry(hass, enable_custom_integrations):
    result = await _menu(hass, "bbk", "bbk_gps_loc")
    result = await _submit(hass, result, {CONF_GPS_LOC: "52.52,13.405"})
    assert result["type"] == "create_entry"
    assert result["data"] == {CONF_PROVIDER: "bbk", CONF_GPS_LOC: "52.52,13.405"}
    assert result["title"] == "CAP Alerts BBK (52.52,13.405)"


@pytest.mark.asyncio
async def test_gps_rejects_bad_coordinates(hass, enable_custom_integrations):
    result = await _menu(hass, "bbk", "bbk_gps_loc")
    result = await _submit(hass, result, {CONF_GPS_LOC: "52.52"})
    assert result["type"] == "form"
    assert result["errors"] == {"base": "invalid_gps"}


@pytest.mark.asyncio
async def test_tracker_creates_an_entry(hass, enable_custom_integrations):
    result = await _menu(hass, "bbk", "bbk_gps_tracker")
    assert result["step_id"] == "bbk_gps_tracker"
    result = await _submit(hass, result, {CONF_TRACKER_ENTITY: _TRACKER})
    assert result["type"] == "create_entry"
    assert result["data"] == {CONF_PROVIDER: "bbk", CONF_TRACKER_ENTITY: _TRACKER}
    assert result["title"] == "CAP Alerts BBK (phone)"


@pytest.mark.asyncio
async def test_the_same_district_twice_is_refused(hass, enable_custom_integrations):
    _entry(hass, zone_id=_ARS)
    result = await _menu(hass, "bbk", "bbk_region")
    result = await _submit(hass, result, {CONF_ZONE_ID: "09564"})
    assert result["type"] == "abort"
    assert result["reason"] == "already_configured"


# ---------------------------------------------------------------------------
# Reconfigure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reconfigure_region_shows_the_stored_code_and_rewrites(
    hass, enable_custom_integrations
):
    entry = _entry(hass, zone_id=_ARS)
    result = await _reconfigure(
        hass, entry, "reconfigure_bbk", "reconfigure_bbk_region"
    )
    assert result["type"] == "form"
    assert _default(result, CONF_ZONE_ID) == _ARS
    result = await _submit(hass, result, {CONF_ZONE_ID: "11000"})
    assert result["type"] == "abort"
    assert result["reason"] == "reconfigure_successful"
    assert entry.data == {CONF_PROVIDER: "bbk", CONF_ZONE_ID: "110000000000"}
    assert entry.title == "CAP Alerts BBK (110000000000)"


@pytest.mark.asyncio
async def test_reconfigure_region_from_gps_renders_an_empty_required_field(
    hass, enable_custom_integrations
):
    entry = _entry(hass, gps_loc="52.52,13.405")
    result = await _reconfigure(
        hass, entry, "reconfigure_bbk", "reconfigure_bbk_region"
    )
    key = next(k for k in result["data_schema"].schema if str(k) == CONF_ZONE_ID)
    assert key.default is vol.UNDEFINED
    result = await _submit(hass, result, {CONF_ZONE_ID: "bad"})
    assert result["errors"] == {"base": "invalid_bbk_region"}


@pytest.mark.asyncio
async def test_reconfigure_region_reports_an_unknown_district(
    hass, enable_custom_integrations, monkeypatch
):
    async def _unknown(self, data):
        return "unknown_bbk_region"

    monkeypatch.setattr(ScopedEntryFlowMixin, "_async_validate_scope", _unknown)
    entry = _entry(hass, zone_id=_ARS)
    result = await _reconfigure(
        hass, entry, "reconfigure_bbk", "reconfigure_bbk_region"
    )
    result = await _submit(hass, result, {CONF_ZONE_ID: "11000"})
    assert result["type"] == "form"
    assert result["errors"] == {"base": "unknown_bbk_region"}
    assert entry.data[CONF_ZONE_ID] == _ARS


@pytest.mark.asyncio
async def test_reconfigure_gps_rewrites_the_entry(hass, enable_custom_integrations):
    entry = _entry(hass, zone_id=_ARS)
    result = await _reconfigure(
        hass, entry, "reconfigure_bbk", "reconfigure_bbk_gps_loc"
    )
    result = await _submit(hass, result, {CONF_GPS_LOC: "52.52,13.405"})
    assert result["type"] == "abort"
    assert entry.data == {CONF_PROVIDER: "bbk", CONF_GPS_LOC: "52.52,13.405"}


@pytest.mark.asyncio
async def test_reconfigure_gps_rejects_bad_coordinates(
    hass, enable_custom_integrations
):
    entry = _entry(hass, gps_loc="52.52,13.405")
    result = await _reconfigure(
        hass, entry, "reconfigure_bbk", "reconfigure_bbk_gps_loc"
    )
    assert _default(result, CONF_GPS_LOC) == "52.52,13.405"
    result = await _submit(hass, result, {CONF_GPS_LOC: "52.52"})
    assert result["type"] == "form"
    assert result["errors"] == {"base": "invalid_gps"}


@pytest.mark.asyncio
async def test_reconfigure_tracker_carries_the_current_entity(
    hass, enable_custom_integrations
):
    entry = _entry(hass, tracker_entity=_TRACKER)
    result = await _reconfigure(
        hass, entry, "reconfigure_bbk", "reconfigure_bbk_gps_tracker"
    )
    assert _default(result, CONF_TRACKER_ENTITY) == _TRACKER
    result = await _submit(hass, result, {CONF_TRACKER_ENTITY: "device_tracker.tablet"})
    assert result["type"] == "abort"
    assert entry.data == {
        CONF_PROVIDER: "bbk",
        CONF_TRACKER_ENTITY: "device_tracker.tablet",
    }
    assert entry.title == "CAP Alerts BBK (tablet)"


# ---------------------------------------------------------------------------
# Options
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_options_offer_language_and_nothing_provider_foreign(
    hass, enable_custom_integrations
):
    entry = _entry(hass, zone_id=_ARS)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    names = _field_names(result)
    assert names == [CONF_SCAN_INTERVAL, "timeout", CONF_LANGUAGE]
    assert CONF_EXCLUDE_MARINE not in names
    assert CONF_GEOCODE_PREFIXES not in names
    assert _default(result, CONF_LANGUAGE) == "auto"
    language_key = next(
        k for k in result["data_schema"].schema if str(k) == CONF_LANGUAGE
    )
    selector = result["data_schema"].schema[language_key]
    offered = [opt["value"] for opt in selector.config["options"]]
    assert offered == list(BBK_LANGUAGES)
    assert "de-LS" in offered
    assert selector.config.get("custom_value", False) is False


@pytest.mark.asyncio
async def test_options_store_the_chosen_language(hass, enable_custom_integrations):
    entry = _entry(hass, zone_id=_ARS)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_SCAN_INTERVAL: 300, "timeout": 30, CONF_LANGUAGE: "de-LS"},
    )
    assert result["type"] == "create_entry"
    assert entry.options[CONF_LANGUAGE] == "de-LS"
