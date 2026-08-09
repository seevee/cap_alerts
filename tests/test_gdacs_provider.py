"""Tests for the GDACS provider — two RSS indexes → CAPAlert + episode GeoJSON."""

from __future__ import annotations

import importlib.util
import json
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
_gdacs_mod = _load_provider("gdacs")

CAPContentCache = _cap_cache_mod.CAPContentCache
GDACSProvider = _gdacs_mod.GDACSProvider
_parse_index = _gdacs_mod._parse_index
_merge_indexes = _gdacs_mod._merge_indexes
_geojson_url = _gdacs_mod._geojson_url
_shapes_from_geojson = _gdacs_mod._shapes_from_geojson
_compute_gdacs_id = _gdacs_mod._compute_gdacs_id

from cap_alerts.const import (  # noqa: E402
    CONF_ALERT_LEVEL,
    CONF_GDACS_EVENT_TYPES,
    CONF_GPS_LOC,
    GDACS_RSS_24H_URL,
    GDACS_RSS_CURRENT_URL,
)
from tests.conftest import StubSession  # noqa: E402 — after module setup


# (eventtype, eventid, episodeid) for each fixture event. The cyclone carries
# the *newer* episode from the 24-hour index, which is the one that must win.
_EQ_GREEN = ("EQ", "1556861", "1723786")
_EQ_RED = ("EQ", "1556999", "1724001")
_TC_ORANGE = ("TC", "1001297", "52")
_TC_ORANGE_STALE = ("TC", "1001297", "50")
_VO_GREEN = ("VO", "1200455", "3")
_EQ_24H_ONLY = ("EQ", "1556844", "1723762")


# Most tests want every fixture event, and the provider's own default floor is
# Orange (see GDACS_DEFAULT_ALERT_LEVEL), so they ask for Green explicitly. The
# default itself is covered by test_unset_alert_level_defaults_to_orange.
_ALL_LEVELS = {CONF_ALERT_LEVEL: "Green"}


def _fixture(name: str) -> str:
    return (_FIXTURES / name).read_text(encoding="utf-8")


def _alert_id(event: tuple[str, str, str]) -> str:
    return _compute_gdacs_id(event[0], event[1])


def _geo_url(event: tuple[str, str, str]) -> str:
    event_type, event_id, episode_id = event
    return (
        f"https://www.gdacs.org/contentdata/resources/{event_type}/{event_id}"
        f"/geojson_{event_id}_{episode_id}.geojson"
    )


# The earthquake fixture's own ring, and three elsewhere on the planet — the
# GPS tests need the derived payloads to cover somewhere the fixture does not.
_RUSSIA_BOX = (156.0, 49.0, 158.0, 51.0)
_CHILE_BOX = (-73.0, -34.0, -71.0, -32.0)
_PAKISTAN_BOX = (66.0, 25.0, 68.0, 27.0)
_INDONESIA_BOX = (105.0, -7.0, 107.0, -5.0)


def _eq_geojson(box: tuple[float, float, float, float]) -> str:
    """Re-key the earthquake GeoJSON fixture onto another circle.

    The indexes list five events but only two distinct GeoJSON shapes are worth
    keeping on disk; the rest differ from the earthquake payload only in where
    the circle sits.
    """
    min_lon, min_lat, max_lon, max_lat = box
    payload = json.loads(_fixture("gdacs_geojson_eq.geojson"))
    payload["features"][0]["geometry"]["coordinates"] = [
        (min_lon + max_lon) / 2,
        (min_lat + max_lat) / 2,
    ]
    payload["features"][1]["geometry"]["coordinates"] = [
        [
            [min_lon, min_lat],
            [max_lon, min_lat],
            [max_lon, max_lat],
            [min_lon, max_lat],
            [min_lon, min_lat],
        ]
    ]
    return json.dumps(payload)


def _full_responses() -> dict[str, Any]:
    return {
        GDACS_RSS_CURRENT_URL: _fixture("gdacs_rss.xml"),
        GDACS_RSS_24H_URL: _fixture("gdacs_rss_24h.xml"),
        _geo_url(_EQ_GREEN): _fixture("gdacs_geojson_eq.geojson"),
        _geo_url(_EQ_RED): _eq_geojson(_CHILE_BOX),
        _geo_url(_TC_ORANGE): _fixture("gdacs_geojson_tc.geojson"),
        _geo_url(_VO_GREEN): _eq_geojson(_INDONESIA_BOX),
        _geo_url(_EQ_24H_ONLY): _eq_geojson(_PAKISTAN_BOX),
    }


# ---------------------------------------------------------------------------
# Index parsing and merging
# ---------------------------------------------------------------------------


def _identities(items) -> list[tuple[str, str, str]]:
    return [(i.event_type, i.event_id, i.episode_id) for i in items]


def test_parse_index_extracts_the_whole_record():
    items = _parse_index(_fixture("gdacs_rss.xml"))
    assert _identities(items) == [_EQ_GREEN, _EQ_RED, _TC_ORANGE_STALE, _VO_GREEN]
    quake = items[0]
    assert quake.alert_level == "Green"
    assert quake.country == "Russia"
    assert quake.iso3 == "RUS"
    assert quake.is_current == "true"
    assert quake.severity_text == "Magnitude 4.8M, Depth:82.521km"
    assert quake.link.endswith("eventtype=EQ&eventid=1556861")


def test_geojson_url_built_from_the_episode():
    """The episode in the path is what makes the URL safe to cache forever."""
    assert _geojson_url(_parse_index(_fixture("gdacs_rss.xml"))[0]) == (
        "https://www.gdacs.org/contentdata/resources/EQ/1556861"
        "/geojson_1556861_1723786.geojson"
    )


def test_parse_index_event_type_filter():
    """An EQ-only selection drops the cyclone and the volcano."""
    items = _parse_index(_fixture("gdacs_rss.xml"), event_types=["EQ"])
    assert _identities(items) == [_EQ_GREEN, _EQ_RED]


def test_parse_index_empty_event_types_keeps_everything():
    """Deselecting every type means "no narrowing", not "no alerts"."""
    items = _parse_index(_fixture("gdacs_rss.xml"), event_types=[])
    assert len(items) == 4


def test_parse_index_alert_level_threshold():
    """An Orange floor drops both green events and keeps orange and red."""
    items = _parse_index(_fixture("gdacs_rss.xml"), min_level="Orange")
    assert _identities(items) == [_EQ_RED, _TC_ORANGE_STALE]


def test_parse_index_red_threshold_keeps_only_red():
    items = _parse_index(_fixture("gdacs_rss.xml"), min_level="Red")
    assert _identities(items) == [_EQ_RED]


def test_parse_index_green_threshold_keeps_everything():
    assert len(_parse_index(_fixture("gdacs_rss.xml"), min_level="Green")) == 4


def test_parse_index_unknown_level_fails_open():
    """A level GDACS has not published before must not silently vanish."""
    feed = _fixture("gdacs_rss.xml").replace(
        "<gdacs:alertlevel>Green</gdacs:alertlevel>",
        "<gdacs:alertlevel>Chartreuse</gdacs:alertlevel>",
        1,
    )
    assert _EQ_GREEN in _identities(_parse_index(feed))


def test_parse_index_unknown_level_survives_a_raised_floor():
    """Unrankable is not lowest: a label plausibly *above* Red must not be
    dropped for exactly the users who raised the floor to keep the severe end."""
    feed = _fixture("gdacs_rss.xml").replace(
        "<gdacs:alertlevel>Green</gdacs:alertlevel>",
        "<gdacs:alertlevel>Chartreuse</gdacs:alertlevel>",
        1,
    )
    assert _identities(_parse_index(feed, min_level="Red")) == [_EQ_GREEN, _EQ_RED]


def test_parse_index_unknown_floor_means_no_floor():
    """Defensive: the options flow only offers known levels, but a floor the
    parser cannot rank must widen the filter, not empty it."""
    assert len(_parse_index(_fixture("gdacs_rss.xml"), min_level="Chartreuse")) == 4


def test_parse_index_combined_filters():
    items = _parse_index(
        _fixture("gdacs_rss.xml"), event_types=["EQ", "TC"], min_level="Orange"
    )
    assert _identities(items) == [_EQ_RED, _TC_ORANGE_STALE]


def test_parse_index_empty_feed_returns_empty():
    assert (
        _parse_index(
            '<?xml version="1.0"?><rss version="2.0"><channel>'
            "<title>Empty</title></channel></rss>"
        )
        == []
    )


def test_parse_index_skips_items_without_identity():
    """Without both fields neither the geometry URL nor the alert id can be built."""
    feed = (
        '<?xml version="1.0"?>'
        '<rss version="2.0" xmlns:gdacs="http://www.gdacs.org"><channel>'
        "<item><title>No id</title><gdacs:eventtype>EQ</gdacs:eventtype>"
        "<gdacs:alertlevel>Green</gdacs:alertlevel></item>"
        "<item><title>Complete</title><gdacs:eventtype>EQ</gdacs:eventtype>"
        "<gdacs:eventid>42</gdacs:eventid><gdacs:episodeid>1</gdacs:episodeid>"
        "<gdacs:alertlevel>Green</gdacs:alertlevel></item>"
        "</channel></rss>"
    )
    assert _identities(_parse_index(feed)) == [("EQ", "42", "1")]


def test_merge_keeps_the_more_recently_modified_episode():
    """The cyclone is in both indexes; the newer episode has to win."""
    merged = _merge_indexes(
        _parse_index(_fixture("gdacs_rss.xml")),
        _parse_index(_fixture("gdacs_rss_24h.xml")),
    )
    cyclone = [i for i in merged if i.event_type == "TC"]
    assert len(cyclone) == 1
    assert cyclone[0].episode_id == "52"


def test_merge_is_order_independent():
    """Whichever index answers first, the newest episode is the one kept."""
    current = _parse_index(_fixture("gdacs_rss.xml"))
    recent = _parse_index(_fixture("gdacs_rss_24h.xml"))
    forwards = {
        (i.event_type, i.event_id): i.episode_id
        for i in _merge_indexes(current, recent)
    }
    backwards = {
        (i.event_type, i.event_id): i.episode_id
        for i in _merge_indexes(recent, current)
    }
    assert forwards == backwards


# ---------------------------------------------------------------------------
# GeoJSON
# ---------------------------------------------------------------------------


def test_shapes_from_geojson_splits_rings_and_points():
    rings, points = _shapes_from_geojson(_fixture("gdacs_geojson_eq.geojson"))
    assert points == [[157.0, 50.0]]
    assert len(rings) == 1
    assert rings[0][0] == [156.0, 49.0]


def test_shapes_from_geojson_excludes_forecast_features():
    """A cyclone's track, cone and wind radii describe where it might go."""
    rings, points = _shapes_from_geojson(_fixture("gdacs_geojson_tc.geojson"))
    assert len(rings) == 1  # Poly_Orange only
    assert rings[0][0] == [130.0, 31.0]
    assert points == [[130.5, 31.5]]


def test_shapes_from_geojson_keeps_an_unknown_class():
    """Over-coverage is recoverable; a silently empty geometry is not."""
    payload = json.loads(_fixture("gdacs_geojson_eq.geojson"))
    payload["features"][1]["properties"]["Class"] = "Poly_SomethingNew"
    rings, _ = _shapes_from_geojson(json.dumps(payload))
    assert len(rings) == 1


def test_shapes_from_geojson_rejects_the_html_miss():
    """GDACS answers a missing file with HTTP 200 and a web page, so content
    is the only thing that distinguishes a real payload from a miss."""
    assert _shapes_from_geojson("<!DOCTYPE html><html><body>GDACS</body></html>") == (
        [],
        [],
    )


# ---------------------------------------------------------------------------
# Provider flow
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_global_unions_both_indexes():
    session = StubSession(_full_responses())
    alerts = await GDACSProvider().async_fetch(
        session, {}, _ALL_LEVELS, cap_content_cache=CAPContentCache()
    )

    assert len(alerts) == 5
    assert all(a.provider == "gdacs" for a in alerts)
    # Neither index contains the other: the cyclone comes from both, the
    # Pakistan quake only from the 24-hour one.
    assert _alert_id(_EQ_24H_ONLY) in {a.id for a in alerts}

    by_id = {a.id: a for a in alerts}
    quake = by_id[_alert_id(_EQ_GREEN)]
    # The hero case: a non-weather alert on the same model as the rest.
    assert quake.category == "Geo"
    assert quake.event == "Earthquake"
    assert quake.certainty == "Observed"
    assert quake.area_desc == "Russia"
    assert quake.identifier == "GDACS_EQ_1556861_1723786"
    assert quake.geometry is not None and quake.geometry["type"] == "Polygon"
    assert quake.sender_name == "Global Disaster Alert and Coordination System"

    cyclone = by_id[_alert_id(_TC_ORANGE)]
    assert cyclone.category == "Met"
    assert cyclone.event == "Tropical Cyclone"
    # The newer episode from the 24-hour index, not the stale one.
    assert cyclone.identifier == "GDACS_TC_1001297_52"


@pytest.mark.asyncio
async def test_timestamps_are_iso_and_expires_is_never_set():
    """``todate`` is the last observation, in the past for live events — putting
    it in ``expires`` would mark every GDACS alert terminal on arrival."""
    session = StubSession(_full_responses())
    alerts = await GDACSProvider().async_fetch(
        session, {}, _ALL_LEVELS, cap_content_cache=CAPContentCache()
    )
    quake = next(a for a in alerts if a.id == _alert_id(_EQ_GREEN))
    assert quake.onset == "2026-08-08T05:39:00+00:00"
    assert quake.sent == "2026-08-08T06:10:00+00:00"
    assert quake.expires == ""
    assert quake.ends is None
    assert quake.parameters["todate"] == "2026-08-08T05:39:00+00:00"


@pytest.mark.asyncio
async def test_severity_comes_from_the_alert_level():
    """There is no CAP body to read a <severity> from, and GDACS's own bodies
    carry a near-constant one per hazard type; the alert level is the per-event
    impact judgement."""
    session = StubSession(_full_responses())
    alerts = await GDACSProvider().async_fetch(
        session, {}, _ALL_LEVELS, cap_content_cache=CAPContentCache()
    )
    by_id = {a.id: a for a in alerts}
    assert by_id[_alert_id(_EQ_GREEN)].severity == "Minor"
    assert by_id[_alert_id(_TC_ORANGE)].severity == "Severe"
    assert by_id[_alert_id(_EQ_RED)].severity == "Extreme"


@pytest.mark.asyncio
async def test_filters_apply_before_any_geometry_fetch():
    """The volume guard: filtered-out events cost no geometry request at all."""
    session = StubSession(_full_responses())
    alerts = await GDACSProvider().async_fetch(
        session,
        {},
        {CONF_GDACS_EVENT_TYPES: ["EQ"], CONF_ALERT_LEVEL: "Red"},
        cap_content_cache=CAPContentCache(),
    )
    assert [a.id for a in alerts] == [_alert_id(_EQ_RED)]
    assert sorted(session.requested) == sorted(
        [GDACS_RSS_CURRENT_URL, GDACS_RSS_24H_URL, _geo_url(_EQ_RED)]
    )


@pytest.mark.asyncio
async def test_identity_survives_an_episode_re_issue():
    """The proof point: a new episode id must not mint a second entity.

    GDACS re-issues the same event under ``GDACS_<type>_<eventid>_<episodeid>``
    with the episode segment bumped, so hashing that identifier — what WMO does
    with the CAP one — would fragment the lifecycle on every update.
    """
    reissued = (
        _fixture("gdacs_rss.xml")
        .replace(
            "<gdacs:episodeid>1723786</gdacs:episodeid>",
            "<gdacs:episodeid>1723999</gdacs:episodeid>",
        )
        .replace(
            "<gdacs:datemodified>Sat, 08 Aug 2026 06:10:00 GMT</gdacs:datemodified>",
            "<gdacs:datemodified>Sat, 08 Aug 2026 07:12:00 GMT</gdacs:datemodified>",
        )
    )
    responses = _full_responses()
    responses[GDACS_RSS_CURRENT_URL] = [_fixture("gdacs_rss.xml"), reissued]
    responses[_geo_url(("EQ", "1556861", "1723999"))] = _eq_geojson(_RUSSIA_BOX)
    session = StubSession(responses)

    provider = GDACSProvider()
    cache = CAPContentCache()
    first = await provider.async_fetch(
        session, {}, _ALL_LEVELS, cap_content_cache=cache
    )
    second = await provider.async_fetch(
        session, {}, _ALL_LEVELS, cap_content_cache=cache
    )

    before = next(a for a in first if a.id == _alert_id(_EQ_GREEN))
    after = next(a for a in second if a.id == _alert_id(_EQ_GREEN))
    # The event revised underneath a single, stable identity.
    assert before.identifier != after.identifier
    assert after.identifier == "GDACS_EQ_1556861_1723999"
    assert before.id == after.id
    assert {a.id for a in first} == {a.id for a in second}


@pytest.mark.asyncio
async def test_one_index_failing_degrades_to_the_other():
    """Losing a feed costs coverage, not the whole update — and must not read
    as every alert in it ending."""
    responses = _full_responses()
    responses[GDACS_RSS_CURRENT_URL] = (503, "")
    session = StubSession(responses)
    alerts = await GDACSProvider().async_fetch(
        session, {}, _ALL_LEVELS, cap_content_cache=CAPContentCache()
    )
    assert {a.id for a in alerts} == {_alert_id(_TC_ORANGE), _alert_id(_EQ_24H_ONLY)}


@pytest.mark.asyncio
async def test_both_indexes_failing_raises():
    """Nothing distinguishes a total outage from a world with no disasters, so
    the coordinator must not be handed an empty list."""
    session = StubSession(
        {GDACS_RSS_CURRENT_URL: (503, ""), GDACS_RSS_24H_URL: (503, "")}
    )
    with pytest.raises(UpdateFailed):
        await GDACSProvider().async_fetch(
            session, {}, _ALL_LEVELS, cap_content_cache=CAPContentCache()
        )


@pytest.mark.asyncio
async def test_both_indexes_malformed_raises():
    session = StubSession(
        {
            GDACS_RSS_CURRENT_URL: "this is not xml <<>>",
            GDACS_RSS_24H_URL: "nor is this <<>>",
        }
    )
    with pytest.raises(UpdateFailed):
        await GDACSProvider().async_fetch(
            session, {}, _ALL_LEVELS, cap_content_cache=CAPContentCache()
        )


@pytest.mark.asyncio
async def test_empty_indexes_return_no_alerts():
    empty = (
        '<?xml version="1.0"?><rss version="2.0"><channel>'
        "<title>Empty</title></channel></rss>"
    )
    session = StubSession({GDACS_RSS_CURRENT_URL: empty, GDACS_RSS_24H_URL: empty})
    alerts = await GDACSProvider().async_fetch(
        session, {}, _ALL_LEVELS, cap_content_cache=CAPContentCache()
    )
    assert alerts == []


@pytest.mark.asyncio
async def test_geometry_fetch_failure_still_ships_the_alert():
    """A missing GeoJSON costs the polygon, not the alert — the text record is
    the whole point of reading the index rather than a CAP body."""
    responses = _full_responses()
    del responses[_geo_url(_TC_ORANGE)]  # StubSession answers 404 → None body
    session = StubSession(responses)
    alerts = await GDACSProvider().async_fetch(
        session, {}, _ALL_LEVELS, cap_content_cache=CAPContentCache()
    )
    cyclone = next(a for a in alerts if a.id == _alert_id(_TC_ORANGE))
    assert len(alerts) == 5
    assert cyclone.geometry is None
    assert cyclone.headline.startswith("Orange notification")


@pytest.mark.asyncio
async def test_html_body_is_treated_as_missing_geometry():
    """The HTTP-200-with-HTML miss is the normal case for several hazards."""
    responses = _full_responses()
    responses[_geo_url(_VO_GREEN)] = "<!DOCTYPE html><html><body>GDACS</body></html>"
    session = StubSession(responses)
    alerts = await GDACSProvider().async_fetch(
        session, {}, _ALL_LEVELS, cap_content_cache=CAPContentCache()
    )
    volcano = next(a for a in alerts if a.id == _alert_id(_VO_GREEN))
    assert volcano.geometry is None


@pytest.mark.asyncio
async def test_fetch_gps_inside_polygon():
    session = StubSession(_full_responses())
    alerts = await GDACSProvider().async_fetch(
        session,
        {CONF_GPS_LOC: "50.0,157.0"},
        _ALL_LEVELS,
        cap_content_cache=CAPContentCache(),
    )
    assert [a.id for a in alerts] == [_alert_id(_EQ_GREEN)]


@pytest.mark.asyncio
async def test_fetch_gps_outside_polygon():
    session = StubSession(_full_responses())
    alerts = await GDACSProvider().async_fetch(
        session,
        {CONF_GPS_LOC: "0.0,0.0"},
        _ALL_LEVELS,
        cap_content_cache=CAPContentCache(),
    )
    assert alerts == []


@pytest.mark.asyncio
async def test_gps_ignores_a_cyclone_forecast_track():
    """Inside the forecast cone is not inside the alert: the GPS filter answers
    "am I in the affected area", not "might this reach me in four days"."""
    session = StubSession(_full_responses())
    inside_impact = await GDACSProvider().async_fetch(
        session,
        {CONF_GPS_LOC: "31.5,130.5"},
        _ALL_LEVELS,
        cap_content_cache=CAPContentCache(),
    )
    assert [a.id for a in inside_impact] == [_alert_id(_TC_ORANGE)]

    session = StubSession(_full_responses())
    inside_cone = await GDACSProvider().async_fetch(
        session,
        {CONF_GPS_LOC: "31.5,141.5"},
        _ALL_LEVELS,
        cap_content_cache=CAPContentCache(),
    )
    assert inside_cone == []


@pytest.mark.asyncio
async def test_gps_filter_rejects_unparseable_coordinates():
    session = StubSession(_full_responses())
    with pytest.raises(UpdateFailed):
        await GDACSProvider().async_fetch(
            session,
            {CONF_GPS_LOC: "not-a-coordinate"},
            _ALL_LEVELS,
            cap_content_cache=CAPContentCache(),
        )


@pytest.mark.asyncio
async def test_gps_filter_fails_loud_when_no_alert_has_geometry():
    """Every alert losing its polygon means the geometry host is down, not that
    the user's location is clear."""
    responses = _full_responses()
    for event in (_EQ_GREEN, _EQ_RED, _TC_ORANGE, _VO_GREEN, _EQ_24H_ONLY):
        responses.pop(_geo_url(event), None)
    session = StubSession(responses)
    with pytest.raises(UpdateFailed):
        await GDACSProvider().async_fetch(
            session,
            {CONF_GPS_LOC: "50.0,157.0"},
            _ALL_LEVELS,
            cap_content_cache=CAPContentCache(),
        )


@pytest.mark.asyncio
async def test_unset_alert_level_defaults_to_orange():
    """The one GDACS default that narrows: a green floor is 327 events
    worldwide, and a global entry would mint an entity for each."""
    session = StubSession(_full_responses())
    alerts = await GDACSProvider().async_fetch(
        session, {}, {}, cap_content_cache=CAPContentCache()
    )
    assert {a.id for a in alerts} == {_alert_id(_EQ_RED), _alert_id(_TC_ORANGE)}
