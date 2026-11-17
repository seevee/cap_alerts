#!/usr/bin/env python3
"""Build the simplified NWS zone dataset read by ``providers/nws_zones.py``.

    build_zone_dataset.py z_16ap26.zip c_16ap26.zip fz16ap26.zip [mz16ap26.zip oz16ap26.zip] -o nws_zones.jsonl

Reads NWS's public zone, county (and optionally marine, fire) shapefiles,
simplifies each ring with Douglas-Peucker, and writes one JSON object per line:

    {"format": 2, "zones": 11634, "lines": 11636,
     "meta": {"build_id": "...", "source_date": "2026-04-16", "epsilon_deg": 0.005, "decimals": 4,
              "tail_epsilon_deg": 0.02, "tail_states": ["AK"],
              "sources": [{"file": "z_16ap26.zip", "layer": "forecast", "bytes": ..., "sha256": "...",
                           "md5": "...", "date": "2026-04-16", "records": 4157, "zones": 4074}, ...]}}
    {"code": "NYZ072", "layer": "forecast", "bbox": [lon_min, lat_min, lon_max, lat_max],
     "rings": [[[lon, lat], ...], ...]}
    ...one line per zone...
    {"eof": true, "zones": 11634, "lines": 11636, "sha256": "<of every byte above this line>"}

Codes follow the alerts API's UGC form:
    forecast        : STATE + "Z" + ZONE        NY + Z + 072  -> NYZ072
    fire            : STATE + "Z" + ZONE        CO + Z + 240  -> COZ240   (same code form; Red Flag Warnings issue against these)
    county          : STATE + "C" + FIPS[2:]    NY + C + 061  -> NYC061   (FIPS 36061)
    marine          : ID as given               ANZ338

Several shapefile records can share one code (split counties, multi-part
zones); they are merged into one zone with several rings.

Only dependency: pyshp (``pip install pyshp`` / ``apt install python3-pyshp``).
Meant for CI or a maintainer's machine; the integration ships the result and
never needs pyshp at runtime.

The same inputs always give the same bytes. Nothing in the output comes from
the clock or from the machine: the zips are processed in a fixed order whatever
order they are named in, ``source_date`` is the newest timestamp stored *inside*
the zips, and the gzip header is written out byte for byte. ``build_id`` is a hash of every
input's SHA-256 together with the build parameters, so it changes exactly when
the output would. A small manifest (``<out>.manifest.json``) is written beside
the dataset with each source's hashes, the parameters, the zone counts and the
SHA-256 of the uncompressed dataset; it is the readable record of a build.

``--if-changed <manifest>`` hashes the inputs, compares the resulting
``build_id`` with the one in an earlier manifest, and stops before reading a
single shapefile when they match: no change at NWS, no build. When they
differ it builds, then compares the zone count of each layer with that
manifest and refuses to write anything if one moved more than
``--max-count-change`` percent (default 5): that is a source file that went
missing or stopped parsing, far more often than NWS redrawing the map.

With ``--gzip`` the manifest also records the size and SHA-256 of the ``.gz``
itself, since that is the file that gets published.

The NWS release pages list each file's MD5, so that is recorded too, for
checking a download against the page. SHA-256 is what identifies a source here.

Measured on the 16 April 2026 release, four layers, epsilon 0.005 deg (0.02 for
Alaska): 11,634 zones, 550 k vertices, 11.6 MB JSON, 3.1 MB gzipped.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import io
import json
import math
import os
import re
import struct
import sys
import tempfile
import zipfile
import zlib
from pathlib import Path

try:
    import shapefile  # pyshp
except ImportError:
    sys.exit("pyshp is required:  pip install pyshp   (or apt install python3-pyshp)")


_READER = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "cap_alerts"
    / "providers"
    / "nws_zones.py"
)
# The reader's ceilings on a tolerance and on rounding, restated so that an
# argument outside them is refused before any work is done.
MAX_EPSILON_DEG = 0.1
MAX_DECIMALS = 9


def reader_refusal(blob: bytes) -> str | None:
    """Why the integration's reader would refuse ``blob``, or ``None`` if it loads.

    The reader is the judge of what a dataset is. It enforces ceilings and
    field rules the builder does not restate, so the only sure way to know a
    build is usable is to load it the way the integration will. Loaded by
    file path: the module is stdlib only, and the package around it imports
    Home Assistant, which a machine that only builds datasets need not have.
    """
    reader = sys.modules.get("_cap_alerts_nws_zones")
    if reader is None:
        spec = importlib.util.spec_from_file_location("_cap_alerts_nws_zones", _READER)
        if spec is None or spec.loader is None:
            return f"the reader could not be loaded from {_READER}"
        reader = importlib.util.module_from_spec(spec)
        sys.modules["_cap_alerts_nws_zones"] = reader
        try:
            spec.loader.exec_module(reader)
        except OSError as err:
            del sys.modules["_cap_alerts_nws_zones"]
            return f"the reader could not be loaded from {_READER}: {err}"
    try:
        reader.ZoneSet.from_lines(io.BytesIO(blob))
    except ValueError as err:
        return str(err)
    return None


def write_atomically(path: str, data: bytes) -> None:
    """Write ``data`` to ``path`` so that ``path`` is never half written.

    Written beside its destination, flushed to disk, read back and compared,
    and only then renamed into place. A build that is killed or runs out of
    disk leaves the old file, or none, never a short one under the real name.
    """
    partial = f"{path}.partial"
    try:
        with open(partial, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        with open(partial, "rb") as f:
            if f.read() != data:
                raise SystemExit(
                    f"{path}: what was written does not read back the same"
                )
        os.replace(partial, path)
    except OSError as err:
        raise SystemExit(f"{path}: cannot be written: {err}") from err
    finally:
        if os.path.exists(partial):
            os.unlink(partial)


def layer_of(filename: str) -> str:
    n = os.path.basename(filename).lower()
    if n.startswith(("mz", "oz", "hz")):
        return "marine"  # coastal, offshore and high-seas files, one layer
    if n.startswith("fz"):
        return "fire"
    if n.startswith("c_"):
        return "county"
    if n.startswith("z_"):
        return "forecast"
    raise SystemExit(f"cannot tell the layer from the filename: {filename}")


def code_of(layer: str, rec: dict) -> str | None:
    f = {k.upper(): v for k, v in rec.items()}
    if layer in ("forecast", "fire"):
        return f"{f['STATE']}Z{str(f['ZONE']).zfill(3)}"
    if layer == "county":
        return f"{f['STATE']}C{str(f['FIPS']).zfill(5)[2:5]}"
    if layer == "marine":
        return str(f["ID"])
    return None


def douglas_peucker(
    points: list[tuple[float, float]], eps: float
) -> list[tuple[float, float]]:
    """Iterative Douglas-Peucker in degrees; keeps both endpoints."""
    if len(points) < 3:
        return points
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        a, b = stack.pop()
        ax, ay = points[a]
        bx, by = points[b]
        dx, dy = bx - ax, by - ay
        length2 = dx * dx + dy * dy
        best, bi = -1.0, -1
        for i in range(a + 1, b):
            px, py = points[i]
            if length2 == 0.0:
                d = ((px - ax) ** 2 + (py - ay) ** 2) ** 0.5
            else:
                t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length2))
                d = ((px - ax - t * dx) ** 2 + (py - ay - t * dy) ** 2) ** 0.5
            if d > best:
                best, bi = d, i
        if best > eps and bi > 0:
            keep[bi] = True
            stack.append((a, bi))
            stack.append((bi, b))
    return [p for p, k in zip(points, keep) if k]


def simplify_ring(ring, eps: float, decimals: int) -> list[list[float]] | None:
    pts = [tuple(p) for p in ring]
    if len(pts) > 1 and pts[0] == pts[-1]:
        pts = pts[:-1]  # shapefiles repeat the first vertex
    if len(pts) < 3:
        return None
    pts = douglas_peucker(pts + [pts[0]], eps)[:-1]
    pts = [(round(x, decimals), round(y, decimals)) for x, y in pts]
    out = [pts[0]]
    for p in pts[1:]:
        if p != out[-1]:
            out.append(p)
    if len(out) > 1 and out[0] == out[-1]:
        out.pop()
    return [list(p) for p in out] if len(out) >= 3 else None


# How many times the tolerance is halved for an outline that collapses. Twelve
# halvings of 0.005 degrees is about 14 cm, far below the rounding, so a ring
# that still collapses has fewer than three distinct points and is kept as
# its bounding box instead.
MAX_REFINEMENTS = 12


def ring_area(ring) -> float:
    """Signed shoelace area in square degrees; the sign is the winding direction."""
    points = list(ring)
    return (
        sum(
            x0 * y1 - x1 * y0
            for (x0, y0), (x1, y1) in zip(points, points[1:] + points[:1])
        )
        / 2.0
    )


def refine_ring(ring, eps: float, decimals: int) -> list[list[float]] | None:
    """Simplify an outline that collapsed, at the coarsest tolerance it survives.

    A ring collapses when every vertex lies within the tolerance of the line
    between its two farthest points. That is true of a small island, and it is
    equally true of a long thin one: a barrier island 12 km long and under a
    kilometre wide vanished whole. The place stays on the map either way, so
    it has to stay in the dataset, or a point on it matches no zone. Halving
    the tolerance until three points survive keeps the outline as coarse as it
    can be while still existing.
    """
    finer = eps
    for _ in range(MAX_REFINEMENTS):
        finer /= 2.0
        kept = simplify_ring(ring, finer, decimals)
        if kept:
            return kept
    return None


def box_ring(ring, decimals: int) -> list[list[float]] | None:
    """The extent of a ring too thin to survive rounding, as a four-point box.

    A sliver narrower than one rounding step has fewer than three distinct
    points however finely it is simplified. Its extent, padded by one step on
    each side, keeps the place in the dataset. The box is at most a step
    (about 11 m) larger than the sliver all round, which is the safe
    direction: a little too much zone, never none.
    """
    points = list(ring)
    if not points:
        return None
    step = 10.0**-decimals
    west = max(-180.0, round(min(p[0] for p in points) - step, decimals))
    east = min(180.0, round(max(p[0] for p in points) + step, decimals))
    south = max(-90.0, round(min(p[1] for p in points) - step, decimals))
    north = min(90.0, round(max(p[1] for p in points) + step, decimals))
    return [[west, south], [east, south], [east, north], [west, north]]


def point_in_rings(x: float, y: float, rings) -> bool:
    """Even-odd containment, the rule the resolver applies to the built dataset."""
    inside = False
    for ring in rings:
        j = len(ring) - 1
        for i in range(len(ring)):
            xi, yi = ring[i]
            xj, yj = ring[j]
            if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
                inside = not inside
            j = i
    return inside


def ring_is_covered(ring, rings) -> bool:
    """Whether every vertex of ``ring`` already lies inside the zone ``rings`` make.

    Containment is even-odd, so a ring added inside a zone does not add to it:
    it cuts a hole. An island in a pond is the case. The pond is a hole too
    small to keep, so the zone is solid there, and the island's place is
    already in the zone. Keeping the island's outline as well would take it
    back out.
    """
    points = list(ring)
    return (
        bool(rings)
        and bool(points)
        and all(point_in_rings(x, y, rings) for x, y in points)
    )


def add_rings(zones: dict, layer: str, code: str, rings: list) -> int:
    """Add rings to a zone, creating it if need be. Returns 1 for a new zone."""
    zone = zones.get((layer, code))
    if zone:
        zone["rings"].extend(rings)
        zone["bbox"] = bbox_of(zone["rings"])
        return 0
    zones[(layer, code)] = {
        "code": code,
        "layer": layer,
        "bbox": bbox_of(rings),
        "rings": list(rings),
    }
    return 1


def bbox_of(rings):
    xs = [p[0] for r in rings for p in r]
    ys = [p[1] for r in rings for p in r]
    return [min(xs), min(ys), max(xs), max(ys)]


# A zone or county shapefile is tens of megabytes, a few hundred unpacked.
MAX_SOURCE_BYTES = 4 * 1024**3
_POLYGON_TYPES = frozenset({5, 15, 25})  # POLYGON, POLYGONZ, POLYGONM
_SHAPEFILE_PARTS = (".shp", ".shx", ".dbf", ".prj", ".cpg")
# A UGC zone code: two letters, C for a county or Z for a zone, three digits.
_CODE = re.compile(r"[A-Z]{2}[CZ][0-9]{3}")


def read_zip(path: str):
    """Yield ``(layer, code, rings)`` for every record of the shapefile in ``path``.

    Refuses, with the reason, an archive that is not one polygon shapefile in
    longitude and latitude: unpacking to an absurd size, holding no ``.shp``
    or more than one, projected, of another shape type, or with a different
    number of records than shapes. Each of those would otherwise be read as
    something it is not, and the dataset built from it would look fine.
    """
    layer = layer_of(path)
    name = os.path.basename(path)
    with tempfile.TemporaryDirectory() as td:
        try:
            archive = zipfile.ZipFile(path)
        except zipfile.BadZipFile as err:
            # What a failed download leaves behind: an error page, or half a file.
            raise SystemExit(f"{name}: is not a zip archive") from err
        except OSError as err:
            raise SystemExit(f"{name}: cannot be opened: {err}") from err
        with archive as z:
            infos = z.infolist()
            unpacked = sum(info.file_size for info in infos)
            if unpacked > MAX_SOURCE_BYTES:
                raise SystemExit(
                    f"{name}: unpacks to {unpacked} bytes, more than {MAX_SOURCE_BYTES}"
                )
            parts = [i for i in infos if i.filename.lower().endswith(_SHAPEFILE_PARTS)]
            for info in parts:
                z.extract(info, td)
        found = sorted(os.path.join(r, f) for r, _, fs in os.walk(td) for f in fs)
        shps = [f for f in found if f.lower().endswith(".shp")]
        if len(shps) != 1:
            raise SystemExit(f"{name}: expected one .shp inside, found {len(shps)}")
        for prj in (f for f in found if f.lower().endswith(".prj")):
            with open(prj, encoding="utf-8", errors="replace") as f:
                if "PROJCS" in f.read().upper():
                    raise SystemExit(
                        f"{name}: is in a projected coordinate system; the "
                        "builder needs longitude and latitude"
                    )
        try:
            # Names in a file can hold bytes that are not valid text in its
            # stated encoding. The builder reads only the code fields, so a
            # bad byte in a place name must not stop it.
            sf = shapefile.Reader(shps[0], encodingErrors="replace")
            records, shapes = sf.records(), sf.shapes()
        except Exception as err:
            raise SystemExit(
                f"{name}: cannot be read as a shapefile: {type(err).__name__}: {err}"
            ) from err
        if sf.shapeType not in _POLYGON_TYPES:
            raise SystemExit(
                f"{name}: holds shapes of type {sf.shapeType}, not polygons"
            )
        if len(records) != len(shapes):
            raise SystemExit(f"{name}: {len(records)} records but {len(shapes)} shapes")
        for rec, shp in zip(records, shapes):
            parts = list(shp.parts) + [len(shp.points)]
            rings = [shp.points[parts[i] : parts[i + 1]] for i in range(len(parts) - 1)]
            try:
                code = code_of(layer, rec.as_dict())
            except KeyError as err:
                raise SystemExit(
                    f"{name}: has no {err} field to read a zone code from"
                ) from err
            yield layer, code, rings


def code_problem(layer: str, code: str | None) -> str | None:
    """Why ``code`` cannot be the code of a zone on ``layer``, or ``None``."""
    if not code:
        return "has no zone code"
    if not _CODE.fullmatch(code):
        return f"has the code {code!r}, which is not of the form AAZ999"
    wanted = "C" if layer == "county" else "Z"
    if code[2] != wanted:
        return f"has the code {code!r}, which is not a {layer} code"
    return None


def ring_problem(ring, decimals: int) -> str | None:
    """Why ``ring`` cannot be an outline in longitude and latitude, or ``None``.

    Judged on the coordinates as they will be stored, rounded to ``decimals``
    places. NWS's own files carry values a hair past the edge of the map,
    such as a longitude of -180.00000001 on a zone that ends at the
    antimeridian. Those round to the edge and are fine. A value that is still
    off the map after rounding is not a longitude or a latitude at all.
    """
    points = list(ring)
    if not points:
        return None
    if not all(math.isfinite(p[0]) and math.isfinite(p[1]) for p in points):
        # NaN compares false with everything, so no range test would catch it.
        return "has a coordinate that is not a number"
    west = round(min(p[0] for p in points), decimals)
    east = round(max(p[0] for p in points), decimals)
    south = round(min(p[1] for p in points), decimals)
    north = round(max(p[1] for p in points), decimals)
    if west < -180.0 or east > 180.0 or south < -90.0 or north > 90.0:
        return (
            "has coordinates that are not longitude and latitude "
            f"(longitude {west} to {east}, latitude {south} to {north})"
        )
    if east - west > 180.0:
        # A ring drawn across the antimeridian. NWS splits such zones in two;
        # one that is not split would be simplified straight across the map.
        return f"spans more than 180 degrees of longitude ({west} to {east})"
    return None


def _hash_of(path: str, algorithm: str) -> str:
    h = hashlib.new(algorithm)
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def md5_of(path: str) -> str:
    return _hash_of(path, "md5")


def sha256_of(path: str) -> str:
    return _hash_of(path, "sha256")


def source_date(path: str) -> str:
    """The newest timestamp stored inside the zip, as YYYY-MM-DD.

    A property of the file's bytes, so it is the same on every machine. The
    file's own modification time is not: it is whenever the download landed.
    """
    with zipfile.ZipFile(path) as z:
        y, m, d = max(info.date_time for info in z.infolist())[:3]
    return f"{y:04d}-{m:02d}-{d:02d}"


def dataset_bytes(meta: dict, zones: list[dict]) -> bytes:
    """The dataset file: a header line, one line per zone, an end-of-file line.

    One zone per line lets the reader hold a single zone at a time. A file of
    lines cut short is still a file of valid lines, so the header states how
    many zones and lines there are, and the last line repeats both, marks the
    end, and carries the SHA-256 of every byte before it.
    """
    count, total = len(zones), len(zones) + 2
    header = {"format": FORMAT, "zones": count, "lines": total, "meta": meta}
    lines = [header, *zones]
    body = "".join(
        json.dumps(line, separators=(",", ":")) + "\n" for line in lines
    ).encode()
    footer = {
        "eof": True,
        "zones": count,
        "lines": total,
        "sha256": hashlib.sha256(body).hexdigest(),
    }
    return body + json.dumps(footer, separators=(",", ":")).encode() + b"\n"


def gzip_bytes(blob: bytes) -> bytes:
    """Gzip with nothing in the header that varies: no time, no file name, a fixed OS byte.

    ``gzip.compress`` cannot promise that. It writes the clock into the header
    on Python 3.12 unless told otherwise, and even with ``mtime=0`` its OS byte
    is 3 on Python 3.12 and 255 on 3.14, so the same data gave two different
    files. The container is ten header bytes, a raw deflate stream, a CRC and a
    length, so it is written out here.
    """
    deflate = zlib.compressobj(9, zlib.DEFLATED, -15)
    header = b"\x1f\x8b\x08\x00\x00\x00\x00\x00\x02\xff"
    trailer = struct.pack("<II", zlib.crc32(blob), len(blob) & 0xFFFFFFFF)
    return header + deflate.compress(blob) + deflate.flush() + trailer


def count_changes(
    previous: dict[str, int], current: dict[str, int], limit_pct: float
) -> list[str]:
    """Layers whose zone count moved more than ``limit_pct`` percent since the last build.

    A dataset that lost a layer, or a third of its zones, is almost never NWS
    redrawing the map. It is a source file that was not fetched, or one whose
    attribute names changed so that no code could be read from its records.
    Either builds without an error and ships places that resolve to nothing.
    A layer that appears or disappears counts as a 100% change.
    """
    problems = []
    for layer in sorted(set(previous) | set(current)):
        before, after = previous.get(layer, 0), current.get(layer, 0)
        change = 100.0 if before == 0 else abs(after - before) * 100.0 / before
        if change > limit_pct:
            problems.append(
                f"{layer}: {before} -> {after} zones ({change:.1f}% change)"
            )
    return problems


# Bump when a change to this script would alter the output for the same inputs
# and parameters, so that ``build_id`` moves with it. 1 was the first dataset
# (2026-10-22), which carried a build time; 2 removed it; 3 keeps an outline
# that collapses at the stated tolerance instead of dropping it.
BUILDER_REVISION = 3
FORMAT = 2
_LAYER_ORDER = {"forecast": 0, "county": 1, "fire": 2, "marine": 3}


def build_id_of(params: dict, inputs: list[dict]) -> str:
    """One identifier for 'these inputs built this way'. Changes iff the output would."""
    canonical = json.dumps(
        {"params": params, "inputs": inputs}, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "zips",
        nargs="+",
        help="NWS shapefile zips: z_*.zip c_*.zip [mz*.zip] [fz*.zip]",
    )
    ap.add_argument("-o", "--out", default="nws_zones.jsonl")
    ap.add_argument(
        "--eps",
        type=float,
        default=0.005,
        help="Douglas-Peucker tolerance in degrees (default 0.005, ~500 m)",
    )
    ap.add_argument(
        "--decimals", type=int, default=4, help="coordinate rounding (default 4, ~11 m)"
    )
    ap.add_argument("--gzip", action="store_true", help="also write <out>.gz")
    ap.add_argument(
        "--tail-eps",
        type=float,
        default=None,
        help="coarser tolerance for --tail-states (e.g. 0.02)",
    )
    ap.add_argument(
        "--tail-states",
        default="AK",
        help="comma-separated state prefixes that get --tail-eps (default AK)",
    )
    ap.add_argument(
        "--no-alias",
        action="store_true",
        help="keep full rings for fire zones identical to their forecast zone",
    )
    ap.add_argument(
        "--manifest",
        default=None,
        help="where to write the manifest (default <out>.manifest.json)",
    )
    ap.add_argument(
        "--if-changed",
        metavar="MANIFEST",
        default=None,
        help="skip the build when the inputs and parameters match this earlier manifest",
    )
    ap.add_argument(
        "--allow-unusual-source",
        action="store_true",
        help="write the dataset even if a record has no usable zone code, has "
        "coordinates that are not longitude and latitude, crosses the "
        "antimeridian, or names a zone another file of the same layer already "
        "named (default: refuse, exit 2). Such records are left out",
    )
    ap.add_argument(
        "--allow-absent",
        action="store_true",
        help="write the dataset even if a zone named in a source file has no "
        "outline in it (default: refuse, exit 2)",
    )
    ap.add_argument(
        "--max-count-change",
        type=float,
        default=5.0,
        metavar="PCT",
        help="with --if-changed: refuse to write when any layer's zone count moved more "
        "than this many percent from that manifest (default 5)",
    )
    a = ap.parse_args()

    # Arguments the reader would refuse in the finished file are refused here.
    for name, value in (("--eps", a.eps), ("--tail-eps", a.tail_eps)):
        if value is not None and not (
            math.isfinite(value) and 0.0 <= value <= MAX_EPSILON_DEG
        ):
            ap.error(
                f"{name} must be between 0 and {MAX_EPSILON_DEG} degrees, not {value}"
            )
    if not 0 <= a.decimals <= MAX_DECIMALS:
        ap.error(f"--decimals must be between 0 and {MAX_DECIMALS}, not {a.decimals}")
    if not (math.isfinite(a.max_count_change) and a.max_count_change >= 0.0):
        ap.error(f"--max-count-change must be 0 or more, not {a.max_count_change}")

    # Every input is looked at before any work is done, so a bad download is
    # named in one line and not found halfway through.
    kinds: dict[str, str] = {}
    for zp in a.zips:
        name = os.path.basename(zp)
        if not os.path.isfile(zp):
            raise SystemExit(f"{name}: cannot be opened: no such file")
        if not zipfile.is_zipfile(zp):
            raise SystemExit(f"{name}: is not a zip archive")
        layer_of(zp)  # exits, naming the file, if its name says nothing
        # One file of each kind. Two forecast-zone files, or the same file
        # named twice, would add each zone's rings to themselves, and under
        # even-odd containment a ring added twice cancels itself out.
        kind = name[:2].lower()
        if kind in kinds:
            raise SystemExit(
                f"{name}: is a second {layer_of(zp)} file of its kind; "
                f"{kinds[kind]} was already given"
            )
        kinds[kind] = name
    for target in (a.out, a.manifest or a.out + ".manifest.json"):
        folder = os.path.dirname(os.path.abspath(target))
        if not os.path.isdir(folder):
            raise SystemExit(f"{target}: its directory does not exist")
        if not os.access(folder, os.W_OK):
            raise SystemExit(f"{target}: its directory cannot be written to")

    tail = (
        {t.strip().upper() for t in a.tail_states.split(",") if t.strip()}
        if a.tail_eps
        else set()
    )
    # A fixed order, whatever order the files were named in: the output must not depend on it.
    zips = sorted(
        a.zips, key=lambda zp: (_LAYER_ORDER[layer_of(zp)], os.path.basename(zp))
    )
    params = {
        "builder": BUILDER_REVISION,
        "format": FORMAT,
        "epsilon_deg": a.eps,
        "decimals": a.decimals,
        "tail_epsilon_deg": a.tail_eps,
        "tail_states": sorted(tail),
        "fire_aliases": not a.no_alias,
    }
    hashed = [
        {"file": os.path.basename(zp), "layer": layer_of(zp), "sha256": sha256_of(zp)}
        for zp in zips
    ]
    build_id = build_id_of(params, hashed)
    baseline: dict[str, int] = {}
    if a.if_changed and os.path.exists(a.if_changed):
        # The comparison with the last build is a safety check. A manifest
        # that cannot be read must stop the build, not quietly switch it off.
        try:
            with open(a.if_changed, encoding="utf-8") as f:
                earlier = json.load(f)
        except (OSError, ValueError) as err:
            raise SystemExit(
                f"{a.if_changed}: cannot be read as a manifest: {err}"
            ) from err
        previous = earlier.get("build_id") if isinstance(earlier, dict) else None
        baseline = earlier.get("zones") if isinstance(earlier, dict) else None
        if not (
            isinstance(previous, str)
            and isinstance(baseline, dict)
            and baseline
            and all(
                isinstance(layer, str)
                and isinstance(count, int)
                and not isinstance(count, bool)
                for layer, count in baseline.items()
            )
        ):
            raise SystemExit(
                f"{a.if_changed}: is not a manifest this script wrote "
                "(no build_id, or no zone count per layer)"
            )
        if previous == build_id:
            print(
                f"unchanged: build_id {build_id} matches {a.if_changed}; nothing built",
                file=sys.stderr,
            )
            return
        print(f"changed: build_id {previous} -> {build_id}", file=sys.stderr)

    zones: dict[
        tuple[str, str], dict
    ] = {}  # (layer, code): fire and forecast share the "Z" code space
    sources = []
    # Manifest only: adding it to ``meta`` would change the dataset's bytes.
    dropped: dict[str, dict[str, int]] = {}
    absent: dict[str, list[str]] = {}
    problems: list[str] = []  # records that could not be used, and why
    owner: dict[tuple[str, str], str] = {}  # which file named each zone
    for zp, entry in zip(zips, hashed):
        layer, n_rec, n_zone = layer_of(zp), 0, 0
        # A ring smaller or thinner than the tolerance collapses. What becomes
        # of it depends on where it lies:
        #   wholly inside its zone as built
        #                 dropped. If it is a hole (a pond), the zone becomes
        #                 solid there, so a point in it matches where it did
        #                 not before: the safe direction. If it is an outline,
        #                 its place is in the zone already, and under even-odd
        #                 containment keeping it would cut that place out.
        #   anywhere else kept, at a finer tolerance or failing that as its
        #                 bounding box, because a place inside one that is
        #                 dropped resolves to nothing.
        # So no place an outline covers is ever left out. All are counted.
        lost_rings = lost_holes = covered = refined = boxed = odd_holes = 0
        named = 0  # records that name a zone
        codes: set[str] = set()  # the zones this file names
        placed: set[int] = set()  # records whose place is in the dataset
        pending: list[tuple[int, str, float, list, bool]] = []
        source = os.path.basename(zp)
        for layer, code, rings in read_zip(zp):
            n_rec += 1
            fault = code_problem(layer, code) or next(
                (found for r in rings if (found := ring_problem(r, a.decimals))), None
            )
            if fault is None and owner.setdefault((layer, code), source) != source:
                fault = f"names {code}, which {owner[(layer, code)]} already named"
            if fault is not None:
                problems.append(f"{source} record {n_rec} {fault}")
                continue
            named += 1
            codes.add(code)
            eps = a.tail_eps if (a.tail_eps and code[:2] in tail) else a.eps
            # The largest ring's winding is the outline's; one wound the other
            # way is, by the shapefile convention, a hole in it. That is only
            # a label here. What is done with a ring that collapses is decided
            # by where it lies, not by how it is wound.
            areas = [ring_area(r) for r in rings]
            outline = max(areas, key=abs, default=0.0)
            srings = []
            for ring, area in zip(rings, areas):
                kept = simplify_ring(ring, eps, a.decimals)
                if kept is not None:
                    srings.append(kept)
                else:
                    pending.append((n_rec, code, eps, ring, area * outline < 0.0))
            if srings:
                placed.add(n_rec)
                n_zone += add_rings(zones, layer, code, srings)
        # The collapsing rings wait until every ring that survives on its own
        # is in place, so each is judged against its whole zone, whichever
        # record that zone's other rings came from.
        built = {key: len(z["rings"]) for key, z in zones.items() if key[0] == layer}
        for record, code, eps, ring, wound_as_hole in pending:
            zone = zones.get((layer, code))
            solid = zone["rings"][: built.get((layer, code), 0)] if zone else []
            if ring_is_covered(ring, solid):
                # Its place is in the zone as built. Dropping a pond makes the
                # zone solid there; dropping an outline changes nothing.
                if wound_as_hole:
                    lost_holes += 1
                else:
                    covered += 1
                placed.add(record)
                continue
            kept = refine_ring(ring, eps, a.decimals)
            if kept is not None:
                refined += 1
            else:
                kept = box_ring(ring, a.decimals)
                if kept is None:
                    lost_rings += 1  # a ring with no points at all
                    continue
                boxed += 1
            # Wound as a hole, yet not wholly inside its zone as built. Most
            # often a real hole that straddles the simplified outline, such as
            # an island at the edge of a marine zone; it could also be an
            # outline the source wound the wrong way. Kept either way: where
            # it overlaps the zone it still cuts its hole, and where it does
            # not, the zone gains a little, which is the safe direction.
            # Counted, so a change in the number is visible.
            odd_holes += wound_as_hole
            placed.add(record)
            n_zone += add_rings(zones, layer, code, [kept])
        lost_records = named - len(placed)
        # Every zone the file names must be in the dataset with an outline. A
        # zone that is not there cannot match anything, and nothing downstream
        # can tell that it is missing: the dataset is the only list there is.
        missing = sorted(code for code in codes if (layer, code) not in zones)
        if missing:
            absent[os.path.basename(zp)] = missing
        sources.append(
            {
                **entry,
                "bytes": os.path.getsize(zp),
                "md5": md5_of(zp),
                "date": source_date(zp),
                "records": n_rec,
                "zones": n_zone,
            }
        )
        dropped[os.path.basename(zp)] = {
            "rings": lost_rings,
            "holes": lost_holes,
            "records": lost_records,
            "codes": len(codes),
            "codes_absent": len(missing),
            "hole_wound_kept": odd_holes,
            "already_inside": covered,
            "kept_finer": refined,
            "kept_as_box": boxed,
        }
        print(
            f"{os.path.basename(zp)}: {n_rec} records -> {n_zone} zones ({layer}); "
            f"kept {refined} collapsing outlines at a finer tolerance and {boxed} "
            f"as a box; dropped {lost_holes} holes, {covered} outlines already "
            f"inside their zone, {lost_rings} empty rings, {lost_records} whole records; "
            f"{odd_holes} wound as holes but kept; "
            f"{len(codes) - len(missing)} of {len(codes)} named zones present",
            file=sys.stderr,
        )

    if problems:
        print(f"error: {len(problems)} record(s) could not be used:", file=sys.stderr)
        for line in problems[:20]:
            print(f"  {line}", file=sys.stderr)
        if len(problems) > 20:
            print(f"  ... and {len(problems) - 20} more", file=sys.stderr)
        if not a.allow_unusual_source:
            print(
                "  nothing written. If the source really is like this, rerun with\n"
                "  --allow-unusual-source and those records are left out",
                file=sys.stderr,
            )
            sys.exit(2)

    if absent:
        for name, missing in absent.items():
            shown = " ".join(missing[:20]) + (" ..." if len(missing) > 20 else "")
            print(
                f"error: {name} names {len(missing)} zone(s) with no outline in "
                f"the dataset: {shown}",
                file=sys.stderr,
            )
        if not a.allow_absent:
            print(
                "  nothing written. A zone that is absent matches nothing. If the\n"
                "  source really carries a zone with no geometry, rerun with --allow-absent",
                file=sys.stderr,
            )
            sys.exit(2)

    doc = {
        "meta": {
            "build_id": build_id,
            "source_date": max(s["date"] for s in sources),
            "epsilon_deg": a.eps,
            "decimals": a.decimals,
            "tail_epsilon_deg": a.tail_eps,
            "tail_states": sorted(tail),
            "sources": sources,
        },
        "zones": sorted(zones.values(), key=lambda z: (z["layer"], z["code"])),
    }
    # Fire weather zones reuse the forecast-zone code form (STATE + "Z" + NNN). Where a code exists in
    # both layers, report whether the geometry is the same (harmless) or different (two zones, one code).
    by_code: dict[str, list[dict]] = {}
    for z in zones.values():
        by_code.setdefault(z["code"], []).append(z)
    same = diff = 0
    for code, zs in by_code.items():
        if len(zs) > 1:
            if all(z["bbox"] == zs[0]["bbox"] for z in zs[1:]):
                same += 1
            else:
                diff += 1
    if same or diff:
        print(
            f"codes present in more than one layer: {same} with identical bbox, {diff} with different geometry",
            file=sys.stderr,
        )
        doc["meta"]["cross_layer_codes"] = {"identical": same, "different": diff}
    # Fire zones are "derived from NWS Public Forecast Zones": most are the forecast zone under the same code.
    # Store those as an alias so the rings exist once; the resolver shares the arrays on load.
    if not a.no_alias:
        aliased = 0
        for z in doc["zones"]:
            if z["layer"] == "fire":
                fz = zones.get(("forecast", z["code"]))
                if fz and fz["rings"] == z["rings"]:
                    z["alias"] = "forecast"
                    z.pop("rings")
                    z.pop("bbox")
                    aliased += 1
        print(
            f"fire zones stored as aliases of the identical forecast zone: {aliased}",
            file=sys.stderr,
        )
        doc["meta"]["fire_aliases"] = aliased
    per_layer: dict[str, int] = {}
    for z in doc["zones"]:
        per_layer[z["layer"]] = per_layer.get(z["layer"], 0) + 1
    # Before anything is written: a build that fails this check leaves no file behind.
    if baseline:
        problems = count_changes(baseline, per_layer, a.max_count_change)
        if problems:
            print(
                f"refused: zone counts moved more than {a.max_count_change:g}% "
                f"from {a.if_changed}; nothing written",
                file=sys.stderr,
            )
            for problem in problems:
                print(f"  {problem}", file=sys.stderr)
            print(
                "  if NWS really changed this much, rerun with a larger --max-count-change",
                file=sys.stderr,
            )
            sys.exit(2)
    blob = dataset_bytes(doc["meta"], doc["zones"])
    # Before anything is written: the file is loaded the way the integration
    # will load it. A dataset the reader refuses is not a dataset.
    refusal = reader_refusal(blob)
    if refusal is not None:
        print(
            f"error: the integration's reader refuses this dataset: {refusal}\n"
            "  nothing written",
            file=sys.stderr,
        )
        sys.exit(2)
    write_atomically(a.out, blob)
    msg = f"{a.out}: {len(doc['zones'])} zones, {len(blob) / 1e6:.2f} MB"
    manifest = {
        **doc["meta"],
        "params": params,
        "zones": dict(sorted(per_layer.items())),
        "zone_count": len(doc["zones"]),
        "content_bytes": len(blob),
        "content_sha256": hashlib.sha256(blob).hexdigest(),
        "dropped": dropped,
    }
    if a.gzip:
        gz = gzip_bytes(blob)
        if zlib.decompress(gz, 31) != blob:  # pragma: no cover - a failing zlib
            raise SystemExit(f"{a.out}.gz: does not decompress to the dataset")
        write_atomically(a.out + ".gz", gz)
        msg += f", {len(gz) / 1e6:.2f} MB gzipped"
        # The .gz is the file that gets published, so its own hash is the one to pin or quote.
        manifest["gzip_bytes"] = len(gz)
        manifest["gzip_sha256"] = hashlib.sha256(gz).hexdigest()
    manifest_path = a.manifest or a.out + ".manifest.json"
    # Last, so that a manifest on disk means the files it describes are there.
    write_atomically(
        manifest_path,
        (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )
    print(f"{msg}; build_id {build_id}; manifest {manifest_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
