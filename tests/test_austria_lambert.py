"""EPSG:31287 → WGS84 without pyproj (issue #300)."""

from __future__ import annotations

import math

import pytest

from custom_components.cap_alerts.providers.austria_lambert import (
    reproject_coordinates,
    to_wgs84,
)

# Reference positions from pyproj 3.x (EPSG:31287 → EPSG:4326, always_xy),
# taken with scripts/geosphere_projection_probe.py. The first two are the spec's
# Schwechat vertex and a live Wien-Innere Stadt vertex, the third the
# projection origin, the last three the corners and far west of the grid.
_PYPROJ = (
    ((640566, 472707), (16.564833, 48.108689)),
    ((625350, 482209), (16.365577, 48.199608)),
    ((400000, 400000), (13.332594, 47.499482)),
    ((110000, 270000), (9.568999, 46.266462)),
    ((700000, 590000), (17.445348, 49.137211)),
    ((164746, 341580), (10.241699, 46.931634)),
)


def _metres(a: tuple[float, float], b: tuple[float, float]) -> float:
    dy = (a[1] - b[1]) * 111_320
    dx = (a[0] - b[0]) * 111_320 * math.cos(math.radians(a[1]))
    return math.hypot(dx, dy)


@pytest.mark.parametrize(("projected", "expected"), _PYPROJ)
def test_matches_pyproj_within_a_metre(projected, expected):
    # 0.74 m is the worst case the probe measured over Austria; one metre is
    # the bound, well under the 375 m the source simplifies its shapes to.
    assert _metres(to_wgs84(*projected), expected) < 1.0


def test_multipolygon_nesting_is_kept():
    # An outer ring with a hole, then a second polygon: the depth GeoJSON
    # gives a MultiPolygon must survive, or the hole becomes a polygon.
    outer = [[600000, 480000], [610000, 480000], [610000, 490000], [600000, 480000]]
    hole = [[602000, 482000], [604000, 482000], [604000, 484000], [602000, 482000]]
    other = [[620000, 480000], [621000, 480000], [621000, 481000], [620000, 480000]]
    out = reproject_coordinates([[outer, hole], [other]])
    assert [len(p) for p in out] == [2, 1]
    assert [len(r) for r in out[0]] == [4, 4]
    lon, lat = out[0][1][0]
    assert 16.0 < lon < 16.3 and 48.0 < lat < 48.3
    # Rings stay closed through the transform: same input, same output.
    assert out[0][0][0] == out[0][0][-1]


def test_elevation_is_dropped_and_ints_accepted():
    assert reproject_coordinates([640566, 472707, 180]) == [
        round(v, 6) for v in to_wgs84(640566, 472707)
    ]


@pytest.mark.parametrize("bad", ["625350,482209", [None, None], 5])
def test_non_coordinates_raise(bad):
    with pytest.raises(ValueError):
        reproject_coordinates(bad)
