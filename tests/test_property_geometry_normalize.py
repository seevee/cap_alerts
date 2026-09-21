"""Property-based tests over the coordinate, geometry, normalization and
payload code paths.

These are the pure functions every provider's output flows through, and the
example tests beside them each pin one shape a feed once sent. Here Hypothesis
generates the shapes, so the invariants hold for inputs nobody has captured
yet: rings always come back closed and in GeoJSON order, bboxes always contain
their vertices, phases always land in the four-value vocabulary, and a trimmed
payload never exceeds its budget by construction.
"""

from __future__ import annotations

import copy
import math
from datetime import datetime, timezone

from hypothesis import given, settings
from hypothesis import strategies as st

from custom_components.cap_alerts.const import (
    BUDDHIST_ERA_OFFSET,
    MIN_BUDDHIST_ERA_YEAR,
)
from custom_components.cap_alerts.normalize import (
    MAX_STATE_LENGTH,
    _bbox_from_geometry,
    _compute_phase,
    _gregorian,
    _normalize_phase,
    _truncate_state,
    normalize_alerts,
)
from custom_components.cap_alerts.payload import (
    DROP_PRIORITY,
    TRIM_PRIORITY,
    fit_to_budget,
    measure,
    truncate_bytes,
)
from custom_components.cap_alerts.providers.cap import parse_cap_polygon_text
from custom_components.cap_alerts.providers.geometry import (
    geometry_from_points,
    geometry_from_polygons,
    geometry_from_shapes,
    normalize_ring,
    points_from_circles,
)
from custom_components.cap_alerts.providers.gps import parse_gps, point_in_polygon
from tests.conftest import make_alert

# The HA test environment is slow to import and Hypothesis's per-example
# deadline is a flakiness source there, not a signal.
settings.register_profile("cap_alerts", deadline=None)
settings.load_profile("cap_alerts")

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Coordinates on a 1e-5 degree grid: distinct grid points stay distinct after
# ``normalize_ring``'s rounding to six decimals, and ``repr`` round-trips
# exactly through ``float``.
lats = st.integers(-9_000_000, 9_000_000).map(lambda i: i / 100_000)
lons = st.integers(-18_000_000, 18_000_000).map(lambda i: i / 100_000)
# GeoJSON position order.
positions = st.tuples(lons, lats).map(list)
# Three or more distinct positions: a valid ring's worth.
rings = st.lists(positions, min_size=3, max_size=10, unique_by=tuple)


def polygon_text(ring: list[list[float]]) -> str:
    """Render a ``[[lon, lat], ...]`` ring the way CAP writes it."""
    return " ".join(f"{lat!r},{lon!r}" for lon, lat in ring)


# ---------------------------------------------------------------------------
# CAP polygon text and rings
# ---------------------------------------------------------------------------


@given(rings)
def test_polygon_text_round_trips_in_geojson_order(ring):
    assert parse_cap_polygon_text(polygon_text(ring)) == ring


@given(st.lists(positions, min_size=0, max_size=2))
def test_polygon_text_with_fewer_than_three_pairs_is_rejected(short):
    assert parse_cap_polygon_text(polygon_text(short)) is None


@given(st.text())
def test_polygon_text_never_raises(text):
    result = parse_cap_polygon_text(text)
    assert result is None or (
        len(result) >= 3
        and all(
            len(pos) == 2 and all(isinstance(v, float) for v in pos) for pos in result
        )
    )


@given(rings, st.booleans())
def test_normalize_ring_closes_and_preserves_order(ring, pre_closed):
    source = [*ring, list(ring[0])] if pre_closed else ring
    result = normalize_ring(source)
    assert result is not None
    assert result[0] == result[-1]
    assert len(result) >= 4
    assert result[:-1] == ring


@given(positions, st.integers(1, 6), positions)
def test_ring_with_under_three_distinct_vertices_is_rejected(a, repeats, b):
    assert normalize_ring([list(a)] * repeats) is None
    assert normalize_ring([list(a), list(b)] * repeats) is None


@given(st.lists(rings, min_size=0, max_size=4), st.lists(positions, max_size=1))
def test_geometry_from_polygons_picks_type_by_ring_count(good, degenerate_point):
    degenerate = [[list(p)] * 3 for p in degenerate_point]  # one repeated vertex
    geometry = geometry_from_polygons([*degenerate, *good])
    if not good:
        assert geometry is None
    elif len(good) == 1:
        assert geometry["type"] == "Polygon"
        assert geometry["coordinates"][0][:-1] == good[0]
    else:
        assert geometry["type"] == "MultiPolygon"
        assert [poly[0][:-1] for poly in geometry["coordinates"]] == good


@given(st.lists(positions, max_size=5))
def test_geometry_from_points_picks_type_by_count(points):
    geometry = geometry_from_points(points)
    if not points:
        assert geometry is None
    elif len(points) == 1:
        assert geometry == {"type": "Point", "coordinates": points[0]}
    else:
        assert geometry == {"type": "MultiPoint", "coordinates": points}


@given(st.lists(rings, max_size=2), st.lists(positions, max_size=3))
def test_geometry_from_shapes_prefers_polygons(polys, points):
    geometry = geometry_from_shapes(polys, points)
    if polys:
        assert geometry["type"] in ("Polygon", "MultiPolygon")
    elif points:
        assert geometry["type"] in ("Point", "MultiPoint")
    else:
        assert geometry is None


@given(
    st.lists(st.tuples(lons, lats, st.floats(0, 10, allow_nan=False)), max_size=8),
    st.floats(0, 10, allow_nan=False),
)
def test_points_from_circles_keeps_only_degenerate_ones_in_order(circles, threshold):
    points = points_from_circles(circles, threshold)
    assert points == [[lon, lat] for lon, lat, r in circles if r <= threshold]


# ---------------------------------------------------------------------------
# Bounding boxes
# ---------------------------------------------------------------------------


@given(st.lists(rings, min_size=1, max_size=3))
def test_bbox_contains_every_vertex(polys):
    bbox = _bbox_from_geometry(geometry_from_polygons(polys))
    assert bbox is not None
    min_lon, min_lat, max_lon, max_lat = bbox
    assert min_lon <= max_lon and min_lat <= max_lat
    for ring in polys:
        for lon, lat in ring:
            assert min_lon <= lon <= max_lon
            assert min_lat <= lat <= max_lat


@given(positions)
def test_bbox_of_a_point_is_degenerate(point):
    lon, lat = point
    assert _bbox_from_geometry({"type": "Point", "coordinates": point}) == (
        lon,
        lat,
        lon,
        lat,
    )


json_scalars = (
    st.none() | st.booleans() | st.integers() | st.floats(allow_nan=False) | st.text()
)
json_values = st.recursive(
    json_scalars,
    lambda inner: (
        st.lists(inner, max_size=4)
        | st.dictionaries(st.text(max_size=8), inner, max_size=4)
    ),
    max_leaves=12,
)


@given(
    st.dictionaries(
        st.sampled_from(["type", "coordinates", "x"]), json_values, max_size=3
    )
)
def test_bbox_never_raises_on_malformed_geometry(geometry):
    bbox = _bbox_from_geometry(geometry)
    assert bbox is None or (len(bbox) == 4 and all(isinstance(v, float) for v in bbox))


# ---------------------------------------------------------------------------
# GPS
# ---------------------------------------------------------------------------


@given(lats, lons, st.sampled_from(["", " ", "  "]))
def test_parse_gps_round_trips(lat, lon, pad):
    assert parse_gps(f"{pad}{lat!r}{pad},{pad}{lon!r}{pad}") == (lat, lon)


@given(st.text())
def test_parse_gps_never_raises(text):
    result = parse_gps(text)
    assert result is None or (
        len(result) == 2 and all(isinstance(v, float) for v in result)
    )


@given(
    st.floats(-60, 60, allow_nan=False),
    st.floats(-150, 150, allow_nan=False),
    st.floats(0.001, 1.0, allow_nan=False),
    st.integers(3, 12),
)
def test_point_in_polygon_on_a_regular_polygon(clat, clon, r, k):
    ring = [
        [
            clon + r * math.cos(2 * math.pi * i / k),
            clat + r * math.sin(2 * math.pi * i / k),
        ]
        for i in range(k)
    ]
    assert point_in_polygon(clat, clon, ring) is True
    assert point_in_polygon(clat, clon + 3 * r, ring) is False
    assert point_in_polygon(clat + 3 * r, clon, ring) is False


@given(lats, lons, st.lists(positions, max_size=8))
def test_point_in_polygon_never_raises_on_degenerate_rings(lat, lon, ring):
    assert point_in_polygon(lat, lon, ring) in (True, False)


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

# A CAP dateTime tail: what follows the year. Starts with a non-digit, as the
# rewrite's regex requires.
iso_tails = st.builds(
    lambda mo, d, h, mi, s, off: f"-{mo:02d}-{d:02d}T{h:02d}:{mi:02d}:{s:02d}{off}",
    st.integers(1, 12),
    st.integers(1, 28),
    st.integers(0, 23),
    st.integers(0, 59),
    st.integers(0, 59),
    st.sampled_from(["+07:00", "Z", "-00:00", "+00:00", "-05:00"]),
)


@given(
    st.integers(MIN_BUDDHIST_ERA_YEAR, MIN_BUDDHIST_ERA_YEAR + BUDDHIST_ERA_OFFSET - 1),
    iso_tails,
)
def test_gregorian_rewrites_only_the_year_and_is_idempotent(year, tail):
    once = _gregorian(f"{year:04d}{tail}")
    assert once == f"{year - BUDDHIST_ERA_OFFSET:04d}{tail}"
    assert _gregorian(once) == once


@given(st.integers(1000, MIN_BUDDHIST_ERA_YEAR - 1), iso_tails)
def test_gregorian_leaves_gregorian_years_alone(year, tail):
    value = f"{year:04d}{tail}"
    assert _gregorian(value) == value


@given(st.text())
def test_gregorian_never_raises(text):
    assert isinstance(_gregorian(text), str)


@given(st.text())
def test_normalize_phase_stays_in_vocabulary(msg_type):
    assert _normalize_phase(msg_type) in {"new", "update", "cancel"}


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
# ``st.datetimes`` takes naive bounds by contract and attaches the zone itself.
expiries = st.datetimes(
    min_value=datetime(2000, 1, 1),  # noqa: DTZ001
    max_value=datetime(2050, 1, 1),  # noqa: DTZ001
    timezones=st.just(timezone.utc),
)
tokens = st.sampled_from(["", "ended", "cancelled", "active", "final"])


@given(
    expiries | st.just(""),
    st.sampled_from(["Alert", "Update", "Cancel", "Ack", "Error", "", "Actual"]),
    tokens,
    st.dictionaries(tokens.filter(bool), st.just("reason"), max_size=3),
)
def test_compute_phase_is_decided_by_the_clock_first(
    expires, msg_type, status, terminal
):
    expires_text = expires.isoformat() if expires else ""
    phase = _compute_phase(expires_text, msg_type, NOW, status, terminal)
    assert phase in {"new", "update", "cancel", "expired"}
    if expires and NOW > expires:
        assert phase == "expired"
    elif status and status in terminal:
        assert phase == "cancel"
    else:
        assert phase == _normalize_phase(msg_type)


@given(st.text(max_size=600))
def test_truncate_state_respects_the_ha_limit(value):
    result = _truncate_state(value)
    assert len(result) <= MAX_STATE_LENGTH
    if len(value) <= MAX_STATE_LENGTH:
        assert result == value
    else:
        assert result.endswith("…")
        assert value.startswith(result[:-1])
    assert _truncate_state(result) == result


providers = st.sampled_from(
    ["nws", "eccc", "meteoalarm", "wmo", "gdacs", "bbk", "au", "unlisted"]
)
severities = st.sampled_from(
    [
        "Extreme",
        "Severe",
        "Moderate",
        "Minor",
        "Unknown",
        "",
        "Green",
        "extreme",
        "SEVERE",
    ]
)
clean_text = st.text(
    alphabet=st.characters(exclude_categories=("Cc", "Cs", "Co", "Cn")), max_size=300
)


@given(
    st.builds(
        make_alert,
        id=st.text(min_size=1, max_size=20),
        event=clean_text,
        severity=severities,
        msg_type=st.sampled_from(["Alert", "Update", "Cancel", "Ack", ""]),
        provider=providers,
        expires=st.sampled_from(
            ["2099-01-01T00:00:00+00:00", "", "2568-01-01T00:00:00+07:00"]
        ),
        geometry=st.none()
        | st.lists(rings, min_size=1, max_size=2).map(geometry_from_polygons),
    ),
    st.sampled_from(["", "entry-1"]),
)
def test_normalization_is_a_fixed_point(alert, entry_id):
    once = normalize_alerts([alert], entry_id)[0]
    twice = normalize_alerts([once], entry_id)[0]
    assert twice == once
    assert once.severity_normalized in {
        "extreme",
        "severe",
        "moderate",
        "minor",
        "unknown",
    }
    assert once.phase in {"new", "update", "cancel", "expired"}
    assert len(once.event) <= MAX_STATE_LENGTH
    if alert.geometry is None:
        assert once.bbox is None and once.geometry_ref == ""
    else:
        assert once.bbox is not None
        assert once.geometry_ref.endswith(f"{alert.provider}:{alert.id}")


# ---------------------------------------------------------------------------
# Payload budget
# ---------------------------------------------------------------------------


@given(st.text(max_size=400), st.integers(3, 600))
def test_truncate_bytes_fits_and_keeps_a_prefix(text, limit):
    result = truncate_bytes(text, limit)
    assert len(result.encode("utf-8")) <= limit
    if len(text.encode("utf-8")) <= limit:
        assert result == text
    else:
        assert result.endswith("…")
        assert text.startswith(result[:-1])


attr_keys = st.sampled_from(
    [
        *TRIM_PRIORITY,
        *DROP_PRIORITY,
        "id",
        "event",
        "headline",
        "area_desc",
        "parameters",
    ]
)
attr_values = st.text(max_size=3000) | st.lists(st.text(max_size=40), max_size=30)


@given(st.dictionaries(attr_keys, attr_values, max_size=9), st.integers(200, 20_000))
def test_fit_to_budget_never_mutates_and_fits_or_exhausts(attrs, budget):
    snapshot = copy.deepcopy(attrs)
    result = fit_to_budget(attrs, budget=budget)
    assert attrs == snapshot

    before = measure(attrs)
    assert before is not None
    if before <= budget:
        assert result is attrs
        return

    assert set(result) <= set(attrs)
    for key, value in result.items():
        if key in TRIM_PRIORITY and isinstance(value, str) and value != attrs[key]:
            assert value.endswith("…") and attrs[key].startswith(value[:-1])
        elif key not in DROP_PRIORITY:
            assert value == attrs[key]

    after = measure(result)
    assert after is not None
    if after > budget:
        # Nothing left to give: every spendable key is gone.
        assert not any(k in result for k in DROP_PRIORITY)
        assert not any(
            isinstance(result.get(k), str) and result[k] for k in TRIM_PRIORITY
        )
