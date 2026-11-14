#!/usr/bin/env python3
"""Does local zone resolution plus local matching reproduce NWS's ``point=`` query?

A repeatable check for issue #316, meant to be re-run on days with real weather:

    scripts/nws_zone_parity.py scan                                # where are polygon warnings now?
    scripts/nws_zone_parity.py run   nws_zones.jsonl.gz --storm    # check where the storms are
    scripts/nws_zone_parity.py run   nws_zones.jsonl.gz            # pick points, then check them
    scripts/nws_zone_parity.py run   nws_zones.jsonl.gz OK TX KS   # or name the states
    scripts/nws_zone_parity.py pick  nws_zones.jsonl.gz            # only print the points
    scripts/nws_zone_parity.py check nws_zones.jsonl.gz 40.7829,-73.9654 40.7357,-74.1724

``check`` asks the API two things for each point and compares them:

    reference  GET alerts/active?point=lat,lon                    what NWS computes server-side
    candidate  GET alerts/active?zone=<containing + nearby codes> what the local mode would send
    local      keep a candidate alert only if
                 - it has a polygon and the point is inside it, or
                 - it has no polygon and one of its zones CONTAINS the point

It prints, per point, the codes sent, both counts, and any alert present on
only one side, and exits 1 if any point differs.

``pick`` chooses the points from today's live alerts, by construction, so the
check is aimed at the cases that can go wrong. For each state it fetches
``alerts/active?area=XX`` (state-level, nothing personal) and builds:

    IN_POLY    inside a warning polygon                           point= returns it
    CTY_OUT    in that warning's county, outside its polygon      point= must NOT return it
    IN_ZONE    inside a zone-only product's forecast zone         point= returns it
    CTY_TRAP   in a county overlapping that zone, but lying in a
               different forecast zone with no product            point= must NOT return it
    IN_MARINE  inside a marine zone-only product's zone, at sea   point= returns it

With no states named it takes the five busiest land areas and the two busiest
marine areas from ``alerts/active/count``. Busiest is not the same as most
useful: most active products are zone-only advisories, and the cases that
exercise polygon matching need a polygon warning to be in effect. ``--storm``
takes the areas from ``scan`` instead.

``scan`` answers "is today worth a run, and where?". It reads the national
``alerts/active`` list once, keeps the alerts that carry a polygon, and prints
one row per area:

    area  polygons  storm  last 3h  newest             events
    TX           2      1        1  11-14 18:00 EST    Flood Warning, Severe Thunderstorm Warning
    FL           3      0        0  11-14 10:14 EST    Flood Warning

    polygons  active alerts in the area that carry a polygon
    storm     of those, the storm-based warnings (tornado, severe thunderstorm,
              flash flood, special marine, snow squall, extreme wind, dust
              storm): short-lived, drawn around one storm, and the hardest
              test of polygon matching
    last 3h   issued within ``--hours`` (default 3): is anything new?
    newest    when the most recent one was issued, in this machine's time zone

Areas are ordered by storm-based count, then by recent, then by polygons. An
alert that spans two states is counted in both. Storm-based warnings last
about an hour, so run the check soon after a scan shows one. ``scan`` needs no
dataset and sends no location; give it one anyway and it prints the ``run``
command ready to paste. The same rows are available to other code through
``polygon_activity``.

Every point this script sends to NWS is one it generated inside a public alert
area. ``check`` sends whatever points it is given: use public places, never a
home.

Needs the integration importable (run it with the project's virtualenv) and
network access to api.weather.gov. Nothing else.
"""

from __future__ import annotations

import argparse
import datetime as dt
import gzip
import json
import random
import sys
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from custom_components.cap_alerts.const import USER_AGENT
from custom_components.cap_alerts.providers.gps import point_in_polygon
from custom_components.cap_alerts.providers.nws_zones import (
    Zone,
    ZoneSet,
    point_in_rings,
)

API = "https://api.weather.gov"
RADIUS_KM, MAX_ZONES = 16.0, 5
PER_CASE = 2

# UGC prefixes of the marine areas. Their zone codes (ANZ338, LMZ846) are served
# under the API's forecast type but live on the dataset's marine layer.
MARINE_AREAS = frozenset(
    {
        "AM",
        "AN",
        "GM",
        "LC",
        "LE",
        "LH",
        "LM",
        "LO",
        "LS",
        "PH",
        "PK",
        "PM",
        "PS",
        "PZ",
        "SL",
    }
)
TYPE_TO_LAYER = {
    "forecast": "forecast",
    "public": "forecast",
    "county": "county",
    "fire": "fire",
    "marine": "marine",
}

# What NWS calls storm-based warnings: issued for one storm, with a polygon
# drawn around its track, usually for under an hour.
STORM_BASED_EVENTS = frozenset(
    {
        "Tornado Warning",
        "Severe Thunderstorm Warning",
        "Flash Flood Warning",
        "Special Marine Warning",
        "Snow Squall Warning",
        "Extreme Wind Warning",
        "Dust Storm Warning",
    }
)
SCAN_HOURS = 3.0

Point = tuple[float, float]  # lat, lon
Feature = dict[str, Any]


def get(url: str) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT.format("parity"),
            "Accept": "application/geo+json",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def load_dataset(path: str) -> ZoneSet:
    """A line-per-zone dataset, gzipped or not; or an October single-document file."""
    if path.endswith(".json"):
        return ZoneSet.from_json(Path(path).read_bytes())
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rb") as f:
        return ZoneSet.from_lines(f)


# ---------------------------------------------------------------------------
# Matching an alert to a point, the way the local mode would
# ---------------------------------------------------------------------------


def _polygons(geometry: dict[str, Any]) -> list[list[list[list[float]]]]:
    if geometry["type"] == "MultiPolygon":
        return geometry["coordinates"]
    return [geometry["coordinates"]]


def in_geometry(lat: float, lon: float, geometry: dict[str, Any] | None) -> bool | None:
    """Whether the point is inside an alert's polygon; None when it has none."""
    if not geometry:
        return None  # no polygon: decide by zone
    for polygon in _polygons(geometry):  # ring 0 outer, the rest holes
        if point_in_polygon(lat, lon, polygon[0]) and not any(
            point_in_polygon(lat, lon, hole) for hole in polygon[1:]
        ):
            return True
    return False


def layer_for(code: str, url_type: str) -> str:
    """The dataset layer a zone code belongs to, given the type in its API URL."""
    if code[:2] in MARINE_AREAS:
        return "marine"
    fallback = "county" if len(code) == 6 and code[2] == "C" else "forecast"
    return TYPE_TO_LAYER.get(url_type, fallback)


def affected(properties: dict[str, Any]) -> set[tuple[str, str]]:
    """(layer, code) for each of an alert's zones.

    Read from ``affectedZones`` URLs such as ``.../zones/fire/COZ240``, because
    437 codes exist as both a fire and a forecast zone with different shapes
    and the bare UGC cannot say which is meant. Bare UGC is the fallback.
    """
    found: set[tuple[str, str]] = set()
    for url in properties.get("affectedZones", []):
        parts = url.rstrip("/").split("/")
        if len(parts) >= 2 and parts[-2] in TYPE_TO_LAYER:
            found.add((layer_for(parts[-1], parts[-2]), parts[-1]))
    if not found:
        for code in properties.get("geocode", {}).get("UGC", []):
            found.add((layer_for(code, ""), code))
    return found


def local_filter(
    lat: float, lon: float, features: list[Feature], containing: set[tuple[str, str]]
) -> list[Feature]:
    """The alerts that apply to the point. ``containing``: every zone that contains it."""
    keep = []
    for feature in features:
        inside = in_geometry(lat, lon, feature.get("geometry"))
        if inside is True or (
            inside is None and affected(feature["properties"]) & containing
        ):
            keep.append(feature)
    return keep


def check(zones: ZoneSet, points: list[Point]) -> int:
    """Compare ``point=`` with the local mode for each point. Returns how many differ."""
    differing = 0
    for lat, lon in points:
        containing = {(h.layer, h.code) for h in zones.containing(lat, lon)}
        nearby = zones.codes(lat, lon, radius_km=RADIUS_KM, max_zones=MAX_ZONES)
        codes = list(dict.fromkeys(nearby + sorted(code for _, code in containing)))
        reference = get(f"{API}/alerts/active?point={lat},{lon}")["features"]
        candidates = (
            get(f"{API}/alerts/active?zone={','.join(codes)}")["features"]
            if codes
            else []
        )
        local = local_filter(lat, lon, candidates, containing)
        events = {
            f["properties"]["id"]: f["properties"]["event"]
            for f in reference + candidates
        }
        reference_ids = {f["properties"]["id"] for f in reference}
        local_ids = {f["properties"]["id"] for f in local}
        same = reference_ids == local_ids
        differing += not same
        print(f"== {lat},{lon}")
        print(
            f"   containing: {sorted(f'{layer}:{code}' for layer, code in containing)}"
        )
        print(f"   sent ({len(codes)}): {','.join(codes)}")
        print(
            f"   point= {len(reference_ids)}   zone= {len(candidates)}   "
            f"local filter -> {len(local_ids)}   {'MATCH' if same else 'DIFFER'}"
        )
        for alert_id in sorted(reference_ids - local_ids):
            print(f"     missing locally : {events[alert_id]}  ({alert_id})")
        for alert_id in sorted(local_ids - reference_ids):
            print(f"     extra locally   : {events[alert_id]}  ({alert_id})")
        time.sleep(1)
    print(f"\n{len(points) - differing} of {len(points)} points match")
    return differing


# ---------------------------------------------------------------------------
# Picking points that can go wrong
# ---------------------------------------------------------------------------


def samples_in_zone(zone: Zone, n: int = 400, seed: int = 1) -> list[Point]:
    """Random points inside a dataset zone, rejection-sampled over its bbox."""
    rnd = random.Random(seed)
    x1, y1, x2, y2 = zone.bbox
    out: list[Point] = []
    for _ in range(n * 20):
        lon, lat = rnd.uniform(x1, x2), rnd.uniform(y1, y2)
        if point_in_rings(lat, lon, zone.rings):
            out.append((round(lat, 4), round(lon, 4)))
            if len(out) >= n:
                break
    return out


def samples_in_geometry(
    geometry: dict[str, Any], n: int = 200, seed: int = 2
) -> list[Point]:
    """Random points inside an alert polygon, rejection-sampled over its bbox."""
    rnd = random.Random(seed)
    outline = [p for polygon in _polygons(geometry) for p in polygon[0]]
    x1, x2 = min(p[0] for p in outline), max(p[0] for p in outline)
    y1, y2 = min(p[1] for p in outline), max(p[1] for p in outline)
    out: list[Point] = []
    for _ in range(n * 20):
        lon, lat = rnd.uniform(x1, x2), rnd.uniform(y1, y2)
        if in_geometry(lat, lon, geometry):
            out.append((round(lat, 4), round(lon, 4)))
            if len(out) >= n:
                break
    return out


def on_land(zones: ZoneSet, point: Point) -> bool:
    """In no county means over water: land zones in the shapefile reach past the coast."""
    return bool(zones.containing(point[0], point[1], layers=("county",)))


def _ugc(properties: dict[str, Any], kind: str) -> list[str]:
    codes = properties.get("geocode", {}).get("UGC", [])
    return [c for c in codes if len(c) == 6 and c[2] == kind]


Chosen = tuple[str, float, float, str, str]  # case, lat, lon, event, note


def _county_trap(
    zones: ZoneSet, zone: Zone, code: str, quiet: set[str]
) -> tuple[Point, str] | None:
    """A point in a county overlapping ``zone`` that lies in another, quiet forecast zone."""
    for point in samples_in_zone(zone, 60, seed=3):
        county = next(
            (h.code for h in zones.containing(point[0], point[1], layers=("county",))),
            None,
        )
        county_zone = zones.get("county", county) if county else None
        if not county_zone:
            continue
        for other in samples_in_zone(county_zone, 200, seed=4):
            found = [
                h.code
                for h in zones.containing(other[0], other[1], layers=("forecast",))
            ]
            if found and found[0] != code and not set(found) & quiet:
                return other, f"in {county} (overlaps {code}) but in zone {found[0]}"
    return None


def pick(zones: ZoneSet, states: list[str]) -> list[Chosen]:
    chosen: list[Chosen] = []
    for state in states:
        features = get(f"{API}/alerts/active?area={state}")["features"]
        with_polygon = [f for f in features if f.get("geometry")]
        zone_only = [f for f in features if not f.get("geometry")]
        active = {c for f in zone_only for c in _ugc(f["properties"], "Z")}
        print(
            f"{state}: {len(features)} active, {len(with_polygon)} with polygons, "
            f"{len(zone_only)} zone-only",
            file=sys.stderr,
        )
        for feature in with_polygon[:PER_CASE]:
            properties, geometry = feature["properties"], feature["geometry"]
            inside = samples_in_geometry(geometry, 1)
            if inside:
                chosen.append(
                    ("IN_POLY", *inside[0], properties["event"], "inside the polygon")
                )
            for county in _ugc(properties, "C")[:3]:
                zone = zones.get("county", county)
                if not zone:
                    continue
                outside = [
                    p
                    for p in samples_in_zone(zone, 300)
                    if not in_geometry(p[0], p[1], geometry)
                ]
                if outside:
                    note = f"in {county}, outside the polygon"
                    chosen.append(("CTY_OUT", *outside[0], properties["event"], note))
                    break
        for feature in zone_only[:PER_CASE]:
            properties = feature["properties"]
            for code in _ugc(properties, "Z")[:2]:
                if (
                    code[:2] in MARINE_AREAS
                ):  # marine product: sample at sea, no county cases
                    zone = zones.get("marine", code)
                    at_sea = (
                        [p for p in samples_in_zone(zone, 40) if not on_land(zones, p)]
                        if zone
                        else []
                    )
                    if at_sea:
                        chosen.append(
                            (
                                "IN_MARINE",
                                *at_sea[0],
                                properties["event"],
                                f"inside {code}",
                            )
                        )
                    continue
                zone = zones.get("forecast", code)
                if not zone:
                    continue
                ashore = [p for p in samples_in_zone(zone, 40) if on_land(zones, p)]
                if ashore:
                    chosen.append(
                        ("IN_ZONE", *ashore[0], properties["event"], f"inside {code}")
                    )
                trap = _county_trap(zones, zone, code, active)
                if trap:
                    chosen.append(("CTY_TRAP", *trap[0], properties["event"], trap[1]))
                break
    return chosen


# ---------------------------------------------------------------------------
# Where polygon warnings are in effect right now
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AreaActivity:
    """The polygon alerts active in one area (a state or a marine area)."""

    area: str  # UGC prefix: "TX", "GM"
    polygons: int  # active alerts that carry a polygon
    storm_based: int  # of those, the ones in STORM_BASED_EVENTS
    recent: int  # of those, the ones issued within the window
    newest: dt.datetime  # when the most recent one was issued
    events: tuple[str, ...]  # distinct event names, sorted


def polygon_activity(
    features: list[Feature], now: dt.datetime, hours: float = SCAN_HOURS
) -> list[AreaActivity]:
    """Summarise an ``alerts/active`` feature list by area, most stormy first.

    Only alerts with a polygon count. An alert whose UGC codes span several
    areas is counted in each. ``now`` must be timezone-aware; it is what
    ``recent`` is measured from, and passing it in keeps this free of the
    clock and the network so it can be used, and tested, on a saved response.
    """
    window = dt.timedelta(hours=hours)
    found: dict[str, dict[str, Any]] = {}
    for feature in features:
        if not feature.get("geometry"):
            continue
        properties = feature["properties"]
        sent = dt.datetime.fromisoformat(properties["sent"])
        event = properties["event"]
        codes = properties.get("geocode", {}).get("UGC", [])
        for area in {code[:2] for code in codes}:
            row = found.setdefault(
                area,
                {
                    "polygons": 0,
                    "storm": 0,
                    "recent": 0,
                    "newest": sent,
                    "events": set(),
                },
            )
            row["polygons"] += 1
            row["storm"] += event in STORM_BASED_EVENTS
            row["recent"] += now - sent <= window
            row["newest"] = max(row["newest"], sent)
            row["events"].add(event)
    rows = [
        AreaActivity(
            area,
            r["polygons"],
            r["storm"],
            r["recent"],
            r["newest"],
            tuple(sorted(r["events"])),
        )
        for area, r in found.items()
    ]
    return sorted(rows, key=lambda r: (-r.storm_based, -r.recent, -r.polygons, r.area))


def scan(hours: float = SCAN_HOURS) -> list[AreaActivity]:
    """Fetch the national active list and print where polygon alerts are in effect."""
    now = dt.datetime.now(dt.UTC)
    features = get(f"{API}/alerts/active")["features"]
    rows = polygon_activity(features, now, hours)
    with_polygon = sum(1 for f in features if f.get("geometry"))
    print(
        f"{len(features)} active alerts, {with_polygon} with polygons, "
        f"checked {now.astimezone():%Y-%m-%d %H:%M %Z}\n"
    )
    if rows:
        recent_title = f"last {hours:g}h"
        print(
            f"{'area':<5}{'polygons':>9}{'storm':>7}{recent_title:>9}  {'newest':<18} events"
        )
    for row in rows:
        print(
            f"{row.area:<5}{row.polygons:>9}{row.storm_based:>7}{row.recent:>9}  "
            f"{row.newest.astimezone():%m-%d %H:%M %Z}".ljust(51)
            + ", ".join(row.events)[:70]
        )
    if any(row.storm_based for row in rows):
        print("\nstorm-based warnings are in effect")
    elif rows:
        print("\nno storm-based warnings right now; the polygons are flood or similar")
    else:
        print("\nno polygon alerts are active")
    return rows


def storm_areas(rows: list[AreaActivity], limit: int = 5) -> list[str]:
    """The areas with storm-based warnings; failing that, the ones with any polygon."""
    stormy = [row.area for row in rows if row.storm_based]
    return (stormy or [row.area for row in rows])[:limit]


def busiest_areas() -> list[str]:
    """The five busiest land areas and the two busiest marine areas right now."""
    areas = get(f"{API}/alerts/active/count").get("areas", {})
    ranked = sorted(areas, key=lambda area: -areas[area])
    states = [a for a in ranked if a not in MARINE_AREAS][:5]
    states += [a for a in ranked if a in MARINE_AREAS][:2]
    print(
        "busiest now: " + ", ".join(f"{a}={areas[a]}" for a in states), file=sys.stderr
    )
    return states


def print_chosen(chosen: list[Chosen]) -> None:
    print(f"\n{'case':<9} {'lat,lon':<20} {'event':<28} note")
    for case, lat, lon, event, note in chosen:
        print(f"{case:<9} {f'{lat},{lon}':<20} {event[:28]:<28} {note}")
    print(
        "\npoints:\n  "
        + " ".join(f"{lat},{lon}" for _, lat, lon, _, _ in chosen)
        + "\n"
    )


def _invocation() -> str:
    """How this script was started, so a printed command can be pasted back as it is."""
    interpreter = Path(sys.executable)
    try:
        interpreter = interpreter.relative_to(Path.cwd())
    except ValueError:
        pass
    return f"{interpreter} {sys.argv[0]}"


def _point(text: str) -> Point:
    lat, lon = (float(value) for value in text.split(","))
    return lat, lon


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    commands = parser.add_subparsers(dest="command", required=True)
    for name, text in (
        ("run", "pick points from live alerts, then check them"),
        ("pick", "print points chosen from live alerts"),
    ):
        sub = commands.add_parser(name, help=text)
        sub.add_argument("dataset")
        sub.add_argument(
            "states", nargs="*", help="two-letter areas; default: the busiest now"
        )
        sub.add_argument(
            "--storm",
            action="store_true",
            help="with no areas named, use the ones where scan finds polygon warnings",
        )
    sub = commands.add_parser(
        "scan", help="show where polygon warnings are in effect now"
    )
    sub.add_argument(
        "dataset",
        nargs="?",
        help="optional; only used to print a ready-to-paste run command",
    )
    sub.add_argument(
        "--hours",
        type=float,
        default=SCAN_HOURS,
        help=f"how far back counts as recent (default {SCAN_HOURS:g})",
    )
    sub = commands.add_parser("check", help="check the given points")
    sub.add_argument("dataset")
    sub.add_argument("points", nargs="+", type=_point, metavar="LAT,LON")
    args = parser.parse_args()

    if args.command == "scan":
        areas = storm_areas(scan(args.hours))
        if areas:
            print(
                f"run:\n  {_invocation()} run {args.dataset or 'DATASET'} {' '.join(areas)}"
            )
        return 0

    zones = load_dataset(args.dataset)
    print(
        f"dataset: {len(zones)} zones, layers {sorted(zones.layers())}", file=sys.stderr
    )
    if args.command == "check":
        return 1 if check(zones, args.points) else 0
    states = [s.upper() for s in args.states]
    if not states and args.storm:
        states = storm_areas(scan())
        if not states:
            print(
                "no polygon alerts to aim at; using the busiest areas", file=sys.stderr
            )
    chosen = pick(zones, states or busiest_areas())
    print_chosen(chosen)
    if args.command == "pick":
        return 0
    return 1 if check(zones, [(lat, lon) for _, lat, lon, _, _ in chosen]) else 0


if __name__ == "__main__":
    sys.exit(main())
