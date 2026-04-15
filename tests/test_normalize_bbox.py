"""bbox derivation from GeoJSON geometry."""

from __future__ import annotations

from custom_components.cap_alerts.normalize import _bbox_from_geometry


def test_bbox_from_polygon():
    geom = {
        "type": "Polygon",
        "coordinates": [[[-80.0, 30.0], [-70.0, 30.0], [-70.0, 40.0], [-80.0, 40.0], [-80.0, 30.0]]],
    }
    assert _bbox_from_geometry(geom) == (-80.0, 30.0, -70.0, 40.0)


def test_bbox_from_multipolygon():
    geom = {
        "type": "MultiPolygon",
        "coordinates": [
            [[[-80.0, 30.0], [-78.0, 30.0], [-78.0, 32.0], [-80.0, 30.0]]],
            [[[-70.0, 40.0], [-65.0, 40.0], [-65.0, 45.0], [-70.0, 40.0]]],
        ],
    }
    assert _bbox_from_geometry(geom) == (-80.0, 30.0, -65.0, 45.0)


def test_bbox_from_point():
    assert _bbox_from_geometry({"type": "Point", "coordinates": [-75.0, 35.0]}) == (
        -75.0,
        35.0,
        -75.0,
        35.0,
    )


def test_bbox_from_linestring():
    geom = {"type": "LineString", "coordinates": [[-75.0, 35.0], [-74.0, 36.0]]}
    assert _bbox_from_geometry(geom) == (-75.0, 35.0, -74.0, 36.0)


def test_bbox_missing_geometry():
    assert _bbox_from_geometry(None) is None
    assert _bbox_from_geometry({}) is None


def test_bbox_unsupported_type():
    assert _bbox_from_geometry({"type": "GeometryCollection", "geometries": []}) is None
