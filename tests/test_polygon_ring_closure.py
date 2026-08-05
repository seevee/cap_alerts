"""Linear-ring closure and validity for GeoJSON output (issue #85)."""

from __future__ import annotations

import pytest

from custom_components.cap_alerts.providers.cap import parse_cap_polygon_text
from custom_components.cap_alerts.providers.eccc import _point_in_polygons
from custom_components.cap_alerts.providers.meteoalarm import _extract_geometries
from custom_components.cap_alerts.providers.geometry import (
    geometry_from_polygons,
    normalize_ring,
)

# A closed unit square, and the same square as feeds sometimes publish it.
SQUARE_OPEN = [[0.0, 0.0], [10.0, 0.0], [10.0, 10.0], [0.0, 10.0]]
SQUARE_CLOSED = [*SQUARE_OPEN, [0.0, 0.0]]


# ---------------------------------------------------------------------------
# normalize_ring
# ---------------------------------------------------------------------------


def test_unclosed_ring_gets_closed():
    assert normalize_ring(list(SQUARE_OPEN)) == SQUARE_CLOSED


def test_already_closed_ring_is_untouched():
    ring = [row[:] for row in SQUARE_CLOSED]
    assert normalize_ring(ring) == SQUARE_CLOSED


def test_three_distinct_points_close_to_the_minimum_valid_ring():
    # CAP 1.2 §3.2.4 / RFC 7946 §3.1.6 both require 4+ positions closed; a
    # triangle is the smallest real polygon and reaches 4 once closed.
    out = normalize_ring([[1.0, 1.0], [2.0, 2.0], [3.0, 1.0]])
    assert out is not None
    assert len(out) == 4
    assert out[0] == out[-1]


@pytest.mark.parametrize(
    "ring",
    [
        [],
        [[1.0, 1.0]],
        [[1.0, 1.0], [1.0, 1.0], [1.0, 1.0]],  # one point repeated
        [[1.0, 1.0], [2.0, 2.0], [1.0, 1.0]],  # two distinct points
    ],
)
def test_rings_without_three_distinct_vertices_are_rejected(ring):
    # Not an area — closing it would produce syntactically valid GeoJSON that
    # still describes nothing.
    assert normalize_ring(ring) is None


def test_near_identical_vertices_collapse_at_parser_precision():
    # Distinctness is judged at ~0.1 m, so sub-millimetre jitter is not three
    # real vertices.
    ring = [[1.0, 1.0], [1.0000000001, 1.0], [1.0, 1.0000000001]]
    assert normalize_ring(ring) is None


# ---------------------------------------------------------------------------
# geometry_from_polygons applies it
# ---------------------------------------------------------------------------


def test_polygon_geometry_is_closed():
    geom = geometry_from_polygons([list(SQUARE_OPEN)])
    assert geom is not None
    ring = geom["coordinates"][0]
    assert ring[0] == ring[-1]
    assert len(ring) >= 4


def test_multipolygon_closes_every_ring():
    geom = geometry_from_polygons([list(SQUARE_OPEN), list(SQUARE_OPEN)])
    assert geom["type"] == "MultiPolygon"
    for poly in geom["coordinates"]:
        assert poly[0][0] == poly[0][-1]


def test_invalid_rings_are_dropped_not_emitted():
    degenerate = [[1.0, 1.0], [1.0, 1.0], [1.0, 1.0]]
    geom = geometry_from_polygons([list(SQUARE_OPEN), degenerate])
    # The good ring survives alone, so this is a Polygon rather than a
    # MultiPolygon with a malformed member.
    assert geom["type"] == "Polygon"
    assert geom["coordinates"][0][0] == geom["coordinates"][0][-1]


def test_all_rings_invalid_yields_no_geometry():
    assert geometry_from_polygons([[[1.0, 1.0], [1.0, 1.0]]]) is None


# ---------------------------------------------------------------------------
# End to end from CAP text
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "1,1 2,2 3,1",  # 3 pairs, unclosed
        "1,1 2,2 3,1 4,4",  # 4 pairs, unclosed — the case a minimum alone misses
        "1,1 2,2 3,1 1,1",  # already conforming
    ],
)
def test_cap_polygon_text_always_reaches_geojson_closed(text):
    geom = geometry_from_polygons([parse_cap_polygon_text(text)])
    ring = geom["coordinates"][0]
    assert ring[0] == ring[-1] and len(ring) >= 4


# ---------------------------------------------------------------------------
# The invariant closure must not disturb
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# MeteoAlarm shares the one parser
# ---------------------------------------------------------------------------


def test_meteoalarm_degenerate_polygon_still_yields_no_geometry():
    # MeteoAlarm used to reject this in its own parser via a distinct-vertex
    # check. That check was redundant once normalize_ring existed — the
    # point-in-polygon path reads back from alert.geometry, so it is normalized
    # too — and the duplicate parser is gone. Outcome must be unchanged.
    info = {"area": [{"polygon": "1,1 1,1 1,1"}]}
    assert geometry_from_polygons(_extract_geometries(info)) is None


def test_meteoalarm_unclosed_polygon_is_closed_like_any_other():
    info = {"area": [{"polygon": "1,1 2,2 3,1"}]}
    geom = geometry_from_polygons(_extract_geometries(info))
    ring = geom["coordinates"][0]
    assert ring[0] == ring[-1] and len(ring) == 4


@pytest.mark.parametrize(
    ("lat", "lon"),
    [(5.0, 5.0), (15.0, 5.0), (5.0, 15.0), (0.0, 0.0), (9.9, 9.9), (5.0, -1.0)],
)
def test_point_in_polygon_unaffected_by_the_closing_vertex(lat, lon):
    # Ray casting already wraps the last edge to the first vertex, so the
    # appended duplicate contributes a zero-length edge that fails the
    # crossing test. Asserted rather than assumed (issue #85).
    assert _point_in_polygons(lat, lon, [list(SQUARE_OPEN)]) == _point_in_polygons(
        lat, lon, [normalize_ring(list(SQUARE_OPEN))]
    )
