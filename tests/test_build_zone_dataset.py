"""The zone dataset builder keeps every place, and the check notices if it does not.

``scripts/build_zone_dataset.py`` simplifies NWS's zone outlines. A ring
thinner than the tolerance collapses under simplification, and the first
builder dropped every ring that did. On the April 2026 shapefiles that lost
about 4,100 places on each land layer, barrier islands 12 km long among them,
and left eleven zones out of the dataset altogether, with nothing to say so.

These tests build datasets from small shapefiles made here, one for each
kind of ring that collapses, and hold what the builder now does with it:

    a hole                               dropped: the zone becomes solid there
    an outline already inside its zone   dropped: keeping it would cut a hole
    any other outline                    kept, however small or thin

and that ``scripts/check_zone_dataset.py`` passes a dataset that kept its
places and fails one that did not.

They need ``pyshp``, which only the builder uses. Without it they skip.
"""

from __future__ import annotations

import gzip
import importlib.util
import json
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any

import pytest

shapefile = pytest.importorskip("shapefile", reason="pyshp builds the zone dataset")

from custom_components.cap_alerts.providers.nws_zones import ZoneSet  # noqa: E402

_ROOT = Path(__file__).parent.parent
_BUILDER = _ROOT / "scripts" / "build_zone_dataset.py"

Ring = list[tuple[float, float]]


def _load_check() -> Any:
    spec = importlib.util.spec_from_file_location(
        "check_zone_dataset", _ROOT / "scripts" / "check_zone_dataset.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_zone_dataset"] = module
    spec.loader.exec_module(module)
    return module


check = _load_check()


def _outline(west: float, south: float, east: float, north: float) -> Ring:
    """A rectangle wound clockwise, which is how a shapefile winds an outline."""
    return [(west, south), (west, north), (east, north), (east, south), (west, south)]


def _hole(west: float, south: float, east: float, north: float) -> Ring:
    """The same rectangle wound the other way: a hole."""
    return list(reversed(_outline(west, south, east, north)))


# One zone, about 17 km by 22 km, and the small things around and inside it.
# At 40.8 N a thousandth of a degree is 84 m east-west and 111 m north-south.
# The tolerance is 0.005 degrees, about 556 m.
MAINLAND = _outline(-74.100, 40.700, -73.900, 40.900)
ISLET_NEAR = _outline(-73.8970, 40.8000, -73.8958, 40.8009)  # 250 m offshore
ISLET_FAR = _outline(-73.8000, 40.8000, -73.7988, 40.8009)  # 8 km offshore
BARRIER_ISLAND = _outline(
    -73.8000, 40.7500, -73.6800, 40.7530
)  # 10 km long, 330 m wide
SLIVER = _outline(-73.7000, 40.8500, -73.6993, 40.85003)  # 60 m long, 3 m wide
POND = _hole(-74.0020, 40.7990, -73.9990, 40.8020)  # 250 m across, inside the mainland
POND_ISLAND = _outline(-74.0008, 40.8002, -74.0002, 40.8008)  # 50 m, inside the pond
TINY_ZONE = _outline(-73.5000, 40.5000, -73.4988, 40.5009)  # a whole zone, 100 m across

_MIDDLE = {
    "mainland": (40.80, -74.05),
    "islet_near": (40.80045, -73.8964),
    "islet_far": (40.80045, -73.7994),
    "barrier_island": (40.7515, -73.74),
    "sliver": (40.850015, -73.69965),
    "pond": (40.8015, -73.9995),  # in the pond, clear of its island
    "pond_island": (40.8005, -74.0005),
    "tiny_zone": (40.50045, -73.4994),
}


def _zip(
    folder: Path,
    name: str,
    fields: list[str],
    records: list[tuple[tuple[str, ...], list[Ring] | None]],
    *,
    prj: str | None = None,
    points: bool = False,
    extra_shp: bool = False,
) -> Path:
    """Write one zipped shapefile the way NWS ships them."""
    work = folder / name
    work.mkdir(parents=True, exist_ok=True)
    writer = shapefile.Writer(str(work / name))
    for field in fields:
        writer.field(field, "C", 10)
    for values, rings in records:
        if rings is None:
            writer.null()
        elif points:
            writer.point(*rings[0][0])
        else:
            writer.poly([list(ring) for ring in rings])
        writer.record(*values)
    writer.close()
    path = folder / f"{name}.zip"
    with zipfile.ZipFile(path, "w") as archive:
        for extension in ("shp", "shx", "dbf"):
            archive.write(work / f"{name}.{extension}", f"{name}.{extension}")
        if prj is not None:
            archive.writestr(f"{name}.prj", prj)
        if extra_shp:
            archive.write(work / f"{name}.shp", "second.shp")
    return path


_GEOGRAPHIC = 'GEOGCS["GCS_North_American_1983",DATUM["D_North_American_1983"]]'


def _sources(
    folder: Path, forecast: list[Ring], *, more: list[Any] | None = None, **kwargs: Any
) -> list[Path]:
    """A forecast-zone file and a county file covering the same ground.

    Every extra forecast zone gets a county of its own with the same rings,
    as real land does.
    """
    land = [ring for ring in forecast if ring is not POND]
    counties = [(("NY", "36061"), land)]
    for (_state, zone), rings in more or []:
        if rings is not None:
            counties.append((("NY", f"36{zone}"), rings))
    return [
        _zip(
            folder,
            "z_16ap26",
            ["STATE", "ZONE"],
            [(("NY", "072"), forecast), *(more or [])],
            prj=_GEOGRAPHIC,
            **kwargs,
        ),
        _zip(folder, "c_16ap26", ["STATE", "FIPS"], counties, prj=_GEOGRAPHIC),
    ]


def _build(
    out: Path, zips: list[Path], *extra: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(_BUILDER),
            *map(str, zips),
            "--gzip",
            "-o",
            str(out),
            *extra,
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def _built(
    tmp_path: Path, forecast: list[Ring], **kwargs: Any
) -> tuple[ZoneSet, dict[str, Any], list[Path], Path]:
    zips = _sources(tmp_path / "src", forecast, **kwargs)
    out = tmp_path / "zones.jsonl"
    done = _build(out, zips)
    assert done.returncode == 0, done.stderr
    with gzip.open(f"{out}.gz", "rb") as lines:
        zones = ZoneSet.from_lines(lines)
    manifest = json.loads(Path(f"{out}.manifest.json").read_text())
    return zones, manifest, zips, Path(f"{out}.gz")


def _zones_at(zones: ZoneSet, where: str) -> set[str]:
    return {hit.code for hit in zones.containing(*_MIDDLE[where])}


# ---------------------------------------------------------------------------
# What becomes of a ring that collapses
# ---------------------------------------------------------------------------


def test_an_islet_far_from_its_zone_is_kept(tmp_path):
    """8 km offshore: beyond any tolerance band, so only the ring itself saves it."""
    zones, manifest, _zips, _gz = _built(tmp_path, [MAINLAND, ISLET_FAR])
    assert _zones_at(zones, "islet_far") == {"NYZ072", "NYC061"}
    assert manifest["dropped"]["z_16ap26.zip"]["kept_finer"] == 1


def test_a_long_thin_island_is_kept(tmp_path):
    """Ten kilometres long and thinner than the tolerance: the shape that vanished."""
    zones, _manifest, _zips, _gz = _built(tmp_path, [MAINLAND, BARRIER_ISLAND])
    assert _zones_at(zones, "barrier_island") == {"NYZ072", "NYC061"}


def test_an_islet_near_its_zone_is_kept_too(tmp_path):
    zones, _manifest, _zips, _gz = _built(tmp_path, [MAINLAND, ISLET_NEAR])
    assert _zones_at(zones, "islet_near") == {"NYZ072", "NYC061"}


def test_a_sliver_too_thin_for_three_points_is_kept_as_a_box(tmp_path):
    """Three metres wide: no simplification leaves it three distinct points."""
    zones, manifest, _zips, _gz = _built(tmp_path, [MAINLAND, SLIVER])
    assert _zones_at(zones, "sliver") == {"NYZ072", "NYC061"}
    assert manifest["dropped"]["z_16ap26.zip"]["kept_as_box"] == 1


def test_a_pond_too_small_to_keep_becomes_solid_ground(tmp_path):
    """The safe direction: a point in it matches the zone where it did not before."""
    zones, manifest, _zips, _gz = _built(tmp_path, [MAINLAND, POND])
    assert "NYZ072" in _zones_at(zones, "pond")
    assert manifest["dropped"]["z_16ap26.zip"]["holes"] == 1
    assert len(zones.get("forecast", "NYZ072").rings) == 1


def test_an_island_in_a_pond_stays_part_of_its_zone(tmp_path):
    """Containment is even-odd, so keeping the island's outline would cut it out.

    The pond is dropped, the zone is solid there, and the island's place is
    already in it. The island's outline is dropped as well, not kept.
    """
    zones, manifest, _zips, _gz = _built(tmp_path, [MAINLAND, POND, POND_ISLAND])
    assert "NYZ072" in _zones_at(zones, "pond_island")
    assert manifest["dropped"]["z_16ap26.zip"]["already_inside"] == 1
    assert len(zones.get("forecast", "NYZ072").rings) == 1


def test_a_zone_made_only_of_a_tiny_ring_is_in_the_dataset(tmp_path):
    """Six forecast zones and five counties were absent for exactly this reason."""
    zones, manifest, _zips, _gz = _built(
        tmp_path, [MAINLAND], more=[(("NY", "099"), [TINY_ZONE])]
    )
    assert zones.get("forecast", "NYZ099") is not None
    assert (
        "NYZ099" in _zones_at(zones, "tiny_zone")
        or zones.get("forecast", "NYZ099").rings
    )
    assert manifest["dropped"]["z_16ap26.zip"]["codes"] == 2
    assert manifest["dropped"]["z_16ap26.zip"]["codes_absent"] == 0
    assert manifest["dropped"]["z_16ap26.zip"]["records"] == 0


def test_every_collapsing_ring_is_counted_once(tmp_path):
    everything = [
        MAINLAND,
        ISLET_NEAR,
        ISLET_FAR,
        BARRIER_ISLAND,
        SLIVER,
        POND,
        POND_ISLAND,
    ]
    _zones, manifest, _zips, _gz = _built(tmp_path, everything)
    counts = manifest["dropped"]["z_16ap26.zip"]
    assert (
        counts["holes"],
        counts["already_inside"],
        counts["kept_finer"],
        counts["kept_as_box"],
    ) == (1, 1, 3, 1)
    assert counts["rings"] == 0  # nothing with points in it was dropped as empty


def test_the_same_inputs_build_the_same_bytes(tmp_path):
    everything = [MAINLAND, ISLET_FAR, BARRIER_ISLAND, SLIVER, POND, POND_ISLAND]
    zips = _sources(tmp_path / "src", everything)
    for run in ("a", "b"):
        (tmp_path / run).mkdir()
        assert _build(tmp_path / run / "zones.jsonl", zips).returncode == 0
    assert (tmp_path / "a" / "zones.jsonl.gz").read_bytes() == (
        tmp_path / "b" / "zones.jsonl.gz"
    ).read_bytes()


# ---------------------------------------------------------------------------
# A zone the source names must be in the dataset
# ---------------------------------------------------------------------------


def test_a_named_zone_with_no_geometry_stops_the_build(tmp_path):
    """Nothing is written: a dataset missing a zone looks exactly like one that is not."""
    zips = _sources(tmp_path / "src", [MAINLAND], more=[(("NY", "098"), None)])
    out = tmp_path / "zones.jsonl"
    done = _build(out, zips)
    assert done.returncode == 2
    assert "NYZ098" in done.stderr
    assert "nothing written" in done.stderr
    assert not list(tmp_path.glob("zones.jsonl*"))


def test_allow_absent_writes_the_dataset_and_still_says_what_is_missing(tmp_path):
    zips = _sources(tmp_path / "src", [MAINLAND], more=[(("NY", "098"), None)])
    out = tmp_path / "zones.jsonl"
    done = _build(out, zips, "--allow-absent")
    assert done.returncode == 0
    assert "NYZ098" in done.stderr
    manifest = json.loads(Path(f"{out}.manifest.json").read_text())
    assert manifest["dropped"]["z_16ap26.zip"]["codes_absent"] == 1


# ---------------------------------------------------------------------------
# A ring is judged by where it lies, not by how it is wound
# ---------------------------------------------------------------------------


def test_an_island_wound_like_a_hole_is_kept_because_it_is_not_inside_its_zone(
    tmp_path,
):
    """A shapefile winds outlines one way and holes the other. If a source ever
    winds an island the wrong way, it is still an island: it lies outside the
    zone, so it cannot be a hole in it. It is kept, and the build counts it."""
    backwards = list(reversed(ISLET_FAR))
    zones, manifest, _zips, _gz = _built(tmp_path, [MAINLAND, backwards])
    assert _zones_at(zones, "islet_far") == {"NYZ072", "NYC061"}
    assert manifest["dropped"]["z_16ap26.zip"]["hole_wound_kept"] == 1
    assert manifest["dropped"]["z_16ap26.zip"]["holes"] == 0


def test_a_pond_wound_like_an_outline_is_still_dropped(tmp_path):
    """And the other way round: it lies inside the zone, so dropping it is safe."""
    backwards = list(reversed(POND))
    zones, manifest, _zips, _gz = _built(tmp_path, [MAINLAND, backwards])
    assert "NYZ072" in _zones_at(zones, "pond")
    assert manifest["dropped"]["z_16ap26.zip"]["already_inside"] == 1
    assert len(zones.get("forecast", "NYZ072").rings) == 1


# ---------------------------------------------------------------------------
# A source that is not what it should be is refused, not read as if it were
# ---------------------------------------------------------------------------


def _refused(
    tmp_path: Path, zips: list[Path], *extra: str
) -> subprocess.CompletedProcess[str]:
    done = _build(tmp_path / "zones.jsonl", zips, *extra)
    assert done.returncode != 0, "the build should have been refused"
    assert not list(tmp_path.glob("zones.jsonl*")), "nothing should have been written"
    return done


@pytest.mark.parametrize(
    ("state", "zone", "said"),
    [
        ("", "072", "not of the form AAZ999"),
        ("N1", "072", "not of the form AAZ999"),
        ("ny", "072", "not of the form AAZ999"),
        ("NY", "07A", "not of the form AAZ999"),
        ("NY", "0072", "not of the form AAZ999"),
    ],
)
def test_a_record_without_a_usable_zone_code_stops_the_build(
    tmp_path, state, zone, said
):
    zips = _sources(tmp_path / "src", [MAINLAND], more=[((state, zone), [TINY_ZONE])])
    done = _refused(tmp_path, zips)
    assert done.returncode == 2
    assert "record 2" in done.stderr
    assert said in done.stderr
    assert "nothing written" in done.stderr


def test_allow_unusual_source_leaves_the_record_out_and_says_so(tmp_path):
    zips = _sources(tmp_path / "src", [MAINLAND], more=[(("N1", "072"), [TINY_ZONE])])
    out = tmp_path / "zones.jsonl"
    done = _build(out, zips, "--allow-unusual-source")
    assert done.returncode == 0
    assert "1 record(s) could not be used" in done.stderr
    with gzip.open(f"{out}.gz", "rb") as lines:
        zones = ZoneSet.from_lines(lines)  # and the result still loads
    assert zones.get("forecast", "NYZ072") is not None
    assert zones.get("forecast", "N1Z072") is None
    manifest = json.loads(Path(f"{out}.manifest.json").read_text())
    assert manifest["dropped"]["z_16ap26.zip"]["codes"] == 1


@pytest.mark.parametrize(
    ("ring", "said"),
    [
        (
            _outline(500_000.0, 4_500_000.0, 520_000.0, 4_520_000.0),
            "not longitude and latitude",
        ),
        (_outline(-74.1, 40.7, -73.9, 95.0), "not longitude and latitude"),
        (_outline(-179.9, 51.0, 179.9, 52.0), "spans more than 180 degrees"),
    ],
    ids=["metres", "latitude-out-of-range", "across-the-antimeridian"],
)
def test_a_ring_that_cannot_be_an_outline_stops_the_build(tmp_path, ring, said):
    zips = _sources(tmp_path / "src", [MAINLAND], more=[(("NY", "099"), [ring])])
    done = _refused(tmp_path, zips)
    assert done.returncode == 2
    assert said in done.stderr


def test_a_coordinate_a_hair_past_the_edge_of_the_map_is_not_a_fault(tmp_path):
    """NWS's marine files carry longitudes like -180.00000001 on zones that end
    at the antimeridian. They round to the edge, and that is what is stored."""
    edge = _outline(-180.00000001, 51.0, -179.0, 52.0)
    src = tmp_path / "src"
    zips = [
        *_sources(src, [MAINLAND]),
        _zip(src, "oz16ap26", ["ID"], [(("PKZ411",), [edge])], prj=_GEOGRAPHIC),
    ]
    out = tmp_path / "zones.jsonl"
    done = _build(out, zips)
    assert done.returncode == 0, done.stderr
    with gzip.open(f"{out}.gz", "rb") as lines:
        zone = ZoneSet.from_lines(lines).get("marine", "PKZ411")
    assert zone is not None
    assert zone.bbox[0] == -180.0


def test_a_refused_ring_says_what_its_coordinates_were(tmp_path):
    zips = _sources(
        tmp_path / "src",
        [MAINLAND],
        more=[(("NY", "099"), [_outline(-180.3, 51.0, -179.0, 52.0)])],
    )
    done = _refused(tmp_path, zips)
    assert "longitude -180.3 to -179.0, latitude 51.0 to 52.0" in done.stderr


def test_a_zone_named_by_two_files_of_one_layer_stops_the_build(tmp_path):
    """The coastal and offshore marine files share a layer. One code, two shapes."""
    water = _outline(-73.95, 40.40, -73.60, 40.75)
    src = tmp_path / "src"
    zips = [
        *_sources(src, [MAINLAND]),
        _zip(src, "mz16ap26", ["ID"], [(("ANZ338",), [water])], prj=_GEOGRAPHIC),
        _zip(src, "oz16ap26", ["ID"], [(("ANZ338",), [water])], prj=_GEOGRAPHIC),
    ]
    done = _refused(tmp_path, zips)
    assert "names ANZ338, which mz16ap26.zip already named" in done.stderr


def test_a_code_of_the_wrong_kind_for_its_layer_stops_the_build(tmp_path):
    water = _outline(-73.95, 40.40, -73.60, 40.75)
    src = tmp_path / "src"
    zips = [
        *_sources(src, [MAINLAND]),
        _zip(src, "mz16ap26", ["ID"], [(("NYC061",), [water])]),
    ]
    assert "which is not a marine code" in _refused(tmp_path, zips).stderr


def test_a_file_that_is_not_a_zip_is_refused_by_name(tmp_path):
    """What a failed download leaves behind: an error page saved under the
    right name. The build says which file, without a traceback."""
    src = tmp_path / "src"
    zips = _sources(src, [MAINLAND])
    page = src / "fz16ap26.zip"
    page.write_text("<html><body>503 Service Unavailable</body></html>")
    done = _refused(tmp_path, [*zips, page])
    assert "fz16ap26.zip: is not a zip archive" in done.stderr
    assert "Traceback" not in done.stderr


def test_a_file_that_is_missing_is_refused_by_name(tmp_path):
    zips = _sources(tmp_path / "src", [MAINLAND])
    done = _refused(tmp_path, [*zips, tmp_path / "src" / "mz16ap26.zip"])
    assert "mz16ap26.zip: cannot be opened" in done.stderr
    assert "Traceback" not in done.stderr


def test_an_archive_whose_shapefile_is_damaged_is_refused_by_name(tmp_path):
    src = tmp_path / "src"
    zips = _sources(src, [MAINLAND])
    damaged = src / "fz16ap26.zip"
    with zipfile.ZipFile(damaged, "w") as archive:
        for extension in ("shp", "shx", "dbf"):
            archive.writestr(f"fz16ap26.{extension}", b"not a shapefile at all")
    done = _refused(tmp_path, [*zips, damaged])
    assert "fz16ap26.zip: cannot be read as a shapefile" in done.stderr
    assert "Traceback" not in done.stderr


def test_a_projected_shapefile_is_refused(tmp_path):
    projected = 'PROJCS["NAD_1983_Albers",GEOGCS["GCS_North_American_1983"]]'
    src = tmp_path / "src"
    zips = [
        _zip(
            src,
            "z_16ap26",
            ["STATE", "ZONE"],
            [(("NY", "072"), [MAINLAND])],
            prj=projected,
        )
    ]
    assert "projected coordinate system" in _refused(tmp_path, zips).stderr


def test_an_archive_holding_two_shapefiles_is_refused(tmp_path):
    src = tmp_path / "src"
    zips = [
        _zip(
            src,
            "z_16ap26",
            ["STATE", "ZONE"],
            [(("NY", "072"), [MAINLAND])],
            extra_shp=True,
        )
    ]
    assert "expected one .shp inside, found 2" in _refused(tmp_path, zips).stderr


def test_a_shapefile_that_is_not_polygons_is_refused(tmp_path):
    src = tmp_path / "src"
    zips = [
        _zip(
            src,
            "z_16ap26",
            ["STATE", "ZONE"],
            [(("NY", "072"), [MAINLAND])],
            points=True,
        )
    ]
    assert "not polygons" in _refused(tmp_path, zips).stderr


def test_a_shapefile_without_the_field_its_codes_come_from_is_refused(tmp_path):
    """If NWS renames a field, the build stops and says which one it wanted."""
    src = tmp_path / "src"
    zips = [_zip(src, "z_16ap26", ["STATE", "NUMBER"], [(("NY", "072"), [MAINLAND])])]
    assert "has no 'ZONE' field" in _refused(tmp_path, zips).stderr


def test_a_coordinate_that_is_not_a_number_stops_the_build(tmp_path):
    """NaN compares false with everything, so no range test would catch it."""
    nan = float("nan")
    broken = [(-73.5, 40.5), (nan, 40.6), (-73.4, 40.6), (-73.5, 40.5)]
    zips = _sources(tmp_path / "src", [MAINLAND], more=[(("NY", "099"), [broken])])
    done = _refused(tmp_path, zips)
    assert done.returncode == 2
    assert "has a coordinate that is not a number" in done.stderr


def test_bad_text_in_a_field_the_builder_never_reads_does_not_stop_it(tmp_path):
    """Place names can hold bytes that are not valid text. Only codes are read."""
    src = tmp_path / "src"
    work = src / "raw"
    work.mkdir(parents=True)
    writer = shapefile.Writer(str(work / "z_16ap26"), encoding="latin-1")
    for field in ("STATE", "ZONE", "NAME"):
        writer.field(field, "C", 30)
    writer.poly([list(MAINLAND)])
    writer.record("NY", "072", "Do\xf1a Ana / Mayag\xfcez")
    writer.close()
    forecast = src / "z_16ap26.zip"
    with zipfile.ZipFile(forecast, "w") as archive:
        for extension in ("shp", "shx", "dbf"):
            archive.write(work / f"z_16ap26.{extension}", f"z_16ap26.{extension}")
    out = tmp_path / "zones.jsonl"
    done = _build(out, [forecast])
    assert done.returncode == 0, done.stderr
    with gzip.open(f"{out}.gz", "rb") as lines:
        assert ZoneSet.from_lines(lines).get("forecast", "NYZ072") is not None


@pytest.mark.parametrize(
    ("arguments", "said"),
    [
        (["--eps", "nan"], "--eps must be between 0 and 0.1"),
        (["--eps=-1"], "--eps must be between 0 and 0.1"),
        (["--eps", "5"], "--eps must be between 0 and 0.1"),
        (["--tail-eps", "nan"], "--tail-eps must be between 0 and 0.1"),
        (["--tail-eps=-0.02"], "--tail-eps must be between 0 and 0.1"),
        (["--decimals=-3"], "--decimals must be between 0 and 9"),
        (["--decimals", "40"], "--decimals must be between 0 and 9"),
        (["--max-count-change", "nan"], "--max-count-change must be 0 or more"),
        (["--max-count-change=-5"], "--max-count-change must be 0 or more"),
    ],
    ids=lambda v: " ".join(v) if isinstance(v, list) else None,
)
def test_an_argument_the_reader_would_refuse_in_the_file_is_refused_first(
    tmp_path, arguments, said
):
    """Each of these used to build a dataset the integration could not load."""
    done = _refused(tmp_path, _sources(tmp_path / "src", [MAINLAND]), *arguments)
    assert said in done.stderr
    assert "Traceback" not in done.stderr


def test_the_same_file_given_twice_is_refused(tmp_path):
    """Each zone's rings would be added to themselves, and under even-odd
    containment a ring added twice cancels itself out."""
    zips = _sources(tmp_path / "src", [MAINLAND])
    done = _refused(tmp_path, [*zips, zips[0]])
    assert "z_16ap26.zip: is a second forecast file of its kind" in done.stderr


def test_two_files_of_one_kind_are_refused_naming_both(tmp_path):
    src = tmp_path / "src"
    zips = _sources(src, [MAINLAND])
    older = _zip(src, "z_03mr26", ["STATE", "ZONE"], [(("NY", "072"), [MAINLAND])])
    done = _refused(tmp_path, [*zips, older])
    assert "is a second forecast file of its kind" in done.stderr
    assert "z_03mr26.zip" in done.stderr
    assert "z_16ap26.zip" in done.stderr


def test_a_file_whose_name_says_nothing_about_its_layer_is_refused(tmp_path):
    src = tmp_path / "src"
    zips = _sources(src, [MAINLAND])
    odd = _zip(src, "zones", ["STATE", "ZONE"], [(("NY", "072"), [MAINLAND])])
    done = _refused(tmp_path, [*zips, odd])
    assert "cannot tell the layer from the filename" in done.stderr
    assert "Traceback" not in done.stderr


def test_an_output_directory_that_does_not_exist_is_refused(tmp_path):
    zips = _sources(tmp_path / "src", [MAINLAND])
    done = _build(tmp_path / "no" / "such" / "zones.jsonl", zips)
    assert done.returncode != 0
    assert "its directory does not exist" in done.stderr
    assert "Traceback" not in done.stderr
    assert not (tmp_path / "no").exists()


@pytest.mark.parametrize(
    ("content", "said"),
    [
        (b"\x8a\xae not json at all", "cannot be read as a manifest"),
        (b"", "cannot be read as a manifest"),
        (b"[1, 2, 3]", "is not a manifest this script wrote"),
        (b'{"build_id": "abc"}', "is not a manifest this script wrote"),
        (
            b'{"build_id": "abc", "zones": {"forecast": "many"}}',
            "is not a manifest this script wrote",
        ),
    ],
    ids=["bytes", "empty", "a-list", "no-counts", "counts-not-numbers"],
)
def test_an_earlier_manifest_that_cannot_be_read_stops_the_build(
    tmp_path, content, said
):
    """The comparison with the last build is a safety check. A manifest that
    cannot be read must stop the build, not quietly switch the check off."""
    earlier = tmp_path / "earlier.json"
    earlier.write_bytes(content)
    zips = _sources(tmp_path / "src", [MAINLAND])
    out = tmp_path / "out"
    out.mkdir()
    done = _build(out / "zones.jsonl", zips, "--if-changed", str(earlier))
    assert done.returncode != 0
    assert said in done.stderr
    assert "Traceback" not in done.stderr
    assert not list(out.iterdir())


def test_a_dataset_the_reader_would_refuse_is_never_written(tmp_path):
    """Keeping every islet means a zone can hold a great many rings, and the
    reader has a ceiling. The builder loads what it built, the way the
    integration will, before it writes anything."""
    islets = [
        _outline(
            -73.0 + 0.01 * (n % 100),
            39.0 + 0.01 * (n // 100),
            -72.9988 + 0.01 * (n % 100),
            39.0009 + 0.01 * (n // 100),
        )
        for n in range(2_500)
    ]
    zips = _sources(tmp_path / "src", [MAINLAND, *islets])
    done = _refused(tmp_path, zips)
    assert done.returncode == 2
    assert "the integration's reader refuses this dataset" in done.stderr
    assert "nothing written" in done.stderr


def test_a_finished_build_leaves_no_partial_files(tmp_path):
    _zones, _manifest, _zips, dataset = _built(tmp_path, [MAINLAND])
    names = sorted(path.name for path in dataset.parent.iterdir() if path.is_file())
    assert names == ["zones.jsonl", "zones.jsonl.gz", "zones.jsonl.manifest.json"]


# ---------------------------------------------------------------------------
# The check a workflow runs before publishing: ten questions
# ---------------------------------------------------------------------------

_EVERYTHING = [
    MAINLAND,
    ISLET_NEAR,
    ISLET_FAR,
    BARRIER_ISLAND,
    SLIVER,
    POND,
    POND_ISLAND,
]


def _verdict(capsys, dataset: Path, zips: list[Path]) -> tuple[int, dict[str, str]]:
    """Run the check; return its exit code and each question's PASS or FAIL."""
    code = check.main([str(dataset), *map(str, zips)])
    lines = capsys.readouterr().out.splitlines()
    verdicts = {
        line[6:30].strip(): line[:4]
        for line in lines
        if line.startswith(("PASS  ", "FAIL  "))
    }
    return code, verdicts


def _failed(verdicts: dict[str, str]) -> set[str]:
    return {
        question.split(".")[0]
        for question, verdict in verdicts.items()
        if verdict == "FAIL"
    }


def test_the_check_passes_a_build_that_kept_every_place(tmp_path, capsys):
    _zones, _manifest, zips, dataset = _built(
        tmp_path, _EVERYTHING, more=[(("NY", "099"), [TINY_ZONE])]
    )
    code, verdicts = _verdict(capsys, dataset, zips)
    assert code == 0
    assert len(verdicts) == 10
    assert _failed(verdicts) == set()
    results = check.survey(str(dataset), [str(path) for path in zips])
    assert all(result.ok for result in results)
    assert results[0].groups["lost"] == results[0].groups["no zone"] == 0
    assert results[0].wound_as_hole == 1
    assert results[0].sampled > 0


def test_the_check_fails_a_file_that_does_not_load(tmp_path, capsys):
    broken = tmp_path / "zones.jsonl.gz"
    broken.write_bytes(gzip.compress(b'{"format": 2, "zones": 1, "lines": 3}\n'))
    code, verdicts = _verdict(capsys, broken, _sources(tmp_path / "src", [MAINLAND]))
    assert code == 1
    assert _failed(verdicts) == {"1"}


def test_the_check_fails_a_dataset_with_no_county_layer(tmp_path, capsys):
    """Without counties every land point reads as at sea."""
    forecast_only = _sources(tmp_path / "src", [MAINLAND])[:1]
    out = tmp_path / "zones.jsonl"
    assert _build(out, forecast_only).returncode == 0
    code, verdicts = _verdict(capsys, Path(f"{out}.gz"), forecast_only)
    assert code == 1
    assert "2" in _failed(verdicts)


def test_the_check_fails_a_dataset_that_records_no_tolerance(tmp_path, capsys):
    """No tolerance in the file means no band, and every sliver is a gap."""
    zips = _sources(tmp_path / "src", [MAINLAND])
    out = tmp_path / "zones.jsonl"
    assert _build(out, zips, "--eps", "0").returncode == 0
    code, verdicts = _verdict(capsys, Path(f"{out}.gz"), zips)
    assert code == 1
    assert "3" in _failed(verdicts)


def test_the_check_fails_a_dataset_too_close_to_the_readers_ceilings(
    tmp_path, capsys, monkeypatch
):
    _zones, _manifest, zips, dataset = _built(tmp_path, [MAINLAND])
    monkeypatch.setattr(check, "HEADROOM", 0.0)
    code, verdicts = _verdict(capsys, dataset, zips)
    assert code == 1
    assert _failed(verdicts) == {"4"}


def test_the_check_fails_a_manifest_that_describes_another_file(tmp_path, capsys):
    _zones, manifest, zips, dataset = _built(tmp_path, [MAINLAND])
    manifest["gzip_sha256"] = "0" * 64
    Path(str(dataset)[:-3] + ".manifest.json").write_text(json.dumps(manifest))
    code, verdicts = _verdict(capsys, dataset, zips)
    assert code == 1
    assert _failed(verdicts) == {"5"}


def test_the_check_fails_a_dataset_that_is_missing_a_zone(tmp_path, capsys):
    _zones, _manifest, _zips, dataset = _built(tmp_path / "without", [MAINLAND])
    with_zone = _sources(
        tmp_path / "with", [MAINLAND], more=[(("NY", "099"), [TINY_ZONE])]
    )
    code, verdicts = _verdict(capsys, dataset, with_zone)
    assert code == 1
    assert "6" in _failed(verdicts)


def test_the_check_fails_a_dataset_that_lost_a_place(tmp_path, capsys):
    """A dataset built without the islands, checked against a source that has them.

    That is what the first builder produced: their rings collapsed and were
    dropped, and the places matched nothing.
    """
    _zones, _manifest, _zips, dataset = _built(tmp_path / "without", [MAINLAND])
    with_islands = _sources(tmp_path / "with", [MAINLAND, ISLET_FAR, BARRIER_ISLAND])
    code, verdicts = _verdict(capsys, dataset, with_islands)
    assert code == 1
    assert "7" in _failed(verdicts)
    lost = check.survey(str(dataset), [str(with_islands[0])])[0]
    assert lost.groups["lost"] == 2
    assert lost.lost_zones == {"NYZ072"}


def test_the_check_fails_an_island_dropped_because_it_was_wound_like_a_hole(
    tmp_path, capsys
):
    """The check judges by where a ring lies, so a builder that trusted the
    winding and dropped this island as a hole would be caught."""
    _zones, _manifest, _zips, dataset = _built(tmp_path / "without", [MAINLAND])
    backwards = _sources(tmp_path / "with", [MAINLAND, list(reversed(ISLET_FAR))])
    code, verdicts = _verdict(capsys, dataset, backwards)
    assert code == 1
    assert "7" in _failed(verdicts)


def test_the_check_fails_a_dataset_whose_outline_strayed_from_its_source(
    tmp_path, capsys
):
    """Built from an outline 5 km east of the one it is checked against."""
    shifted = _outline(-74.040, 40.700, -73.840, 40.900)
    _zones, _manifest, _zips, dataset = _built(tmp_path / "shifted", [shifted])
    code, verdicts = _verdict(capsys, dataset, _sources(tmp_path / "true", [MAINLAND]))
    assert code == 1
    assert "8" in _failed(verdicts)


def test_the_check_fails_a_zone_built_at_a_fraction_of_its_area(tmp_path, capsys):
    quarter = _outline(-74.100, 40.700, -74.000, 40.800)
    _zones, _manifest, _zips, dataset = _built(tmp_path / "quarter", [quarter])
    code, verdicts = _verdict(capsys, dataset, _sources(tmp_path / "true", [MAINLAND]))
    assert code == 1
    assert "9" in _failed(verdicts)


def test_the_check_fails_land_that_has_no_county(tmp_path, capsys):
    """A forecast zone whose ground no county covers would read as open sea."""
    src = tmp_path / "src"
    zips = [
        _zip(
            src,
            "z_16ap26",
            ["STATE", "ZONE"],
            [(("NY", "072"), [MAINLAND]), (("NY", "099"), [TINY_ZONE])],
        ),
        _zip(src, "c_16ap26", ["STATE", "FIPS"], [(("NY", "36061"), [MAINLAND])]),
    ]
    out = tmp_path / "zones.jsonl"
    assert _build(out, zips).returncode == 0
    code, verdicts = _verdict(capsys, Path(f"{out}.gz"), zips)
    assert code == 1
    assert _failed(verdicts) == {"10"}


def test_the_check_reports_a_dataset_cut_short_without_a_traceback(tmp_path, capsys):
    """A gzip file cut short raises EOFError, which is not an OSError."""
    _zones, _manifest, zips, dataset = _built(tmp_path, [MAINLAND])
    whole = dataset.read_bytes()
    dataset.write_bytes(whole[: len(whole) // 2])
    code, verdicts = _verdict(capsys, dataset, zips)
    assert code == 1
    assert _failed(verdicts) == {"1"}


def test_the_check_reports_a_damaged_dataset_without_a_traceback(tmp_path, capsys):
    """And a damaged one raises zlib.error, which is neither."""
    _zones, _manifest, zips, dataset = _built(tmp_path, [MAINLAND])
    damaged = bytearray(dataset.read_bytes())
    for position in range(20, len(damaged) - 8, 7):
        damaged[position] ^= 0xFF
    dataset.write_bytes(bytes(damaged))
    code, verdicts = _verdict(capsys, dataset, zips)
    assert code == 1
    assert _failed(verdicts) == {"1"}


@pytest.mark.parametrize("content", [b"\x8a\xae not json", b"", b"[1, 2]", b"{}"])
def test_the_check_fails_a_manifest_it_cannot_read(tmp_path, capsys, content):
    _zones, _manifest, zips, dataset = _built(tmp_path, [MAINLAND])
    Path(str(dataset)[:-3] + ".manifest.json").write_bytes(content)
    code, verdicts = _verdict(capsys, dataset, zips)
    assert code == 1
    assert _failed(verdicts) == {"5"}


def test_the_check_fails_a_source_it_cannot_read_by_name(tmp_path, capsys):
    _zones, _manifest, zips, dataset = _built(tmp_path, [MAINLAND])
    zips[1].write_text("<html>503 Service Unavailable</html>")
    code = check.main([str(dataset), *map(str, zips)])
    printed = capsys.readouterr().out
    assert code == 1
    assert "FAIL  sources readable" in printed
    assert "c_16ap26.zip: is not a zip archive" in printed


def test_the_check_copes_with_a_part_that_has_no_points(tmp_path, monkeypatch):
    """A damaged shapefile can hold a part with no points, or with one. The
    shapefile writer will not make one, so the reader is stood in for."""
    zones, _manifest, zips, _dataset = _built(tmp_path, [MAINLAND])
    _resolver, builder = check._modules()

    def odd_parts(path):
        yield "forecast", "NYZ072", [list(MAINLAND), [], [(-73.5, 40.5)]]

    monkeypatch.setattr(builder, "read_zip", odd_parts)
    result = check.survey_file(zones, str(zips[0]))
    assert result.rings == 3
    assert result.groups["lost"] == 1  # the lone point lies far from the zone
    assert result.groups["no zone"] == 0


def test_the_check_run_with_nothing_to_check_explains_itself(capsys):
    assert check.main([]) == 2
    assert "check_zone_dataset.py nws_zones.jsonl.gz" in capsys.readouterr().out
