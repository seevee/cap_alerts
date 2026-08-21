"""Repairs for ECCC entries that lose NAAD coverage at the sunset (issue #163).

Two configurations depend on the retiring GeoRSS host: streaming off, and a
feed source pinned to ``pelmorex``. Each owes a fixable repairs issue, raised
from the configuration alone — at setup, on every entry update, and cleared on
removal — and each fix flow writes the recommended option. These run against a
real Home Assistant instance so the lifecycle goes through the real setup,
update listener, and repairs flow manager.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.components.repairs import ConfirmRepairFlow
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component

from custom_components.cap_alerts.const import (
    ISSUE_ECCC_FEED_SOURCE_PELMOREX,
    ISSUE_ECCC_STREAMING_OFF,
)
from custom_components.cap_alerts.issues import (
    ISSUE_STEMS,
    async_sync_issues,
    issue_id,
)
from custom_components.cap_alerts.repairs import (
    OptionRepairFlow,
    async_create_fix_flow,
)

DOMAIN = "cap_alerts"
ALERTREADY = "https://rss.alertready.ca/"
PELMOREX = "https://rss.naad-adna.pelmorex.com/"
EMPTY_FEED = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<feed xmlns="http://www.w3.org/2005/Atom" '
    'xmlns:georss="http://www.georss.org/georss"></feed>'
)
_STRINGS = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "cap_alerts"
    / "strings.json"
)


@pytest.fixture
def fake_stream(monkeypatch):
    """Replace the NAAD socket client so streaming entries set up offline."""

    class _FakeStreamClient:
        def __init__(self, *args, **kwargs) -> None:
            self._stopped = asyncio.Event()

        async def run(self) -> None:
            await self._stopped.wait()

        def stop(self) -> None:
            self._stopped.set()

    monkeypatch.setattr(
        "custom_components.cap_alerts.coordinator.NAADStreamClient",
        _FakeStreamClient,
    )


@pytest.fixture
def feeds(aioclient_mock):
    aioclient_mock.get(ALERTREADY, text=EMPTY_FEED)
    aioclient_mock.get(PELMOREX, text=EMPTY_FEED)


async def _setup(hass, title: str = "ECCC: Ontario", **options) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=title,
        data={"provider": "eccc", "province": "ON"},
        options=options or None,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _issues(hass) -> dict[str, ir.IssueEntry]:
    """Every cap_alerts issue, keyed by issue id."""
    return {
        iid: issue
        for (domain, iid), issue in ir.async_get(hass).issues.items()
        if domain == DOMAIN
    }


async def _fix_client(hass, hass_client):
    assert await async_setup_component(hass, "repairs", {})
    await hass.async_block_till_done()
    return await hass_client()


async def _run_fix_flow(client, iid: str) -> dict:
    """Drive one confirm flow through the repairs HTTP API, as the frontend does."""
    resp = await client.post(
        "/api/repairs/issues/fix", json={"handler": DOMAIN, "issue_id": iid}
    )
    assert resp.status == 200, await resp.text()
    step = await resp.json()
    assert step["type"] == "form"
    assert step["step_id"] == "confirm"
    assert step["description_placeholders"] == {"title": "ECCC: Ontario"}
    resp = await client.post(f"/api/repairs/issues/fix/{step['flow_id']}", json={})
    assert resp.status == 200, await resp.text()
    return await resp.json()


# ---------------------------------------------------------------------------
# Which configurations owe an issue
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_default_eccc_entry_owes_nothing(
    hass, feeds, fake_stream, enable_custom_integrations
):
    await _setup(hass)
    assert _issues(hass) == {}


@pytest.mark.asyncio
async def test_condition_is_eccc_only(hass):
    """An NWS entry with the same options is not polling NAAD at all."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="NWS: COZ040",
        data={"provider": "nws", "zone_id": "COZ040"},
        options={"streaming": False, "feed_source": "pelmorex"},
    )
    entry.add_to_hass(hass)
    async_sync_issues(hass, entry)
    assert _issues(hass) == {}


@pytest.mark.asyncio
async def test_streaming_off_raises_the_streaming_issue(
    hass, feeds, enable_custom_integrations
):
    entry = await _setup(hass, streaming=False)

    issues = _issues(hass)
    assert set(issues) == {issue_id(ISSUE_ECCC_STREAMING_OFF, entry)}
    issue = issues[issue_id(ISSUE_ECCC_STREAMING_OFF, entry)]
    assert issue.is_fixable
    assert issue.severity is ir.IssueSeverity.WARNING
    assert issue.translation_key == ISSUE_ECCC_STREAMING_OFF
    assert issue.translation_placeholders == {"title": "ECCC: Ontario"}
    assert issue.data == {"entry_id": entry.entry_id}
    assert issue.learn_more_url is not None and "163" in issue.learn_more_url


@pytest.mark.asyncio
async def test_pelmorex_pin_raises_the_feed_source_issue_even_when_streaming(
    hass, feeds, fake_stream, enable_custom_integrations
):
    """Streaming does not excuse the pin: the backfill and resync still die."""
    entry = await _setup(hass, feed_source="pelmorex")

    issues = _issues(hass)
    assert set(issues) == {issue_id(ISSUE_ECCC_FEED_SOURCE_PELMOREX, entry)}
    assert issues[issue_id(ISSUE_ECCC_FEED_SOURCE_PELMOREX, entry)].is_fixable


@pytest.mark.asyncio
async def test_both_conditions_raise_both_issues(
    hass, feeds, enable_custom_integrations
):
    entry = await _setup(hass, streaming=False, feed_source="pelmorex")
    assert set(_issues(hass)) == {issue_id(stem, entry) for stem in ISSUE_STEMS}


# ---------------------------------------------------------------------------
# Lifecycle: update listener and removal
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_feed_source_change_syncs_without_a_reload(
    hass, feeds, fake_stream, enable_custom_integrations
):
    """feed_source is read per fetch, so the listener applies it in place."""
    entry = await _setup(hass, feed_source="pelmorex")
    coordinator = entry.runtime_data
    pelmorex = issue_id(ISSUE_ECCC_FEED_SOURCE_PELMOREX, entry)
    assert pelmorex in _issues(hass)

    hass.config_entries.async_update_entry(entry, options={"feed_source": "auto"})
    await hass.async_block_till_done()
    assert _issues(hass) == {}
    assert entry.runtime_data is coordinator

    hass.config_entries.async_update_entry(entry, options={"feed_source": "pelmorex"})
    await hass.async_block_till_done()
    assert set(_issues(hass)) == {pelmorex}
    assert entry.runtime_data is coordinator


@pytest.mark.asyncio
async def test_retitled_entry_refreshes_the_card(
    hass, feeds, enable_custom_integrations
):
    entry = await _setup(hass, streaming=False)
    iid = issue_id(ISSUE_ECCC_STREAMING_OFF, entry)

    hass.config_entries.async_update_entry(entry, title="ECCC: Quebec")
    await hass.async_block_till_done()

    issues = _issues(hass)
    assert set(issues) == {iid}
    assert issues[iid].translation_placeholders == {"title": "ECCC: Quebec"}


@pytest.mark.asyncio
async def test_removing_the_entry_clears_its_issues(
    hass, feeds, enable_custom_integrations
):
    entry = await _setup(hass, streaming=False, feed_source="pelmorex")
    assert len(_issues(hass)) == 2

    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert _issues(hass) == {}


# ---------------------------------------------------------------------------
# Fix flows
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_streaming_fix_turns_streaming_on_and_reloads(
    hass, hass_client, feeds, fake_stream, enable_custom_integrations
):
    entry = await _setup(hass, streaming=False, scan_interval=300)
    before = entry.runtime_data
    assert not before.streaming
    client = await _fix_client(hass, hass_client)

    result = await _run_fix_flow(client, issue_id(ISSUE_ECCC_STREAMING_OFF, entry))
    await hass.async_block_till_done()

    assert result["type"] == "create_entry"
    assert entry.options == {"streaming": True, "scan_interval": 300}
    # The toggle needs a rebuild: a fresh coordinator, now streaming.
    assert entry.runtime_data is not before
    assert entry.runtime_data.streaming
    assert _issues(hass) == {}


@pytest.mark.asyncio
async def test_pelmorex_fix_sets_auto_in_place(
    hass, hass_client, feeds, fake_stream, enable_custom_integrations
):
    entry = await _setup(hass, feed_source="pelmorex")
    before = entry.runtime_data
    client = await _fix_client(hass, hass_client)

    result = await _run_fix_flow(
        client, issue_id(ISSUE_ECCC_FEED_SOURCE_PELMOREX, entry)
    )
    await hass.async_block_till_done()

    assert result["type"] == "create_entry"
    assert entry.options == {"feed_source": "auto"}
    assert entry.runtime_data is before
    assert _issues(hass) == {}


@pytest.mark.asyncio
async def test_fix_flow_for_a_removed_entry_only_closes_the_card(hass):
    flow = await async_create_fix_flow(
        hass, f"{ISSUE_ECCC_STREAMING_OFF}_gone", {"entry_id": "gone"}
    )
    assert isinstance(flow, ConfirmRepairFlow)
    assert not isinstance(flow, OptionRepairFlow)


@pytest.mark.asyncio
async def test_fix_flow_rejects_an_unknown_issue(hass):
    with pytest.raises(ValueError, match="unknown repair"):
        await async_create_fix_flow(hass, "something_else_x", None)


# ---------------------------------------------------------------------------
# Strings
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("stem", ISSUE_STEMS)
def test_every_issue_stem_has_a_card_and_a_confirm_step(stem: str) -> None:
    issues = json.loads(_STRINGS.read_text(encoding="utf-8"))["issues"]
    assert "{title}" in issues[stem]["title"]
    confirm = issues[stem]["fix_flow"]["step"]["confirm"]
    assert confirm["title"]
    assert "{title}" in confirm["description"]
