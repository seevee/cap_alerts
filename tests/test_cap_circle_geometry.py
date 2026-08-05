"""CAP <circle> parsing and GeoJSON shape selection (issue #27)."""

from __future__ import annotations

from pathlib import Path

from custom_components.cap_alerts.normalize import _bbox_from_geometry
from custom_components.cap_alerts.providers.cap import (
    _parse_cap_circle_text,
    parse_cap_alert,
)
from custom_components.cap_alerts.providers.geometry import (
    geometry_from_points,
    geometry_from_polygons,
    geometry_from_shapes,
    points_from_circles,
)

_FIXTURES = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# Circle text parsing
# ---------------------------------------------------------------------------


def test_circle_text_flips_to_lon_lat_and_keeps_km_radius():
    # CAP publishes "lat,lon radius"; GeoJSON order is [lon, lat] and the
    # radius stays in kilometres, unconverted (CAP 1.2 §3.2.4).
    assert _parse_cap_circle_text("-33.8688,151.2093 12.5") == (
        151.2093,
        -33.8688,
        12.5,
    )


def test_circle_text_zero_radius_is_preserved_not_collapsed():
    # The parser stays faithful — interpreting radius 0 belongs to geometry.py.
    assert _parse_cap_circle_text("-33.8688,151.2093 0") == (151.2093, -33.8688, 0.0)


def test_circle_text_rejects_malformed_input():
    for raw in ("", "   ", "-33.8688 12.5", "-33.8688,151.2093", "a,b 1", "1,2 3 4"):
        assert _parse_cap_circle_text(raw) is None, raw


# ---------------------------------------------------------------------------
# Parsing a live-shaped document
# ---------------------------------------------------------------------------


def _fixture_info():
    doc = parse_cap_alert((_FIXTURES / "cap_circle_point.xml").read_text())
    assert doc is not None
    return doc.infos[0]


def test_circles_collected_from_every_area():
    # <circle> is 0..* per <area> and areas repeat, so all three land in order.
    info = _fixture_info()
    assert info.circles == [
        (151.2093, -33.8688, 0.0),
        (151.25, -33.75, 0.0),
        (151.1, -33.9, 12.5),
    ]


def test_polygons_still_parsed_alongside_circles():
    info = _fixture_info()
    assert len(info.polygons) == 1
    assert info.polygons[0][0] == [151.0, -34.0]


def test_only_zero_radius_circles_become_points():
    # The 12.5 km circle describes an area GeoJSON cannot express losslessly;
    # polygonizing it would invent precision the feed never published.
    info = _fixture_info()
    assert points_from_circles(info.circles) == [
        [151.2093, -33.8688],
        [151.25, -33.75],
    ]


# ---------------------------------------------------------------------------
# Shape selection
# ---------------------------------------------------------------------------


def test_single_point_becomes_point_geometry():
    assert geometry_from_points([[151.0, -33.0]]) == {
        "type": "Point",
        "coordinates": [151.0, -33.0],
    }


def test_multiple_points_become_multipoint():
    pts = [[151.0, -33.0], [151.5, -33.5]]
    assert geometry_from_points(pts) == {"type": "MultiPoint", "coordinates": pts}


def test_polygons_win_the_geometry_slot_over_points():
    # CAP unions the shapes with no precedence, so nothing is discarded: the
    # areal shape takes `geometry` and the points ship in CAPAlert.points.
    ring = [[151.0, -34.0], [151.5, -34.0], [151.5, -33.5], [151.0, -34.0]]
    geom = geometry_from_shapes([ring], [[151.25, -33.75]])
    assert geom == {"type": "Polygon", "coordinates": [ring]}


def test_points_are_the_geometry_when_no_polygon_exists():
    # The 15-of-21 RFS case: location lives only in the circle.
    geom = geometry_from_shapes([], [[151.2093, -33.8688]])
    assert geom == {"type": "Point", "coordinates": [151.2093, -33.8688]}


def test_no_shapes_leaves_geometry_none():
    assert geometry_from_shapes([], []) is None
    assert geometry_from_polygons([]) is None
    assert geometry_from_points([]) is None


# ---------------------------------------------------------------------------
# bbox derivation
# ---------------------------------------------------------------------------


def test_point_geometry_yields_degenerate_bbox():
    # What the card reads a location off of (weather_alerts_card#207).
    geom = {"type": "Point", "coordinates": [151.2093, -33.8688]}
    assert _bbox_from_geometry(geom) == (151.2093, -33.8688, 151.2093, -33.8688)


def test_multipoint_geometry_bounds_all_points():
    geom = {"type": "MultiPoint", "coordinates": [[151.0, -34.0], [151.5, -33.5]]}
    assert _bbox_from_geometry(geom) == (151.0, -34.0, 151.5, -33.5)


# ---------------------------------------------------------------------------
# Published attribute surface
# ---------------------------------------------------------------------------


def test_points_are_published_as_an_attribute(alert_factory):
    alert = alert_factory(points=((151.2093, -33.8688), (151.25, -33.75)))
    assert alert.to_attributes()["points"] == [
        (151.2093, -33.8688),
        (151.25, -33.75),
    ]


def test_points_attribute_omitted_when_empty(alert_factory):
    # Sparse attributes: every provider without circle geometry is unaffected.
    assert "points" not in alert_factory().to_attributes()


def test_points_survive_alongside_a_polygon_geometry(alert_factory):
    # Option (b): both surfaces get their best input from one alert.
    ring = [[151.0, -34.0], [151.5, -34.0], [151.5, -33.5], [151.0, -34.0]]
    alert = alert_factory(
        geometry={"type": "Polygon", "coordinates": [ring]},
        points=((151.25, -33.75),),
    )
    attrs = alert.to_attributes()
    assert attrs["points"] == [(151.25, -33.75)]
    # geometry itself is never inlined — it goes out via geometry_ref.
    assert "geometry" not in attrs
