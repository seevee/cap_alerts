"""The shared CAP status rule and the option that widens it (issue #438).

Every provider screens ``<status>`` at ingest with ``admits_status``, ahead of
its own chain resolution and the store's reference matching. The per-provider
tests live beside each provider's other fetch tests; this file holds the rule
itself and the options-flow surface.
"""

from __future__ import annotations

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.cap_alerts.const import CONF_INCLUDE_TEST_MESSAGES
from custom_components.cap_alerts.providers import wants_test_messages
from custom_components.cap_alerts.providers.cap import admits_status

DOMAIN = "cap_alerts"


# ---------------------------------------------------------------------------
# The rule
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "default", "opted_in"),
    [
        ("Actual", True, True),
        # Mandatory in CAP 1.2, so a missing one is a malformed document and
        # fails open rather than dropping a real alert.
        ("", True, True),
        ("Test", False, True),
        ("Exercise", False, True),
        # Never actionable, whatever the option says.
        ("Draft", False, False),
        ("System", False, False),
        # Case matters: CAP's values are an enumeration, not free text.
        ("test", False, False),
    ],
)
def test_admits_status(status: str, default: bool, opted_in: bool):
    assert admits_status(status) is default
    assert admits_status(status, include_tests=True) is opted_in


@pytest.mark.parametrize(
    ("options", "expected"),
    [
        ({}, False),
        ({CONF_INCLUDE_TEST_MESSAGES: False}, False),
        ({CONF_INCLUDE_TEST_MESSAGES: True}, True),
    ],
)
def test_wants_test_messages_is_off_by_default(options: dict, expected: bool):
    assert wants_test_messages(options) is expected


# ---------------------------------------------------------------------------
# Options flow
# ---------------------------------------------------------------------------


def _entry(hass, provider: str, **data) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN, title=provider, data={"provider": provider, **data}
    )
    entry.add_to_hass(hass)
    return entry


def _field(result, name: str):
    return next((k for k in result["data_schema"].schema if str(k) == name), None)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider", "data"),
    [
        ("nws", {"zone_id": "OHC049"}),
        ("eccc", {"province": "ON"}),
        ("meteoalarm", {"country": "DE"}),
        ("wmo", {"source_id": "cn-cma-xx"}),
        ("bbk", {"zone_id": "095640000000"}),
        ("au", {"province": "NSW"}),
        ("twc", {"province": "HI"}),
    ],
)
async def test_offered_off_where_the_feed_carries_a_status(
    hass, enable_custom_integrations, provider: str, data: dict
):
    entry = _entry(hass, provider, **data)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    key = _field(result, CONF_INCLUDE_TEST_MESSAGES)
    assert key is not None
    assert key.default() is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider", "data"),
    [
        ("gdacs", {}),
        ("geosphere", {"gps_loc": "48.2082,16.3738"}),
    ],
)
async def test_withheld_where_every_record_is_built_actual(
    hass, enable_custom_integrations, provider: str, data: dict
):
    """GDACS (RSS items) and GeoSphere (GeoJSON) have no status to screen."""
    entry = _entry(hass, provider, **data)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert _field(result, CONF_INCLUDE_TEST_MESSAGES) is None


@pytest.mark.asyncio
async def test_stored_choice_is_the_rendered_default(hass, enable_custom_integrations):
    entry = _entry(hass, "nws", zone_id="OHC049")
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            "scan_interval": 300,
            "timeout": 30,
            CONF_INCLUDE_TEST_MESSAGES: True,
        },
    )
    assert entry.options[CONF_INCLUDE_TEST_MESSAGES] is True

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert _field(result, CONF_INCLUDE_TEST_MESSAGES).default() is True
