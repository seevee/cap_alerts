"""Resolving a point to NWS zones locally, with no request (#316).

``providers/nws_zones.py`` answers the two questions the NWS provider asks the
API today by sending coordinates: which zones contain this point, and which
zones lie near it. These tests pin the geometry on a small synthetic set, laid
out around New York Harbor so the codes read like real ones, where every
expected answer can be worked out by hand:

    NJZ006 / NJC017      NYZ072 / NYC061      CTZ009 (CTC001 is its north half)
    -74.5 .. -74.2       -74.1 .. -73.9       -73.7 .. -73.4
                               ANZ338 (marine) below and between

The land-sea rule and the antimeridian case each get their own section,
because each is a place where the obvious implementation is wrong.
"""

from __future__ import annotations

import ast
import hashlib
import json
import sys
from array import array
from pathlib import Path
from typing import Any

import pytest

from custom_components.cap_alerts.providers import nws_zones
from custom_components.cap_alerts.providers.nws_zones import (
    Zone,
    ZoneDataError,
    ZoneSet,
    bbox_distance_km,
    distance_to_rings_km,
    haversine_km,
    needs_recompute,
    point_in_rings,
)

Ring = list[list[float]]


def _square(lon0: float, lat0: float, lon1: float, lat1: float) -> Ring:
    return [[lon0, lat0], [lon1, lat0], [lon1, lat1], [lon0, lat1]]


def _zone(code: str, layer: str, *rings: Ring) -> dict[str, Any]:
    lons = [p[0] for ring in rings for p in ring]
    lats = [p[1] for ring in rings for p in ring]
    return {
        "code": code,
        "layer": layer,
        "bbox": [min(lons), min(lats), max(lons), max(lats)],
        "rings": list(rings),
    }


_NY = _square(-74.1, 40.7, -73.9, 40.9)
_NJ = _square(-74.5, 40.7, -74.2, 40.9)
_CT = _square(-73.7, 40.6, -73.4, 41.0)
_CT_COUNTY = _square(-73.7, 40.8, -73.4, 41.0)
_SEA = _square(-73.95, 40.4, -73.6, 40.75)

_DOC = {
    "meta": {
        "epsilon_deg": 0.005,
        "sources": [
            {"file": "z_16ap26.zip", "md5": "0" * 32, "layer": "forecast"},
            {"file": "c_16ap26.zip", "md5": "1" * 32, "layer": "county"},
        ],
    },
    "zones": [
        _zone("NYZ072", "forecast", _NY),
        _zone("NJZ006", "forecast", _NJ),
        _zone("CTZ009", "forecast", _CT),
        _zone("NYC061", "county", _NY),
        _zone("NJC017", "county", _NJ),
        _zone("CTC001", "county", _CT_COUNTY),
        _zone("ANZ338", "marine", _SEA),
        # Fire zones: one identical to its forecast zone (stored as an alias),
        # one sharing a forecast zone's code with different geometry.
        {"code": "NYZ072", "layer": "fire", "alias": "forecast"},
        _zone("NJZ006", "fire", _square(-74.5, 40.7, -74.35, 40.9)),
    ],
}

# Inside NYZ072 / NYC061, clear of every other zone.
_MANHATTAN = (40.8, -74.0)
# Inside ANZ338 only.
_OPEN_WATER = (40.5, -73.8)


@pytest.fixture(scope="module")
def zones() -> ZoneSet:
    return ZoneSet.from_json(json.dumps(_DOC))


def _pairs(hits: list[nws_zones.ZoneHit]) -> set[tuple[str, str]]:
    return {(h.layer, h.code) for h in hits}


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def test_from_json_reads_every_zone_and_the_manifest(zones):
    assert len(zones) == 9
    assert zones.layers() == {"forecast", "county", "fire", "marine"}
    assert zones.epsilon_deg == 0.005
    assert [s["file"] for s in zones.sources] == ["z_16ap26.zip", "c_16ap26.zip"]


def test_from_json_accepts_bytes_and_a_document_without_meta():
    doc = json.dumps({"zones": [_zone("NYZ072", "forecast", _NY)]}).encode()
    loaded = ZoneSet.from_json(doc)
    assert len(loaded) == 1
    assert loaded.sources == ()
    assert loaded.epsilon_deg == 0.0


def test_rings_are_flat_float_arrays(zones):
    zone = zones.get("forecast", "NYZ072")
    assert zone is not None
    (ring,) = zone.rings
    assert isinstance(ring, array)
    assert ring.typecode == "f"
    assert len(ring) == 8  # four vertices, lon then lat
    assert zone.bbox == (-74.1, 40.7, -73.9, 40.9)


def test_an_aliased_fire_zone_shares_the_forecast_zone_arrays(zones):
    fire = zones.get("fire", "NYZ072")
    forecast = zones.get("forecast", "NYZ072")
    assert fire is not None and forecast is not None
    assert fire.rings[0] is forecast.rings[0]
    assert fire.bbox == forecast.bbox


def test_get_distinguishes_layers_that_share_a_code(zones):
    """437 real codes exist in both fire and forecast with different shapes."""
    fire = zones.get("fire", "NJZ006")
    forecast = zones.get("forecast", "NJZ006")
    assert fire is not None and forecast is not None
    assert fire.bbox != forecast.bbox
    assert zones.get("marine", "NJZ006") is None


def test_resident_bytes_counts_a_shared_array_once(zones):
    # Eight distinct rings of four vertices, two float32 each. The aliased
    # fire zone adds nothing.
    assert zones.resident_bytes() == 8 * 4 * 2 * 4


# ---------------------------------------------------------------------------
# The shipped layout: a header line, one line per zone, an end-of-file line
# ---------------------------------------------------------------------------


def _lines(
    doc: dict[str, Any] = _DOC,
    *,
    header: dict[str, Any] | None = None,
    footer: dict[str, Any] | None = None,
) -> list[bytes]:
    """The file ``scripts/build_zone_dataset.py`` writes for ``doc``."""
    records = doc["zones"]
    counts = {"zones": len(records), "lines": len(records) + 2}
    first = {"format": 2, **counts, "meta": doc.get("meta", {}), **(header or {})}
    body = [json.dumps(line).encode() + b"\n" for line in (first, *records)]
    last = {
        "eof": True,
        **counts,
        "sha256": hashlib.sha256(b"".join(body)).hexdigest(),
        **(footer or {}),
    }
    return [*body, json.dumps(last).encode() + b"\n"]


def test_from_lines_reads_what_from_json_reads(zones):
    loaded = ZoneSet.from_lines(_lines())
    assert len(loaded) == len(zones)
    assert loaded.epsilon_deg == zones.epsilon_deg
    assert loaded.sources == zones.sources
    assert _pairs(loaded.containing(*_MANHATTAN)) == _pairs(
        zones.containing(*_MANHATTAN)
    )


def test_from_lines_resolves_an_alias_that_precedes_its_zone():
    """Sorted by layer, every fire alias comes before the forecast zones."""
    doc = {
        "zones": [
            {"code": "NYZ072", "layer": "fire", "alias": "forecast"},
            _zone("NYZ072", "forecast", _NY),
        ]
    }
    loaded = ZoneSet.from_lines(_lines(doc))
    fire = loaded.get("fire", "NYZ072")
    forecast = loaded.get("forecast", "NYZ072")
    assert fire is not None and forecast is not None
    assert fire.rings[0] is forecast.rings[0]


def test_from_lines_ignores_blank_lines():
    lines = _lines()
    assert len(ZoneSet.from_lines([*lines, b"\n", b"  \n"])) == 9


def test_a_file_cut_short_is_refused():
    """Half a file of lines is still valid lines; the missing last line tells."""
    lines = _lines()
    with pytest.raises(ZoneDataError, match="cut short"):
        ZoneSet.from_lines(lines[:5])


def test_an_empty_file_is_refused():
    with pytest.raises(ZoneDataError, match="empty"):
        ZoneSet.from_lines([])


def test_a_missing_zone_line_is_refused():
    lines = _lines()
    del lines[3]
    with pytest.raises(ZoneDataError, match="zones: header says 9"):
        ZoneSet.from_lines(lines)


def test_a_header_and_footer_that_disagree_are_refused():
    with pytest.raises(ZoneDataError, match="lines: header says 11"):
        ZoneSet.from_lines(_lines(footer={"lines": 12}))


def test_a_damaged_zone_line_is_refused():
    lines = _lines()
    lines[2] = lines[2].replace(b"NJZ006", b"NJZ007")
    with pytest.raises(ZoneDataError, match="checksum"):
        ZoneSet.from_lines(lines)


def test_data_after_the_end_of_file_line_is_refused():
    lines = _lines()
    with pytest.raises(ZoneDataError, match="after the end-of-file line"):
        ZoneSet.from_lines([*lines, lines[1]])


def test_a_line_that_is_not_json_is_refused():
    lines = _lines()
    lines[4] = b'{"code": "CTZ009", "layer"\n'
    with pytest.raises(ZoneDataError, match="line 5 is not JSON"):
        ZoneSet.from_lines(lines)


@pytest.mark.parametrize("first", [b"[]\n", b'{"format": 1}\n', b'{"zones": []}\n'])
def test_another_layout_is_refused(first):
    with pytest.raises(ZoneDataError, match="not a format 2 zone dataset"):
        ZoneSet.from_lines([first])


# ---------------------------------------------------------------------------
# Geometry primitives
# ---------------------------------------------------------------------------


def test_haversine_matches_a_known_distance():
    # One degree of latitude on this sphere.
    assert haversine_km(40.0, -74.0, 41.0, -74.0) == pytest.approx(111.195, abs=0.01)
    assert haversine_km(40.0, -74.0, 40.0, -74.0) == 0.0


def test_point_in_rings_handles_holes_and_separate_parts():
    outer = array("f", [0, 0, 10, 0, 10, 10, 0, 10])
    hole = array("f", [4, 4, 6, 4, 6, 6, 4, 6])
    island = array("f", [20, 0, 22, 0, 22, 2, 20, 2])
    shape = (outer, hole, island)
    assert point_in_rings(2.0, 2.0, shape)
    assert not point_in_rings(5.0, 5.0, shape)  # in the hole
    assert point_in_rings(1.0, 21.0, shape)  # on the island
    assert not point_in_rings(1.0, 15.0, shape)  # between the parts


def test_distance_to_rings_is_the_distance_to_the_nearest_edge():
    ring = array("f", [c for point in _NY for c in point])
    # 0.1 degrees of longitude west of the west edge, at 40.8 N.
    expected = haversine_km(40.8, -74.2, 40.8, -74.1)
    assert distance_to_rings_km(40.8, -74.2, (ring,), 50.0) == pytest.approx(
        expected, rel=1e-3
    )


def test_distance_to_rings_is_infinite_beyond_the_cutoff():
    ring = array("f", [c for point in _NY for c in point])
    assert distance_to_rings_km(40.8, -76.0, (ring,), 10.0) == float("inf")


def test_distance_to_rings_survives_a_zero_length_edge():
    ring = array("f", [0, 0, 0, 0, 1, 0, 1, 1])
    assert distance_to_rings_km(0.0, -0.1, (ring,), 50.0) == pytest.approx(
        haversine_km(0.0, -0.1, 0.0, 0.0), rel=1e-3
    )


def test_bbox_distance_is_zero_inside_and_a_lower_bound_outside():
    bbox = (-74.1, 40.7, -73.9, 40.9)
    assert bbox_distance_km(40.8, -74.0, bbox) == 0.0
    west = bbox_distance_km(40.8, -74.2, bbox)
    assert west == pytest.approx(haversine_km(40.8, -74.2, 40.8, -74.1), rel=1e-3)
    assert bbox_distance_km(41.0, -74.2, bbox) > west  # diagonal is farther


# ---------------------------------------------------------------------------
# Nearby zones: what builds the request
# ---------------------------------------------------------------------------


def test_resolve_puts_a_containing_zone_first_at_distance_zero(zones):
    hits = zones.resolve(*_MANHATTAN, radius_km=1.0)
    assert _pairs(hits) == {
        ("forecast", "NYZ072"),
        ("county", "NYC061"),
        ("fire", "NYZ072"),
    }
    assert all(h.distance_km == 0.0 for h in hits)


def test_resolve_reaches_a_neighbour_only_when_the_radius_does(zones):
    # NJZ006's east edge is 0.1 degrees of longitude from NYZ072's west edge:
    # 8.4 km at this latitude, so 16.8 km from the middle of NYZ072.
    near = zones.resolve(*_MANHATTAN, radius_km=16.0, layers=("forecast",))
    assert _pairs(near) == {("forecast", "NYZ072")}
    far = zones.resolve(*_MANHATTAN, radius_km=18.0, layers=("forecast",))
    assert [h.code for h in far] == ["NYZ072", "NJZ006"]
    assert far[1].distance_km == pytest.approx(16.8, abs=0.2)


def test_resolve_caps_each_layer_and_keeps_the_nearest(zones):
    hits = zones.resolve(*_MANHATTAN, radius_km=60.0, max_zones=1)
    by_layer = {h.layer: h.code for h in hits}
    assert by_layer == {
        "forecast": "NYZ072",
        "county": "NYC061",
        "fire": "NYZ072",
        "marine": "ANZ338",
    }


def test_resolve_ignores_layers_not_asked_for(zones):
    hits = zones.resolve(*_MANHATTAN, radius_km=60.0, layers=("marine",))
    assert _pairs(hits) == {("marine", "ANZ338")}


def test_codes_are_deduplicated_across_layers(zones):
    """A fire zone and a forecast zone can carry one code; send it once."""
    codes = zones.codes(*_MANHATTAN, radius_km=1.0)
    assert sorted(codes) == ["NYC061", "NYZ072"]


# ---------------------------------------------------------------------------
# Containing zones: what decides whether an alert applies
# ---------------------------------------------------------------------------


def test_containing_on_land_returns_the_land_layers(zones):
    assert _pairs(zones.containing(*_MANHATTAN)) == {
        ("forecast", "NYZ072"),
        ("county", "NYC061"),
        ("fire", "NYZ072"),
    }


def test_containing_at_sea_returns_the_marine_zone(zones):
    assert _pairs(zones.containing(*_OPEN_WATER)) == {("marine", "ANZ338")}


def test_land_sea_rule_drops_the_marine_zone_under_a_county(zones):
    """A marine polygon can reach inland; a point inside a county is on land."""
    harbour_edge = (40.72, -73.92)  # inside NYC061 and inside ANZ338
    assert ("marine", "ANZ338") in _pairs(
        zones.containing(*harbour_edge, land_sea_rule=False)
    )
    assert _pairs(zones.containing(*harbour_edge)) == {
        ("forecast", "NYZ072"),
        ("county", "NYC061"),
        ("fire", "NYZ072"),
    }


def test_land_sea_rule_drops_a_forecast_zone_that_reaches_past_the_coast(zones):
    """In no county means at sea, even inside a forecast zone's polygon."""
    off_the_coast = (40.7, -73.65)  # inside CTZ009 and ANZ338, south of CTC001
    assert _pairs(zones.containing(*off_the_coast, land_sea_rule=False)) == {
        ("forecast", "CTZ009"),
        ("marine", "ANZ338"),
    }
    assert _pairs(zones.containing(*off_the_coast)) == {("marine", "ANZ338")}


def test_land_sea_rule_consults_counties_the_caller_did_not_ask_for(zones):
    """Asking for forecast zones only must not make every point look at sea."""
    hits = zones.containing(*_MANHATTAN, layers=("forecast",))
    assert _pairs(hits) == {("forecast", "NYZ072")}
    at_sea = zones.containing(*_OPEN_WATER, layers=("forecast", "marine"))
    assert _pairs(at_sea) == {("marine", "ANZ338")}


def test_containing_nothing_far_from_every_zone(zones):
    assert zones.containing(10.0, 10.0) == []


# ---------------------------------------------------------------------------
# The antimeridian
# ---------------------------------------------------------------------------

_ALEUTIAN = {
    "zones": [
        # NWS splits a zone that crosses 180 degrees into one ring per side.
        _zone(
            "PKZ785",
            "marine",
            _square(177.0, 50.0, 180.0, 52.0),
            _square(-180.0, 50.0, -177.0, 52.0),
        ),
    ],
}


def test_a_zone_split_at_the_antimeridian_contains_points_on_both_sides():
    """Its min/max box runs -180..180; wrapping that box hid the whole zone.

    The western Aleutians (AKC016) and three Bering Sea marine zones are built
    this way, so Adak and Attu resolved to nothing.
    """
    split = ZoneSet.from_json(json.dumps(_ALEUTIAN))
    zone = split.get("marine", "PKZ785")
    assert zone is not None
    assert zone.bbox[2] - zone.bbox[0] > 180.0
    assert _pairs(split.containing(51.0, -178.0)) == {("marine", "PKZ785")}
    assert _pairs(split.containing(51.0, 178.0)) == {("marine", "PKZ785")}


def test_a_split_zone_still_excludes_points_between_its_parts():
    split = ZoneSet.from_json(json.dumps(_ALEUTIAN))
    assert split.containing(51.0, 0.0) == []
    assert split.containing(51.0, -170.0) == []
    assert split.resolve(51.0, 0.0, radius_km=50.0) == []


def test_a_split_zone_is_still_pruned_by_latitude():
    bbox = (-180.0, 50.0, 180.0, 52.0)
    assert bbox_distance_km(51.0, 0.0, bbox) == 0.0
    assert bbox_distance_km(40.0, 0.0, bbox) == pytest.approx(
        haversine_km(40.0, 0.0, 50.0, 0.0), rel=1e-3
    )


def test_distance_across_the_antimeridian_is_short():
    split = ZoneSet.from_json(json.dumps(_ALEUTIAN))
    zone = split.get("marine", "PKZ785")
    assert zone is not None
    # Half a degree north of the zone's top edge, just west of 180.
    distance = distance_to_rings_km(52.5, 179.9, zone.rings, 100.0)
    assert distance == pytest.approx(55.6, abs=0.5)


# ---------------------------------------------------------------------------
# Tracker hysteresis
# ---------------------------------------------------------------------------


def test_needs_recompute_on_the_first_position():
    assert needs_recompute(40.8, -74.0, None, radius_km=16.0)


def test_needs_recompute_only_after_moving_past_the_threshold():
    last = (40.8, -74.0)
    # 0.05 degrees of latitude is 5.6 km; the default threshold is 8 km.
    assert not needs_recompute(40.85, -74.0, last, radius_km=16.0)
    assert needs_recompute(40.9, -74.0, last, radius_km=16.0)
    assert needs_recompute(40.85, -74.0, last, radius_km=16.0, hysteresis_pct=25.0)


# ---------------------------------------------------------------------------
# What the module may import
# ---------------------------------------------------------------------------


def test_the_module_imports_only_the_standard_library():
    """No Home Assistant, no third party: it has to run in a bare interpreter."""
    tree = ast.parse(Path(nws_zones.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "relative import"
            assert node.module is not None
            imported.add(node.module.split(".")[0])
    assert imported <= set(sys.stdlib_module_names)


def test_zone_is_immutable(zones):
    zone = zones.get("forecast", "NYZ072")
    assert isinstance(zone, Zone)
    with pytest.raises(AttributeError):
        zone.code = "NYZ073"  # type: ignore[misc]
