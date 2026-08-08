"""Tests for the GDACS provider — RSS index → per-event CAP XML two-step fetch."""

from __future__ import annotations

import importlib.util
import sys
import types
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
_cap_mod = _load_provider("cap")  # gdacs imports CAP parsing helpers from it
_gdacs_mod = _load_provider("gdacs")

CAPContentCache = _cap_cache_mod.CAPContentCache
GDACSProvider = _gdacs_mod.GDACSProvider
_parse_rss_entries = _gdacs_mod._parse_rss_entries
_cap_url = _gdacs_mod._cap_url
_compute_gdacs_id = _gdacs_mod._compute_gdacs_id

from cap_alerts.const import (  # noqa: E402
    CONF_ALERT_LEVEL,
    CONF_GDACS_EVENT_TYPES,
    CONF_GPS_LOC,
    GDACS_RSS_URL,
)
from tests.conftest import StubSession  # noqa: E402 — after module setup


_EQ_GREEN = ("EQ", "1556861")
_EQ_RED = ("EQ", "1556999")
_TC_ORANGE = ("TC", "1001297")
_VO_GREEN = ("VO", "1200455")


def _fixture(name: str) -> str:
    return (_FIXTURES / name).read_text(encoding="utf-8")


# The earthquake fixture's own ring, and two elsewhere on the planet — the GPS
# tests need the derived bodies to cover somewhere the fixture does not.
_EQ_RING = "49.0,156.0 49.0,158.0 51.0,158.0 51.0,156.0 49.0,156.0"
_CHILE_RING = "-34.0,-73.0 -34.0,-71.0 -32.0,-71.0 -32.0,-73.0 -34.0,-73.0"
_JAPAN_RING = "31.0,130.0 31.0,131.0 32.0,131.0 32.0,130.0 31.0,130.0"


def _eq_body(
    event_id: str, *, episode: str, event: str = "Earthquake", ring: str = _EQ_RING
) -> str:
    """Re-key the earthquake CAP fixture onto another event/episode.

    The RSS index lists four events but only two distinct CAP shapes are worth
    keeping on disk; the rest differ from the earthquake body only in the
    fields this re-keys.
    """
    return (
        _fixture("gdacs_cap_eq.xml")
        .replace("GDACS_EQ_1556861_1723786", f"GDACS_EQ_{event_id}_{episode}")
        .replace("<event>Earthquake</event>", f"<event>{event}</event>")
        .replace(_EQ_RING, ring)
        .replace("1556861", event_id)
    )


def _full_responses() -> dict[str, Any]:
    return {
        GDACS_RSS_URL: _fixture("gdacs_rss.xml"),
        _cap_url(*_EQ_GREEN): _fixture("gdacs_cap_eq.xml"),
        _cap_url(*_EQ_RED): _eq_body("1556999", episode="1724001", ring=_CHILE_RING),
        _cap_url(*_TC_ORANGE): _fixture("gdacs_cap_tc.xml"),
        _cap_url(*_VO_GREEN): _eq_body(
            "1200455", episode="3", event="Volcano", ring=_JAPAN_RING
        ),
    }


# ---------------------------------------------------------------------------
# _parse_rss_entries
# ---------------------------------------------------------------------------


def test_parse_rss_entries_extracts_type_and_id():
    entries = _parse_rss_entries(_fixture("gdacs_rss.xml"))
    assert entries == [_EQ_GREEN, _EQ_RED, _TC_ORANGE, _VO_GREEN]


def test_cap_url_built_from_rss_identity_fields():
    assert (
        _cap_url(*_EQ_GREEN)
        == "https://www.gdacs.org/cap.aspx?eventtype=EQ&eventid=1556861"
    )


def test_parse_rss_entries_event_type_filter():
    """An EQ-only selection drops the cyclone and the volcano."""
    entries = _parse_rss_entries(_fixture("gdacs_rss.xml"), event_types=["EQ"])
    assert entries == [_EQ_GREEN, _EQ_RED]


def test_parse_rss_entries_empty_event_types_keeps_everything():
    """Deselecting every type means "no narrowing", not "no alerts"."""
    assert _parse_rss_entries(_fixture("gdacs_rss.xml"), event_types=[]) == [
        _EQ_GREEN,
        _EQ_RED,
        _TC_ORANGE,
        _VO_GREEN,
    ]


def test_parse_rss_entries_alert_level_threshold():
    """An Orange floor drops both green events and keeps orange and red."""
    entries = _parse_rss_entries(_fixture("gdacs_rss.xml"), min_level="Orange")
    assert entries == [_EQ_RED, _TC_ORANGE]


def test_parse_rss_entries_red_threshold_keeps_only_red():
    entries = _parse_rss_entries(_fixture("gdacs_rss.xml"), min_level="Red")
    assert entries == [_EQ_RED]


def test_parse_rss_entries_green_threshold_keeps_everything():
    entries = _parse_rss_entries(_fixture("gdacs_rss.xml"), min_level="Green")
    assert len(entries) == 4


def test_parse_rss_entries_unknown_level_fails_open():
    """A level GDACS has not published before must not silently vanish."""
    feed = _fixture("gdacs_rss.xml").replace(
        "<gdacs:alertlevel>Green</gdacs:alertlevel>",
        "<gdacs:alertlevel>Chartreuse</gdacs:alertlevel>",
        1,
    )
    assert _EQ_GREEN in _parse_rss_entries(feed)


def test_parse_rss_entries_combined_filters():
    entries = _parse_rss_entries(
        _fixture("gdacs_rss.xml"), event_types=["EQ", "TC"], min_level="Orange"
    )
    assert entries == [_EQ_RED, _TC_ORANGE]


def test_parse_rss_entries_empty_feed_returns_empty():
    entries = _parse_rss_entries(
        '<?xml version="1.0"?><rss version="2.0"><channel>'
        "<title>Empty</title></channel></rss>"
    )
    assert entries == []


def test_parse_rss_entries_skips_items_without_identity():
    """Without both fields neither the CAP URL nor the alert id can be built."""
    feed = (
        '<?xml version="1.0"?>'
        '<rss version="2.0" xmlns:gdacs="http://www.gdacs.org"><channel>'
        "<item><title>No id</title><gdacs:eventtype>EQ</gdacs:eventtype>"
        "<gdacs:alertlevel>Green</gdacs:alertlevel></item>"
        "<item><title>Complete</title><gdacs:eventtype>EQ</gdacs:eventtype>"
        "<gdacs:eventid>42</gdacs:eventid>"
        "<gdacs:alertlevel>Green</gdacs:alertlevel></item>"
        "</channel></rss>"
    )
    assert _parse_rss_entries(feed) == [("EQ", "42")]


# ---------------------------------------------------------------------------
# Provider flow
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_global():
    session = StubSession(_full_responses())
    alerts = await GDACSProvider().async_fetch(
        session, {}, {}, cap_content_cache=CAPContentCache()
    )

    assert len(alerts) == 4
    assert all(a.provider == "gdacs" for a in alerts)

    by_id = {a.id: a for a in alerts}
    quake = by_id[_compute_gdacs_id(*_EQ_GREEN)]
    # The hero case: a non-weather CAP alert on the same model as the rest.
    assert quake.category == "Geo"
    assert quake.event == "Earthquake"
    assert quake.severity == "Minor"
    assert quake.identifier == "GDACS_EQ_1556861_1723786"
    assert quake.geometry is not None and quake.geometry["type"] == "Polygon"

    cyclone = by_id[_compute_gdacs_id(*_TC_ORANGE)]
    assert cyclone.category == "Met"
    assert cyclone.event == "Tropical Cyclone"
    assert cyclone.severity == "Moderate"


@pytest.mark.asyncio
async def test_severity_comes_from_cap_not_alert_level():
    """A red GDACS event is CAP ``Minor`` here; the CAP body is what counts."""
    session = StubSession(_full_responses())
    alerts = await GDACSProvider().async_fetch(
        session, {}, {}, cap_content_cache=CAPContentCache()
    )
    red = next(a for a in alerts if a.id == _compute_gdacs_id(*_EQ_RED))
    assert red.severity == "Minor"


@pytest.mark.asyncio
async def test_filters_apply_before_any_cap_fetch():
    """The volume guard: filtered-out events cost no CAP request at all."""
    session = StubSession(_full_responses())
    alerts = await GDACSProvider().async_fetch(
        session,
        {},
        {CONF_GDACS_EVENT_TYPES: ["EQ"], CONF_ALERT_LEVEL: "Red"},
        cap_content_cache=CAPContentCache(),
    )
    assert [a.id for a in alerts] == [_compute_gdacs_id(*_EQ_RED)]
    assert session.requested == [GDACS_RSS_URL, _cap_url(*_EQ_RED)]


@pytest.mark.asyncio
async def test_identity_survives_an_episode_re_issue():
    """The proof point: a new episode id must not mint a second entity.

    GDACS re-issues the same event under ``GDACS_<type>_<eventid>_<episodeid>``
    with the episode segment bumped, so hashing the CAP identifier — what WMO
    does — would fragment the lifecycle on every update.
    """
    reissued = _eq_body("1556861", episode="1723999").replace(
        "<sent>2026-08-08T05:39:39-00:00</sent>",
        "<sent>2026-08-08T07:12:00-00:00</sent>",
    )
    session = StubSession(
        {
            GDACS_RSS_URL: _fixture("gdacs_rss.xml"),
            _cap_url(*_EQ_GREEN): [_fixture("gdacs_cap_eq.xml"), reissued],
            _cap_url(*_EQ_RED): _eq_body(
                "1556999", episode="1724001", ring=_CHILE_RING
            ),
            _cap_url(*_TC_ORANGE): _fixture("gdacs_cap_tc.xml"),
            _cap_url(*_VO_GREEN): _eq_body(
                "1200455", episode="3", event="Volcano", ring=_JAPAN_RING
            ),
        }
    )
    provider = GDACSProvider()
    first = await provider.async_fetch(
        session, {}, {}, cap_content_cache=CAPContentCache()
    )
    second = await provider.async_fetch(
        session, {}, {}, cap_content_cache=CAPContentCache()
    )

    before = next(a for a in first if a.id == _compute_gdacs_id(*_EQ_GREEN))
    after = next(a for a in second if a.id == _compute_gdacs_id(*_EQ_GREEN))
    # The document changed underneath a single, stable identity.
    assert before.identifier != after.identifier
    assert after.identifier == "GDACS_EQ_1556861_1723999"
    assert before.id == after.id
    assert {a.id for a in first} == {a.id for a in second}


# Two index items naming one event under different episodes — both resolve to
# the same cap.aspx URL, and so to one alert.
_DUPLICATE_FEED = (
    '<?xml version="1.0"?>'
    '<rss version="2.0" xmlns:gdacs="http://www.gdacs.org"><channel>'
    "<item><title>Episode 1</title><gdacs:eventtype>EQ</gdacs:eventtype>"
    "<gdacs:eventid>1556861</gdacs:eventid>"
    "<gdacs:episodeid>1723786</gdacs:episodeid>"
    "<gdacs:alertlevel>Green</gdacs:alertlevel></item>"
    "<item><title>Episode 2</title><gdacs:eventtype>EQ</gdacs:eventtype>"
    "<gdacs:eventid>1556861</gdacs:eventid>"
    "<gdacs:episodeid>1723999</gdacs:episodeid>"
    "<gdacs:alertlevel>Green</gdacs:alertlevel></item>"
    "</channel></rss>"
)


@pytest.mark.asyncio
async def test_duplicate_event_in_one_poll_yields_one_alert():
    """Two index items for one event collapse to a single alert."""
    session = StubSession(
        {
            GDACS_RSS_URL: _DUPLICATE_FEED,
            _cap_url(*_EQ_GREEN): _fixture("gdacs_cap_eq.xml"),
        }
    )
    alerts = await GDACSProvider().async_fetch(
        session, {}, {}, cap_content_cache=CAPContentCache()
    )
    assert [a.id for a in alerts] == [_compute_gdacs_id(*_EQ_GREEN)]


@pytest.mark.asyncio
async def test_fetch_gps_inside_polygon():
    session = StubSession(_full_responses())
    alerts = await GDACSProvider().async_fetch(
        session,
        {CONF_GPS_LOC: "50.0,157.0"},
        {},
        cap_content_cache=CAPContentCache(),
    )
    assert [a.id for a in alerts] == [_compute_gdacs_id(*_EQ_GREEN)]


@pytest.mark.asyncio
async def test_fetch_gps_outside_polygon():
    session = StubSession(_full_responses())
    alerts = await GDACSProvider().async_fetch(
        session,
        {CONF_GPS_LOC: "0.0,0.0"},
        {},
        cap_content_cache=CAPContentCache(),
    )
    assert alerts == []


@pytest.mark.asyncio
async def test_gps_filter_rejects_unparseable_coordinates():
    session = StubSession(_full_responses())
    with pytest.raises(UpdateFailed):
        await GDACSProvider().async_fetch(
            session,
            {CONF_GPS_LOC: "not-a-coordinate"},
            {},
            cap_content_cache=CAPContentCache(),
        )


@pytest.mark.asyncio
async def test_rss_parse_error():
    session = StubSession({GDACS_RSS_URL: "this is not xml <<>>"})
    with pytest.raises(UpdateFailed):
        await GDACSProvider().async_fetch(
            session, {}, {}, cap_content_cache=CAPContentCache()
        )


@pytest.mark.asyncio
async def test_rss_non_200():
    session = StubSession({GDACS_RSS_URL: (503, "")})
    with pytest.raises(UpdateFailed):
        await GDACSProvider().async_fetch(
            session, {}, {}, cap_content_cache=CAPContentCache()
        )


@pytest.mark.asyncio
async def test_empty_feed_returns_no_alerts():
    session = StubSession(
        {
            GDACS_RSS_URL: '<?xml version="1.0"?><rss version="2.0"><channel>'
            "<title>Empty</title></channel></rss>"
        }
    )
    alerts = await GDACSProvider().async_fetch(
        session, {}, {}, cap_content_cache=CAPContentCache()
    )
    assert alerts == []


@pytest.mark.asyncio
async def test_cap_fetch_failure_graceful():
    """One CAP URL 404s → that event is skipped, the others still return."""
    responses = _full_responses()
    del responses[_cap_url(*_TC_ORANGE)]  # StubSession answers 404 → None body
    session = StubSession(responses)
    alerts = await GDACSProvider().async_fetch(
        session, {}, {}, cap_content_cache=CAPContentCache()
    )
    assert len(alerts) == 3
    assert _compute_gdacs_id(*_TC_ORANGE) not in {a.id for a in alerts}
