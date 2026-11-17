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
 3. **Does it record its tolerance?** Banded matching reads it from the file.
    Without it there is no band and every sliver between zones is a gap.
 4. **Is there headroom?** The reader refuses a file over its ceilings (line
    length, size, zones, vertices, rings in a zone). A dataset more than
    halfway to any of them fails here, while there is still time to act.
 5. **Does the manifest agree?** If one sits beside the dataset, its hash and
    zone count must be the file's.
 6. **Is every zone there?** Each zone code a shapefile names, on its layer.
 7. **Is every place still in its zone?** Each ring that collapses at the
    stated tolerance is sorted by where its place ended up: ``inside`` the
    zone as built, within its tolerance ``band``, ``lost`` beyond it, or in
    ``no zone``. Judged by geometry alone; how a ring is wound only labels it.
 8. **Did simplification stay within its tolerance?** A sample of the
    original vertices of every ring, each of which must be inside its zone as
    built or within the zone's tolerance of it. This is the promise the band
    rests on, checked directly.
 9. **Are the areas plausible?** A zone of any size whose built area is under
    half or over twice its original has been mangled, whatever else passed.
10. **Does every piece of land have a county?** The land-sea rule calls a
    point land only if a county is within tolerance. A forecast or fire zone
    outline with none would read as open sea and lose its land products.

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
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
DECIMALS = 4  # the builder's default rounding
GROUPS = ("inside", "band", "lost", "no zone")
BUCKETS = (
    (0.1, "under 100 m"),
    (0.5, "100 to 500 m"),
    (2.0, "0.5 to 2 km"),
    (1e9, "over 2 km"),
)
# Every ring's first vertex, and every SAMPLE_EVERY-th after it.
SAMPLE_EVERY = 25
# A dataset this far toward one of the reader's ceilings is a warning to act on.
HEADROOM = 0.5
# A zone fewer than this many tolerances across is too small for an area
# ratio to mean anything; its rings are covered by question 7.
AREA_FLOOR_TOLERANCES = 10.0
LAND_LAYERS = ("forecast", "fire")


def _load(name: str, path: Path) -> ModuleType:
    """Load one file as a module, without importing the package around it.

    The resolver is stdlib only, but the integration's package imports Home
    Assistant, which a machine that only builds datasets need not have.
    """
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:  # pragma: no cover - a broken checkout
        raise SystemExit(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _modules() -> tuple[ModuleType, ModuleType]:
    """(the resolver, the builder), loaded by path."""
    resolver = sys.modules.get("_cap_alerts_nws_zones") or _load(
        "_cap_alerts_nws_zones",
        _ROOT / "custom_components" / "cap_alerts" / "providers" / "nws_zones.py",
    )
    builder = sys.modules.get("_cap_alerts_build_zone_dataset") or _load(
        "_cap_alerts_build_zone_dataset", _ROOT / "scripts" / "build_zone_dataset.py"
    )
    return resolver, builder


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
    except (OSError, ValueError) as err:
        findings.append(Finding("1. loads", False, f"{dataset} does not load: {err}"))
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
            zones.max_tolerance_km > 0.0,
            f"{zones.epsilon_deg} deg, {zones.tail_epsilon_deg} deg for "
            f"{sorted(zones.tail_states)}; rounding {zones.rounding_deg} deg",
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
        manifest = json.loads(manifest_path.read_text())
        digest = hashlib.sha256(Path(dataset).read_bytes()).hexdigest()
        wrong = []
        if manifest.get("gzip_sha256") != digest:
            wrong.append(
                f"hash is {digest}, manifest says {manifest.get('gzip_sha256')}"
            )
        if manifest.get("zone_count") != len(zones):
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
    """Questions 6 to 10 for one shapefile."""
    resolver, builder = _modules()
    eps, tail_eps, tail = zones.epsilon_deg, zones.tail_epsilon_deg, zones.tail_states
    result = FileSurvey(os.path.basename(path), builder.layer_of(path))
    started = time.perf_counter()
    original_area: collections.Counter[str] = collections.Counter()
    for _layer, code, rings in builder.read_zip(path):
        result.records += 1
        if builder.code_problem(result.layer, code):
            continue
        result.named.add(code)
        zone = zones.get(result.layer, code)
        if zone is None:
            result.absent.add(code)
        ring_eps = tail_eps if (tail_eps and code[:2] in tail) else eps
        areas = [builder.ring_area(ring) for ring in rings]
        outline = max(areas, key=abs, default=0.0)
        original_area[code] += sum(areas)
        tolerance = zones.tolerance_km(code)
        for ring, area in zip(rings, areas, strict=True):
            result.rings += 1
            points = list(ring)
            if zone is not None:
                # Question 8: a sample of this ring's own vertices.
                for lon, lat in points[:1] + points[SAMPLE_EVERY::SAMPLE_EVERY]:
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
                    result.stray.append((away, code, round(lat, 3), round(lon, 3)))
            if builder.simplify_ring(ring, ring_eps, DECIMALS):
                continue
            # Question 7: a ring that collapses. Where did its place end up?
            result.wound_as_hole += area * outline < 0.0
            lat, lon, size = _middle_and_size(points, resolver.KM_PER_DEGREE)
            if zone is None:
                result.groups["no zone"] += 1
            elif resolver.point_in_rings(lat, lon, zone.rings):
                result.groups["inside"] += 1
            else:
                away = resolver.distance_to_rings_km(
                    lat, lon, zone.rings, tolerance + 50.0
                )
                if away <= tolerance:
                    result.groups["band"] += 1
                else:
                    result.groups["lost"] += 1
                    result.lost_zones.add(code)
                    label = next(label for limit, label in BUCKETS if size < limit)
                    result.lost_sizes[label] += 1
                    result.lost_areas[code[:2]] += 1
                    result.worst.append(
                        (size, away, code, round(lat, 2), round(lon, 2))
                    )

    counties = "county" in zones.layers()
    for code in sorted(result.named - result.absent):
        zone = zones.get(result.layer, code)
        # Question 9: built area against original, for zones big enough to judge.
        across = (
            AREA_FLOOR_TOLERANCES * zones.tolerance_km(code) / resolver.KM_PER_DEGREE
        )
        before = abs(original_area[code])
        if before > across * across > 0.0:
            ratio = abs(sum(_flat_area(ring) for ring in zone.rings)) / before
            if not 0.5 <= ratio <= 2.0:
                result.mangled.append((round(ratio, 3), code))
        # Question 10: every outline of a land zone has a county within tolerance.
        if counties and result.layer in LAND_LAYERS:
            for ring in zone.rings:
                lon, lat = ring[0], ring[1]
                if not zones.resolve(
                    lat,
                    lon,
                    radius_km=zones.max_tolerance_km,
                    max_zones=1,
                    layers=("county",),
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
        results = [survey_file(zones, path) for path in zips]
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
