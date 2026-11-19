#!/usr/bin/env python3
"""Check a built zone dataset against the shapefiles it was built from.

    check_zone_dataset.py nws_zones.jsonl.gz z_*.zip c_*.zip fz*.zip mz*.zip oz*.zip

``build_zone_dataset.py`` simplifies every outline NWS publishes, and a
simplified outline can be wrong in ways that neither the builder nor the
reader can see from the inside. The first builder left eleven zones out of the
dataset and about 4,100 places on each land layer out of their zones; the file
loaded, the parity check passed, and nothing said so, because nothing compared
the dataset with what it was made from. This script is that comparison. It is
the step to run between building a dataset and publishing it.

It takes nothing the builder says on trust: it reads the shapefiles again and
the dataset as the integration would, and asks ten questions.

 1. **Does it load?** Through the reader the integration uses.
 2. **Are the layers there?** One for every kind of shapefile given, and the
    county layer, which the land-sea rule cannot work without.
 3. **Does it record its tolerance and its rounding?** Banded matching reads
    the tolerance from the file; without it there is no band and every sliver
    between zones is a gap. The rounding decides which rings collapse, so it
    is read from the file too and never assumed.
 4. **Is there headroom?** The reader refuses a file over its ceilings (line
    length, size, zones, vertices, rings in a zone). A dataset more than
    halfway to any of them fails here, while there is still time to act.
 5. **Does the manifest agree?** If one sits beside the dataset, its hash and
    zone count must be the file's.
 6. **Is every zone there?** Each zone code a shapefile names, on its layer.
 7. **Is every place still in its zone?** Each ring that collapses at the
    stated tolerance is sorted by where its place ended up: ``inside`` the
    zone as built, within its tolerance ``band``, ``lost`` beyond it, or in
    ``no zone``. A ring is ``lost`` if its middle or any one of its vertices
    is beyond the band. Judged by geometry alone; how a ring is wound only
    labels it. ``inside`` and ``band`` are reported separately, because
    within the band is not the same as inside: see the note on overlapping
    outlines below.
 8. **Did simplification stay within its tolerance?** Original vertices,
    each of which must be inside its zone as built or within the zone's
    tolerance of it: every vertex of a ring that collapses, and a sample of
    the vertices of a ring that survives. This is the promise the band rests
    on, checked directly.
 9. **Are the areas plausible?** A zone of any size whose built area is under
    half or over twice its original has been mangled, whatever else passed.
10. **Does every piece of land have a county?** The land-sea rule calls a
    point land only if a county is within tolerance. A forecast or fire zone
    outline with no county within that zone's own tolerance would read as
    open sea and lose its land products.

It also reports, without judging, how each collapsing ring lies against the
rest of its zone as that rest survives simplification: wholly inside it,
overlapping it, or separate from it. An outline that overlaps the rest of its
own zone is kept, and the dataset stores a zone's rings as one even-odd set,
so the area the two share is counted in neither. ``containing()`` does not
find the zone there; the band does. The report gives the number of such
outlines and how many have their middle in the shared area.

It exits 0 when all ten pass and 1 otherwise, having said which and why.
It reads the shapefiles and the dataset; it writes nothing and contacts
nothing. Needs ``pyshp``, as the builder does. Run it from anywhere.

The same checks run under pytest in ``tests/test_zone_dataset_acceptance.py``
when ``CAP_ALERTS_ZONE_SOURCES`` names a directory holding the shapefiles.
"""

from __future__ import annotations

import collections
import gzip
import hashlib
import importlib.util
import json
import math
import os
import sys
import time
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
GROUPS = ("inside", "band", "lost", "no zone")
# How a ring that collapses lies against the rest of its zone, as that rest
# survives simplification.
RELATIONS = ("wholly inside", "overlapping", "separate")
BUCKETS = (
    (0.1, "under 100 m"),
    (0.5, "100 to 500 m"),
    (2.0, "0.5 to 2 km"),
    (1e9, "over 2 km"),
)
# Of a ring that survives simplification: its first vertex and every
# SAMPLE_EVERY-th after it. Of a ring that collapses: every vertex.
SAMPLE_EVERY = 25
# A dataset this far toward one of the reader's ceilings is a warning to act on.
HEADROOM = 0.5
# A zone fewer than this many tolerances across is too small for an area
# ratio to mean anything; its rings are covered by question 7.
AREA_FLOOR_TOLERANCES = 10.0
LAND_LAYERS = ("forecast", "fire")


BUILDER_MODULE = "build_zone_dataset"


def _modules() -> tuple[ModuleType, ModuleType]:
    """(the reader, the builder).

    The builder is a script and is loaded from its file. The reader is an
    integration module, and the builder decides which copy to use: the one
    the integration has already loaded if there is one, so that a process
    never holds two, and otherwise the file itself, so that a machine without
    Home Assistant can still build and check a dataset.
    """
    builder = sys.modules.get(BUILDER_MODULE)
    if builder is None:
        path = _ROOT / "scripts" / "build_zone_dataset.py"
        spec = importlib.util.spec_from_file_location(BUILDER_MODULE, path)
        if spec is None or spec.loader is None:  # pragma: no cover - a broken checkout
            raise SystemExit(f"cannot load {path}")
        builder = importlib.util.module_from_spec(spec)
        sys.modules[BUILDER_MODULE] = builder
        spec.loader.exec_module(builder)
    return builder.load_reader(), builder


def _boxed(rings: list) -> list[tuple[float, float, float, float, list]]:
    """Each ring with its bounding box, so a point outside the box skips the ring."""
    boxed = []
    for ring in rings:
        xs = [p[0] for p in ring]
        ys = [p[1] for p in ring]
        boxed.append((min(xs), max(xs), min(ys), max(ys), ring))
    return boxed


def _in_rest(x: float, y: float, boxed: list, point_in_rings: Any) -> bool:
    """Even-odd containment of a point in the rest of a zone."""
    near = [
        ring
        for west, east, south, north, ring in boxed
        if west <= x <= east and south <= y <= north
    ]
    return bool(near) and point_in_rings(x, y, near)


def decimals_of(zones: Any) -> int | None:
    """The rounding the dataset says its coordinates were given, in decimal places.

    Which rings collapse depends on it, so it is read from the dataset and
    not assumed. ``None`` if the dataset does not record it.
    """
    step = zones.rounding_deg
    return round(-math.log10(step)) if step > 0.0 else None


@dataclass
class Finding:
    """The answer to one of the ten questions."""

    question: str
    ok: bool
    detail: str


@dataclass
class FileSurvey:
    """What one shapefile's rings came to in the dataset."""

    name: str
    layer: str
    records: int = 0
    rings: int = 0
    groups: collections.Counter[str] = field(default_factory=collections.Counter)
    relation: collections.Counter[str] = field(default_factory=collections.Counter)
    overlapping_outlines: int = 0
    overlapping_outlines_cancelled: int = 0
    wound_as_hole: int = 0
    named: set[str] = field(default_factory=set)
    absent: set[str] = field(default_factory=set)
    lost_zones: set[str] = field(default_factory=set)
    lost_sizes: collections.Counter[str] = field(default_factory=collections.Counter)
    lost_areas: collections.Counter[str] = field(default_factory=collections.Counter)
    worst: list[tuple[float, float, str, float, float]] = field(default_factory=list)
    sampled: int = 0
    stray: list[tuple[float, str, float, float]] = field(default_factory=list)
    mangled: list[tuple[float, str]] = field(default_factory=list)
    landless: list[tuple[str, float, float]] = field(default_factory=list)
    seconds: float = 0.0

    @property
    def collapsing(self) -> int:
        return sum(self.groups.values())

    @property
    def ok(self) -> bool:
        return not (
            self.absent
            or self.groups["lost"]
            or self.groups["no zone"]
            or self.stray
            or self.mangled
            or self.landless
        )


def _middle_and_size(ring: list, km_per_degree: float) -> tuple[float, float, float]:
    """(lat, lon) of the ring's bounding-box centre and its longest side in km."""
    lons = [p[0] for p in ring]
    lats = [p[1] for p in ring]
    lat = (min(lats) + max(lats)) / 2.0
    lon = (min(lons) + max(lons)) / 2.0
    wide = (max(lons) - min(lons)) * km_per_degree * math.cos(math.radians(lat))
    tall = (max(lats) - min(lats)) * km_per_degree
    return lat, lon, max(wide, tall)


def _flat_area(ring: Any) -> float:
    """Signed shoelace area of a ring stored flat as lon, lat, lon, lat, ..."""
    n = len(ring) // 2
    total = 0.0
    for i in range(n):
        j = (i + 1) % n
        total += ring[2 * i] * ring[2 * j + 1] - ring[2 * j] * ring[2 * i + 1]
    return total / 2.0


def _opener(path: str) -> Any:
    return gzip.open if str(path).endswith(".gz") else open


def load_dataset(path: str) -> Any:
    """The dataset as the integration would load it; raises if it would not."""
    resolver, _builder = _modules()
    with _opener(path)(path, "rb") as lines:
        return resolver.ZoneSet.from_lines(lines)


def measure(path: str) -> dict[str, int]:
    """The dataset's own sizes, to set against the reader's ceilings."""
    sizes = {
        "longest line": 0,
        "bytes": 0,
        "zones": 0,
        "vertices": 0,
        "most rings in a zone": 0,
    }
    with _opener(path)(path, "rb") as lines:
        for number, line in enumerate(lines):
            sizes["longest line"] = max(sizes["longest line"], len(line))
            sizes["bytes"] += len(line)
            if number == 0 or not line.strip():
                continue
            record = json.loads(line)
            if record.get("eof"):
                continue
            sizes["zones"] += 1
            rings = record.get("rings") or []
            sizes["most rings in a zone"] = max(
                sizes["most rings in a zone"], len(rings)
            )
            sizes["vertices"] += sum(len(ring) for ring in rings)
    return sizes


def check_file(findings: list[Finding], dataset: str, zips: list[str]) -> Any | None:
    """Questions 1 to 5, about the dataset on its own. Returns it, or ``None``."""
    resolver, builder = _modules()
    try:
        zones = load_dataset(dataset)
    except (OSError, ValueError, EOFError, zlib.error) as err:
        # A gzip file cut short raises EOFError, and a damaged one zlib.error;
        # neither is an OSError or a ValueError.
        reason = str(err) or type(err).__name__
        findings.append(
            Finding("1. loads", False, f"{dataset} does not load: {reason}")
        )
        return None
    findings.append(Finding("1. loads", True, f"{len(zones)} zones"))

    wanted = {builder.layer_of(path) for path in zips} | {"county"}
    missing = sorted(wanted - zones.layers())
    findings.append(
        Finding(
            "2. layers",
            not missing,
            f"missing {missing}" if missing else f"{sorted(zones.layers())}",
        )
    )

    findings.append(
        Finding(
            "3. tolerance recorded",
            zones.max_tolerance_km > 0.0 and decimals_of(zones) is not None,
            f"{zones.epsilon_deg} deg, {zones.tail_epsilon_deg} deg for "
            f"{sorted(zones.tail_states)}; rounding "
            + (
                f"{decimals_of(zones)} decimal places"
                if decimals_of(zones) is not None
                else "NOT RECORDED, so which rings collapse cannot be known"
            ),
        )
    )

    sizes = measure(dataset)
    ceilings = {
        "longest line": resolver.MAX_LINE_BYTES,
        "bytes": resolver.MAX_DATASET_BYTES,
        "zones": resolver.MAX_ZONES,
        "vertices": resolver.MAX_VERTICES,
        "most rings in a zone": resolver.MAX_RINGS_PER_ZONE,
    }
    tight = [name for name, size in sizes.items() if size > HEADROOM * ceilings[name]]
    shown = "; ".join(
        f"{name} {size} of {ceilings[name]} ({100 * size / ceilings[name]:.0f}%)"
        for name, size in sizes.items()
    )
    findings.append(
        Finding(
            "4. headroom", not tight, shown + (f"; OVER HALF: {tight}" if tight else "")
        )
    )

    plain = str(dataset).removesuffix(".gz")
    manifest_path = Path(plain + ".manifest.json")
    if manifest_path.exists() and str(dataset).endswith(".gz"):
        digest = hashlib.sha256(Path(dataset).read_bytes()).hexdigest()
        wrong = []
        manifest: Any = None
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as err:
            wrong.append(f"{manifest_path.name} cannot be read: {err}")
        else:
            if not isinstance(manifest, dict):
                wrong.append(f"{manifest_path.name} is not a manifest")
            elif manifest.get("gzip_sha256") != digest:
                wrong.append(
                    f"hash is {digest}, manifest says {manifest.get('gzip_sha256')}"
                )
            elif manifest.get("zone_count") != len(zones):
                wrong.append(
                    f"{len(zones)} zones, manifest says {manifest.get('zone_count')}"
                )
        detail = "; ".join(wrong) or f"sha256 {digest[:16]}..., {len(zones)} zones"
        findings.append(Finding("5. manifest agrees", not wrong, detail))
    else:
        findings.append(
            Finding(
                "5. manifest agrees",
                True,
                "no manifest beside the dataset; not checked",
            )
        )
    return zones


def survey_file(zones: Any, path: str) -> FileSurvey:
    """Questions 6 to 10 for one shapefile.

    The shapefile is read twice. The first reading finds which rings collapse
    and collects, for each zone, the rings that survive simplification on
    their own: the rest of the zone that a collapsing ring is judged against.
    The second reading judges every ring against the dataset.
    """
    resolver, builder = _modules()
    eps, tail_eps, tail = zones.epsilon_deg, zones.tail_epsilon_deg, zones.tail_states
    # The dataset's own rounding decides which rings collapse. Without it the
    # builder's default is used, and question 3 has already failed.
    decimals = decimals_of(zones)
    if decimals is None:
        decimals = 4
    result = FileSurvey(os.path.basename(path), builder.layer_of(path))
    started = time.perf_counter()

    solid: dict[str, list] = collections.defaultdict(list)
    collapses: list[bool] = []
    for _layer, code, rings in builder.read_zip(path):
        if builder.code_problem(result.layer, code):
            continue
        ring_eps = tail_eps if (tail_eps and code[:2] in tail) else eps
        for ring in rings:
            kept = builder.simplify_ring(ring, ring_eps, decimals)
            collapses.append(not kept)
            if kept:
                solid[code].append(kept)

    original_area: collections.Counter[str] = collections.Counter()
    boxes: dict[str, list] = {}
    ring_number = -1
    for _layer, code, rings in builder.read_zip(path):
        result.records += 1
        if builder.code_problem(result.layer, code):
            continue
        result.named.add(code)
        zone = zones.get(result.layer, code)
        if zone is None:
            result.absent.add(code)
        areas = [builder.ring_area(ring) for ring in rings]
        outline = max(areas, key=abs, default=0.0)
        original_area[code] += sum(areas)
        tolerance = zones.tolerance_km(code)
        for ring, area in zip(rings, areas, strict=True):
            ring_number += 1
            collapsing = collapses[ring_number]
            result.rings += 1
            points = list(ring)
            if not points:
                continue  # a part with no points covers no place
            # Question 8. Every vertex of a ring that collapses, since the
            # ring as a whole is at stake; a sample of one that survives.
            beyond = 0.0
            if zone is not None:
                chosen = (
                    points
                    if collapsing
                    else points[:1] + points[SAMPLE_EVERY::SAMPLE_EVERY]
                )
                for lon, lat in chosen:
                    result.sampled += 1
                    near = resolver.distance_to_rings_km(
                        lat, lon, zone.rings, tolerance
                    )
                    if near <= tolerance or resolver.point_in_rings(
                        lat, lon, zone.rings
                    ):
                        continue
                    away = resolver.distance_to_rings_km(
                        lat, lon, zone.rings, tolerance + 50.0
                    )
                    beyond = max(beyond, away)
                    result.stray.append((away, code, round(lat, 3), round(lon, 3)))
            if not collapsing:
                continue
            # Question 7: a ring that collapses. Where did its place end up?
            hole_wound = area * outline < 0.0
            result.wound_as_hole += hole_wound
            lat, lon, size = _middle_and_size(points, resolver.KM_PER_DEGREE)
            rest = boxes.get(code)
            if rest is None:
                rest = boxes[code] = _boxed(solid[code])
            middle_in_rest = _in_rest(lon, lat, rest, builder.point_in_rings)
            covered = [_in_rest(x, y, rest, builder.point_in_rings) for x, y in points]
            if all(covered):
                result.relation["wholly inside"] += 1
            elif any(covered):
                result.relation["overlapping"] += 1
                if not hole_wound:
                    # An outline lying partly over the rest of its own zone.
                    # Both are kept, and under even-odd containment the part
                    # they share belongs to neither.
                    result.overlapping_outlines += 1
                    result.overlapping_outlines_cancelled += middle_in_rest
            else:
                result.relation["separate"] += 1
            if zone is None:
                result.groups["no zone"] += 1
                continue
            away = beyond
            if not away and not resolver.point_in_rings(lat, lon, zone.rings):
                away = resolver.distance_to_rings_km(
                    lat, lon, zone.rings, tolerance + 50.0
                )
                away = away if away > tolerance else 0.0
                if not away:
                    result.groups["band"] += 1
                    continue
            if not away:
                result.groups["inside"] += 1
                continue
            # The middle, or one of the ring's own vertices, is farther from
            # the zone than the zone's tolerance.
            result.groups["lost"] += 1
            result.lost_zones.add(code)
            label = next(label for limit, label in BUCKETS if size < limit)
            result.lost_sizes[label] += 1
            result.lost_areas[code[:2]] += 1
            result.worst.append((size, away, code, round(lat, 2), round(lon, 2)))

    counties = "county" in zones.layers()
    for code in sorted(result.named - result.absent):
        zone = zones.get(result.layer, code)
        tolerance = zones.tolerance_km(code)
        # Question 9: built area against original, for zones big enough to judge.
        across = AREA_FLOOR_TOLERANCES * tolerance / resolver.KM_PER_DEGREE
        before = abs(original_area[code])
        if before > across * across > 0.0:
            ratio = abs(sum(_flat_area(ring) for ring in zone.rings)) / before
            if not 0.5 <= ratio <= 2.0:
                result.mangled.append((round(ratio, 3), code))
        # Question 10: every outline of a land zone has a county within the
        # zone's own tolerance, which is the distance the land-sea rule allows.
        if counties and result.layer in LAND_LAYERS:
            for ring in zone.rings:
                lon, lat = ring[0], ring[1]
                if not zones.resolve(
                    lat, lon, radius_km=tolerance, max_zones=1, layers=("county",)
                ):
                    result.landless.append((code, round(lat, 2), round(lon, 2)))
                    break
    result.seconds = time.perf_counter() - started
    return result


def survey(dataset: str, zips: list[str]) -> list[FileSurvey]:
    """Load ``dataset`` and survey each shapefile against it."""
    zones = load_dataset(dataset)
    return [survey_file(zones, path) for path in zips]


def judge(results: list[FileSurvey]) -> list[Finding]:
    """Questions 6 to 10 as findings, over every file."""

    def each(say: Any) -> str:
        return "; ".join(f"{r.name}: {say(r)}" for r in results if say(r))

    absent = each(
        lambda r: (
            f"{len(r.absent)} absent ({' '.join(sorted(r.absent)[:10])})"
            if r.absent
            else ""
        )
    )
    lost = each(
        lambda r: (
            f"{r.groups['lost']} lost, {r.groups['no zone']} in no zone"
            if r.groups["lost"] or r.groups["no zone"]
            else ""
        )
    )
    stray = each(
        lambda r: (
            f"{len(r.stray)} of {r.sampled} sampled vertices beyond tolerance, "
            f"worst {sorted(r.stray, reverse=True)[:3]}"
            if r.stray
            else ""
        )
    )
    mangled = each(
        lambda r: (
            f"{len(r.mangled)} zone(s), worst {sorted(r.mangled)[:5]}"
            if r.mangled
            else ""
        )
    )
    landless = each(
        lambda r: (
            f"{len(r.landless)} zone(s), such as {r.landless[:5]}" if r.landless else ""
        )
    )
    named = sum(len(r.named) for r in results)
    collapsing = sum(r.collapsing for r in results)
    sampled = sum(r.sampled for r in results)
    return [
        Finding(
            "6. every zone present",
            not absent,
            absent or f"{named} named zones, all present",
        ),
        Finding(
            "7. every place kept",
            not lost,
            lost or f"{collapsing} collapsing rings, none lost",
        ),
        Finding(
            "8. within tolerance",
            not stray,
            stray or f"{sampled} original vertices sampled, all within tolerance",
        ),
        Finding(
            "9. areas plausible",
            not mangled,
            mangled or "no zone under half or over twice its original area",
        ),
        Finding(
            "10. land has a county",
            not landless,
            landless or "every forecast and fire outline has a county within tolerance",
        ),
    ]


def report(results: list[FileSurvey]) -> None:
    for result in results:
        print(
            f"\n{result.name} ({result.layer}): {result.records} records, "
            f"{result.rings} rings, {result.collapsing} collapse at the stated "
            f"tolerance  [{result.seconds:.0f}s]"
        )
        present = len(result.named) - len(result.absent)
        print(f"    named zones present: {present} of {len(result.named)}")
        for group in GROUPS:
            share = (
                100.0 * result.groups[group] / result.collapsing
                if result.collapsing
                else 0.0
            )
            print(f"    {group:<8} {result.groups[group]:>6}  {share:5.1f}%")
        print(f"    of those, wound as holes: {result.wound_as_hole}")
        print(
            "    against the rest of their zone: "
            + ", ".join(f"{name} {result.relation[name]}" for name in RELATIONS)
        )
        print(
            f"    outlines overlapping the rest of their zone: "
            f"{result.overlapping_outlines}, of which the middle lies in the "
            f"overlap: {result.overlapping_outlines_cancelled}"
        )
        print(
            f"    original vertices sampled: {result.sampled}, "
            f"beyond tolerance: {len(result.stray)}"
        )
        if result.absent:
            shown = " ".join(sorted(result.absent)[:20])
            print(f"    ABSENT from the dataset ({len(result.absent)}): {shown}")
        if result.groups["lost"]:
            print(f"    LOST rings are in {len(result.lost_zones)} zones")
            sizes = ", ".join(
                f"{label} {result.lost_sizes[label]}" for _l, label in BUCKETS
            )
            print(f"    lost, by size: {sizes}")
            areas = ", ".join(f"{a} {n}" for a, n in result.lost_areas.most_common(10))
            print(f"    lost, by area: {areas}")
            print(
                "    largest lost rings (size, distance from the built zone, zone, lat, lon):"
            )
            for size, away, code, lat, lon in sorted(result.worst, reverse=True)[:8]:
                far = f"{away:.1f} km" if away < 1e6 else "over 50 km"
                print(
                    f"        {size * 1000:>6.0f} m   {far:>10}   {code}   {lat}, {lon}"
                )
        if result.stray:
            print("    STRAY vertices (distance from the built zone, zone, lat, lon):")
            for away, code, lat, lon in sorted(result.stray, reverse=True)[:8]:
                far = f"{away:.2f} km" if away < 1e6 else "over 50 km"
                print(f"        {far:>10}   {code}   {lat}, {lon}")
        if result.mangled:
            shown = ", ".join(
                f"{ratio} {code}" for ratio, code in sorted(result.mangled)[:10]
            )
            print(f"    MANGLED zones (built area over original, zone): {shown}")
        if result.landless:
            shown = ", ".join(f"{c} {la},{lo}" for c, la, lo in result.landless[:10])
            print(f"    LAND WITH NO COUNTY (zone, lat, lon): {shown}")


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) < 2 or args[0] in ("-h", "--help"):
        print(__doc__)
        return 2
    dataset, zips = args[0], args[1:]
    findings: list[Finding] = []
    zones = check_file(findings, dataset, zips)
    if zones is not None:
        try:
            results = [survey_file(zones, path) for path in zips]
        except SystemExit as err:
            # The builder's own refusal of a source it cannot read, by name.
            findings.append(Finding("sources readable", False, str(err)))
        else:
            report(results)
            findings.extend(judge(results))
    print()
    for finding in findings:
        print(
            f"{'PASS' if finding.ok else 'FAIL'}  {finding.question:<24} {finding.detail}"
        )
    failed = [finding.question for finding in findings if not finding.ok]
    if failed:
        print(
            f"\nFAIL: {len(failed)} of {len(findings)} checks failed: {', '.join(failed)}"
        )
        return 1
    print(f"\nOK: all {len(findings)} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
