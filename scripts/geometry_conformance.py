#!/usr/bin/env python3
"""Check published alert geometry for GeoJSON conformance on a running HA.

Walks every alert entity a live Home Assistant has published, fetches each
alert's geometry back over the REST endpoint, and asserts the invariants the
geometry pipeline is supposed to guarantee — ring closure, vertex counts,
coordinate ranges, bbox agreement, and the polygon/point split.

**How this differs from ``provider_probe.py``.** That script runs a provider's
``async_fetch`` *outside* HA and reports what the feed produced; it answers
"does this source publish polygons?" and needs no running instance. This one
starts from the other end: it reads what HA actually published and pulls the
geometry back through ``GeometryStore`` and ``CAPAlertsGeometryView``. The
round-trip is the point — a value can be well-formed leaving the parser and
still be wrong after storage and serialization, and only this path exercises
that. Use ``provider_probe.py`` to ask what a feed contains; use this to ask
whether what shipped is valid.

What it checks, and why each one exists:

* **Ring closure** (issue #85) — ``normalize_ring`` closes rings and drops ones
  with fewer than three distinct vertices, at the single choke point every
  parser feeds. A ring that arrives here unclosed means something bypassed it.
* **Multi-ring geometries** — exercises the shared builders that replaced the
  per-provider copies (CAP XML, CAP-over-JSON, GeoRSS).
* **Coordinate ranges and bbox agreement** — a bbox that fails to contain its
  own geometry is the signature of a lon/lat flip, the recurring failure in
  this area. ``bbox`` is lon-first ``[W, S, E, N]``.
* **Polygon/point split** (issue #27) — polygons keep the ``geometry`` slot and
  points ride alongside in ``points``; point geometry appears only when an
  alert publishes no areal shape.

The closing report states which of those paths the run actually covered, since
coverage depends entirely on what the configured feeds happen to be publishing
at the time. A path with no live alerts is reported as uncovered rather than
passing silently — an empty check is not a green one.

A 404 from the geometry endpoint is **not** a failure. The store is in-memory,
LRU-evicted at 5 MB, and does not survive a restart, so a missing polygon is
expected and is counted separately.

Usage (stdlib only — run with system python3, no venv needed):

    scripts/geometry_conformance.py
    scripts/geometry_conformance.py --host http://localhost:8123
    scripts/geometry_conformance.py --json findings.json

Auth: ``$HA_TOKEN``, or ``--token``. ``scripts/dev_relocate.py`` documents how
to mint one from the dev container's refresh token if you have no long-lived
token to hand.

Exit status is 1 when any conformance check failed, so it can gate a release.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from typing import Any

AREAL = {"Polygon", "MultiPolygon"}
PUNCTUAL = {"Point", "MultiPoint"}
KNOWN_TYPES = AREAL | PUNCTUAL

# Must match providers/geometry.py:_VERTEX_PRECISION, so this script accepts
# exactly the rings normalize_ring accepts.
VERTEX_PRECISION = 6

# Tolerance for bbox containment, in degrees. The bbox is derived from the same
# coordinates, so this only absorbs float round-tripping through JSON.
BBOX_TOLERANCE = 1e-6


def api(host: str, token: str, path: str, timeout: int = 30) -> tuple[Any | None, Any]:
    """GET a JSON endpoint. Returns ``(payload, error)`` — never raises."""
    req = urllib.request.Request(
        f"{host}{path}", headers={"Authorization": f"Bearer {token}"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read()), None
    except urllib.error.HTTPError as err:
        return None, err.code
    except Exception as err:  # noqa: BLE001 — a probe reports, it does not raise
        return None, str(err)


def iter_rings(geom: dict) -> Any:
    """Yield ``(path, ring)`` for every linear ring in a geometry."""
    kind, coords = geom.get("type"), geom.get("coordinates")
    if kind == "Polygon":
        for i, ring in enumerate(coords or []):
            yield f"ring[{i}]", ring
    elif kind == "MultiPolygon":
        for p, poly in enumerate(coords or []):
            for i, ring in enumerate(poly or []):
                yield f"poly[{p}].ring[{i}]", ring


def iter_positions(geom: dict) -> Any:
    """Yield every ``[lon, lat]`` position in a geometry, whatever its type."""
    kind, coords = geom.get("type"), geom.get("coordinates")
    if kind == "Point":
        yield coords
    elif kind == "MultiPoint":
        yield from (coords or [])
    else:
        for _, ring in iter_rings(geom):
            yield from (ring or [])


def check_geometry(geom: dict, bbox: Any, points: Any) -> tuple[list[str], list[str]]:
    """Return ``(failures, notes)`` for one alert's geometry."""
    failures: list[str] = []
    notes: list[str] = []

    kind = geom.get("type")
    if kind not in KNOWN_TYPES:
        failures.append(
            f"unknown geometry type {kind!r} — consumers fall back to the bbox"
        )
        return failures, notes

    # Ring validity, judged by normalize_ring's own rule (issue #85).
    for path, ring in iter_rings(geom):
        if not ring:
            failures.append(f"{path}: empty ring emitted")
            continue
        if ring[0] != ring[-1]:
            failures.append(
                f"{path}: unclosed — first {ring[0]} != last {ring[-1]} "
                "(RFC 7946 §3.1.6)"
            )
        if len(ring) < 4:
            failures.append(
                f"{path}: {len(ring)} positions, a closed ring needs 4 or more"
            )
        distinct = {
            (round(pos[0], VERTEX_PRECISION), round(pos[1], VERTEX_PRECISION))
            for pos in ring
            if isinstance(pos, (list, tuple)) and len(pos) >= 2
        }
        if len(distinct) < 3:
            failures.append(
                f"{path}: {len(distinct)} distinct vertices — not an area, "
                "normalize_ring should have dropped it"
            )

    # Interior rings are valid GeoJSON but geometry_from_polygons emits exactly
    # one ring per polygon, so any hole arrived by a route that bypasses it —
    # worth surfacing, not a defect in itself.
    if kind == "Polygon" and len(geom.get("coordinates") or []) > 1:
        notes.append("polygon carries interior ring(s) — not from our builder")
    if kind == "MultiPolygon":
        holed = [p for p in (geom.get("coordinates") or []) if len(p) > 1]
        if holed:
            notes.append(
                f"{len(holed)} polygon(s) carry interior rings — not from our builder"
            )

    positions = [p for p in iter_positions(geom) if isinstance(p, (list, tuple))]

    # Coordinate ranges. Catches a lon/lat flip, which otherwise produces
    # geometry that looks plausible until it is drawn.
    for pos in positions:
        if len(pos) < 2:
            failures.append(f"position {pos} has fewer than two values")
            continue
        lon, lat = pos[0], pos[1]
        if not -180 <= lon <= 180:
            failures.append(f"longitude {lon} out of range — lon/lat swapped?")
            break
        if not -90 <= lat <= 90:
            failures.append(f"latitude {lat} out of range — lon/lat swapped?")
            break

    # bbox is lon-first [W, S, E, N] and must contain its own geometry.
    if bbox and positions:
        try:
            west, south, east, north = (float(v) for v in bbox)
        except (TypeError, ValueError):
            failures.append(f"bbox is not four numbers: {bbox!r}")
        else:
            lons = [p[0] for p in positions]
            lats = [p[1] for p in positions]
            if min(lons) < west - BBOX_TOLERANCE or max(lons) > east + BBOX_TOLERANCE:
                failures.append(
                    f"bbox longitude span [{west}, {east}] excludes geometry "
                    f"[{min(lons)}, {max(lons)}]"
                )
            if min(lats) < south - BBOX_TOLERANCE or max(lats) > north + BBOX_TOLERANCE:
                failures.append(
                    f"bbox latitude span [{south}, {north}] excludes geometry "
                    f"[{min(lats)}, {max(lats)}]"
                )
            if kind in PUNCTUAL and (
                abs(east - west) > BBOX_TOLERANCE or abs(north - south) > BBOX_TOLERANCE
            ):
                notes.append("point geometry has a non-degenerate bbox")

    # The polygon/point split (issue #27).
    if points:
        for point in points:
            if not (isinstance(point, (list, tuple)) and len(point) == 2):
                failures.append(f"points entry {point!r} is not [lon, lat]")
        if kind in AREAL:
            notes.append(
                f"{len(points)} point(s) published alongside areal geometry — "
                "the CAP 1.2 §3.2.4 union, both shapes kept"
            )
    if kind in PUNCTUAL and not points:
        notes.append("point geometry but no points attribute")

    return failures, notes


def collect(host: str, token: str, states: list[dict]) -> dict[str, Any]:
    """Fetch and check every alert geometry. Returns the findings."""
    alerts = [
        state
        for state in states
        if state["entity_id"].startswith("sensor.cap_alerts_")
        and (state["attributes"].get("geometry_ref") or state["attributes"].get("bbox"))
    ]
    geocode_only = [
        state
        for state in states
        if state["entity_id"].startswith("sensor.cap_alerts_")
        and "cap_alert_" in state["entity_id"]
        and not state["attributes"].get("geometry_ref")
        and not state["attributes"].get("bbox")
    ]

    types: Counter = Counter()
    providers: Counter = Counter()
    failures: list[tuple[str, list[str]]] = []
    notes: list[tuple[str, list[str]]] = []
    records: list[dict] = []
    evicted = 0
    fetched = 0
    with_points = 0

    for entity in sorted(alerts, key=lambda s: s["entity_id"]):
        entity_id = entity["entity_id"]
        attrs = entity["attributes"]
        ref = attrs.get("geometry_ref") or ""
        bbox = attrs.get("bbox")
        points = attrs.get("points") or []
        if points:
            with_points += 1

        # geometry_ref is "{entry_id}:{provider}:{id_hash}".
        provider = ref.split(":")[1] if ref.count(":") >= 2 else "?"
        providers[provider] += 1

        if not ref:
            notes.append((entity_id, ["bbox present with no geometry_ref"]))
            continue

        quoted = urllib.parse.quote(ref, safe="")
        payload, err = api(host, token, f"/api/cap_alerts/geometry/{quoted}")
        if payload is None:
            if err == 404:
                evicted += 1
            else:
                failures.append((entity_id, [f"geometry fetch failed: {err}"]))
            continue

        fetched += 1
        features = payload.get("features") or []
        if len(features) != 1:
            failures.append(
                (
                    entity_id,
                    [f"FeatureCollection has {len(features)} features, expected 1"],
                )
            )
            continue

        geom = features[0].get("geometry") or {}
        types[geom.get("type")] += 1
        entity_failures, entity_notes = check_geometry(geom, bbox, points)
        if entity_failures:
            failures.append((entity_id, entity_failures))
        if entity_notes:
            notes.append((entity_id, entity_notes))
        records.append(
            {
                "entity_id": entity_id,
                "provider": provider,
                "type": geom.get("type"),
                "bbox": bbox,
                "points": points,
                "failures": entity_failures,
                "notes": entity_notes,
            }
        )

    return {
        "counts": {
            "alerts": len(alerts),
            "fetched": fetched,
            "evicted": evicted,
            "geocode_only": len(geocode_only),
            "with_points": with_points,
        },
        "types": dict(types),
        "providers": dict(providers),
        "failures": failures,
        "notes": notes,
        "records": records,
    }


def report(found: dict[str, Any]) -> None:
    """Print the human-readable findings."""
    counts = found["counts"]
    types: dict = found["types"]

    print("=" * 72)
    print("cap_alerts geometry conformance")
    print("=" * 72)
    print(f"alert entities with geometry:   {counts['alerts']}")
    print(f"  geometry fetched:             {counts['fetched']}")
    print(f"  evicted from store (404):     {counts['evicted']}   [normal — LRU]")
    print(f"geocode-only alerts (no shape): {counts['geocode_only']}")
    print()
    print(
        "by provider:  "
        + (", ".join(f"{k}={v}" for k, v in found["providers"].items()) or "none")
    )
    print(
        "by type:      " + (", ".join(f"{k}={v}" for k, v in types.items()) or "none")
    )
    print()

    areal = sum(v for k, v in types.items() if k in AREAL)
    punctual = sum(v for k, v in types.items() if k in PUNCTUAL)
    multi = types.get("MultiPolygon", 0)
    with_points = counts["with_points"]

    print("--- path coverage in this run ---")
    for label, count, covered in (
        ("ring closure / validity", areal, areal > 0),
        ("shared multi-ring builders", multi, multi > 0),
        (
            "circle → point publication",
            punctual + with_points,
            bool(punctual or with_points),
        ),
    ):
        status = "covered" if covered else "NO LIVE COVERAGE"
        print(f"  {label:<28} {count:>4}   {status}")
    print()

    if found["failures"]:
        print(f"--- {len(found['failures'])} failure(s) ---")
        for entity_id, messages in found["failures"]:
            print(f"  {entity_id}")
            for message in messages:
                print(f"      ! {message}")
        print()
    else:
        print("--- no conformance failures ---")
        print()

    if found["notes"]:
        print(f"--- {len(found['notes'])} note(s) ---")
        for entity_id, messages in found["notes"]:
            print(f"  {entity_id}")
            for message in messages:
                print(f"      . {message}")
        print()


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("HA_HOST", "http://localhost:8123"),
        help="Home Assistant base URL (default: %(default)s)",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("HA_TOKEN", ""),
        help="long-lived access token (default: $HA_TOKEN)",
    )
    parser.add_argument("--json", dest="json_out", help="write full findings here")
    args = parser.parse_args()

    if not args.token:
        print("error: pass --token or set $HA_TOKEN", file=sys.stderr)
        return 2

    states, err = api(args.host, args.token, "/api/states")
    if states is None:
        print(f"error: cannot read /api/states: {err}", file=sys.stderr)
        return 2

    found = collect(args.host, args.token, states)
    report(found)

    if args.json_out:
        serializable = {
            key: value
            for key, value in found.items()
            if key not in ("failures", "notes")
        }
        with open(args.json_out, "w") as handle:
            json.dump(serializable, handle, indent=2)
        print(f"full findings written to {args.json_out}")

    return 1 if found["failures"] else 0


if __name__ == "__main__":
    sys.exit(main())
