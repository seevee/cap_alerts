"""Which NWS alerts are within the radius of a point, decided locally (#316).

``providers/nws_matching.py`` names the zones to ask about and places each
alert in the reply: here, nearby with its distance, or beyond the radius and
discarded. "Here" is the decision ``alerts/active?point=`` makes on the
server. The interesting part of it is the tolerance band: a zone counts when
the point is within that zone's simplification tolerance of it, so a point in
a sliver between two simplified zones still matches the zone it is really in.

The synthetic set is laid out so every case sits a known distance from an
edge. At 40.8 N a thousandth of a degree of longitude is 84 m, and the
tolerance here is 0.005 degrees, 556 m:

    NJZ006 (forecast)   -74.500 .. -74.104   a 0.004 degree sliver, 337 m,
    NYZ072 (forecast)   -74.100 .. -73.900   separates the two forecast zones
    NJC017 / NYC061     counties meet at -74.100 with no gap
    ANZ338 (marine)     -73.896 .. -73.600   and another sliver at the shore
    ANZ335 (marine)     a marine polygon lying wholly inside NYC061

Alaska's zones are simplified four times more coarsely, so its two forecast
zones sit 0.04 degrees apart: too wide for 556 m, closed by 2.2 km.
"""

from __future__ import annotations

import inspect
import json
import random
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from custom_components.cap_alerts.providers import nws_matching
from custom_components.cap_alerts.providers.nws_matching import (
    REQUEST_LIMIT,
    Placed,
    affected_zones,
    alert_applies,
    applicable_alerts,
    in_alert_polygon,
    layer_for,
    matching_zones,
    nearby_zones,
    place_alerts,
    request_codes,
    request_margin_km,
    split_codes,
    strict_zones,
)
from custom_components.cap_alerts.providers.nws_offset import LockStore, displaced
from custom_components.cap_alerts.providers.nws_zones import ZoneSet

settings.register_profile("cap_alerts", deadline=None)
settings.load_profile("cap_alerts")

Ring = list[list[float]]
_ZONES_URL = "https://api.weather.gov/zones"


def _square(lon0: float, lat0: float, lon1: float, lat1: float) -> Ring:
    return [[lon0, lat0], [lon1, lat0], [lon1, lat1], [lon0, lat1]]


def _zone(code: str, layer: str, ring: Ring) -> dict[str, Any]:
    lons, lats = [p[0] for p in ring], [p[1] for p in ring]
    return {
        "code": code,
        "layer": layer,
        "bbox": [min(lons), min(lats), max(lons), max(lats)],
        "rings": [ring],
    }


_RECORDS = [
    _zone("NJZ006", "forecast", _square(-74.500, 40.7, -74.104, 40.9)),
    _zone("NYZ072", "forecast", _square(-74.100, 40.7, -73.900, 40.9)),
    _zone("NJC017", "county", _square(-74.500, 40.7, -74.100, 40.9)),
    _zone("NYC061", "county", _square(-74.100, 40.7, -73.900, 40.9)),
    _zone("ANZ338", "marine", _square(-73.896, 40.4, -73.600, 40.9)),
    _zone("ANZ335", "marine", _square(-74.000, 40.78, -73.950, 40.82)),
    _zone("AKZ101", "forecast", _square(-150.00, 61.0, -149.90, 61.2)),
    _zone("AKZ102", "forecast", _square(-149.86, 61.0, -149.80, 61.2)),
    _zone("AKC020", "county", _square(-150.00, 61.0, -149.80, 61.2)),
]
_META = {"epsilon_deg": 0.005, "tail_epsilon_deg": 0.02, "tail_states": ["AK"]}

_NY = {("forecast", "NYZ072"), ("county", "NYC061")}
_NJ = {("forecast", "NJZ006"), ("county", "NJC017")}
_SEA = {("marine", "ANZ338")}


@pytest.fixture(scope="module")
def zones() -> ZoneSet:
    return ZoneSet.from_json(json.dumps({"meta": _META, "zones": _RECORDS}))


def _without_counties() -> ZoneSet:
    records = [record for record in _RECORDS if record["layer"] != "county"]
    return ZoneSet.from_json(json.dumps({"meta": _META, "zones": records}))


def test_a_dataset_with_no_county_layer_is_refused_not_matched_wrongly():
    """Without counties the land-sea rule calls every point sea. The point here
    is in Manhattan, and its forecast zone would not count: every zone-only
    land alert for it would be dropped, and nothing would report it."""
    zones = _without_counties()
    lat, lon = 40.8, -74.0
    assert zones.get("forecast", "NYZ072") is not None
    with pytest.raises(ValueError, match="county layer"):
        matching_zones(zones, lat, lon)
    with pytest.raises(ValueError, match="county layer"):
        strict_zones(zones, lat, lon)
    with pytest.raises(ValueError, match="county layer"):
        place_alerts(zones, [], lat, lon, 16.0)


def _zone_only(
    alert_id: str, *urls: str, ugc: list[str] | None = None
) -> dict[str, Any]:
    properties: dict[str, Any] = {"id": alert_id, "affectedZones": list(urls)}
    if ugc is not None:
        properties["geocode"] = {"UGC": ugc}
    return {"geometry": None, "properties": properties}


def _with_polygon(alert_id: str, *rings: Ring) -> dict[str, Any]:
    return {
        "geometry": {"type": "Polygon", "coordinates": [[*r, r[0]] for r in rings]},
        "properties": {
            "id": alert_id,
            "affectedZones": [f"{_ZONES_URL}/county/NYC061"],
        },
    }


# ---------------------------------------------------------------------------
# Away from every edge the band changes nothing
# ---------------------------------------------------------------------------


def test_deep_inside_a_zone_banded_and_strict_agree(zones):
    assert strict_zones(zones, 40.8, -74.0) == _NY
    assert matching_zones(zones, 40.85, -74.05) == _NY


def test_far_from_every_zone_nothing_counts(zones):
    assert matching_zones(zones, 10.0, 10.0) == frozenset()


# ---------------------------------------------------------------------------
# The sliver between two simplified zones
# ---------------------------------------------------------------------------


def test_a_point_in_a_sliver_matches_no_forecast_zone_strictly(zones):
    """The failure the band exists for: on land, in a county, in no forecast zone."""
    assert strict_zones(zones, 40.8, -74.102) == {("county", "NJC017")}


def test_the_band_gives_a_sliver_point_both_neighbours(zones):
    assert matching_zones(zones, 40.8, -74.102) == _NY | _NJ


def test_a_point_near_a_border_also_gets_the_neighbour(zones):
    """168 m inside NYZ072: the price of the band, and the accepted one."""
    assert strict_zones(zones, 40.8, -74.098) == _NY
    assert matching_zones(zones, 40.8, -74.098) == _NY | _NJ


def test_a_point_beyond_the_tolerance_does_not_get_the_neighbour(zones):
    # 0.010 degrees, 842 m, from the county line and farther from NJZ006.
    assert matching_zones(zones, 40.8, -74.090) == _NY


# ---------------------------------------------------------------------------
# Land and sea, with the band
# ---------------------------------------------------------------------------


def test_a_shoreline_sliver_strictly_belongs_to_nothing(zones):
    """In no county and in no marine zone: every zone-only alert would be lost."""
    assert strict_zones(zones, 40.8, -73.898) == frozenset()


def test_the_band_gives_a_shoreline_sliver_both_land_and_sea(zones):
    assert matching_zones(zones, 40.8, -73.898) == _NY | _SEA


def test_just_offshore_the_coastal_land_zones_also_count(zones):
    # 0.006 degrees, 505 m, off the county's edge.
    assert strict_zones(zones, 40.8, -73.894) == _SEA
    assert matching_zones(zones, 40.8, -73.894) == _NY | _SEA


def test_well_offshore_only_the_marine_zone_counts(zones):
    assert matching_zones(zones, 40.8, -73.80) == _SEA


def test_on_land_beside_the_water_the_marine_zone_also_counts(zones):
    # 84 m inside the county's east edge, 421 m from the marine zone.
    assert strict_zones(zones, 40.8, -73.901) == _NY
    assert matching_zones(zones, 40.8, -73.901) == _NY | _SEA


def test_inland_a_marine_polygon_over_the_county_still_does_not_count(zones):
    """The land-sea rule holds away from the county's edge: on land is on land."""
    inside_both = (40.80, -73.975)  # inside NYC061 and inside ANZ335, far from any edge
    assert ("marine", "ANZ335") not in strict_zones(zones, *inside_both)
    assert matching_zones(zones, *inside_both) == _NY


# ---------------------------------------------------------------------------
# Each zone's own tolerance
# ---------------------------------------------------------------------------


def test_alaska_closes_a_gap_the_lower_48_tolerance_would_not(zones):
    """Midway across a 0.04 degree gap at 61 N: 1.08 km from each zone."""
    middle = (61.1, -149.88)
    assert strict_zones(zones, *middle) == {("county", "AKC020")}
    assert matching_zones(zones, *middle) == {
        ("county", "AKC020"),
        ("forecast", "AKZ101"),
        ("forecast", "AKZ102"),
    }


def test_a_dataset_that_records_no_tolerance_matches_strictly():
    """An October single-document file has none; it must not gain a band by default."""
    plain = ZoneSet.from_json(json.dumps({"zones": _RECORDS}))
    for point in ((40.8, -74.102), (40.8, -74.098), (40.8, -73.898), (40.8, -73.901)):
        assert matching_zones(plain, *point) == strict_zones(plain, *point)


@given(
    lat=st.integers(40_650, 40_950).map(lambda v: v / 1000),
    lon=st.integers(-74_550, -73_550).map(lambda v: v / 1000),
)
def test_the_band_only_ever_adds(zones, lat, lon):
    """Whatever strict containment gives, the banded result includes."""
    assert strict_zones(zones, lat, lon) <= matching_zones(zones, lat, lon)


# ---------------------------------------------------------------------------
# What an alert names
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "url_type", "layer"),
    [
        ("NYZ072", "forecast", "forecast"),
        ("NYZ072", "public", "forecast"),
        ("NYZ072", "fire", "fire"),
        ("NYC061", "county", "county"),
        ("ANZ338", "forecast", "marine"),  # marine codes are served as forecast
        ("NYC061", "", "county"),  # bare UGC: the third letter decides
        ("NYZ072", "", "forecast"),
    ],
)
def test_layer_for(code, url_type, layer):
    assert layer_for(code, url_type) == layer


def test_affected_zones_reads_the_layer_from_the_url():
    properties = {
        "affectedZones": [
            f"{_ZONES_URL}/fire/COZ240",
            f"{_ZONES_URL}/forecast/ANZ338/",
        ],
        "geocode": {"UGC": ["COZ240"]},
    }
    assert affected_zones(properties) == {("fire", "COZ240"), ("marine", "ANZ338")}


def test_affected_zones_falls_back_to_bare_ugc():
    properties = {"affectedZones": [], "geocode": {"UGC": ["NYC061", "NYZ072"]}}
    assert affected_zones(properties) == {("county", "NYC061"), ("forecast", "NYZ072")}


@pytest.mark.parametrize(
    "properties",
    [
        {},
        {"affectedZones": None, "geocode": None},
        {"affectedZones": ["not a zone url"]},
    ],
)
def test_affected_zones_of_an_alert_that_names_none(properties):
    assert affected_zones(properties) == set()


# ---------------------------------------------------------------------------
# Whether an alert applies
# ---------------------------------------------------------------------------

_OUTLINE = _square(-74.05, 40.75, -73.98, 40.82)
_HOLE = _square(-74.03, 40.77, -74.00, 40.80)


def test_an_alert_without_a_polygon_has_no_polygon_answer():
    assert in_alert_polygon(40.8, -74.0, None) is None


def test_polygon_containment_respects_holes():
    geometry = {"type": "Polygon", "coordinates": [_OUTLINE, _HOLE]}
    assert in_alert_polygon(40.76, -74.04, geometry) is True
    assert in_alert_polygon(40.78, -74.02, geometry) is False  # in the hole
    assert in_alert_polygon(40.9, -74.04, geometry) is False


def test_polygon_containment_over_a_multipolygon():
    geometry = {
        "type": "MultiPolygon",
        "coordinates": [[_OUTLINE], [_square(-73.7, 40.5, -73.6, 40.6)]],
    }
    assert in_alert_polygon(40.55, -73.65, geometry) is True
    assert in_alert_polygon(40.55, -73.90, geometry) is False


def test_a_polygon_alert_is_decided_by_its_polygon_alone(zones):
    """No band and no zones: outside the polygon is outside, even in its county."""
    alert = _with_polygon("tornado", _OUTLINE)
    counted = matching_zones(zones, 40.85, -74.05)
    assert ("county", "NYC061") in counted
    assert not alert_applies(alert, 40.85, -74.05, counted)
    assert alert_applies(alert, 40.76, -74.04, frozenset())


def test_a_zone_only_alert_applies_through_a_counted_zone(zones):
    alert = _zone_only("winter", f"{_ZONES_URL}/forecast/NJZ006")
    assert not alert_applies(alert, 40.8, -74.098, strict_zones(zones, 40.8, -74.098))
    assert alert_applies(alert, 40.8, -74.098, matching_zones(zones, 40.8, -74.098))


def test_a_fire_alert_does_not_match_the_forecast_zone_of_the_same_code(zones):
    alert = _zone_only("red-flag", f"{_ZONES_URL}/fire/NYZ072")
    assert not alert_applies(alert, 40.8, -74.0, matching_zones(zones, 40.8, -74.0))


def test_applicable_alerts_keeps_the_order_given(zones):
    alerts = [
        _zone_only("b", f"{_ZONES_URL}/forecast/NYZ072"),
        _zone_only("x", f"{_ZONES_URL}/forecast/NJZ006"),
        _with_polygon("a", _OUTLINE),
    ]
    kept = applicable_alerts(
        alerts, 40.76, -74.04, matching_zones(zones, 40.76, -74.04)
    )
    assert [alert["properties"]["id"] for alert in kept] == ["b", "a"]


# ---------------------------------------------------------------------------
# What to ask for
# ---------------------------------------------------------------------------


def _lattice(
    south: float, west: float, rows: int, columns: int, prefix: str
) -> ZoneSet:
    """Half-degree squares, each a forecast zone and a county."""
    records = []
    for row in range(rows):
        for column in range(columns):
            number = f"{row * columns + column:03d}"
            ring = _square(
                west + 0.5 * column,
                south + 0.5 * row,
                west + 0.5 * column + 0.5,
                south + 0.5 * row + 0.5,
            )
            records.append(_zone(f"{prefix}Z{number}", "forecast", ring))
            records.append(_zone(f"{prefix}C{number}", "county", ring))
    return ZoneSet.from_json(json.dumps({"meta": _META, "zones": records}))


@pytest.fixture(scope="module")
def lattice() -> ZoneSet:
    """16 by 24 squares over 36 to 44 north, 80 to 68 west: 768 zones."""
    return _lattice(36.0, -80.0, 16, 24, "NY")


@pytest.fixture(scope="module")
def northern() -> ZoneSet:
    """The same at 60 to 68 north, where the flat projection is at its worst."""
    return _lattice(60.0, -160.0, 16, 24, "AK")


def test_the_request_names_every_zone_in_the_circle_whatever_their_number(lattice):
    """No limit per layer: 100 km around a point reaches dozens of each."""
    codes = request_codes(lattice, 40.25, -74.25, 100.0)
    assert codes == sorted(set(codes))
    assert sum(code[2] == "Z" for code in codes) > 20
    assert sum(code[2] == "C" for code in codes) > 20
    plain = {
        h.code for h in lattice.resolve(40.25, -74.25, radius_km=100.0, max_zones=999)
    }
    assert plain <= set(codes)


def test_the_request_is_a_function_of_the_centre_and_radius_given(lattice):
    """It is handed the apparent centre and nothing else, so it cannot depend
    on the true position. The same centre and radius give the same list."""
    assert request_codes(lattice, 40.25, -74.25, 100.0) == request_codes(
        lattice, 40.25, -74.25, 100.0
    )
    assert request_codes(lattice, 40.25, -74.25, 100.0) != request_codes(
        lattice, 40.25, -75.75, 100.0
    )


def test_the_request_is_wider_than_asked_by_the_tolerance_and_the_margin(northern):
    """At 64 north a 300 km circle gets the dataset's 2.2 km tolerance and
    45 km of margin more. Zones that begin beyond 300 km and within that are
    asked about; the plain lookup at 300 km leaves them out."""
    lat, lon = 64.25, -148.25
    tolerance = northern.max_tolerance_km
    assert tolerance == pytest.approx(2.2, abs=0.1)
    margin = request_margin_km(lat, 300.0 + tolerance)
    assert 30.0 < margin < 60.0

    def within(radius: float) -> set[str]:
        hits = northern.resolve(lat, lon, radius_km=radius, max_zones=999)
        return {hit.code for hit in hits}

    asked = set(request_codes(northern, lat, lon, 300.0))
    assert asked == within(300.0 + tolerance + margin)
    assert within(300.0) < asked
    assert within(300.0 + tolerance) < asked  # so the margin is in it too


def test_a_zone_that_counts_for_the_point_is_asked_about_at_a_radius_of_zero(zones):
    """168 m inside NYZ072 the neighbour NJZ006 counts as well, through the
    tolerance band, though it is not within a radius of zero. With the offset
    drawn at its largest the true position is at the very edge of the circle
    asked about, and without the tolerance added the neighbour, which lies
    beyond that edge, is left out and its alerts never arrive."""
    lat, lon = 40.8, -74.098
    assert ("forecast", "NJZ006") in matching_zones(zones, lat, lon)
    assert ("forecast", "NJZ006") not in nearby_zones(zones, lat, lon, 0.0)
    largest = 56.33
    centre = displaced(lat, lon, largest, 90.0)  # due east: New Jersey is beyond
    asked = request_codes(zones, *centre, largest)
    plain = {
        hit.code for hit in zones.resolve(*centre, radius_km=largest, max_zones=99)
    }
    assert "NJZ006" not in plain
    assert "NJZ006" in asked


def test_an_absurd_radius_is_answered_with_every_zone_there_is(zones, lattice):
    """The offset module can ask for more than the Earth's circumference when
    the user's radius is itself absurd. That is every zone, not an error."""
    everything = sorted({record["code"] for record in _RECORDS})
    assert request_codes(zones, 40.8, -74.0, 47_000.0) == everything
    assert len(request_codes(lattice, 40.8, -74.0, 47_000.0)) == 768
    assert len(nearby_zones(zones, 40.8, -74.0, 47_000.0)) == len(_RECORDS)


def test_the_margin_grows_with_the_radius_and_with_latitude():
    assert request_margin_km(40.0, 0.0) == 0.0
    assert request_margin_km(0.0, 200.0) < request_margin_km(40.0, 200.0)
    assert request_margin_km(40.0, 200.0) < request_margin_km(64.0, 200.0)
    assert request_margin_km(40.0, 100.0) < request_margin_km(40.0, 200.0)
    assert request_margin_km(-64.0, 200.0) == request_margin_km(64.0, 200.0)
    assert request_margin_km(40.0, 206.0) == pytest.approx(6.4, abs=0.3)  # 3.1%
    assert request_margin_km(40.0, 298.0) == pytest.approx(14.2, abs=0.5)  # 4.8%


@pytest.mark.parametrize(
    ("lat", "radius"), [(86.0, 100.0), (60.0, 2000.0), (89.9, 1.0)]
)
def test_near_a_pole_the_margin_is_the_radius_itself(lat, radius):
    assert request_margin_km(lat, radius) == radius
    assert request_margin_km(-lat, radius) == radius


@pytest.mark.parametrize("lat", [float("nan"), 90.1, -91.0, "40", None, True])
def test_the_margin_refuses_a_latitude_that_is_not_one(lat):
    with pytest.raises(ValueError, match="not a position"):
        request_margin_km(lat, 100.0)


def test_the_margin_never_exceeds_the_radius():
    for lat in range(0, 91, 3):
        for radius in (1.0, 50.0, 300.0, 1500.0, 9000.0, 20_000.0):
            assert 0.0 <= request_margin_km(float(lat), radius) <= radius


@given(
    lat=st.floats(36.5, 43.5),
    lon=st.floats(-79.5, -68.5),
    radius=st.sampled_from([0.0, 16.0, 100.0, 161.0]),
    pct=st.sampled_from([0.0, 15.0, 50.0, 100.0]),
    seed=st.integers(0, 500),
    moved=st.floats(0.0, 1.0),
    bearing=st.floats(0.0, 360.0),
)
@settings(max_examples=150)
def test_the_request_from_the_apparent_centre_names_every_zone_in_range(
    lattice, lat, lon, radius, pct, seed, moved, bearing
):
    """The two modules together: wherever the true position is within its
    record's hold, every zone within the radius of it is in the list that was
    calculated from the apparent centre alone."""
    store = LockStore(rng=random.Random(seed))
    lock = store.lock_for(lat, lon, radius_km=radius, hysteresis_pct=pct, today=1)
    true_lat, true_lon = displaced(lat, lon, moved * lock.hold_km, bearing)
    asked = set(request_codes(lattice, *lock.centre, lock.request_radius_km(radius)))
    wanted = {
        code for _layer, code in nearby_zones(lattice, true_lat, true_lon, radius)
    }
    assert wanted <= asked
    counted = {code for _layer, code in matching_zones(lattice, true_lat, true_lon)}
    assert counted <= asked


@pytest.mark.parametrize(
    ("radius", "pct"),
    [(160.9, 15.0), (100.0, 50.0), (16.0, 10.0), (1.0, 50.0), (0.0, 0.0)],
)
def test_on_a_drive_every_report_s_zones_are_in_the_request_in_force(
    lattice, radius, pct
):
    """Battery Park to Philadelphia City Hall and back, a report every 0.4 to
    1.3 km. The request changes only when the record in force does, and
    between changes the position moves by up to the hold distance. At every
    report the zones that decide its alerts must be in the request made when
    the record was taken up, and what is placed must be what the true
    position calls for."""
    rnd = random.Random(7)
    store = LockStore(rng=random.Random(8))
    start, end = (40.7033, -74.0170), (39.9526, -75.1652)
    reports = []
    for a, b in ((start, end), (end, start)):
        at = rnd.uniform(0.0, 0.6)
        while at < 130.0:
            share = at / 130.0
            lat, lon = a[0] + (b[0] - a[0]) * share, a[1] + (b[1] - a[1]) * share
            reports.append(
                displaced(lat, lon, abs(rnd.gauss(0.0, 0.008)), rnd.uniform(0, 360))
            )
            at += rnd.uniform(0.4, 1.3)
    lock = None
    asked: set[str] = set()
    changes = 0
    for lat, lon in reports:
        now = store.lock_for(
            lat, lon, radius_km=radius, hysteresis_pct=pct, today=1, min_hold_km=8.0
        )
        if now != lock:
            lock = now
            changes += 1
            asked = set(
                request_codes(lattice, *lock.centre, lock.request_radius_km(radius))
            )
        counted = matching_zones(lattice, lat, lon)
        near = nearby_zones(lattice, lat, lon, radius)
        assert {code for _layer, code in counted} <= asked
        assert {code for _layer, code in near} <= asked
    assert 4 <= changes < len(reports) / 4
    # One alert for every zone asked about, as the service would return them.
    alerts = [
        _zone_only(
            code, f"{_ZONES_URL}/{'county' if code[2] == 'C' else 'forecast'}/{code}"
        )
        for code in sorted(asked)
    ]
    placed = {
        where.feature["properties"]["id"]: where
        for where in place_alerts(lattice, alerts, lat, lon, radius)
    }
    assert set(placed) == {code for _layer, code in counted | set(near)}
    assert {code for code, where in placed.items() if where.here} == {
        code for _layer, code in counted
    }


def test_the_request_cannot_be_given_the_true_position_by_accident():
    """The list is a function of the centre and radius handed to it. It has
    no other parameter, and this module does not import the one that knows
    where the true position is."""
    assert list(inspect.signature(request_codes).parameters) == [
        "zones",
        "lat",
        "lon",
        "radius_km",
    ]
    source = Path(nws_matching.__file__).read_text(encoding="utf-8")
    imports = [
        line for line in source.splitlines() if line.startswith(("from ", "import "))
    ]
    assert not any("nws_offset" in line for line in imports)
    assert not any("homeassistant" in line for line in imports)


@pytest.mark.parametrize(
    "radius", [-1.0, float("nan"), float("inf"), 100_001.0, "100", None, True]
)
def test_a_radius_that_is_not_a_usable_number_is_refused(zones, radius):
    with pytest.raises(ValueError, match="radius_km"):
        request_codes(zones, 40.8, -74.0, radius)
    with pytest.raises(ValueError, match="radius_km"):
        place_alerts(zones, [], 40.8, -74.0, radius)
    with pytest.raises(ValueError, match="radius_km"):
        nearby_zones(zones, 40.8, -74.0, radius)
    with pytest.raises(ValueError, match="radius_km"):
        request_margin_km(40.8, radius)


@pytest.mark.parametrize(
    ("lat", "lon"),
    [
        (float("nan"), -74.0),
        (40.8, float("nan")),
        (float("inf"), -74.0),
        (90.1, -74.0),
        (40.8, 180.1),
        ("40.8", -74.0),
        (None, -74.0),
        (40.8, None),
        (True, -74.0),
    ],
)
def test_a_position_that_is_not_a_number_is_refused_not_answered_with_nothing(
    zones, lat, lon
):
    """Without the check the lookup finds no zone, the request is empty and
    every alert is discarded as out of range, with no error raised."""
    with pytest.raises(ValueError, match="not a position"):
        request_codes(zones, lat, lon, 100.0)
    with pytest.raises(ValueError, match="not a position"):
        place_alerts(zones, [_GOOD], lat, lon, 100.0)
    with pytest.raises(ValueError, match="not a position"):
        nearby_zones(zones, lat, lon, 100.0)


# ---------------------------------------------------------------------------
# Splitting a long list
# ---------------------------------------------------------------------------


def _codes(count: int) -> list[str]:
    return [f"NYZ{i:03d}" for i in range(count)]


def test_the_working_limit_is_well_under_what_the_service_accepted():
    """426 zones were answered and 462 failed, on 20 November 2026."""
    assert REQUEST_LIMIT == 300


@pytest.mark.parametrize(
    ("count", "sizes"),
    [
        (0, []),
        (1, [1]),
        (300, [300]),
        (301, [151, 150]),
        (469, [235, 234]),
        (600, [300, 300]),
        (601, [201, 200, 200]),
        (11_651, [299] * 29 + [298] * 10),
    ],
)
def test_a_list_is_split_into_as_few_nearly_equal_parts_as_hold_it(count, sizes):
    codes = _codes(count)
    parts = split_codes(codes)
    assert [len(part) for part in parts] == sizes
    assert [code for part in parts for code in part] == codes
    assert all(0 < len(part) <= REQUEST_LIMIT for part in parts)


@given(count=st.integers(0, 2000), limit=st.integers(1, 700))
def test_splitting_loses_nothing_and_respects_any_limit(count, limit):
    codes = _codes(count)
    parts = split_codes(codes, limit)
    assert [code for part in parts for code in part] == codes
    assert all(0 < len(part) <= limit for part in parts)
    assert len(parts) == -(-count // limit)
    if parts:
        assert max(map(len, parts)) - min(map(len, parts)) <= 1


def test_a_limit_too_large_for_a_float_still_gives_one_list():
    """Divided as floats, 10 over such a number is zero and there would be
    no lists to put the codes in."""
    codes = _codes(10)
    assert split_codes(codes, 10**400) == [codes]
    assert split_codes(codes, 2**70) == [codes]


@pytest.mark.parametrize("limit", [0, -1, 2.5, "300", None, True])
def test_a_limit_that_is_not_a_whole_number_from_1_is_refused(limit):
    with pytest.raises(ValueError, match="limit"):
        split_codes(_codes(10), limit)


# ---------------------------------------------------------------------------
# Here, nearby, or beyond the radius
# ---------------------------------------------------------------------------

_MANHATTAN_POINT = (40.8, -74.0)
# From 74.0 west to New Jersey's edge at 74.104 west, along 40.8 north.
_TO_NEW_JERSEY_KM = 8.75
# To the edge of the open-water marine zone at 73.896 west.
_TO_THE_SEA_KM = 8.75


def _placed(
    zones: ZoneSet, alerts: list[Any], radius: float = 50.0
) -> dict[str, Placed]:
    return {
        where.feature["properties"]["id"]: where
        for where in place_alerts(zones, alerts, *_MANHATTAN_POINT, radius)
    }


def test_an_alert_for_the_zone_the_point_is_in_is_here(zones):
    where = _placed(zones, [_zone_only("a", f"{_ZONES_URL}/forecast/NYZ072")])["a"]
    assert (where.here, where.distance_km) == (True, 0.0)


def test_an_alert_for_a_zone_within_the_radius_is_nearby_with_its_distance(zones):
    """Under the old rule this alert was discarded. It is the neighbouring
    zone's, 8.75 km away."""
    alert = _zone_only("nj", f"{_ZONES_URL}/forecast/NJZ006")
    where = _placed(zones, [alert])["nj"]
    assert where.here is False
    assert where.distance_km == pytest.approx(_TO_NEW_JERSEY_KM, abs=0.05)
    assert where.feature is alert


def test_an_alert_for_a_zone_beyond_the_radius_is_discarded(zones):
    alerts = [
        _zone_only("nj", f"{_ZONES_URL}/forecast/NJZ006"),
        _zone_only("ak", f"{_ZONES_URL}/forecast/AKZ101"),
    ]
    assert set(_placed(zones, alerts, radius=50.0)) == {"nj"}
    assert set(_placed(zones, alerts, radius=8.0)) == set()
    assert set(_placed(zones, alerts, radius=9.0)) == {"nj"}
    # Anchorage is 5,400 km away. The resolver's flat projection puts it at
    # about 6,400 km: at such a range its distances are rough.
    assert set(_placed(zones, alerts, radius=6000.0)) == {"nj"}
    assert set(_placed(zones, alerts, radius=7000.0)) == {"nj", "ak"}


def test_the_distance_is_to_the_nearest_of_the_zones_an_alert_names(zones):
    alert = _zone_only(
        "both", f"{_ZONES_URL}/forecast/AKZ101", f"{_ZONES_URL}/forecast/NJZ006"
    )
    near = nearby_zones(zones, *_MANHATTAN_POINT, 7000.0)
    assert near[("forecast", "AKZ101")] > 5000.0  # both zones are in range
    where = _placed(zones, [alert], radius=7000.0)["both"]
    assert where.distance_km == pytest.approx(_TO_NEW_JERSEY_KM, abs=0.05)


def test_an_alert_for_a_zone_that_counts_through_the_band_is_here_not_nearby(zones):
    """168 m inside NYZ072, an alert for NJZ006 next door. The point is not
    inside that zone, but within its tolerance, which is how a point in a
    sliver between two simplified zones keeps the alerts of the zone it is
    really in. It is here at any radius, zero included."""
    lat, lon = 40.8, -74.098
    alert = _zone_only("nj", f"{_ZONES_URL}/forecast/NJZ006")
    assert nearby_zones(zones, lat, lon, 50.0)[("forecast", "NJZ006")] > 0.0
    for radius in (0.0, 50.0):
        (where,) = place_alerts(zones, [alert], lat, lon, radius)
        assert (where.here, where.distance_km) == (True, 0.0)
    # Beyond the tolerance it is nearby, at its distance.
    (where,) = place_alerts(zones, [alert], 40.8, -74.090, 50.0)
    assert where.here is False
    assert where.distance_km == pytest.approx(1.18, abs=0.05)
    assert place_alerts(zones, [alert], 40.8, -74.090, 1.0) == []


def test_a_marine_alert_whose_zone_lies_over_the_land_point_is_nearby_not_here(zones):
    """ANZ335 lies over Manhattan in this set. On land the marine zone does
    not apply, so the alert is not here, as ``point=`` would have it; it is
    nearby, at no distance."""
    inland = (40.80, -73.975)
    alert = _zone_only("marine", f"{_ZONES_URL}/forecast/ANZ335")
    assert not alert_applies(alert, *inland, matching_zones(zones, *inland))
    (where,) = place_alerts(zones, [alert], *inland, 50.0)
    assert (where.here, where.distance_km) == (False, 0.0)


def test_a_polygon_alert_is_here_inside_its_polygon(zones):
    where = _placed(
        zones, [_with_polygon("tornado", _square(-74.05, 40.75, -73.98, 40.82))]
    )
    assert (where["tornado"].here, where["tornado"].distance_km) == (True, 0.0)


def test_a_polygon_alert_outside_is_nearby_at_the_distance_to_its_edge(zones):
    """The polygon decides, not the zones: this one names the point's own
    county and lies 16.8 km to the west of the point."""
    polygon = _square(-74.40, 40.75, -74.20, 40.85)
    where = _placed(zones, [_with_polygon("storm", polygon)])["storm"]
    assert where.here is False
    assert where.distance_km == pytest.approx(16.83, abs=0.1)


def test_a_polygon_alert_beyond_the_radius_is_discarded_whatever_zone_it_names(zones):
    far = _with_polygon("far", _square(-76.0, 40.7, -75.8, 40.9))  # 150 km west
    assert "NYC061" in far["properties"]["affectedZones"][0]
    assert _placed(zones, [far], radius=100.0) == {}
    assert _placed(zones, [far], radius=160.0)["far"].here is False


def test_a_point_in_a_polygon_s_hole_is_outside_it_at_the_distance_to_the_hole_edge(
    zones,
):
    outline = _square(-74.3, 40.6, -73.7, 41.0)
    hole = _square(-74.02, 40.78, -73.98, 40.82)  # the point is at its centre
    where = _placed(zones, [_with_polygon("ring", outline, hole)])["ring"]
    assert where.here is False
    assert where.distance_km == pytest.approx(
        1.68, abs=0.05
    )  # 0.02 degrees of longitude


def test_an_alert_naming_a_zone_the_dataset_does_not_have_is_kept_without_a_distance(
    zones,
):
    """A zone added by NWS after the dataset was built. The alert may be for
    the point itself, and nothing here can show that it is beyond the radius,
    so it is kept."""
    unknown = _zone_only("new", f"{_ZONES_URL}/forecast/NYZ999")
    mixed = _zone_only(
        "mixed", f"{_ZONES_URL}/forecast/AKZ101", f"{_ZONES_URL}/forecast/NYZ999"
    )
    known_far = _zone_only("far", f"{_ZONES_URL}/forecast/AKZ101")
    where = _placed(zones, [unknown, mixed, known_far])
    assert set(where) == {"new", "mixed"}
    assert (where["new"].here, where["new"].distance_km) == (False, None)
    assert (where["mixed"].here, where["mixed"].distance_km) == (False, None)


def test_a_known_zone_in_range_places_an_alert_that_also_names_an_unknown_one(zones):
    alert = _zone_only(
        "both", f"{_ZONES_URL}/forecast/NYZ999", f"{_ZONES_URL}/forecast/NJZ006"
    )
    where = _placed(zones, [alert])["both"]
    assert where.distance_km == pytest.approx(_TO_NEW_JERSEY_KM, abs=0.05)


def test_an_alert_with_nothing_readable_in_it_is_kept_without_a_distance(zones):
    """It cannot be shown to be beyond the radius. What is not a record at
    all is not an alert and is left out."""
    empty = {"properties": {"id": "empty"}}
    placed = place_alerts(
        zones, [empty, None, "text", 7, [], {}], *_MANHATTAN_POINT, 50.0
    )
    assert [(where.feature, where.here, where.distance_km) for where in placed] == [
        (empty, False, None),
        ({}, False, None),
    ]


def test_the_alerts_come_back_in_the_order_given(zones):
    alerts = [
        _zone_only("z", f"{_ZONES_URL}/forecast/NJZ006"),
        _zone_only("gone", f"{_ZONES_URL}/forecast/AKZ101"),
        _with_polygon("a", _square(-74.05, 40.75, -73.98, 40.82)),
        _zone_only("m", f"{_ZONES_URL}/forecast/NYZ072"),
    ]
    placed = place_alerts(zones, alerts, *_MANHATTAN_POINT, 50.0)
    assert [where.feature["properties"]["id"] for where in placed] == ["z", "a", "m"]


def test_what_is_not_a_list_of_alerts_places_nothing(zones):
    for features in (None, "text", {"features": []}, 7):
        assert place_alerts(zones, features, *_MANHATTAN_POINT, 50.0) == []


def test_at_a_radius_of_zero_only_what_touches_the_point_is_kept(zones):
    alerts = [
        _zone_only("here", f"{_ZONES_URL}/county/NYC061"),
        _zone_only("nj", f"{_ZONES_URL}/forecast/NJZ006"),
        _with_polygon("in", _square(-74.05, 40.75, -73.98, 40.82)),
        _with_polygon("out", _square(-74.40, 40.75, -74.20, 40.85)),
    ]
    assert set(_placed(zones, alerts, radius=0.0)) == {"here", "in"}


def test_nearby_zones_gives_each_zone_in_range_its_distance(zones):
    near = nearby_zones(zones, *_MANHATTAN_POINT, 20.0)
    assert near[("forecast", "NYZ072")] == 0.0
    assert near[("county", "NYC061")] == 0.0
    assert near[("marine", "ANZ335")] == 0.0
    assert near[("forecast", "NJZ006")] == pytest.approx(_TO_NEW_JERSEY_KM, abs=0.05)
    assert near[("marine", "ANZ338")] == pytest.approx(_TO_THE_SEA_KM, abs=0.05)
    assert ("forecast", "AKZ101") not in near


_ALERT = st.one_of(
    st.sampled_from(
        ["NYZ072", "NJZ006", "NYC061", "NJC017", "ANZ338", "ANZ335", "NYZ999"]
    ).map(
        lambda code: _zone_only(
            code, f"{_ZONES_URL}/{'county' if code[2] == 'C' else 'forecast'}/{code}"
        )
    ),
    st.tuples(
        st.floats(-74.6, -73.7), st.floats(40.5, 40.95), st.floats(0.01, 0.3)
    ).map(
        lambda v: _with_polygon(
            "polygon", _square(v[0], v[1], v[0] + v[2], v[1] + v[2])
        )
    ),
)


@given(
    lat=st.integers(40_650, 40_950).map(lambda v: v / 1000),
    lon=st.integers(-74_550, -73_550).map(lambda v: v / 1000),
    alerts=st.lists(_ALERT, max_size=6),
    radius=st.sampled_from([0.0, 5.0, 20.0, 100.0]),
)
@settings(max_examples=300)
def test_what_is_placed_here_is_exactly_what_applies_at_the_point(
    zones, lat, lon, alerts, radius
):
    """The group that must equal ``point=`` is decided by the same rule as
    before, at any radius. Everything else placed is within the radius, or
    could not be measured."""
    counted = matching_zones(zones, lat, lon)
    placed = place_alerts(zones, alerts, lat, lon, radius)
    here = [where.feature for where in placed if where.here]
    assert here == applicable_alerts(alerts, lat, lon, counted)
    for where in placed:
        if where.here:
            assert where.distance_km == 0.0
        elif where.distance_km is not None:
            assert 0.0 <= where.distance_km <= radius


# ---------------------------------------------------------------------------
# Records that are not what the API documents
# ---------------------------------------------------------------------------

_GOOD = {
    "id": "good",
    "geometry": None,
    "properties": {"id": "good", "affectedZones": [f"{_ZONES_URL}/forecast/NYZ072"]},
}
_MANHATTAN = (40.8, -74.0)

_JSON = st.recursive(
    st.one_of(
        st.none(),
        st.booleans(),
        st.integers(-(10**30), 10**30),
        st.floats(allow_nan=True, allow_infinity=True),
        st.text(max_size=12),
        st.sampled_from(
            ["Polygon", "MultiPolygon", "Point", "geometry", "properties", "NYZ072"]
        ),
    ),
    lambda inner: st.one_of(
        st.lists(inner, max_size=4),
        st.dictionaries(
            st.sampled_from(
                ["geometry", "properties", "type", "coordinates", "affectedZones"]
                + ["geocode", "UGC", "id"]
            ),
            inner,
            max_size=5,
        ),
    ),
    max_leaves=12,
)


@given(record=_JSON)
@settings(max_examples=400)
def test_no_record_however_malformed_costs_the_poll_its_other_alerts(zones, record):
    """The records come from the network. Whatever one of them is, the call
    must return, and the well-formed alert beside it must still be found."""
    lat, lon = _MANHATTAN
    counted = matching_zones(zones, lat, lon)
    kept = applicable_alerts([record, _GOOD, record], lat, lon, counted)
    assert _GOOD in kept
    placed = place_alerts(zones, [record, _GOOD, record], lat, lon, 50.0)
    assert sum(where.feature is _GOOD and where.here for where in placed) == 1
    for where in placed:
        assert where.distance_km is None or 0.0 <= where.distance_km <= 50.0
        assert isinstance(where.feature, dict)


@pytest.mark.parametrize(
    "geometry",
    [
        "a string",
        [],
        {"type": "Polygon"},
        {"type": "Polygon", "coordinates": None},
        {"type": "Polygon", "coordinates": [[[-74.1, 40.7], [-73.9, 40.7]]]},
        {
            "type": "Polygon",
            "coordinates": [[[-74.1, "x"], [-73.9, 40.7], [-73.9, 40.9]]],
        },
        {
            "type": "Polygon",
            "coordinates": [[[float("nan"), 40.7], [-73.9, 40.7], [-73.9, 40.9]]],
        },
        {
            "type": "Polygon",
            "coordinates": [[[True, 40.7], [-73.9, 40.7], [-73.9, 40.9]]],
        },
        {"type": "Polygon", "coordinates": [["x", [-73.9, 40.7], [-73.9, 40.9]]]},
        {"type": "Polygon", "coordinates": [[[-74.1], [-73.9, 40.7], [-73.9, 40.9]]]},
        {
            "type": "Polygon",
            "coordinates": [[[10**30, 40.7], [-73.9, 40.7], [-73.9, 40.9]]],
        },
        {"type": "MultiPolygon", "coordinates": []},
        {"type": "MultiPolygon", "coordinates": [[]]},
        {"type": "Point", "coordinates": [-74.0, 40.8]},
        {"coordinates": [[[-74.1, 40.7], [-73.9, 40.7], [-73.9, 40.9]]]},
    ],
)
def test_a_polygon_that_cannot_be_read_leaves_the_decision_to_the_zones(
    zones, geometry
):
    """It cannot rule the point out, so the alert is decided as one issued
    without a polygon is: kept if it names a zone that counts, dropped if not."""
    lat, lon = _MANHATTAN
    counted = matching_zones(zones, lat, lon)
    assert in_alert_polygon(lat, lon, geometry) is None
    here = {**_GOOD, "geometry": geometry}
    elsewhere = {
        "geometry": geometry,
        "properties": {"affectedZones": [f"{_ZONES_URL}/forecast/AKZ101"]},
    }
    assert alert_applies(here, lat, lon, counted) is True
    assert alert_applies(elsewhere, lat, lon, counted) is False


@pytest.mark.parametrize(
    "properties",
    [
        None,
        "text",
        [],
        {"affectedZones": "not a list"},
        {"affectedZones": [None, 5, {}, ["nested"]]},
        {"affectedZones": [f"{_ZONES_URL}/forecast/"]},
        {"geocode": "text"},
        {"geocode": {"UGC": "NYZ072"}},
        {"geocode": {"UGC": [None, 7, "", {}]}},
    ],
)
def test_zone_names_that_cannot_be_read_name_no_zone(properties):
    assert affected_zones(properties) == set()


def test_a_usable_name_is_still_read_from_among_unusable_ones():
    properties = {"affectedZones": [None, 5, f"{_ZONES_URL}/forecast/NYZ072", {}]}
    assert affected_zones(properties) == {("forecast", "NYZ072")}
    properties = {"affectedZones": "broken", "geocode": {"UGC": [None, "NYZ072"]}}
    assert affected_zones(properties) == {("forecast", "NYZ072")}
