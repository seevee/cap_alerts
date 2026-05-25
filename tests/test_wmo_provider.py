"""Tests for the WMO SWIC provider — RSS → CAP XML two-step fetch."""

from __future__ import annotations

import importlib.util
import sys
import types
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from homeassistant.helpers.update_coordinator import UpdateFailed

_REPO_ROOT = Path(__file__).resolve().parent.parent
_PKG_DIR = _REPO_ROOT / "custom_components" / "cap_alerts"
_FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> types.ModuleType:
    full = f"cap_alerts.{name}"
    if full in sys.modules:
        return sys.modules[full]
    pkg = sys.modules.get("cap_alerts")
    if pkg is None:
        pkg = types.ModuleType("cap_alerts")
        pkg.__path__ = [str(_PKG_DIR)]
        sys.modules["cap_alerts"] = pkg
    spec = importlib.util.spec_from_file_location(full, _PKG_DIR / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[full] = mod
    spec.loader.exec_module(mod)
    return mod


def _load_provider(name: str) -> types.ModuleType:
    full = f"cap_alerts.providers.{name}"
    if full in sys.modules:
        return sys.modules[full]
    pkg_key = "cap_alerts.providers"
    if pkg_key not in sys.modules:
        providers_pkg = types.ModuleType(pkg_key)
        providers_pkg.__path__ = [str(_PKG_DIR / "providers")]
        sys.modules[pkg_key] = providers_pkg
    spec = importlib.util.spec_from_file_location(
        full, _PKG_DIR / "providers" / f"{name}.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[full] = mod
    spec.loader.exec_module(mod)
    return mod


# Ensure const and model are available before loading provider modules.
_load("const")
_load("model")
_cap_cache_mod = _load_provider("cap_content_cache")
_eccc_mod = _load_provider("eccc")  # wmo imports CAP parsing helpers from it
_wmo_mod = _load_provider("wmo")

CAPContentCache = _cap_cache_mod.CAPContentCache
WMOProvider = _wmo_mod.WMOProvider
_parse_rss_links = _wmo_mod._parse_rss_links
_compute_wmo_id = _wmo_mod._compute_wmo_id

from cap_alerts.const import CONF_GPS_LOC, CONF_SOURCE_ID  # noqa: E402
from tests.conftest import StubSession  # noqa: E402 — after module setup


_RSS_URL = "https://severeweather.wmo.int/v2/cap-alerts/mx-smn-es/rss.xml"
_CAP_URL_1 = "https://swic.example/cap/MX-SMN-2026-001.xml"
_CAP_URL_2 = "https://swic.example/cap/MX-SMN-2026-002.xml"


def _fixture(name: str) -> str:
    return (_FIXTURES / name).read_text(encoding="utf-8")


def _full_responses() -> dict[str, Any]:
    return {
        _RSS_URL: _fixture("wmo_rss.xml"),
        _CAP_URL_1: _fixture("wmo_cap_1.xml"),
        _CAP_URL_2: _fixture("wmo_cap_2.xml"),
    }


# ---------------------------------------------------------------------------
# _parse_rss_links
# ---------------------------------------------------------------------------


def test_parse_rss_links_extracts_item_links():
    links = _parse_rss_links(_fixture("wmo_rss.xml"))
    assert links == [_CAP_URL_1, _CAP_URL_2]


def test_parse_rss_links_empty_feed_returns_empty():
    links = _parse_rss_links(
        '<?xml version="1.0"?><rss version="2.0"><channel>'
        "<title>Empty</title></channel></rss>"
    )
    assert links == []


_CAP_RSS_NS = (
    '<?xml version="1.0"?>'
    '<rss version="2.0" xmlns:cap="urn:oasis:names:tc:emergency:cap:1.1">'
    "<channel><title>Feed</title>"
    "<item><title>Expired</title><link>https://x/expired.xml</link>"
    "<cap:expires>Mon, 25 May 2026 09:00:00 +0000</cap:expires></item>"
    "<item><title>Live</title><link>https://x/live.xml</link>"
    "<cap:expires>Tue, 26 May 2026 09:00:00 +0000</cap:expires></item>"
    "<item><title>NoExpiry</title><link>https://x/noexp.xml</link></item>"
    "</channel></rss>"
)


def test_parse_rss_links_skips_expired_items():
    """Items whose cap:expires is in the past are dropped before CAP fetch."""
    now = datetime(2026, 5, 25, 12, 0, 0, tzinfo=timezone.utc)
    links = _parse_rss_links(_CAP_RSS_NS, now=now)
    # Expired item dropped; live + missing-expires (fail-open) kept.
    assert links == ["https://x/live.xml", "https://x/noexp.xml"]


def test_parse_rss_links_keeps_all_before_expiry():
    """When the cutoff precedes every expiry, all linked items are kept."""
    now = datetime(2026, 5, 24, 0, 0, 0, tzinfo=timezone.utc)
    links = _parse_rss_links(_CAP_RSS_NS, now=now)
    assert links == [
        "https://x/expired.xml",
        "https://x/live.xml",
        "https://x/noexp.xml",
    ]


# ---------------------------------------------------------------------------
# Provider flow
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_country_wide():
    session = StubSession(_full_responses())
    provider = WMOProvider()
    alerts = await provider.async_fetch(
        session,
        {CONF_SOURCE_ID: "mx-smn-es"},
        {},
        cap_content_cache=CAPContentCache(),
    )

    assert len(alerts) == 2
    assert all(a.provider == "wmo" for a in alerts)

    by_event = {a.event: a for a in alerts}
    assert set(by_event) == {"Severe Thunderstorm Warning", "Flash Flood Warning"}

    storm = by_event["Severe Thunderstorm Warning"]
    assert storm.severity == "Severe"
    assert storm.identifier == "urn:wmo:mx-smn:2026:001"
    assert storm.id == _compute_wmo_id(storm.identifier, _CAP_URL_1)
    assert storm.area_desc == "Western Jalisco"
    assert storm.geometry is not None and storm.geometry["type"] == "Polygon"

    flood = by_event["Flash Flood Warning"]
    assert flood.severity == "Moderate"


@pytest.mark.asyncio
async def test_fetch_gps_inside_polygon():
    session = StubSession(_full_responses())
    provider = WMOProvider()
    alerts = await provider.async_fetch(
        session,
        {CONF_SOURCE_ID: "mx-smn-es", CONF_GPS_LOC: "20.5,-104.0"},
        {},
        cap_content_cache=CAPContentCache(),
    )
    assert len(alerts) == 1
    assert alerts[0].event == "Severe Thunderstorm Warning"


@pytest.mark.asyncio
async def test_fetch_gps_outside_polygon():
    session = StubSession(_full_responses())
    provider = WMOProvider()
    alerts = await provider.async_fetch(
        session,
        {CONF_SOURCE_ID: "mx-smn-es", CONF_GPS_LOC: "30.0,-90.0"},
        {},
        cap_content_cache=CAPContentCache(),
    )
    assert alerts == []


@pytest.mark.asyncio
async def test_rss_parse_error():
    session = StubSession({_RSS_URL: "this is not xml <<>>"})
    provider = WMOProvider()
    with pytest.raises(UpdateFailed):
        await provider.async_fetch(
            session,
            {CONF_SOURCE_ID: "mx-smn-es"},
            {},
            cap_content_cache=CAPContentCache(),
        )


@pytest.mark.asyncio
async def test_rss_non_200():
    session = StubSession({_RSS_URL: (503, "")})
    provider = WMOProvider()
    with pytest.raises(UpdateFailed):
        await provider.async_fetch(
            session,
            {CONF_SOURCE_ID: "mx-smn-es"},
            {},
            cap_content_cache=CAPContentCache(),
        )


@pytest.mark.asyncio
async def test_cap_fetch_failure_graceful():
    """One CAP URL 404s → that alert is skipped, the other still returns."""
    responses = {
        _RSS_URL: _fixture("wmo_rss.xml"),
        _CAP_URL_1: _fixture("wmo_cap_1.xml"),
        # _CAP_URL_2 intentionally absent → StubSession returns 404 → None body.
    }
    session = StubSession(responses)
    provider = WMOProvider()
    alerts = await provider.async_fetch(
        session,
        {CONF_SOURCE_ID: "mx-smn-es"},
        {},
        cap_content_cache=CAPContentCache(),
    )
    assert len(alerts) == 1
    assert alerts[0].event == "Severe Thunderstorm Warning"


@pytest.mark.asyncio
async def test_revision_chain_resolution():
    """CAP 2 references CAP 1's identifier → only the leaf (CAP 2) survives."""
    cap_2_with_ref = _fixture("wmo_cap_2.xml").replace(
        "<scope>Public</scope>",
        "<scope>Public</scope>\n"
        "  <references>smn.conagua.gob.mx,urn:wmo:mx-smn:2026:001,"
        "2026-05-12T18:00:00-06:00</references>",
    )
    responses = {
        _RSS_URL: _fixture("wmo_rss.xml"),
        _CAP_URL_1: _fixture("wmo_cap_1.xml"),
        _CAP_URL_2: cap_2_with_ref,
    }
    session = StubSession(responses)
    provider = WMOProvider()
    alerts = await provider.async_fetch(
        session,
        {CONF_SOURCE_ID: "mx-smn-es"},
        {},
        cap_content_cache=CAPContentCache(),
    )
    assert len(alerts) == 1
    assert alerts[0].event == "Flash Flood Warning"
    assert alerts[0].identifier == "urn:wmo:mx-smn:2026:002"


@pytest.mark.asyncio
async def test_alert_identity_stable():
    """The same source over two polls yields the same alert id per identifier."""
    provider = WMOProvider()
    first = await provider.async_fetch(
        session := StubSession(_full_responses()),
        {CONF_SOURCE_ID: "mx-smn-es"},
        {},
        cap_content_cache=CAPContentCache(),
    )
    second = await provider.async_fetch(
        session,
        {CONF_SOURCE_ID: "mx-smn-es"},
        {},
        cap_content_cache=CAPContentCache(),
    )
    ids_first = {a.identifier: a.id for a in first}
    ids_second = {a.identifier: a.id for a in second}
    assert ids_first == ids_second
    assert all(ids_first.values())
