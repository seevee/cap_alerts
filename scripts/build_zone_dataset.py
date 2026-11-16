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
import json
import os
import struct
import sys
import tempfile
import zipfile
import zlib

try:
    import shapefile  # pyshp
except ImportError:
    sys.exit("pyshp is required:  pip install pyshp   (or apt install python3-pyshp)")


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


def bbox_of(rings):
    xs = [p[0] for r in rings for p in r]
    ys = [p[1] for r in rings for p in r]
    return [min(xs), min(ys), max(xs), max(ys)]


def read_zip(path: str):
    layer = layer_of(path)
    with tempfile.TemporaryDirectory() as td:
        with zipfile.ZipFile(path) as z:
            z.extractall(td)
        shps = [
            os.path.join(r, f)
            for r, _, fs in os.walk(td)
            for f in fs
            if f.lower().endswith(".shp")
        ]
        if not shps:
            raise SystemExit(f"no .shp inside {path}")
        sf = shapefile.Reader(shps[0])
        for rec, shp in zip(sf.records(), sf.shapes()):
            parts = list(shp.parts) + [len(shp.points)]
            rings = [shp.points[parts[i] : parts[i + 1]] for i in range(len(parts) - 1)]
            yield layer, code_of(layer, rec.as_dict()), rings


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
# (2026-10-22), which carried a build time; 2 removed it.
BUILDER_REVISION = 2
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
        "--max-count-change",
        type=float,
        default=5.0,
        metavar="PCT",
        help="with --if-changed: refuse to write when any layer's zone count moved more "
        "than this many percent from that manifest (default 5)",
    )
    a = ap.parse_args()

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
        with open(a.if_changed, encoding="utf-8") as f:
            earlier = json.load(f)
        previous = earlier.get("build_id")
        baseline = earlier.get("zones") or {}
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
    for zp, entry in zip(zips, hashed):
        layer, n_rec, n_zone = layer_of(zp), 0, 0
        # A ring smaller than the tolerance collapses and is dropped; a record
        # whose every ring collapses is dropped whole. Counted, because a place
        # inside one resolves to nothing.
        lost_rings = lost_records = 0
        for layer, code, rings in read_zip(zp):
            n_rec += 1
            if not code:
                continue
            eps = a.tail_eps if (a.tail_eps and code[:2] in tail) else a.eps
            srings = [
                s for s in (simplify_ring(r, eps, a.decimals) for r in rings) if s
            ]
            lost_rings += len(rings) - len(srings)
            if not srings:
                lost_records += 1
                continue
            z = zones.get((layer, code))
            if z:
                z["rings"].extend(srings)
                z["bbox"] = bbox_of(z["rings"])
            else:
                zones[(layer, code)] = {
                    "code": code,
                    "layer": layer,
                    "bbox": bbox_of(srings),
                    "rings": srings,
                }
                n_zone += 1
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
        dropped[os.path.basename(zp)] = {"rings": lost_rings, "records": lost_records}
        print(
            f"{os.path.basename(zp)}: {n_rec} records -> {n_zone} zones ({layer}); "
            f"dropped {lost_rings} collapsed rings, {lost_records} whole records",
            file=sys.stderr,
        )

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
    with open(a.out, "wb") as f:
        f.write(blob)
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
        with open(a.out + ".gz", "wb") as f:
            f.write(gz)
        msg += f", {len(gz) / 1e6:.2f} MB gzipped"
        # The .gz is the file that gets published, so its own hash is the one to pin or quote.
        manifest["gzip_bytes"] = len(gz)
        manifest["gzip_sha256"] = hashlib.sha256(gz).hexdigest()
    manifest_path = a.manifest or a.out + ".manifest.json"
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, sort_keys=True)
        f.write("\n")
    print(f"{msg}; build_id {build_id}; manifest {manifest_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
