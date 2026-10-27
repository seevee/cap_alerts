"""GeoSphere Austria setup, reconfigure and options steps (issue #300).

The shared menu lists live in ``test_config_flow_setup`` and
``test_config_flow_reconfigure``; this file holds what is GeoSphere's alone:
the GPS form checked against the API, the tracker form, and the language-only
options form with area-code narrowing kept.
"""

from __future__ import annotations

from typing import Any

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.cap_alerts.const import (
    CONF_EXCLUDE_MARINE,
    CONF_GEOCODE_PREFIXES,
    CONF_GPS_LOC,
    CONF_LANGUAGE,
    CONF_PROVIDER,
    CONF_SCAN_INTERVAL,
    CONF_TRACKER_ENTITY,
    GEOSPHERE_LANGUAGES,
)
from custom_components.cap_alerts.flows.common import ScopedEntryFlowMixin

DOMAIN = "cap_alerts"
_TRACKER = "device_tracker.phone"
_VIENNA = "48.2082,16.3738"
_GRAZ = "47.0707,15.4395"


@pytest.fixture
def scope_checks(monkeypatch) -> list[dict[str, Any]]:
    """Record every scope check and answer it as a point inside Austria."""
    seen: list[dict[str, Any]] = []

    async def _inside(self, data):
        seen.append(dict(data))

    monkeypatch.setattr(ScopedEntryFlowMixin, "_async_validate_scope", _inside)
    return seen


@pytest.fixture
def outside(monkeypatch) -> None:
    async def _outside(self, data):
        return "geosphere_outside_austria"

    monkeypatch.setattr(ScopedEntryFlowMixin, "_async_validate_scope", _outside)


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
        domain=DOMAIN, title="existing", data={CONF_PROVIDER: "geosphere", **data}
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
async def test_gps_is_checked_then_creates_an_entry(
    hass, enable_custom_integrations, scope_checks
):
    result = await _menu(hass, "geosphere", "geosphere_gps_loc")
    assert result["type"] == "form"
    assert _field_names(result) == [CONF_GPS_LOC]
    result = await _submit(hass, result, {CONF_GPS_LOC: _VIENNA})
    assert result["type"] == "create_entry"
    assert result["data"] == {CONF_PROVIDER: "geosphere", CONF_GPS_LOC: _VIENNA}
    assert result["title"] == f"CAP Alerts GEOSPHERE ({_VIENNA})"
    assert scope_checks == [result["data"]]


@pytest.mark.asyncio
async def test_gps_outside_austria_is_reported_on_the_form(
    hass, enable_custom_integrations, outside
):
    result = await _menu(hass, "geosphere", "geosphere_gps_loc")
    result = await _submit(hass, result, {CONF_GPS_LOC: "52.52,13.405"})
    assert result["type"] == "form"
    assert result["errors"] == {"base": "geosphere_outside_austria"}


@pytest.mark.asyncio
async def test_gps_rejects_bad_coordinates_before_asking(
    hass, enable_custom_integrations, scope_checks
):
    result = await _menu(hass, "geosphere", "geosphere_gps_loc")
    result = await _submit(hass, result, {CONF_GPS_LOC: "48.2"})
    assert result["type"] == "form"
    assert result["errors"] == {"base": "invalid_gps"}
    assert scope_checks == []


@pytest.mark.asyncio
async def test_tracker_creates_an_entry_unchecked(
    hass, enable_custom_integrations, scope_checks
):
    result = await _menu(hass, "geosphere", "geosphere_gps_tracker")
    assert result["step_id"] == "geosphere_gps_tracker"
    result = await _submit(hass, result, {CONF_TRACKER_ENTITY: _TRACKER})
    assert result["type"] == "create_entry"
    assert result["data"] == {
        CONF_PROVIDER: "geosphere",
        CONF_TRACKER_ENTITY: _TRACKER,
    }
    assert result["title"] == "CAP Alerts GEOSPHERE (phone)"
    assert scope_checks == []


@pytest.mark.asyncio
async def test_the_same_point_twice_is_refused(
    hass, enable_custom_integrations, scope_checks
):
    _entry(hass, gps_loc=_VIENNA)
    result = await _menu(hass, "geosphere", "geosphere_gps_loc")
    result = await _submit(hass, result, {CONF_GPS_LOC: _VIENNA})
    assert result["type"] == "abort"
    assert result["reason"] == "already_configured"


# ---------------------------------------------------------------------------
# Reconfigure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reconfigure_gps_shows_the_stored_point_and_rewrites(
    hass, enable_custom_integrations, scope_checks
):
    entry = _entry(hass, gps_loc=_VIENNA)
    result = await _reconfigure(
        hass, entry, "reconfigure_geosphere", "reconfigure_geosphere_gps_loc"
    )
    assert _default(result, CONF_GPS_LOC) == _VIENNA
    result = await _submit(hass, result, {CONF_GPS_LOC: _GRAZ})
    assert result["type"] == "abort"
    assert result["reason"] == "reconfigure_successful"
    assert entry.data == {CONF_PROVIDER: "geosphere", CONF_GPS_LOC: _GRAZ}
    assert entry.title == f"CAP Alerts GEOSPHERE ({_GRAZ})"


@pytest.mark.asyncio
async def test_reconfigure_gps_rejects_bad_coordinates(
    hass, enable_custom_integrations, scope_checks
):
    entry = _entry(hass, tracker_entity=_TRACKER)
    result = await _reconfigure(
        hass, entry, "reconfigure_geosphere", "reconfigure_geosphere_gps_loc"
    )
    result = await _submit(hass, result, {CONF_GPS_LOC: "48.2"})
    assert result["type"] == "form"
    assert result["errors"] == {"base": "invalid_gps"}


@pytest.mark.asyncio
async def test_reconfigure_gps_outside_austria_keeps_the_entry(
    hass, enable_custom_integrations, outside
):
    entry = _entry(hass, gps_loc=_VIENNA)
    result = await _reconfigure(
        hass, entry, "reconfigure_geosphere", "reconfigure_geosphere_gps_loc"
    )
    result = await _submit(hass, result, {CONF_GPS_LOC: "52.52,13.405"})
    assert result["type"] == "form"
    assert result["errors"] == {"base": "geosphere_outside_austria"}
    assert entry.data[CONF_GPS_LOC] == _VIENNA


@pytest.mark.asyncio
async def test_reconfigure_tracker_carries_the_current_entity(
    hass, enable_custom_integrations
):
    entry = _entry(hass, tracker_entity=_TRACKER)
    result = await _reconfigure(
        hass, entry, "reconfigure_geosphere", "reconfigure_geosphere_gps_tracker"
    )
    assert _default(result, CONF_TRACKER_ENTITY) == _TRACKER
    result = await _submit(hass, result, {CONF_TRACKER_ENTITY: "device_tracker.tablet"})
    assert result["type"] == "abort"
    assert entry.data == {
        CONF_PROVIDER: "geosphere",
        CONF_TRACKER_ENTITY: "device_tracker.tablet",
    }
    assert entry.title == "CAP Alerts GEOSPHERE (tablet)"


# ---------------------------------------------------------------------------
# Options
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_options_offer_language_and_area_codes(hass, enable_custom_integrations):
    entry = _entry(hass, gps_loc=_VIENNA)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    names = _field_names(result)
    assert names == [
        CONF_SCAN_INTERVAL,
        "timeout",
        CONF_LANGUAGE,
        CONF_GEOCODE_PREFIXES,
    ]
    assert CONF_EXCLUDE_MARINE not in names
    assert _default(result, CONF_LANGUAGE) == "de"
    language_key = next(
        k for k in result["data_schema"].schema if str(k) == CONF_LANGUAGE
    )
    selector = result["data_schema"].schema[language_key]
    assert [opt["value"] for opt in selector.config["options"]] == list(
        GEOSPHERE_LANGUAGES
    )


@pytest.mark.asyncio
async def test_options_store_the_chosen_language(hass, enable_custom_integrations):
    entry = _entry(hass, gps_loc=_VIENNA)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_SCAN_INTERVAL: 300, "timeout": 30, CONF_LANGUAGE: "en"},
    )
    assert result["type"] == "create_entry"
    assert entry.options[CONF_LANGUAGE] == "en"
