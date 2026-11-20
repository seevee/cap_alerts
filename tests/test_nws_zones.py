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
from unittest.mock import patch

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

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

settings.register_profile("cap_alerts", deadline=None)
settings.load_profile("cap_alerts")

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
    # Held at the precision of the rings, so the box is exactly their extent.
    assert zone.bbox == tuple(array("f", (-74.1, 40.7, -73.9, 40.9)))
    assert zone.bbox == (
        min(ring[0::2]),
        min(ring[1::2]),
        max(ring[0::2]),
        max(ring[1::2]),
    )


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
# A hostile or malformed file
#
# The file is only parsed after its hash has matched a published dataset, but
# the parser does not rely on that. Everything below assumes the bytes could
# be anything, and asks for two things: a bad file is refused, and the only
# exception that ever leaves the parser is ZoneDataError.
# ---------------------------------------------------------------------------

_GOOD = _zone("NYZ072", "forecast", _NY)


def _one(record: Any, **meta: Any) -> list[bytes]:
    """A dataset holding ``record`` as its only zone line."""
    return _lines({"meta": meta, "zones": [record]})


def _changed(**fields: Any) -> dict[str, Any]:
    return {**_GOOD, **fields}


@pytest.mark.parametrize(
    "record",
    [
        "a string",
        ["a", "list"],
        42,
        None,
        {},
        _changed(code=7),
        _changed(code="NYZ72"),
        _changed(code="nyz072"),
        _changed(code="NYZ072; DROP"),
        _changed(code="../../etc"),
        _changed(layer="bogus"),
        _changed(layer=None),
        _changed(rings=None),
        _changed(rings=[]),
        _changed(rings="rings"),
        _changed(rings=[[[0, 0], [1, 1]]]),
        _changed(rings=[[[0, 0], [1, 1], [2]]]),
        _changed(rings=[[[0, 0, 0], [1, 1, 1], [2, 2, 2]]]),
        _changed(rings=[[0, 0, 1, 1, 2, 2]]),
        _changed(rings=[[["a", "b"], [1, 1], [2, 2]]]),
        _changed(rings=[[[True, False], [1, 1], [2, 2]]]),
        _changed(rings=[[[None, 0], [1, 1], [2, 2]]]),
        _changed(rings=[[[-181.0, 40.0], [-74.0, 40.0], [-74.0, 41.0]]]),
        _changed(rings=[[[-74.0, 91.0], [-74.0, 40.0], [-73.0, 41.0]]]),
        _changed(rings=[[[1e39, 40.0], [-74.0, 40.0], [-73.0, 41.0]]]),
        _changed(bbox=None),
        _changed(bbox=[-74.1, 40.7, -73.9]),
        _changed(bbox=[-74.1, 40.7, -73.9, 40.9, 0]),
        _changed(bbox=["a", 40.7, -73.9, 40.9]),
        _changed(bbox=[True, 40.7, -73.9, 40.9]),
        _changed(bbox=[-999.0, 40.7, -73.9, 40.9]),
        _changed(bbox=[10**400, 40.7, -73.9, 40.9]),  # too large for float()
        {"code": "NYZ072", "layer": "fire", "alias": "county"},
        {"code": "NYZ072", "layer": "forecast", "alias": "forecast"},
        {"code": "NYZ072", "layer": "fire", "alias": "forecast"},  # nothing to alias
    ],
)
def test_a_malformed_zone_record_is_refused(record):
    with pytest.raises(ZoneDataError):
        ZoneSet.from_lines(_one(record))


def test_a_bounding_box_that_hides_its_zone_is_refused():
    """The box decides whether a zone is looked at; a small one would hide it."""
    with pytest.raises(ZoneDataError, match="bbox does not match"):
        ZoneSet.from_lines(_one(_changed(bbox=[-74.05, 40.75, -74.0, 40.8])))


def test_a_bounding_box_larger_than_its_zone_is_refused():
    with pytest.raises(ZoneDataError, match="bbox does not match"):
        ZoneSet.from_lines(_one(_changed(bbox=[-180.0, -90.0, 180.0, 90.0])))


def test_the_same_zone_twice_is_refused():
    with pytest.raises(ZoneDataError, match="appears twice"):
        ZoneSet.from_lines(_lines({"zones": [_GOOD, _GOOD]}))


@pytest.mark.parametrize("literal", [b"NaN", b"Infinity", b"-Infinity"])
def test_not_a_number_and_infinity_are_refused(literal):
    """json accepts them by default; a NaN coordinate breaks every comparison."""
    lines = _one(_GOOD)
    lines[1] = lines[1].replace(b"-74.1", literal, 1)
    with pytest.raises(ZoneDataError):
        ZoneSet.from_lines(_resealed(lines))


@pytest.mark.parametrize("literal", [b"NaN", b"Infinity", b"1e999", b"-1e999"])
def test_a_coordinate_that_is_not_finite_is_refused(literal):
    """1e999 is valid JSON and parses to infinity without any special literal."""
    lines = _one(_GOOD)
    planted = lines[1].replace(b'"rings": [[[-74.1', b'"rings": [[[' + literal, 1)
    assert planted != lines[1]
    lines[1] = planted
    with pytest.raises(ZoneDataError):
        ZoneSet.from_lines(_resealed(lines))


def _resealed(lines: list[bytes]) -> list[bytes]:
    """Recompute the end-of-file checksum, so only the planted fault is wrong."""
    body = lines[:-1]
    footer = json.loads(lines[-1])
    footer["sha256"] = hashlib.sha256(b"".join(body)).hexdigest()
    return [*body, json.dumps(footer).encode() + b"\n"]


@pytest.mark.parametrize(
    "header",
    [
        {"zones": "1"},
        {"zones": True},
        {"zones": 1.0},
        {"zones": -1},
        {"zones": 10**9},
        {"lines": "3"},
        {"lines": 4},  # not zones plus two
        {"meta": []},
        {"meta": "meta"},
    ],
)
def test_a_malformed_header_is_refused(header):
    with pytest.raises(ZoneDataError):
        ZoneSet.from_lines(_lines({"zones": [_GOOD]}, header=header))


@pytest.mark.parametrize(
    "meta",
    [
        {"epsilon_deg": "0.005"},
        {"epsilon_deg": True},
        {"epsilon_deg": -0.005},
        {"epsilon_deg": 50.0},  # would widen every zone by 5,500 km
        {"tail_epsilon_deg": 50.0},
        {"epsilon_deg": 10**400},  # valid JSON, too large for float()
        {"tail_epsilon_deg": -(10**400)},
        {"tail_epsilon_deg": "0.02"},
        {"tail_states": "AK"},
        {"tail_states": ["Alaska"]},
        {"tail_states": [7]},
        {"tail_states": ["AK"] * 100},
        {"sources": "z_16ap26.zip"},
        {"sources": ["z_16ap26.zip"]},
        {"sources": [{}] * 100},
    ],
)
def test_a_malformed_manifest_is_refused(meta):
    for attempt in (
        lambda: ZoneSet.from_lines(_one(_GOOD, **meta)),
        lambda: ZoneSet.from_json(json.dumps({"meta": meta, "zones": [_GOOD]})),
    ):
        with pytest.raises(ZoneDataError):
            attempt()


@pytest.mark.parametrize("checksum", [None, 7, "", "xyz", "0" * 63, "G" * 64])
def test_an_end_line_without_a_real_checksum_is_refused(checksum):
    with pytest.raises(ZoneDataError):
        ZoneSet.from_lines(_lines(footer={"sha256": checksum}))


@pytest.mark.parametrize(
    "line",
    [
        b"[" * 200_000
        + b"]" * 200_000
        + b"\n",  # nesting deep enough to exhaust the stack
        b'{"code": ' + b"9" * 10_000 + b"}\n",  # an integer too long to convert
        b"\xff\xfe not utf-8\n",
        b'{"code": "NYZ072"\n',
        b"[]\n",
        b"7\n",
        b'"eof"\n',
    ],
    ids=[
        "deep-nesting",
        "huge-integer",
        "bad-utf8",
        "cut-off",
        "a-list",
        "a-number",
        "a-string",
    ],
)
def test_a_line_that_is_not_a_zone_record_is_refused(line):
    lines = _lines()
    lines[2] = line
    with pytest.raises(ZoneDataError):
        ZoneSet.from_lines(lines)


def test_a_line_over_the_length_ceiling_is_refused_unparsed(monkeypatch):
    monkeypatch.setattr(nws_zones, "MAX_LINE_BYTES", 400)
    lines = _lines()
    assert len(lines[0]) < 400
    lines[1] = b'{"padding": "' + b"x" * 1000 + b'"}\n'
    with (
        patch.object(nws_zones.json, "loads", wraps=json.loads) as parse,
        pytest.raises(ZoneDataError, match="longer than 400 bytes"),
    ):
        ZoneSet.from_lines(lines)
    assert parse.call_count == 1  # the header only


def test_a_dataset_over_the_size_ceiling_is_refused(monkeypatch):
    monkeypatch.setattr(nws_zones, "MAX_DATASET_BYTES", 300)
    with pytest.raises(ZoneDataError, match="larger than 300 bytes"):
        ZoneSet.from_lines(_lines())


def test_more_zones_than_the_ceiling_is_refused(monkeypatch):
    monkeypatch.setattr(nws_zones, "MAX_ZONES", 3)
    with pytest.raises(ZoneDataError, match="header zones"):
        ZoneSet.from_lines(_lines())
    # The single-document layout has no header to declare a count up front.
    with pytest.raises(ZoneDataError, match="more than 3 zones"):
        ZoneSet.from_json(json.dumps(_DOC))


def test_more_vertices_than_the_ceiling_is_refused(monkeypatch):
    monkeypatch.setattr(nws_zones, "MAX_VERTICES", 10)
    with pytest.raises(ZoneDataError, match="more than 10 vertices"):
        ZoneSet.from_lines(_lines())


def test_more_rings_in_a_zone_than_the_ceiling_is_refused(monkeypatch):
    monkeypatch.setattr(nws_zones, "MAX_RINGS_PER_ZONE", 1)
    with pytest.raises(ZoneDataError, match="too many"):
        ZoneSet.from_lines(_one(_zone("NYZ072", "forecast", _NY, _NJ)))


@pytest.mark.parametrize(
    "document",
    [
        "[]",
        "7",
        '{"zones": 7}',
        '{"zones": [7]}',
        '{"meta": 7, "zones": []}',
        "{",
        "NaN",
    ],
)
def test_the_single_document_reader_refuses_the_same_things(document):
    with pytest.raises(ZoneDataError):
        ZoneSet.from_json(document)


@given(data=st.data())
def test_no_damage_to_a_good_file_gets_past_as_anything_but_a_refusal(data):
    """Flip, drop or insert bytes anywhere; the checksum is recomputed each time.

    Recomputing it matters: otherwise the checksum would catch every change
    and nothing behind it would be exercised. Whatever the damage, the file
    either still loads or is refused with ZoneDataError. No other exception,
    whatever the bytes.
    """
    lines = _lines()
    index = data.draw(st.integers(0, len(lines) - 2))
    line = bytearray(lines[index])
    for _ in range(data.draw(st.integers(1, 4))):
        position = data.draw(st.integers(0, len(line) - 1))
        action = data.draw(st.sampled_from(["flip", "drop", "insert"]))
        if action == "flip":
            line[position] = data.draw(st.integers(0, 255))
        elif action == "drop":
            del line[position]
        else:
            line.insert(position, data.draw(st.integers(0, 255)))
        if not line:
            line = bytearray(b"\n")
    lines[index] = bytes(line)
    try:
        ZoneSet.from_lines(_resealed(lines))
    except ZoneDataError:
        pass


_JSON = st.recursive(
    st.none()
    | st.booleans()
    | st.integers(-(10**6), 10**6)
    | st.floats(allow_nan=False, allow_infinity=False, width=32)
    | st.text(max_size=8),
    lambda inner: (
        st.lists(inner, max_size=4)
        | st.dictionaries(
            st.sampled_from(["code", "layer", "bbox", "rings", "alias", "x"]),
            inner,
            max_size=5,
        )
    ),
    max_leaves=12,
)


@given(record=_JSON)
def test_any_json_value_as_a_zone_record_loads_or_is_refused(record):
    try:
        ZoneSet.from_lines(_one(record))
    except ZoneDataError:
        pass


# ---------------------------------------------------------------------------
# The tolerances the dataset was simplified with
# ---------------------------------------------------------------------------


def test_tolerance_is_the_simplification_tolerance_in_kilometres(zones):
    """0.005 degrees along a meridian. Matching uses it to close slivers."""
    assert zones.tolerance_km("NYZ072") == pytest.approx(0.556, abs=0.001)
    assert zones.max_tolerance_km == pytest.approx(0.556, abs=0.001)


def test_the_coarser_tolerance_applies_to_the_states_the_manifest_names():
    doc = {
        "meta": {"epsilon_deg": 0.005, "tail_epsilon_deg": 0.02, "tail_states": ["AK"]},
        "zones": [_zone("NYZ072", "forecast", _NY)],
    }
    for loaded in (ZoneSet.from_json(json.dumps(doc)), ZoneSet.from_lines(_lines(doc))):
        assert loaded.tolerance_km("AKZ787") == pytest.approx(2.224, abs=0.001)
        assert loaded.tolerance_km("AKC016") == pytest.approx(2.224, abs=0.001)
        # Alaska's marine zones carry the PK prefix and were not coarsened.
        assert loaded.tolerance_km("PKZ767") == pytest.approx(0.556, abs=0.001)
        assert loaded.tolerance_km("NYZ072") == pytest.approx(0.556, abs=0.001)
        assert loaded.max_tolerance_km == pytest.approx(2.224, abs=0.001)


def test_tolerance_includes_one_step_of_the_stored_rounding():
    """Vertices are rounded after simplifying, which moves a boundary a step more."""
    doc = {
        "meta": {"epsilon_deg": 0.005, "decimals": 4},
        "zones": [_zone("NYZ072", "forecast", _NY)],
    }
    for loaded in (ZoneSet.from_json(json.dumps(doc)), ZoneSet.from_lines(_lines(doc))):
        assert loaded.rounding_deg == pytest.approx(0.0001)
        assert loaded.tolerance_km("NYZ072") == pytest.approx(
            0.0051 * 111.195, abs=0.001
        )
        assert loaded.max_tolerance_km == pytest.approx(0.0051 * 111.195, abs=0.001)


def test_rounding_alone_gives_no_tolerance():
    """Only a dataset that records a tolerance has a band at all."""
    loaded = ZoneSet.from_json(
        json.dumps(
            {"meta": {"decimals": 4}, "zones": [_zone("NYZ072", "forecast", _NY)]}
        )
    )
    assert loaded.tolerance_km("NYZ072") == 0.0
    assert loaded.max_tolerance_km == 0.0


@pytest.mark.parametrize("decimals", ["4", 4.0, True, -1, 10, [4]])
def test_a_malformed_rounding_is_refused(decimals):
    doc = {
        "meta": {"epsilon_deg": 0.005, "decimals": decimals},
        "zones": [_zone("NYZ072", "forecast", _NY)],
    }
    with pytest.raises(ZoneDataError):
        ZoneSet.from_json(json.dumps(doc))


def test_a_dataset_that_records_no_tolerance_has_none():
    loaded = ZoneSet.from_json(
        json.dumps({"zones": [_zone("NYZ072", "forecast", _NY)]})
    )
    assert loaded.tolerance_km("NYZ072") == 0.0
    assert loaded.tolerance_km("AKZ787") == 0.0
    assert loaded.max_tolerance_km == 0.0


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


def test_land_sea_rule_refuses_a_set_without_counties():
    """No county layer would make every land point read as at sea."""
    no_counties = ZoneSet.from_json(json.dumps(_ALEUTIAN))
    with pytest.raises(ValueError, match="county layer"):
        no_counties.containing(51.0, 178.0)


def test_a_point_on_a_zone_s_extreme_vertex_is_not_pruned_by_its_box():
    """40.9 as float32 lies just north of 40.9 as float64."""
    loaded = ZoneSet.from_lines(_one(_GOOD))
    zone = loaded.get("forecast", "NYZ072")
    assert zone is not None
    north = max(zone.rings[0][1::2])
    assert north > 40.9
    assert bbox_distance_km(north, -74.0, zone.bbox) == 0.0


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
    for lon in (-178.0, 178.0):
        hits = split.containing(51.0, lon, land_sea_rule=False)
        assert _pairs(hits) == {("marine", "PKZ785")}


def test_a_split_zone_still_excludes_points_between_its_parts():
    split = ZoneSet.from_json(json.dumps(_ALEUTIAN))
    assert split.containing(51.0, 0.0, land_sea_rule=False) == []
    assert split.containing(51.0, -170.0, land_sea_rule=False) == []
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


# ---------------------------------------------------------------------------
# The grid a lookup goes through
# ---------------------------------------------------------------------------


def _scattered() -> ZoneSet:
    """Counties in four parts of the country, one zone that is very wide, one
    split at the antimeridian, and one that touches it from the east."""
    return ZoneSet.from_json(
        json.dumps(
            {
                "meta": {"epsilon_deg": 0.005},
                "zones": [
                    _zone("NYC061", "county", _NY),
                    _zone("CAC037", "county", _square(-118.9, 33.7, -117.6, 34.8)),
                    _zone("FLC086", "county", _square(-80.9, 25.1, -80.1, 26.0)),
                    _zone("AKC020", "county", _square(-150.5, 60.7, -148.9, 61.5)),
                    # On a cell corner: its box ends exactly at 40 N, 100 W.
                    _zone("KSC001", "county", _square(-101.0, 39.0, -100.0, 40.0)),
                    # Forty degrees by thirty: more cells than the grid takes.
                    _zone("PZZ900", "marine", _square(-170.0, 20.0, -130.0, 50.0)),
                    # Two rings either side of 180, as NWS splits such a zone.
                    _zone(
                        "PKZ786",
                        "marine",
                        _square(172.0, 51.0, 180.0, 53.0),
                        _square(-180.0, 51.0, -176.0, 53.0),
                    ),
                    # Wholly east of the line, reaching it.
                    _zone("PKZ787", "marine", _square(-180.0, 54.0, -178.5, 55.0)),
                    # Wholly west of the line, reaching it.
                    _zone("PKZ785", "marine", _square(178.0, 48.0, 180.0, 49.0)),
                ],
            }
        )
    )


def _scan_everything(zones: ZoneSet) -> Any:
    """Make every lookup test every zone, as it did before the grid."""
    return patch.object(ZoneSet, "_near", lambda self, lat, lon, radius_km: self._zones)


def test_a_lookup_tests_the_zones_near_the_point_and_not_the_rest():
    zones = _scattered()
    near = list(zones._near(*_MANHATTAN, 16.0))
    assert zones.get("county", "NYC061") in near
    assert zones.get("county", "CAC037") not in near
    assert zones.get("county", "FLC086") not in near
    assert len(near) < len(zones)


def test_a_zone_too_wide_for_the_grid_is_tested_on_every_lookup():
    """Its box covers 1,200 cells. It is kept apart, and still found."""
    zones = _scattered()
    wide = {zones._zones[position].code for position in zones._wide}
    assert wide == {"PZZ900", "PKZ786"}
    assert _pairs(zones.containing(35.0, -150.0, land_sea_rule=False)) == {
        ("marine", "PZZ900")
    }


def test_a_point_on_the_corner_of_a_cell_finds_the_zone_that_ends_there():
    zones = _scattered()
    assert [h.code for h in zones.resolve(40.0, -100.0, radius_km=0.0)] == ["KSC001"]
    assert [h.code for h in zones.resolve(40.001, -99.999, radius_km=1.0)] == ["KSC001"]


def test_a_lookup_reaches_across_the_antimeridian():
    """A point just west of the line is a few kilometres from a zone whose box
    lies wholly east of it, in a cell 359 columns away."""
    zones = _scattered()
    assert zones._zones.index(zones.get("marine", "PKZ787")) not in zones._wide
    hits = zones.resolve(54.5, 179.95, radius_km=10.0)
    assert [h.code for h in hits] == ["PKZ787"]
    assert hits[0].distance_km < 4.0
    # And the other way: from just east of the line to a zone that ends at it.
    assert zones._zones.index(zones.get("marine", "PKZ785")) not in zones._wide
    hits = zones.resolve(48.5, -179.99, radius_km=5.0)
    assert [h.code for h in hits] == ["PKZ785"]
    assert hits[0].distance_km < 1.0
    # On the line itself, where the cell at 180 E and the cell at 180 W are
    # one cell under two names.
    # On the line itself, under either of its two names.
    assert [h.code for h in zones.resolve(48.5, 180.0, radius_km=0.0)] == ["PKZ785"]
    assert [h.code for h in zones.resolve(48.5, -180.0, radius_km=0.0)] == ["PKZ785"]


@pytest.mark.parametrize(
    ("lat", "lon", "radius_km"),
    [
        (90.0, 0.0, 16.0),  # at the pole a degree of longitude has no length
        (89.9999, 20.0, 50.0),
        (-90.0, 0.0, 5.0),
        (40.0, -100.0, 25_000.0),  # further than anywhere is
        (91.0, 0.0, 1.0),  # not a latitude; the scan decides, as before
    ],
)
def test_near_a_pole_or_at_a_great_radius_every_zone_is_tested(lat, lon, radius_km):
    zones = _scattered()
    assert len(list(zones._near(lat, lon, radius_km))) == len(zones)


@pytest.mark.parametrize(
    ("lat", "lon", "radius_km"),
    [
        (40.8, float("nan"), 16.0),
        (40.8, float("inf"), 16.0),
        (40.8, float("-inf"), 0.0),
        (float("nan"), -74.0, 16.0),
        (float("inf"), -74.0, 1.0),
        (40.8, -74.0, float("nan")),
        (40.8, -74.0, float("inf")),
        (40.8, -74.0, -5.0),
        (40.8, 1e300, 5.0),
    ],
)
def test_a_coordinate_that_is_not_a_number_is_handled_as_the_scan_handles_it(
    lat, lon, radius_km
):
    """The grid must not add a way to fail. A longitude of NaN or infinity has
    no cell, and looking one up raised where the scan returned nothing."""

    def outcome(zones: ZoneSet) -> Any:
        try:
            return [h.code for h in zones.resolve(lat, lon, radius_km=radius_km)]
        except (ValueError, OverflowError) as err:
            return type(err).__name__

    zones = _scattered()
    through_the_grid = outcome(zones)
    with _scan_everything(zones):
        by_scanning = outcome(zones)
    assert through_the_grid == by_scanning


_ANYWHERE = st.one_of(
    st.tuples(st.floats(24.0, 50.0), st.floats(-125.0, -66.0)),
    st.tuples(st.floats(50.0, 62.0), st.floats(170.0, 180.0)),
    st.tuples(st.floats(50.0, 62.0), st.floats(-180.0, -170.0)),
    st.tuples(st.floats(-90.0, 90.0), st.floats(-180.0, 180.0)),
    st.tuples(st.integers(20, 62).map(float), st.integers(-180, 180).map(float)),
    st.sampled_from([_MANHATTAN, (34.2, -118.2), (25.5, -80.5), (61.0, -149.9)]),
)
_REACH = st.sampled_from([0.0, 0.6, 2.2, 16.0, 80.0, 400.0, 3000.0, 25_000.0])


@given(point=_ANYWHERE, radius_km=_REACH)
@settings(max_examples=300, deadline=None)
def test_the_grid_never_changes_what_a_lookup_returns(point, radius_km):
    """The grid is only a way to skip zones that cannot match. Whatever the
    point and the radius, the result is what testing every zone gives."""
    zones = _scattered()
    lat, lon = point
    through_the_grid = zones.resolve(lat, lon, radius_km=radius_km, max_zones=10_000)
    with _scan_everything(zones):
        by_scanning = zones.resolve(lat, lon, radius_km=radius_km, max_zones=10_000)
    assert through_the_grid == by_scanning
