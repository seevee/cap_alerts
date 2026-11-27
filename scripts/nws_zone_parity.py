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
    candidate  GET alerts/active?zone=<every zone in the circle>  what the local mode sends
    local      place each candidate alert relative to the point:
                 here    - it has a polygon and the point is inside it, or
                         - it has no polygon and one of its zones counts for the point
                 nearby  - within the radius, with its distance
               and compare the alerts placed here with the reference

The candidate request is the one the local mode makes: the zone list is
calculated around an apparent centre some distance from the point
(``providers/nws_offset.py``), over a circle enlarged to cover the radius
around the point, and a list longer than one request may carry is split. So
the check also shows that a request calculated away from the point still
returns everything ``point=`` does.

The local decision is the integration's own (``providers/nws_matching.py``),
so this checks the code that ships, not a copy of it. Zone-only alerts are
matched with a tolerance band, which closes the slivers simplification leaves
between zones and, near a border, also admits the neighbouring zone. So there
are three outcomes per point:

    MATCH    exactly what point= returned
    MATCH+   everything point= returned, plus alerts only the tolerance admitted
    DIFFER   something point= returned is missing, or an extra alert that
             the tolerance does not explain

The tolerance admits an extra alert in two ways, and both are its accepted
cost. A zone the point is just outside counts through the band ("extra, from
the band"). And a zone the dataset says contains the point may not contain it
on the ground when its edge is within the tolerance: the shapes are simplified
by that much, and the service decides on the shapes as surveyed ("extra, at a
zone's edge"). A point 70 m inside the simplified outline of an island can be
in the water. An extra alert is unexplained only when the point is inside the
alert's polygon, or deeper inside one of its zones than the tolerance.

It prints, per point, the zones that contain it, any the band added, how many
zones were asked about in how many requests and how far away the apparent
centre was, the counts, and each alert present on only one side with the
reason. It exits 1 if any point DIFFERs. MATCH+ is a pass.

``pick`` chooses the points from today's live alerts, by construction, so the
check is aimed at the cases that can go wrong. For each state it fetches
``alerts/active?area=XX`` (state-level, nothing personal) and builds:

    IN_POLY    inside a warning polygon                           point= returns it
    CTY_OUT    in that warning's county, outside its polygon      point= must NOT return it
    IN_ZONE    inside a zone-only product's forecast zone         point= returns it
    CTY_TRAP   in a county overlapping that zone, but lying in a
               different forecast zone with no product            point= must NOT return it
    IN_MARINE  inside a marine zone-only product's zone, at sea   point= returns it
    BAND       on land just outside that product's zone, within   point= does NOT return it;
               the zone's tolerance, in a zone with no product    local matching does (MATCH+)

BAND is the one case built to differ. It sits where the tolerance band admits
a neighbouring zone, so the expected outcome is MATCH+ with that product
listed as "extra, from the band". It shows the band doing what it was chosen
to do, and what that costs, on the day's real products.

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
import math
import random
import sys
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from custom_components.cap_alerts.const import USER_AGENT
from custom_components.cap_alerts.providers.nws_matching import (
    MARINE_AREAS,
    applicable_alerts,
    in_alert_polygon,
    matching_zones,
    place_alerts,
    request_codes,
    split_codes,
    strict_zones,
)
from custom_components.cap_alerts.providers.nws_offset import LockStore
from custom_components.cap_alerts.providers.nws_zones import (
    KM_PER_DEGREE,
    Zone,
    ZoneSet,
    distance_to_rings_km,
    haversine_km,
    point_in_rings,
)

API = "https://api.weather.gov"
RADIUS_KM, HYSTERESIS_PCT = 16.0, 50.0
PER_CASE = 2

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
# Comparing the local decision with point=
# ---------------------------------------------------------------------------


def _polygons(geometry: dict[str, Any]) -> list[list[list[list[float]]]]:
    if geometry["type"] == "MultiPolygon":
        return geometry["coordinates"]
    return [geometry["coordinates"]]


def _ids(features: list[Feature]) -> set[str]:
    return {feature["properties"]["id"] for feature in features}


def zone_alerts(codes: list[str]) -> tuple[list[Feature], int]:
    """The alerts for a list of zones, and how many requests it took.

    A long list is split as the local mode splits it. An alert that names
    zones in two parts comes back twice and is counted once, by its ID.
    """
    found: dict[str, Feature] = {}
    parts = split_codes(codes)
    for part in parts:
        for feature in get(f"{API}/alerts/active?zone={','.join(part)}")["features"]:
            found.setdefault(feature["properties"]["id"], feature)
    return list(found.values()), len(parts)


def _well_inside(zones: ZoneSet, key: tuple[str, str], lat: float, lon: float) -> bool:
    """Whether a zone that contains the point does so by more than its tolerance.

    The dataset's shapes are simplified to within each zone's tolerance, so
    nearer an edge than that the point may be on either side of the edge as
    surveyed, which is the one the service decides on.
    """
    layer, code = key
    zone = zones.get(layer, code)
    if zone is None:
        return False
    tolerance = zones.tolerance_km(code)
    return distance_to_rings_km(lat, lon, zone.rings, 2 * tolerance + 1.0) > tolerance


def check(zones: ZoneSet, points: list[Point]) -> int:
    """Compare ``point=`` with the local decision for each point. Returns how many fail.

    The decision under test is the integration's own, from
    ``providers/nws_matching.py``. Three outcomes per point:

        MATCH    the local result is exactly what point= returned
        MATCH+   everything point= returned is there, and each extra alert
                 was admitted only by the tolerance: through the band, or
                 through a zone whose edge is within the tolerance of the
                 point. The accepted cost of simplified shapes
        DIFFER   an alert point= returned is missing, or an extra one for a
                 polygon the point is inside or a zone it is well inside.
                 Either is a real difference
    """
    failing = 0
    for lat, lon in points:
        strict = strict_zones(zones, lat, lon)
        counted = matching_zones(zones, lat, lon)
        lock = LockStore().lock_for(
            lat, lon, radius_km=RADIUS_KM, hysteresis_pct=HYSTERESIS_PCT, today=0
        )
        centre = lock.centre
        codes = request_codes(zones, *centre, lock.request_radius_km(RADIUS_KM))
        reference = get(f"{API}/alerts/active?point={lat},{lon}")["features"]
        candidates, requests = zone_alerts(codes)
        placed = place_alerts(zones, candidates, lat, lon, RADIUS_KM)
        local = _ids([where.feature for where in placed if where.here])
        nearby = sum(not where.here for where in placed)
        local_strict = _ids(applicable_alerts(candidates, lat, lon, strict))
        # The zones the point is inside by more than their tolerance. Only
        # these contain it whatever the simplification did to their edges.
        deep = frozenset(key for key in strict if _well_inside(zones, key, lat, lon))
        local_deep = _ids(applicable_alerts(candidates, lat, lon, deep))
        wanted = _ids(reference)
        events = {
            f["properties"]["id"]: f["properties"]["event"]
            for f in reference + candidates
        }
        missing = wanted - local
        extra = local - wanted
        unexplained = extra & local_deep
        at_an_edge = (extra & local_strict) - unexplained
        if missing or unexplained:
            verdict = "DIFFER"
            failing += 1
        else:
            verdict = "MATCH+" if extra else "MATCH"
        print(f"== {lat},{lon}")
        print(f"   containing: {sorted(f'{layer}:{code}' for layer, code in strict)}")
        if counted != strict:
            added = sorted(f"{layer}:{code}" for layer, code in counted - strict)
            print(f"   within the band: {added}")
        if deep != strict:
            edged = sorted(f"{layer}:{code}" for layer, code in strict - deep)
            print(f"   at the edge of: {edged}")
        print(
            f"   asked about {len(codes)} zones in {requests} request(s), from a "
            f"centre {haversine_km(lat, lon, *centre):.0f} km away"
        )
        print(
            f"   point= {len(wanted)}   zone= {len(candidates)}   "
            f"here -> {len(local)} (strict {len(local_strict)})   "
            f"nearby within {RADIUS_KM:g} km -> {nearby}   {verdict}"
        )
        for alert_id in sorted(missing):
            print(f"     missing locally        : {events[alert_id]}  ({alert_id})")
        for alert_id in sorted(unexplained):
            print(f"     extra, well inside     : {events[alert_id]}  ({alert_id})")
        for alert_id in sorted(at_an_edge):
            print(f"     extra, at a zone's edge: {events[alert_id]}  ({alert_id})")
        for alert_id in sorted(extra - unexplained - at_an_edge):
            print(f"     extra, from the band   : {events[alert_id]}  ({alert_id})")
        time.sleep(1)
    print(f"\n{len(points) - failing} of {len(points)} points match")
    return failing


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


def samples_in_alert_polygon(
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
        if in_alert_polygon(lat, lon, geometry):
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


def _band_point(
    zones: ZoneSet,
    zone: Zone,
    code: str,
    quiet: set[str],
    tries: int = 400,
    seed: int = 5,
) -> tuple[Point, str] | None:
    """A land point just outside ``zone``, inside its tolerance band, in a quiet zone.

    Walks out from random vertices of the zone's outline by a third to two
    thirds of the tolerance. Kept only if it is strictly outside ``zone``, in
    a county, and in a forecast zone with no zone-only product of its own, so
    that the only reason for the product to apply there is the band.
    """
    tolerance = zones.tolerance_km(code)
    if tolerance <= 0.0:
        return None
    rnd = random.Random(seed)
    outline = zone.rings[0]
    for _ in range(tries):
        i = 2 * rnd.randrange(len(outline) // 2)
        lon, lat = outline[i], outline[i + 1]
        step, bearing = (
            rnd.uniform(0.33, 0.67) * tolerance,
            rnd.uniform(0.0, 2.0 * math.pi),
        )
        lat += step * math.cos(bearing) / KM_PER_DEGREE
        lon += step * math.sin(bearing) / (KM_PER_DEGREE * math.cos(math.radians(lat)))
        point = (round(lat, 4), round(lon, 4))
        if point_in_rings(point[0], point[1], zone.rings):
            continue
        away = distance_to_rings_km(point[0], point[1], zone.rings, tolerance)
        if away > 0.8 * tolerance or not on_land(zones, point):
            continue
        inside = [
            h.code for h in zones.containing(point[0], point[1], layers=("forecast",))
        ]
        if inside and not set(inside) & quiet:
            return point, f"in {inside[0]}, {away * 1000:.0f} m outside {code}"
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
            inside = samples_in_alert_polygon(geometry, 1)
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
                    if not in_alert_polygon(p[0], p[1], geometry)
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
                band = _band_point(zones, zone, code, active)
                if band:
                    chosen.append(("BAND", *band[0], properties["event"], band[1]))
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
