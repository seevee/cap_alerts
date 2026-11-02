"""Providers through the shared caches, a real coordinator and real setup (#374).

Both cache defects fixed in #367 lived between one call and the next: a BBK
withdrawal hidden by a body cached on the first poll, and one cancelled caller
cancelling a fetch others shared. Provider tests built a fresh
``CAPContentCache()`` per call, so neither could show up there. These tests set
entries up through ``async_setup_entry``, which is where the domain-wide cache
is built and handed to every coordinator, and drive later polls with
``async_refresh``. Responses come from ``aioclient_mock``, swapped between polls
the way a host's answers change.

Add a case here when a bug shows that one call is not the whole story.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMockResponse,
)

from custom_components.cap_alerts.const import (
    BBK_DASHBOARD_URL,
    BBK_GEOJSON_URL,
    BBK_WARNING_URL,
    DOMAIN,
    EVENT_INCIDENT_CREATED,
    EVENT_INCIDENT_REMOVED,
)

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def _custom_integrations(enable_custom_integrations):
    """Load ``custom_components.cap_alerts`` through the real loader."""


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


async def _setup(
    hass: HomeAssistant, data: dict[str, Any], options: dict[str, Any] | None = None
) -> MockConfigEntry:
    """One entry set up the way Home Assistant does it, shared caches and all."""
    entry = MockConfigEntry(
        domain=DOMAIN, title=json.dumps(data), data=data, options=options or {}
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def _poll(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """The next scheduled poll, without waiting out the interval."""
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()


def _alert_entities(hass: HomeAssistant, entry: MockConfigEntry) -> list[str]:
    provider = entry.data["provider"]
    prefix = f"{entry.entry_id}_{provider}_"
    return [
        e.entity_id
        for e in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
        if e.unique_id.startswith(prefix)
    ]


def _events(hass: HomeAssistant) -> list[str]:
    seen: list[str] = []
    for event_type in (EVENT_INCIDENT_CREATED, EVENT_INCIDENT_REMOVED):
        # On the loop, so the list keeps the bus's order (see _record_events
        # in test_coordinator_restore.py for what a plain function does).
        @callback
        def _note(event: Event, event_type: str = event_type) -> None:
            seen.append(event_type)

        hass.bus.async_listen(event_type, _note)
    return seen


def _requests_to(aioclient_mock, url: str) -> list[dict[str, str]]:
    """Request headers sent to ``url`` (an empty dict when none were)."""
    return [
        dict(h or {}) for _m, u, _d, h in aioclient_mock.mock_calls if str(u) == url
    ]


# ---------------------------------------------------------------------------
# BBK: a withdrawal lands under a URL the shared cache already holds
# ---------------------------------------------------------------------------

_ARS = "100410000000"
_MOW_ID = "mow.DE-SL-SLS-W038-20260904-000"
_DASHBOARD = BBK_DASHBOARD_URL.format(ars=_ARS)
_DOC = BBK_WARNING_URL.format(warning_id=_MOW_ID)
_GEO = BBK_GEOJSON_URL.format(warning_id=_MOW_ID)
_ARCHIVE = f"https://warnung.bund.de/api31/archive/alerts/{_MOW_ID}?contentType=json"
# The index keeps listing a withdrawn warning for hours, so both polls see it.
_INDEX = json.dumps([{"id": _MOW_ID}])


def _serve_live_bbk(aioclient_mock) -> None:
    aioclient_mock.get(_DASHBOARD, text=_INDEX)
    aioclient_mock.get(
        _DOC, text=_fixture("bbk_warning_mowas.json"), headers={"ETag": '"1"'}
    )
    aioclient_mock.get(_GEO, text=_fixture("bbk_geojson_mowas.geojson"))


async def test_bbk_withdrawal_after_the_body_is_cached(hass, aioclient_mock):
    """MoWaS publishes no expiry, so the 302 into ``archive/`` is the end.

    Poll one caches the live document. On poll two the index still lists the
    warning (it lags by hours), and only the revalidation sees the redirect.
    Before #367 the cached body answered without a request and the warning
    stayed live until the index caught up.
    """
    seen = _events(hass)
    _serve_live_bbk(aioclient_mock)
    entry = await _setup(hass, {"provider": "bbk", "zone_id": _ARS})
    assert len(_alert_entities(hass, entry)) == 1
    assert seen == [EVENT_INCIDENT_CREATED]

    aioclient_mock.clear_requests()
    aioclient_mock.get(_DASHBOARD, text=_INDEX)
    aioclient_mock.get(_DOC, status=302, headers={"Location": _ARCHIVE})
    await _poll(hass, entry)

    assert [h.get("If-None-Match") for h in _requests_to(aioclient_mock, _DOC)] == [
        '"1"'
    ]
    assert seen == [EVENT_INCIDENT_CREATED, EVENT_INCIDENT_REMOVED]
    assert not entry.runtime_data.data


async def test_bbk_live_document_revalidates_to_a_304(hass, aioclient_mock):
    """The steady state: one empty conditional request, alert unchanged."""
    seen = _events(hass)
    _serve_live_bbk(aioclient_mock)
    entry = await _setup(hass, {"provider": "bbk", "zone_id": _ARS})
    before = dict(entry.runtime_data.data)

    aioclient_mock.clear_requests()
    aioclient_mock.get(_DASHBOARD, text=_INDEX)
    aioclient_mock.get(_DOC, status=304)
    await _poll(hass, entry)

    assert len(_requests_to(aioclient_mock, _DOC)) == 1
    assert _requests_to(aioclient_mock, _GEO) == []  # geometry stays a plain hit
    assert entry.runtime_data.data.keys() == before.keys()
    assert seen == [EVENT_INCIDENT_CREATED]


# ---------------------------------------------------------------------------
# ECCC: three entries share one in-flight CAP fetch and one is cancelled
# ---------------------------------------------------------------------------

_FEED = "https://rss.alertready.ca/"
_ECCC_OPTIONS = {"streaming": False, "scan_interval": 300, "feed_source": "alertready"}
_POLYGON = "45.0 -76.0 45.0 -75.5 45.5 -75.5 45.5 -76.0 45.0 -76.0"
_IN_POLYGON = "45.25,-75.75"


def _cap_xml(identifier: str, severity: str) -> str:
    """One Ontario warning; ``sent`` pinned so the bilingual key is stable."""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<alert xmlns="urn:oasis:names:tc:emergency:cap:1.2">'
        f"<identifier>{identifier}</identifier>"
        "<sender>CWTO</sender><sent>2026-10-27T10:00:00+00:00</sent>"
        "<status>Actual</status><msgType>Alert</msgType><scope>Public</scope>"
        "<info><language>en-CA</language><category>Met</category>"
        "<event>Wind Warning</event><urgency>Immediate</urgency>"
        f"<severity>{severity}</severity><certainty>Likely</certainty>"
        "<expires>2099-01-01T00:00:00+00:00</expires>"
        "<headline>Wind Warning in effect</headline><description>desc</description>"
        "<area><areaDesc>Ottawa</areaDesc>"
        "<polygon>45.0,-76.0 45.0,-75.5 45.5,-75.5 45.5,-76.0 45.0,-76.0</polygon>"
        "<geocode><valueName>profile:CAP-CP:Location:0.3</valueName>"
        "<value>3506008</value></geocode>"
        "</area></info></alert>"
    )


def _atom(cap_url: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<feed xmlns="http://www.w3.org/2005/Atom" xmlns:georss="http://www.georss.org/georss">'
        "<entry><id>atom-0</id><title>Wind Warning</title>"
        '<category term="status=Actual"/><category term="language=en-CA"/>'
        '<category term="event=wind"/>'
        f"<georss:polygon>{_POLYGON}</georss:polygon>"
        f'<link type="application/cap+xml" href="{cap_url}"/></entry>'
        "</feed>"
    )


async def test_cancelled_entry_does_not_cancel_a_shared_fetch(hass, aioclient_mock):
    """Owner, one waiter cancelled, one waiter kept: the kept one gets the body.

    Three entries because two cannot show it: a cancelled owner settles the
    future itself, and a lone cancelled waiter has no sibling to poison.
    Before #367 the third entry's poll got a CancelledError it never asked for.
    HA's coordinator swallows one its task did not request and marks the cycle
    failed, so here that was an unavailable entry. The NAAD stream has no such
    guard, and there it ended the task for good.
    """
    first = "https://cap.example/r1.cap"
    aioclient_mock.get(_FEED, text=_atom(first))
    aioclient_mock.get(first, text=_cap_xml("urn:oid:WIRING-1", "Moderate"))
    owner = await _setup(hass, {"provider": "eccc", "province": "QC"}, _ECCC_OPTIONS)
    # A GPS point inside the polygon: any entry whose prefilter rejects the
    # envelope never reaches the CAP fetch, and so is never a waiter.
    cancelled = await _setup(
        hass, {"provider": "eccc", "gps_loc": _IN_POLYGON}, _ECCC_OPTIONS
    )
    kept = await _setup(hass, {"provider": "eccc", "province": "ON"}, _ECCC_OPTIONS)
    assert len(kept.runtime_data.data) == 1

    # A new revision, held at the host until all three polls are waiting on it.
    second = "https://cap.example/r2.cap"
    gate = asyncio.Event()

    async def _held(method, url, data):
        await gate.wait()
        return AiohttpClientMockResponse(
            method, url, text=_cap_xml("urn:oid:WIRING-2", "Severe")
        )

    aioclient_mock.clear_requests()
    aioclient_mock.get(_FEED, text=_atom(second))
    aioclient_mock.get(second, side_effect=_held)

    tasks = []
    for entry in (owner, cancelled, kept):
        tasks.append(asyncio.create_task(entry.runtime_data.async_refresh()))
        for _ in range(20):  # let this poll reach the cache before the next starts
            await asyncio.sleep(0)
    tasks[1].cancel()
    await asyncio.sleep(0)
    gate.set()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    await hass.async_block_till_done()

    assert len(_requests_to(aioclient_mock, second)) == 1  # one fetch, coalesced
    assert isinstance(results[1], asyncio.CancelledError)
    assert not isinstance(results[2], BaseException)
    assert kept.runtime_data.last_update_success
    (alert,) = kept.runtime_data.data.values()
    assert alert.severity == "Severe"


# ---------------------------------------------------------------------------
# ECCC: a failed body, then the real one (#370)
# ---------------------------------------------------------------------------


@pytest.mark.xfail(
    strict=True,
    reason="#370: the metadata-only fallback keeps its own id and is retained",
)
async def test_failed_body_then_real_body_is_one_incident(hass, aioclient_mock):
    """A GPS entry with streaming off, one 503 on the body, then a good poll.

    Expected: one entity and one ``incident_created``. Today the fallback mints
    ``sha256(atom_id|language)``, the real alert arrives under its bilingual
    key, and the fallback, with no expiry and ECCC's terminal vocabulary
    declared, is retained as stale indefinitely. Strict, so fixing #370 flips
    it and the marker has to come off.
    """
    seen = _events(hass)
    cap_url = "https://cap.example/flaky.cap"
    aioclient_mock.get(_FEED, text=_atom(cap_url))
    aioclient_mock.get(cap_url, status=503)
    entry = await _setup(
        hass, {"provider": "eccc", "gps_loc": _IN_POLYGON}, _ECCC_OPTIONS
    )

    aioclient_mock.clear_requests()
    aioclient_mock.get(_FEED, text=_atom(cap_url))
    aioclient_mock.get(cap_url, text=_cap_xml("urn:oid:FLAKY", "Moderate"))
    await _poll(hass, entry)

    assert len(entry.runtime_data.data) == 1
    assert seen.count(EVENT_INCIDENT_CREATED) == 1
