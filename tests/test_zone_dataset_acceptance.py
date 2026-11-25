"""Acceptance tests for a zone dataset built from NWS's real shapefiles.

These are the tests a dataset has to pass before it is published. They need
the shapefiles, which are not in the repository, so they run only when told
where the shapefiles are:

    CAP_ALERTS_ZONE_SOURCES=/path/to/shapefiles pytest tests/test_zone_dataset_acceptance.py

The directory holds NWS's zipped shapefiles under the names NWS gives them
(``z_*.zip`` forecast zones, ``c_*.zip`` counties, ``fz*.zip`` fire zones,
``mz*.zip`` and ``oz*.zip`` marine zones). A workflow that downloads a new
release and builds a dataset from it sets the variable and runs this file
between building and publishing. Everywhere else the tests skip.

They build the dataset here, with the settings a release is built with, and
then ask it the ten questions of ``scripts/check_zone_dataset.py``, one test
each, so that a failure names the question it failed. The questions come from
what was learned the hard way on the April 2026 release, where the first
builder left out eleven zones and about 4,100 places on each land layer and
nothing reported it. Beyond those ten: all four layers are present, no record
was left out, the builder's own counts add up, and building twice gives the
same bytes.

Three tests put the same file through the integration's own reader,
``providers/nws_zone_data.py``, which is the route a Home Assistant install
takes: that it loads from disk within the reader's ceilings, whether its hash
is on the reader's list, and that no damaged copy of it is taken for anything
but a refusal.

The last two are about the local mode (#316) and are here because only the
real zones can answer them: that a request calculated from an apparent centre
names every zone a position needs, and that the tracker's minimum hold shows
nothing but the position's own alerts where zones are smaller than the hold.

Expect several minutes: the build runs twice, and the survey reads every ring
of every shapefile.
"""

from __future__ import annotations

import gzip
import hashlib
import importlib.util
import json
import os
import random
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from custom_components.cap_alerts.providers import nws_zone_data
from custom_components.cap_alerts.providers.nws_matching import (
    matching_zones,
    nearby_zones,
    place_alerts,
    request_codes,
    split_codes,
)
from custom_components.cap_alerts.providers.nws_offset import (
    TRACKER_MIN_HOLD_KM,
    LockStore,
    displaced,
)
from custom_components.cap_alerts.providers.nws_zones import (
    MAX_DATASET_BYTES,
    ZoneSet,
    haversine_km,
)

_SOURCES = os.environ.get("CAP_ALERTS_ZONE_SOURCES", "")
# The settings a release is built with. Alaska's zones are simplified more
# coarsely; see AGENTS.md.
_BUILD_ARGS = shlex.split(
    os.environ.get("CAP_ALERTS_ZONE_BUILD_ARGS", "--tail-eps 0.02")
)

pytestmark = pytest.mark.skipif(
    not _SOURCES,
    reason="set CAP_ALERTS_ZONE_SOURCES to a directory holding NWS's zone shapefiles",
)

_ROOT = Path(__file__).parent.parent
_PATTERNS = ("z_*.zip", "c_*.zip", "fz*.zip", "mz*.zip", "oz*.zip")


def _load_check() -> Any:
    spec = importlib.util.spec_from_file_location(
        "check_zone_dataset", _ROOT / "scripts" / "check_zone_dataset.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_zone_dataset"] = module
    spec.loader.exec_module(module)
    return module


def _build(out: Path, zips: list[Path]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(_ROOT / "scripts" / "build_zone_dataset.py"),
            *map(str, zips),
            *_BUILD_ARGS,
            "--gzip",
            "-o",
            str(out),
        ],
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture(scope="module")
def zips() -> list[Path]:
    """The shapefiles of one release: one of each kind, and no others."""
    pytest.importorskip("shapefile", reason="pyshp builds the zone dataset")
    folder = Path(_SOURCES)
    found: list[Path] = []
    for pattern in _PATTERNS:
        matches = sorted(folder.glob(pattern))
        if len(matches) > 1:
            pytest.fail(
                f"{folder} holds more than one {pattern}: "
                f"{', '.join(path.name for path in matches)}. A release is built "
                "from one of each kind; point CAP_ALERTS_ZONE_SOURCES at a "
                "directory holding only that release's files.",
                pytrace=False,
            )
        found.extend(matches)
    kinds = {path.name[:2] for path in found}
    if not {"z_", "c_"} <= kinds:
        pytest.fail(
            f"{folder} needs at least a forecast-zone file (z_*.zip) and a county "
            f"file (c_*.zip); found {[path.name for path in found] or 'nothing'}",
            pytrace=False,
        )
    return found


@pytest.fixture(scope="module")
def build(zips, tmp_path_factory) -> dict[str, Any]:
    out = tmp_path_factory.mktemp("zone-dataset") / "nws_zones.jsonl"
    done = _build(out, zips)
    if done.returncode != 0:
        pytest.fail(f"the build failed:\n{done.stderr[-2000:]}", pytrace=False)
    return {
        "dataset": Path(f"{out}.gz"),
        "manifest": json.loads(Path(f"{out}.manifest.json").read_text()),
        "log": done.stderr,
    }


QUESTIONS = (
    "1. loads",
    "2. layers",
    "3. tolerance recorded",
    "4. headroom",
    "5. manifest agrees",
    "6. every zone present",
    "7. every place kept",
    "8. within tolerance",
    "9. areas plausible",
    "10. land has a county",
)


@pytest.fixture(scope="module")
def checked(build, zips) -> dict[str, Any]:
    """Everything ``scripts/check_zone_dataset.py`` finds, asked once."""
    check = _load_check()
    paths = [str(path) for path in zips]
    findings: list[Any] = []
    zones = check.check_file(findings, str(build["dataset"]), paths)
    assert zones is not None, findings[0].detail
    results = [check.survey_file(zones, path) for path in paths]
    check.report(results)
    findings.extend(check.judge(results))
    return {
        "findings": {found.question: found for found in findings},
        "results": results,
    }


@pytest.mark.parametrize("question", QUESTIONS)
def test_the_dataset_passes_the_check(checked, question):
    """One test for each of the ten questions, so a failure names its question.

    What each asks, and why, is at the top of ``scripts/check_zone_dataset.py``.
    """
    found = checked["findings"][question]
    assert found.ok, f"{question}: {found.detail}"


def test_the_check_asked_every_question(checked):
    assert tuple(checked["findings"]) == QUESTIONS


def test_it_holds_all_four_layers(build):
    """A release is built from all five shapefiles. A missing one is a failed download."""
    with gzip.open(build["dataset"], "rb") as lines:
        assert ZoneSet.from_lines(lines).layers() == {
            "forecast",
            "county",
            "fire",
            "marine",
        }
    assert build["manifest"]["content_bytes"] < MAX_DATASET_BYTES


def test_no_record_was_left_out(build):
    assert "could not be used" not in build["log"], build["log"]
    for name, counts in build["manifest"]["dropped"].items():
        assert counts["records"] == 0, (
            f"{name}: {counts['records']} record(s) left nothing"
        )
        assert counts["rings"] == 0, f"{name}: {counts['rings']} empty ring(s)"
        assert counts["codes_absent"] == 0, name


def test_the_builder_accounts_for_every_ring_that_collapses(build, checked):
    """What the builder says it did with them adds up to what collapses."""
    for result in checked["results"]:
        counts = build["manifest"]["dropped"][result.name]
        handled = (
            counts["holes"]
            + counts["already_inside"]
            + counts["kept_finer"]
            + counts["kept_as_box"]
            + counts["rings"]
        )
        assert handled == result.collapsing, result.name
        # And the check, reading the shapefiles for itself, sorts them the
        # same way: what lies wholly inside the rest of its zone is what the
        # builder dropped, and everything else is what it kept.
        dropped = counts["holes"] + counts["already_inside"]
        kept = counts["kept_finer"] + counts["kept_as_box"]
        assert dropped == result.relation["wholly inside"], result.name
        assert kept == result.relation["overlapping"] + result.relation["separate"], (
            result.name
        )


def test_building_it_again_gives_the_same_bytes(build, zips, tmp_path):
    again = tmp_path / "nws_zones.jsonl"
    assert _build(again, zips).returncode == 0
    assert Path(f"{again}.gz").read_bytes() == build["dataset"].read_bytes()
    assert (
        json.loads(Path(f"{again}.manifest.json").read_text())["build_id"]
        == (build["manifest"]["build_id"])
    )


# ---------------------------------------------------------------------------
# The integration's reader, on the real dataset
# ---------------------------------------------------------------------------


def test_the_reader_loads_the_built_dataset_from_disk(build, monkeypatch):
    """The route an install takes: the file on disk, through every check the
    reader makes, under the reader's own ceilings."""
    stored = build["dataset"].read_bytes()
    digest = hashlib.sha256(stored).hexdigest()
    assert digest == build["manifest"]["gzip_sha256"]
    monkeypatch.setitem(nws_zone_data.KNOWN_ZONE_DATA, digest, "this build")

    zones, loaded = nws_zone_data.read_verified(build["dataset"])

    assert loaded == digest
    assert len(zones) == build["manifest"]["zone_count"]
    assert set(zones.layers()) == {"county", "fire", "forecast", "marine"}
    # Headroom: the file is under half of what the reader will read.
    assert len(stored) * 2 <= nws_zone_data.MAX_ZONE_DATA_BYTES


def test_the_built_dataset_is_on_the_readers_list(build):
    """The reader loads only a file whose hash it lists.

    For shapefiles of a release that is already listed, this proves that the
    listed hash is what those shapefiles build, on this machine. For a new
    release the hash cannot be listed yet, so the test skips and says which
    hash to add when the dataset is published.
    """
    digest = build["manifest"]["gzip_sha256"]
    if digest not in nws_zone_data.KNOWN_ZONE_DATA:
        pytest.skip(
            f"this build is not on the reader's list; add {digest} to "
            "KNOWN_ZONE_DATA in providers/nws_zone_data.py when it is published"
        )
    zones, loaded = nws_zone_data.verify_zone_data(build["dataset"].read_bytes())
    assert loaded == digest
    assert len(zones) == build["manifest"]["zone_count"]


class _EveryHash(dict[str, str]):
    """A list that claims to hold every hash, to reach the checks behind it."""

    def __contains__(self, key: object) -> bool:
        return True

    def __getitem__(self, key: str) -> str:
        return "any"


def _damage(data: bytes, rng: random.Random) -> bytes:
    damaged = bytearray(data)
    kind = rng.choice(("flip", "cut", "zero", "insert", "repeat", "swap"))
    at = rng.randrange(len(damaged))
    if kind == "flip":
        for _ in range(rng.choice((1, 2, 8))):
            damaged[rng.randrange(len(damaged))] ^= 1 << rng.randrange(8)
    elif kind == "cut":
        del damaged[at:]
    elif kind == "zero":
        run = rng.choice((1, 8, 64, 4096))
        damaged[at : at + run] = bytes(len(damaged[at : at + run]))
    elif kind == "insert":
        damaged[at:at] = rng.randbytes(rng.choice((1, 16, 4096)))
    elif kind == "repeat":
        damaged += damaged[at:]
    else:
        other = rng.randrange(len(damaged))
        damaged[at], damaged[other] = damaged[other], damaged[at]
    return bytes(damaged)


def test_no_damaged_copy_of_the_built_dataset_is_taken_for_anything_but_a_refusal(
    build, monkeypatch
):
    """Damage the real file in 200 ways and give each copy to the reader.

    First with the reader's real list: a copy that differs by one bit has a
    different hash and must be refused as unrecognised, before anything is
    decompressed. Then with a list that accepts every hash, which is the case
    of a wrong entry on the list: every copy must load or be refused as
    damaged, and nothing else may come out. 160 copies are damaged in the
    compressed bytes and 40 in the text before it is compressed again.
    """
    stored = build["dataset"].read_bytes()
    text = gzip.decompress(stored)
    rng = random.Random(316)
    copies = [_damage(stored, rng) for _ in range(160)]
    copies += [gzip.compress(_damage(text, rng), mtime=0) for _ in range(40)]
    copies += [b"", b"\x00", rng.randbytes(64), rng.randbytes(1_000_000)]

    for copy in copies:
        if copy == stored:
            continue
        with pytest.raises(nws_zone_data.ZoneDataUnavailableError) as refused:
            nws_zone_data.verify_zone_data(copy)
        assert refused.value.reason in (
            nws_zone_data.REASON_UNRECOGNISED,
            nws_zone_data.REASON_TOO_LARGE,
        )

    monkeypatch.setattr(nws_zone_data, "KNOWN_ZONE_DATA", _EveryHash())
    outcomes = {"loaded": 0, "damaged": 0, "too_large": 0}
    for copy in copies:
        try:
            nws_zone_data.verify_zone_data(copy)
        except nws_zone_data.ZoneDataUnavailableError as refused:
            assert refused.reason in (
                nws_zone_data.REASON_DAMAGED,
                nws_zone_data.REASON_TOO_LARGE,
            ), refused.reason
            outcomes[refused.reason] += 1
        else:
            outcomes["loaded"] += 1
    # Most damage must be caught, or the test is not damaging anything.
    assert outcomes["damaged"] >= len(copies) // 2, outcomes


# ---------------------------------------------------------------------------
# The request and the placing of alerts, on the real zones (#316)
# ---------------------------------------------------------------------------

# Public places: three cities, the Front Range, the Nevada desert, Anchorage,
# Miami, Honolulu, and the Aleutians at the antimeridian.
_PLACES = (
    (40.7128, -74.0060),
    (35.7800, -78.6400),
    (41.8800, -87.6300),
    (39.7400, -104.9900),
    (39.5000, -116.8000),
    (61.2200, -149.9000),
    (25.7600, -80.1900),
    (21.3000, -157.8500),
    (52.0000, 179.5000),
)


@pytest.fixture(scope="module")
def real_zones(build) -> ZoneSet:
    with gzip.open(build["dataset"], "rb") as lines:
        return ZoneSet.from_lines(lines)


def _needed(zones: ZoneSet, lat: float, lon: float, radius: float) -> set[str]:
    """The codes that decide a position's alerts: within the radius, or
    counting for the position through the tolerance band."""
    near = {code for _layer, code in nearby_zones(zones, lat, lon, radius)}
    return near | {code for _layer, code in matching_zones(zones, lat, lon)}


def test_a_request_from_an_offset_centre_names_every_zone_the_position_needs(
    real_zones,
):
    """The request is calculated around a centre 24 km or more from the
    position, by a function that is never told the position. Wherever the
    position is within its record's hold, every zone that decides its alerts
    must be in the list. This is the property the whole local mode rests on,
    on the real zones: the resolver measures in a flat projection, and it is
    the margin in ``request_codes`` that keeps that from leaving a zone out."""
    rnd = random.Random(316)
    checked = 0
    for radius, pct in (
        (0.0, 50.0),
        (16.0, 50.0),
        (100.0, 50.0),
        (161.0, 15.0),
        (161.0, 50.0),
    ):
        for place in _PLACES:
            store = LockStore(rng=random.Random(rnd.random()))
            lock = store.lock_for(
                *place,
                radius_km=radius,
                hysteresis_pct=pct,
                today=1,
                min_hold_km=TRACKER_MIN_HOLD_KM,
            )
            codes = request_codes(
                real_zones, *lock.centre, lock.request_radius_km(radius)
            )
            asked = set(codes)
            parts = split_codes(codes)
            assert [code for part in parts for code in part] == codes
            for _ in range(4):
                lat, lon = displaced(
                    *place, rnd.uniform(0.0, lock.hold_km), rnd.uniform(0, 360)
                )
                missing = _needed(real_zones, lat, lon, radius) - asked
                assert not missing, (
                    f"{sorted(missing)} needed at {lat:.4f},{lon:.4f} with radius "
                    f"{radius:g} km, and not in the request made from "
                    f"{lock.centre[0]:.4f},{lock.centre[1]:.4f}"
                )
                checked += 1
    assert checked == 180


def test_at_the_smallest_zones_a_small_radius_shows_the_position_s_alerts_and_no_others(
    build, real_zones
):
    """Someone who wants their own zone only sets a radius of a kilometre or
    none. The tracker's minimum hold is 8 km, longer than some zones are
    wide. The hold must not change what is shown: with one alert for every
    zone asked about, those placed are exactly the alerts of the zones the
    true position needs, none farther than the radius, and those placed here
    are exactly the zones that count for it."""
    with gzip.open(build["dataset"], "rt", encoding="utf-8") as lines:
        # Not every line is a shape: the header, the closing line, and the
        # fire zones recorded as the same shape as a forecast zone.
        records = [json.loads(line) for line in lines]
    boxes = [record["bbox"] for record in records if "bbox" in record]
    assert len(boxes) > 1000
    across = sorted(
        (haversine_km(south, west, north, east), (south + north) / 2, (west + east) / 2)
        for west, south, east, north in boxes
        if east - west < 180
    )
    assert across[0][0] < TRACKER_MIN_HOLD_KM  # some zones are smaller than the hold
    rnd = random.Random(8)
    layers = {
        "forecast": "forecast",
        "marine": "forecast",
        "county": "county",
        "fire": "fire",
    }
    checked = 0
    for radius in (0.0, 1.0, 3.0):
        for _size, lat0, lon0 in across[:25]:
            store = LockStore(rng=random.Random(rnd.random()))
            lock = store.lock_for(
                lat0,
                lon0,
                radius_km=radius,
                hysteresis_pct=1.0,
                today=1,
                min_hold_km=TRACKER_MIN_HOLD_KM,
            )
            assert lock.hold_km == TRACKER_MIN_HOLD_KM
            reach = lock.request_radius_km(radius)
            hits = real_zones.resolve(
                *lock.centre, radius_km=reach, max_zones=len(real_zones)
            )
            alerts = [
                {
                    "geometry": None,
                    "properties": {
                        "id": f"{hit.layer}:{hit.code}",
                        "affectedZones": [
                            f"https://api.weather.gov/zones/{layers[hit.layer]}/{hit.code}"
                        ],
                    },
                }
                for hit in hits
            ]
            for _ in range(3):
                lat, lon = displaced(
                    lat0, lon0, rnd.uniform(0.0, 8.0), rnd.uniform(0, 360)
                )
                held = store.lock_for(
                    lat,
                    lon,
                    radius_km=radius,
                    hysteresis_pct=1.0,
                    today=1,
                    min_hold_km=TRACKER_MIN_HOLD_KM,
                )
                assert held == lock
                counted = matching_zones(real_zones, lat, lon)
                near = nearby_zones(real_zones, lat, lon, radius)
                placed = place_alerts(real_zones, alerts, lat, lon, radius)
                got = {where.feature["properties"]["id"] for where in placed}
                assert got == {f"{layer}:{code}" for layer, code in counted | set(near)}
                assert {
                    where.feature["properties"]["id"] for where in placed if where.here
                } == {f"{layer}:{code}" for layer, code in counted}
                for where in placed:
                    assert where.distance_km is not None
                    assert where.distance_km <= radius
                checked += 1
    assert checked == 225
