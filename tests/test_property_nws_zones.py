"""Property-based tests for the local zone resolver's geometry (#316).

``providers/nws_zones.py`` keeps each ring as a flat ``array('f')`` of
``lon, lat`` values and has its own ray-casting test over that layout.
``providers/gps.py`` has the integration's other containment test,
``point_in_polygon``, over lists of ``[lon, lat]`` pairs. Reusing it would mean
rebuilding the lists the flat arrays exist to avoid, so the resolver keeps its
own. Two implementations of one question can drift, and these tests are what
stops that: Hypothesis draws the rings and the point, and the two must agree.

Both are fed the same numbers. The rings go into ``array('f')`` first and the
pairs handed to ``point_in_polygon`` are read back out of it, so neither side
sees a coordinate the other does not.
"""

from __future__ import annotations

from array import array
from functools import reduce
from operator import xor

from hypothesis import given, settings
from hypothesis import strategies as st

from custom_components.cap_alerts.providers.gps import point_in_polygon
from custom_components.cap_alerts.providers.nws_zones import (
    bbox_distance_km,
    distance_to_rings_km,
    point_in_rings,
)

# The HA test environment is slow to import and Hypothesis's per-example
# deadline is a flakiness source there, not a signal.
settings.register_profile("cap_alerts", deadline=None)
settings.load_profile("cap_alerts")

# A patch of the mid-latitudes on a 1e-3 degree grid. Coarse on purpose: with
# few distinct values, points land exactly on vertices and on horizontal
# edges, which is where two ray-casting loops would part ways if they could.
_LON = st.integers(-74_500, -73_500).map(lambda v: v / 1000)
_LAT = st.integers(40_000, 41_000).map(lambda v: v / 1000)
_VERTEX = st.tuples(_LON, _LAT)
_RING = st.lists(_VERTEX, min_size=3, max_size=12)
_RINGS = st.lists(_RING, min_size=1, max_size=3)


def _flat(ring: list[tuple[float, float]]) -> array[float]:
    return array("f", (value for vertex in ring for value in vertex))


def _pairs(ring: array[float]) -> list[list[float]]:
    return [[ring[i], ring[i + 1]] for i in range(0, len(ring), 2)]


@given(rings=_RINGS, lon=_LON, lat=_LAT)
def test_containment_agrees_with_gps_point_in_polygon(rings, lon, lat):
    """Even-odd over several rings is the exclusive-or of each ring's answer."""
    flat = [_flat(ring) for ring in rings]
    expected = reduce(
        xor, (point_in_polygon(lat, lon, _pairs(ring)) for ring in flat), False
    )
    assert point_in_rings(lat, lon, flat) is expected


@given(ring=_RING, lon=_LON, lat=_LAT)
def test_one_ring_matches_gps_point_in_polygon_exactly(ring, lon, lat):
    flat = _flat(ring)
    assert point_in_rings(lat, lon, [flat]) is point_in_polygon(lat, lon, _pairs(flat))


@given(ring=_RING, lon=_LON, lat=_LAT)
def test_the_bbox_distance_never_exceeds_the_distance_to_the_shape(ring, lon, lat):
    """The box is the prefilter: if it overestimated, a near zone would be skipped."""
    flat = _flat(ring)
    lons, lats = flat[0::2], flat[1::2]
    bbox = (min(lons), min(lats), max(lons), max(lats))
    to_shape = distance_to_rings_km(lat, lon, [flat], 1e9)
    # Both use the same flat-earth projection about the query point; allow for
    # float rounding and nothing more.
    assert bbox_distance_km(lat, lon, bbox) <= to_shape + 1e-6
