"""The User-Agent carries a derived token, never the installation UUID (#317).

Home Assistant's instance ID is one UUID per installation, shared by every
integration that asks for it. Sent as-is, it let any provider line this
integration's requests up against any other service that holds the same ID.
``user_agent_for`` sends a truncated SHA-256 of it instead, with the project
URL as the contact.

Providers never build the header. They send whatever string the coordinator
or a flow hands them, so the function and its three callers are the whole
surface: setup in ``__init__.py``, the scope check in ``flows/common.py`` and
the feed check in ``flows/wmo.py``.
"""

from __future__ import annotations

import json
import re
from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.cap_alerts.const import (
    BBK_DASHBOARD_URL,
    CONF_FEED_SOURCE,
    CONF_LANGUAGE,
    CONF_ZONE_ID,
    DOMAIN,
    user_agent_for,
)

_INSTANCE_ID = "0123456789abcdef0123456789abcdef"
_OTHER_ID = "fedcba9876543210fedcba9876543210"
_PROJECT_URL = "https://github.com/seevee/cap_alerts"

_SETUP_ID = "custom_components.cap_alerts.async_get_instance_id"
_FLOW_ID = "custom_components.cap_alerts.flows.common.async_get_instance_id"
_WMO_ID = "custom_components.cap_alerts.flows.wmo.async_get_instance_id"
_WMO_ANY_AVAILABLE = "custom_components.cap_alerts.flows.wmo.async_any_feed_available"

_ARS = "100410000000"
_BBK_DASHBOARD = BBK_DASHBOARD_URL.format(ars=_ARS)
_NWS_ZONES = "https://api.weather.gov/zones?id=NYZ999"


def _user_agents(aioclient_mock, url: str) -> list[str | None]:
    """The User-Agent of each request sent to ``url``."""
    return [
        (headers or {}).get("User-Agent")
        for _method, requested, _data, headers in aioclient_mock.mock_calls
        if str(requested) == url
    ]


# ---------------------------------------------------------------------------
# The function
# ---------------------------------------------------------------------------


def test_same_instance_id_gives_the_same_token():
    assert user_agent_for(_INSTANCE_ID) == user_agent_for(_INSTANCE_ID)


def test_different_instance_ids_give_different_tokens():
    """Per-install on purpose: a provider can still block one installation."""
    assert user_agent_for(_INSTANCE_ID) != user_agent_for(_OTHER_ID)


def test_user_agent_carries_the_project_url():
    assert f"(+{_PROJECT_URL})" in user_agent_for(_INSTANCE_ID)


def test_user_agent_never_contains_the_instance_id():
    assert _INSTANCE_ID not in user_agent_for(_INSTANCE_ID)


def test_token_is_twelve_hex_characters():
    assert re.fullmatch(
        rf"HomeAssistant-CAPAlerts/[0-9a-f]{{12}} \(\+{re.escape(_PROJECT_URL)}\)",
        user_agent_for(_INSTANCE_ID),
    )


# ---------------------------------------------------------------------------
# The three callers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_setup_sends_the_token_not_the_instance_id(
    hass, aioclient_mock, enable_custom_integrations
):
    aioclient_mock.get(_BBK_DASHBOARD, text=json.dumps([]))
    entry = MockConfigEntry(
        domain=DOMAIN, title="bbk", data={"provider": "bbk", CONF_ZONE_ID: _ARS}
    )
    entry.add_to_hass(hass)
    with patch(_SETUP_ID, new_callable=AsyncMock, return_value=_INSTANCE_ID):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    sent = _user_agents(aioclient_mock, _BBK_DASHBOARD)
    assert sent
    assert all(agent == user_agent_for(_INSTANCE_ID) for agent in sent)
    assert not any(_INSTANCE_ID in str(agent) for agent in sent)


@pytest.mark.validate_scope
@pytest.mark.asyncio
async def test_scope_check_sends_the_token_not_the_instance_id(
    hass, aioclient_mock, enable_custom_integrations
):
    # An empty collection: the zone is unknown, the form stays open and no
    # entry is set up behind the request under test.
    aioclient_mock.get(
        _NWS_ZONES, text=json.dumps({"type": "FeatureCollection", "features": []})
    )
    with patch(_FLOW_ID, new_callable=AsyncMock, return_value=_INSTANCE_ID):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": "nws"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": "nws_zone"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_ZONE_ID: "NYZ999"}
        )

    assert result["errors"] == {"base": "unknown_zone"}
    sent = _user_agents(aioclient_mock, _NWS_ZONES)
    assert sent == [user_agent_for(_INSTANCE_ID)]
    assert _INSTANCE_ID not in sent[0]


@pytest.mark.asyncio
async def test_wmo_feed_check_sends_the_token_not_the_instance_id(
    hass, enable_custom_integrations
):
    entry = MockConfigEntry(
        domain=DOMAIN, title="wmo", data={"provider": "wmo", "source_id": "th-tmd-en"}
    )
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(entry, options={CONF_FEED_SOURCE: "mirror"})
    with (
        patch(_WMO_ID, new_callable=AsyncMock, return_value=_INSTANCE_ID),
        patch(
            _WMO_ANY_AVAILABLE, new_callable=AsyncMock, return_value=True
        ) as available,
    ):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        await hass.config_entries.options.async_configure(
            result["flow_id"],
            user_input={
                "scan_interval": 300,
                "timeout": 30,
                CONF_LANGUAGE: "auto",
                CONF_FEED_SOURCE: "auto",
                "geocode_prefixes": "",
            },
        )

    available.assert_awaited_once()
    sent = available.await_args.kwargs["user_agent"]
    assert sent == user_agent_for(_INSTANCE_ID)
    assert _INSTANCE_ID not in sent
