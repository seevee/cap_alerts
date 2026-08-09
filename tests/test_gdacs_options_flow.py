"""Options-flow surface for the GDACS tuning fields.

Runs against a real Home Assistant test instance so the rendered schema and
the stored options are the ones HA actually produces — the point under test
is what a *save* materializes, which no pure-function test can see.
"""

from __future__ import annotations

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.cap_alerts.const import (
    CONF_ALERT_LEVEL,
    CONF_GDACS_EVENT_TYPES,
    CONF_GEOCODE_PREFIXES,
    GDACS_EVENT_TYPES,
)

DOMAIN = "cap_alerts"


def _entry(hass) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, title="gdacs", data={"provider": "gdacs"})
    entry.add_to_hass(hass)
    return entry


async def _submit(hass, entry, event_types: list[str]):
    result = await hass.config_entries.options.async_init(entry.entry_id)
    return await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            "scan_interval": 300,
            "timeout": 30,
            CONF_GDACS_EVENT_TYPES: event_types,
            CONF_ALERT_LEVEL: "Green",
        },
    )


@pytest.mark.asyncio
async def test_saving_all_types_stores_no_key(hass, enable_custom_integrations):
    """The untouched default must not become a closed set on save.

    A stored list pins the hazard codes known today, so a type GDACS adds
    later would be silently dropped by an entry that never chose to narrow —
    the exact failure the provider's "empty means no narrowing" rule exists
    to avoid, and the behavior the option's description promises.
    """
    result = await _submit(hass, _entry(hass), list(GDACS_EVENT_TYPES))
    assert result["type"] == "create_entry"
    assert CONF_GDACS_EVENT_TYPES not in result["data"]


@pytest.mark.asyncio
async def test_deselecting_every_type_stores_no_key(hass, enable_custom_integrations):
    """No types selected already means "no narrowing" at the provider."""
    result = await _submit(hass, _entry(hass), [])
    assert result["type"] == "create_entry"
    assert CONF_GDACS_EVENT_TYPES not in result["data"]


@pytest.mark.asyncio
async def test_a_real_subset_is_stored(hass, enable_custom_integrations):
    result = await _submit(hass, _entry(hass), ["EQ", "TC"])
    assert result["type"] == "create_entry"
    assert result["data"][CONF_GDACS_EVENT_TYPES] == ["EQ", "TC"]


@pytest.mark.asyncio
async def test_stored_subset_renders_as_the_default(hass, enable_custom_integrations):
    entry = _entry(hass)
    hass.config_entries.async_update_entry(
        entry, options={CONF_GDACS_EVENT_TYPES: ["EQ"]}
    )
    result = await hass.config_entries.options.async_init(entry.entry_id)
    key = next(
        k for k in result["data_schema"].schema if str(k) == CONF_GDACS_EVENT_TYPES
    )
    assert key.default() == ["EQ"]


@pytest.mark.asyncio
async def test_geocode_prefixes_not_offered(hass, enable_custom_integrations):
    """No GDACS CAP body carries a <geocode>, so the area-code field's only
    possible effect would be a permanently unavailable entry. The convention
    table's ``publishes_geocodes`` withholds it, like the marine toggle."""
    entry = _entry(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert CONF_GEOCODE_PREFIXES not in {str(k) for k in result["data_schema"].schema}
