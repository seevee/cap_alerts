"""GPS coordinate fields prefill Home Assistant's home location (issue #128).

Every GPS-coordinate step used to render an empty box, so the first thing a
user did was go look up a latitude and longitude Home Assistant already knew.
Two things are worth pinning: the *sentinel* handling in ``_home_gps`` — there
are two ways for a home to be unset and only one of them is falsy — and the
fact that all five setup steps actually reach the helper, since each lives in a
different provider module and the wiring is per-step.

The ``hass`` fixture's home is San Diego (32.87336, -117.22743), set by
pytest-homeassistant-custom-component.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
import voluptuous as vol

from custom_components.cap_alerts.const import (
    CONF_COUNTRY,
    CONF_GPS_LOC,
    CONF_PROVIDER,
    CONF_SOURCE_ID,
    CONF_ZONE_ID,
    HA_ONBOARDING_LATITUDE,
    HA_ONBOARDING_LONGITUDE,
)
from custom_components.cap_alerts.flows.common import _gps_schema, _home_gps

from pytest_homeassistant_custom_component.common import MockConfigEntry

DOMAIN = "cap_alerts"

_HOME = "32.87336,-117.22743"
_WMO_FETCH = "custom_components.cap_alerts.flows.wmo.fetch_wmo_sources"
_WMO_OPTIONS = [("mx-smn-es", "Mexico — SMN")]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


def _default(result, key_name: str = CONF_GPS_LOC):
    """The rendered default, or ``None`` when the key carries none at all.

    ``vol.Required`` without a default holds the ``UNDEFINED`` sentinel rather
    than a callable, so this cannot just call ``key.default()`` the way the
    reconfigure suite's helper does — the no-default case is exactly what half
    of these tests are checking.
    """
    schema = result["data_schema"].schema
    key = next(k for k in schema if str(k) == key_name)
    if key.default is vol.UNDEFINED:
        return None
    return key.default()


def _set_home(hass, lat: float, lon: float) -> None:
    hass.config.latitude = lat
    hass.config.longitude = lon


# ---------------------------------------------------------------------------
# _home_gps
# ---------------------------------------------------------------------------


def test_home_gps_formats_a_real_home(hass):
    assert _home_gps(hass) == _HOME


def test_home_gps_rejects_the_zero_sentinel(hass):
    """``core_config.py`` initializes both coordinates to ``0``."""
    _set_home(hass, 0, 0)
    assert _home_gps(hass) is None


def test_home_gps_rejects_the_onboarding_default(hass):
    """The frontend's map centers on Amsterdam when the step is skipped.

    This is the sentinel a bare falsy check misses, which is why core's own
    ``met`` integration guards on the pair explicitly.
    """
    _set_home(hass, HA_ONBOARDING_LATITUDE, HA_ONBOARDING_LONGITUDE)
    assert _home_gps(hass) is None


def test_home_gps_keeps_a_home_on_a_zero_meridian(hass):
    """Greenwich has a real longitude of ``0``, and it is not an unset home.

    Guards the sentinel check against being loosened to ``or``: a home is only
    unset when *both* coordinates are falsy.
    """
    _set_home(hass, 51.4779, 0)
    assert _home_gps(hass) == "51.4779,0"


def test_home_gps_keeps_a_home_that_shares_one_onboarding_coordinate(hass):
    """Only the full Amsterdam pair is the sentinel, not either half."""
    _set_home(hass, HA_ONBOARDING_LATITUDE, -117.22743)
    assert _home_gps(hass) == f"{HA_ONBOARDING_LATITUDE},-117.22743"


def test_home_gps_round_trips_through_the_validator(hass):
    """A prefilled value the user accepts unchanged must key the same scope.

    ``_validate_gps`` normalizes through ``float``, and the scope key is built
    from what it returns, so a prefill in a different format would silently
    make "the home I was offered" and "the home I typed" two scopes.
    """
    from custom_components.cap_alerts.flows.common import _validate_gps

    prefill = _home_gps(hass)
    assert prefill is not None
    cleaned, err = _validate_gps(prefill)
    assert err is None
    assert cleaned == prefill


# ---------------------------------------------------------------------------
# _gps_schema
# ---------------------------------------------------------------------------


def test_gps_schema_without_a_default_is_bare_required():
    """``None`` has to mean "no default", not "empty default".

    An empty-string default renders identically but lets a blank submission
    through as ``""``, which surfaces as ``invalid_gps`` instead of the
    frontend's own "required" complaint.
    """
    schema = _gps_schema(None)
    key = next(k for k in schema.schema if str(k) == CONF_GPS_LOC)
    assert key.default is vol.UNDEFINED


def test_gps_schema_carries_a_default():
    schema = _gps_schema("1.5,2.5")
    key = next(k for k in schema.schema if str(k) == CONF_GPS_LOC)
    assert key.default() == "1.5,2.5"


# ---------------------------------------------------------------------------
# Setup steps
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_nws_gps_prefills_home(hass, enable_custom_integrations):
    result = await _menu(hass, "nws", "nws_gps_loc")
    assert _default(result) == _HOME


@pytest.mark.asyncio
async def test_eccc_gps_prefills_home(hass, enable_custom_integrations):
    result = await _menu(hass, "eccc", "eccc_gps_loc")
    assert _default(result) == _HOME


@pytest.mark.asyncio
async def test_gdacs_gps_prefills_home(hass, enable_custom_integrations):
    result = await _menu(hass, "gdacs", "gdacs_gps_loc")
    assert _default(result) == _HOME


@pytest.mark.asyncio
async def test_meteoalarm_gps_prefills_home(hass, enable_custom_integrations):
    result = await _menu(hass, "meteoalarm", "meteoalarm_country")
    result = await _submit(hass, result, {CONF_COUNTRY: "FI"})
    result = await _submit(hass, result, {"next_step_id": "meteoalarm_gps_polygon"})
    assert _default(result) == _HOME


@pytest.mark.asyncio
async def test_wmo_gps_prefills_home(hass, enable_custom_integrations):
    with patch(_WMO_FETCH, return_value=list(_WMO_OPTIONS)):
        result = await _menu(hass, "wmo")
        result = await _submit(hass, result, {CONF_SOURCE_ID: "mx-smn-es"})
    result = await _submit(hass, result, {"next_step_id": "wmo_gps_loc"})
    assert _default(result) == _HOME


@pytest.mark.asyncio
async def test_setup_leaves_the_field_bare_when_no_home_is_set(
    hass, enable_custom_integrations
):
    """No home means the form is exactly what it was before this feature."""
    _set_home(hass, HA_ONBOARDING_LATITUDE, HA_ONBOARDING_LONGITUDE)
    result = await _menu(hass, "nws", "nws_gps_loc")
    assert _default(result) is None


@pytest.mark.asyncio
async def test_a_prefilled_value_submits_unchanged(hass, enable_custom_integrations):
    """The whole point: press Submit and get a working entry."""
    result = await _menu(hass, "nws", "nws_gps_loc")
    result = await _submit(hass, result, {CONF_GPS_LOC: _default(result)})
    assert result["type"] == "create_entry"
    assert result["data"] == {CONF_PROVIDER: "nws", CONF_GPS_LOC: _HOME}
    assert result["title"] == f"CAP Alerts NWS ({_HOME})"


# ---------------------------------------------------------------------------
# Reconfigure
# ---------------------------------------------------------------------------


async def _reconfigure(hass, entry, *steps: str):
    result = await entry.start_reconfigure_flow(hass)
    for step in steps:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": step}
        )
    return result


@pytest.mark.asyncio
async def test_reconfigure_keeps_the_stored_point_over_home(
    hass, enable_custom_integrations
):
    """A stored point always wins — reconfigure must not relocate an entry."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="existing",
        data={CONF_PROVIDER: "nws", CONF_GPS_LOC: "39.96,-82.99"},
    )
    entry.add_to_hass(hass)
    result = await _reconfigure(
        hass, entry, "reconfigure_nws", "reconfigure_nws_gps_loc"
    )
    assert _default(result) == "39.96,-82.99"


@pytest.mark.asyncio
async def test_reconfigure_mode_switch_prefills_home(hass, enable_custom_integrations):
    """Switching a zone entry to GPS mode has no stored point to carry.

    #128 scoped itself to first-time setup on the grounds that "the reconfigure
    counterparts already carry the current value forward". True while the mode
    stays put, false the moment it changes — and a mode switch is the one
    reconfigure where the user has to supply a coordinate from nothing.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="existing",
        data={CONF_PROVIDER: "nws", CONF_ZONE_ID: "OHZ049"},
    )
    entry.add_to_hass(hass)
    result = await _reconfigure(
        hass, entry, "reconfigure_nws", "reconfigure_nws_gps_loc"
    )
    assert _default(result) == _HOME


@pytest.mark.asyncio
async def test_reconfigure_mode_switch_stays_bare_without_a_home(
    hass, enable_custom_integrations
):
    _set_home(hass, 0, 0)
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="existing",
        data={CONF_PROVIDER: "gdacs"},
    )
    entry.add_to_hass(hass)
    result = await _reconfigure(
        hass, entry, "reconfigure_gdacs", "reconfigure_gdacs_gps_loc"
    )
    assert _default(result) is None
