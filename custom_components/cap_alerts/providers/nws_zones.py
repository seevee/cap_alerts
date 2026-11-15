"""Resolve a point and radius to nearby NWS zone codes, entirely locally.

Pure Python, standard library only, no Home Assistant imports. Written for a
GPS or tracker scope that queries ``alerts/active?zone=`` with the returned
codes instead of sending ``point=lat,lon`` (issue #316).

Data: a file produced by ``scripts/build_zone_dataset.py`` from NWS's public
zone and county shapefiles, one zone per line (see ``ZoneSet.from_lines``). It
is read once into a :class:`ZoneSet`, which keeps every ring as a flat
``array('f')`` of ``lon, lat`` pairs: 4.4 MB resident for the national
four-layer set at the default simplification, versus ~40 MB as lists of tuples.

All distances are kilometres, as in Home Assistant core; convert for display
at the config boundary only.
"""

from __future__ import annotations

import hashlib
import json
from array import array
from collections.abc import Iterable
from dataclasses import dataclass
from math import asin, cos, pi, radians, sin, sqrt
from typing import Any

EARTH_RADIUS_KM = 6371.0088
KM_PER_DEGREE = EARTH_RADIUS_KM * pi / 180.0  # along a meridian

# The dataset layout this module reads (see ``ZoneSet.from_lines``).
DATASET_FORMAT = 2


class ZoneDataError(ValueError):
    """The dataset is cut short, damaged, or not in the layout this module reads."""


@dataclass(frozen=True, slots=True)
class Zone:
    """One NWS zone: a UGC code, its layer, a lon/lat bbox and flat rings."""

    code: str  # "NYZ072", "NYC061", "ANZ338"
    layer: str  # "forecast" | "county" | "fire" | "marine"
    bbox: tuple[float, float, float, float]  # lon_min, lat_min, lon_max, lat_max
    rings: tuple[array[float], ...]  # each array('f'): lon0, lat0, lon1, lat1, ...


@dataclass(frozen=True, slots=True)
class ZoneHit:
    """One resolved zone and how far the point is from it."""

    code: str
    layer: str
    distance_km: float  # 0.0 when the point is inside


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in kilometres."""
    p1, p2 = radians(lat1), radians(lat2)
    dp, dl = p2 - p1, radians(lon2 - lon1)
    a = sin(dp / 2) ** 2 + cos(p1) * cos(p2) * sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_KM * asin(sqrt(a))


def _dlon(lon: float, lon0: float) -> float:
    d = lon - lon0
    if d > 180.0:
        return d - 360.0
    if d < -180.0:
        return d + 360.0
    return d


def point_in_rings(lat: float, lon: float, rings: Iterable[array[float]]) -> bool:
    """Even-odd ray casting over every ring of a shape.

    Holes and multi-part shapes come out right without inspecting ring
    orientation: a point inside a hole is crossed an even number of times.
    """
    inside = False
    for ring in rings:
        n = len(ring) // 2
        xj, yj = ring[2 * n - 2], ring[2 * n - 1]
        for i in range(n):
            xi, yi = ring[2 * i], ring[2 * i + 1]
            if (yi > lat) != (yj > lat) and lon < (xj - xi) * (lat - yi) / (
                yj - yi
            ) + xi:
                inside = not inside
            xj, yj = xi, yi
    return inside


def distance_to_rings_km(
    lat: float, lon: float, rings: Iterable[array[float]], cutoff: float
) -> float:
    """Shortest distance from the point to any ring edge, in km; ``inf`` if every edge is beyond ``cutoff``.

    Uses a local equirectangular projection centred on the point: error under
    0.1 % within ~80 km at mid-latitudes, far inside what a zone filter needs.
    The caller handles the inside case (distance 0). ``cutoff`` only prunes work;
    it is never returned as a distance.
    """
    k = cos(radians(lat))
    best = float("inf")
    limit = cutoff
    for ring in rings:
        n = len(ring) // 2
        # project the ring once
        xs = [EARTH_RADIUS_KM * radians(_dlon(ring[2 * i], lon)) * k for i in range(n)]
        ys = [EARTH_RADIUS_KM * radians(ring[2 * i + 1] - lat) for i in range(n)]
        for i in range(n):
            j = (i + 1) % n
            ax, ay, bx, by = xs[i], ys[i], xs[j], ys[j]
            # the segment cannot matter if its own bbox is farther than the best so far, or the cutoff
            lim = min(limit, best)
            if (
                min(ax, bx) > lim
                or max(ax, bx) < -lim
                or min(ay, by) > lim
                or max(ay, by) < -lim
            ):
                continue
            dx, dy = bx - ax, by - ay
            length2 = dx * dx + dy * dy
            t = (
                0.0
                if length2 == 0.0
                else max(0.0, min(1.0, -(ax * dx + ay * dy) / length2))
            )
            px, py = ax + t * dx, ay + t * dy
            d = sqrt(px * px + py * py)
            best = min(best, d)
    return best


def bbox_distance_km(
    lat: float, lon: float, bbox: tuple[float, float, float, float]
) -> float:
    """Lower bound on the distance from the point to anything inside the bbox.

    Four zones (the western Aleutians and three Bering Sea marine zones) have
    parts on both sides of the antimeridian. NWS splits their rings at 180
    degrees, so each ring is sound, but the min/max box over all of them runs
    from about -180 to +180 and wrapping its edges would put every point
    outside it. Such a box bounds latitude only.
    """
    y1 = EARTH_RADIUS_KM * radians(bbox[1] - lat)
    y2 = EARTH_RADIUS_KM * radians(bbox[3] - lat)
    dy = max(y1, 0.0, -y2)
    if bbox[2] - bbox[0] > 180.0:
        return dy
    k = cos(radians(lat))
    x1 = EARTH_RADIUS_KM * radians(_dlon(bbox[0], lon)) * k
    x2 = EARTH_RADIUS_KM * radians(_dlon(bbox[2], lon)) * k
    dx = max(x1, 0.0, -x2)
    return sqrt(dx * dx + dy * dy)


class _ZoneBuilder:
    """Turns dataset records into zones one at a time.

    Each record's coordinate lists become a compact array as soon as the
    record arrives and are then dropped, so reading a file line by line never
    holds more than one zone in its parsed form. Collecting the records first
    and converting them afterwards put the whole file in memory as Python
    lists, and the peak was the same 100 MB as parsing a single document.
    """

    def __init__(self) -> None:
        self.count = 0
        self._zones: list[Zone] = []
        self._by_key: dict[tuple[str, str], Zone] = {}
        self._aliases: list[tuple[str, str, str]] = []

    def add(self, record: dict[str, Any]) -> None:
        self.count += 1
        code, layer = record["code"], record["layer"]
        # A fire zone identical to the forecast zone of the same code. It can
        # come before that zone in the file, so it waits for the end.
        if "alias" in record:
            self._aliases.append((code, layer, record["alias"]))
            return
        rings = tuple(
            array("f", (c for pt in ring for c in pt)) for ring in record["rings"]
        )
        x0, y0, x1, y1 = record["bbox"]
        zone = Zone(code, layer, (x0, y0, x1, y1), rings)
        self._zones.append(zone)
        self._by_key[(layer, code)] = zone

    def finish(self) -> list[Zone]:
        for code, layer, alias in self._aliases:
            src = self._by_key[(alias, code)]
            # The same array objects, no copy.
            self._zones.append(Zone(code, layer, src.bbox, src.rings))
        return self._zones


class ZoneSet:
    """The national zone set, resident as compact arrays, with point/radius resolution."""

    def __init__(
        self,
        zones: Iterable[Zone],
        *,
        sources: Iterable[dict[str, Any]] = (),
        epsilon_deg: float = 0.0,
        tail_epsilon_deg: float | None = None,
        tail_states: Iterable[str] = (),
    ) -> None:
        self._zones: tuple[Zone, ...] = tuple(zones)
        self._index: dict[tuple[str, str], Zone] = {
            (z.layer, z.code): z for z in self._zones
        }
        # One entry per shapefile the dataset was built from: file, md5, layer.
        self.sources: tuple[dict[str, Any], ...] = tuple(sources)
        # How far simplification was allowed to move a boundary, in degrees:
        # ``epsilon_deg`` everywhere, ``tail_epsilon_deg`` for codes that begin
        # with one of ``tail_states`` (Alaska, whose zones are far larger).
        self.epsilon_deg = epsilon_deg
        self.tail_epsilon_deg = tail_epsilon_deg
        self.tail_states: frozenset[str] = frozenset(tail_states)

    # -- construction ----------------------------------------------------

    @classmethod
    def from_lines(cls, lines: Iterable[bytes]) -> ZoneSet:
        """Read the dataset ``scripts/build_zone_dataset.py`` writes, a line at a time.

        Three kinds of line, each one JSON object::

            {"format": 2, "zones": 11634, "lines": 11636, "meta": {...}}     first
            {"code": "NYZ072", "layer": "forecast", "bbox": [...], "rings": [...]}
            {"eof": true, "zones": 11634, "lines": 11636, "sha256": "..."}   last

        One zone per line keeps the peak memory near the resident size: parsing
        the same data as a single document builds every coordinate pair as a
        Python list first, 98 MB at the peak against 7 MB this way.

        The price of lines is that a file cut short is still a run of valid
        lines. So the header says how many zones and lines to expect, and the
        last line repeats both and carries the SHA-256 of every byte before it.
        A missing last line, a count that disagrees, a hash that does not match
        or anything after the last line raises :class:`ZoneDataError`; a
        partial set is never returned.

        Pass an open binary file (``gzip.open(path, "rb")``). Do this in an
        executor: it is CPU-bound for a few hundred milliseconds.
        """
        digest = hashlib.sha256()
        build = _ZoneBuilder()
        header: dict[str, Any] | None = None
        footer: dict[str, Any] | None = None
        seen = 0
        for line in lines:
            if not line.strip():
                continue
            if footer is not None:
                raise ZoneDataError("data after the end-of-file line")
            seen += 1
            try:
                record = json.loads(line)
            except ValueError as err:
                raise ZoneDataError(f"line {seen} is not JSON") from err
            if header is None:
                if (
                    not isinstance(record, dict)
                    or record.get("format") != DATASET_FORMAT
                ):
                    raise ZoneDataError(f"not a format {DATASET_FORMAT} zone dataset")
                header = record
            elif record.get("eof") is True:
                footer = record
                continue
            else:
                build.add(record)
            digest.update(line)
        if header is None:
            raise ZoneDataError("empty dataset")
        if footer is None:
            raise ZoneDataError("no end-of-file line: the dataset is cut short")
        for name, found in (("zones", build.count), ("lines", seen)):
            if not header.get(name) == footer.get(name) == found:
                raise ZoneDataError(
                    f"{name}: header says {header.get(name)}, "
                    f"end-of-file line says {footer.get(name)}, found {found}"
                )
        if footer.get("sha256") != digest.hexdigest():
            raise ZoneDataError("checksum mismatch: the dataset is damaged")
        return cls._built(build, header.get("meta", {}))

    @classmethod
    def from_json(cls, data: str | bytes) -> ZoneSet:
        """Parse the single-document layout: ``{"meta": {...}, "zones": [...]}``.

        The first builds were written this way. Kept for the parity scripts
        and for reading those files; the shipped dataset uses ``from_lines``.
        """
        doc = json.loads(data)
        build = _ZoneBuilder()
        for record in doc["zones"]:
            build.add(record)
        return cls._built(build, doc.get("meta", {}))

    @classmethod
    def _built(cls, build: _ZoneBuilder, meta: dict[str, Any]) -> ZoneSet:
        return cls(
            build.finish(),
            sources=meta.get("sources", ()),
            epsilon_deg=float(meta.get("epsilon_deg", 0.0)),
            tail_epsilon_deg=meta.get("tail_epsilon_deg"),
            tail_states=meta.get("tail_states") or (),
        )

    # -- introspection ---------------------------------------------------

    def tolerance_km(self, code: str) -> float:
        """How far the stored boundary of zone ``code`` may lie from the true one.

        Each ring was simplified alone, so two neighbours can each pull their
        shared border toward themselves and leave a sliver that neither
        contains. A point in such a sliver is at most this far from the zone
        it really belongs to. Taken along a meridian, the longer of the two
        directions a degree can be measured in, so it is never too small.
        """
        tail = self.tail_epsilon_deg is not None and code[:2] in self.tail_states
        degrees = self.tail_epsilon_deg if tail else self.epsilon_deg
        return (degrees or 0.0) * KM_PER_DEGREE

    @property
    def max_tolerance_km(self) -> float:
        """The largest ``tolerance_km`` any zone in the set can have."""
        return max(self.epsilon_deg, self.tail_epsilon_deg or 0.0) * KM_PER_DEGREE

    def __len__(self) -> int:
        return len(self._zones)

    def layers(self) -> set[str]:
        return {z.layer for z in self._zones}

    def get(self, layer: str, code: str) -> Zone | None:
        """One zone by layer and code, or None."""
        return self._index.get((layer, code))

    def resident_bytes(self) -> int:
        """Approximate size of the coordinate arrays (the dominant term); shared arrays counted once."""
        seen: set[int] = set()
        total = 0
        for z in self._zones:
            for ring in z.rings:
                if id(ring) not in seen:
                    seen.add(id(ring))
                    total += ring.buffer_info()[1] * ring.itemsize
        return total

    # -- resolution ------------------------------------------------------

    def resolve(
        self,
        lat: float,
        lon: float,
        *,
        radius_km: float = 16.0,
        max_zones: int = 5,
        layers: Iterable[str] = ("forecast", "county", "fire", "marine"),
    ) -> list[ZoneHit]:
        """Zones within ``radius_km`` of the point, nearest first, at most ``max_zones`` per layer.

        A zone containing the point has distance 0 and therefore always survives
        the cap. The test is "does a disk of radius_km around the point touch the
        polygon", not a centroid distance, so large or irregular zones are handled
        correctly.
        """
        wanted = set(layers)
        per_layer: dict[str, list[ZoneHit]] = {layer: [] for layer in wanted}
        for z in self._zones:
            if z.layer not in wanted:
                continue
            if bbox_distance_km(lat, lon, z.bbox) > radius_km:
                continue
            if point_in_rings(lat, lon, z.rings):
                per_layer[z.layer].append(ZoneHit(z.code, z.layer, 0.0))
                continue
            d = distance_to_rings_km(lat, lon, z.rings, radius_km)
            if d <= radius_km:
                per_layer[z.layer].append(ZoneHit(z.code, z.layer, d))
        out: list[ZoneHit] = []
        for layer in sorted(per_layer):
            hits = sorted(per_layer[layer], key=lambda h: (h.distance_km, h.code))
            out.extend(hits[:max_zones])
        out.sort(key=lambda h: (h.distance_km, h.layer, h.code))
        return out

    def codes(self, lat: float, lon: float, **kwargs: Any) -> list[str]:
        """Just the UGC codes, ready for ``alerts/active?zone=``, de-duplicated across layers."""
        seen: set[str] = set()
        out: list[str] = []
        for h in self.resolve(lat, lon, **kwargs):
            if h.code not in seen:
                seen.add(h.code)
                out.append(h.code)
        return out

    def containing(
        self,
        lat: float,
        lon: float,
        layers: Iterable[str] = ("forecast", "county", "fire", "marine"),
        *,
        land_sea_rule: bool = True,
    ) -> list[ZoneHit]:
        """Every zone that contains the point: the set /points/ would return, computed locally.

        Use this set, not the capped nearby set, when deciding whether a zone-based alert applies.

        ``land_sea_rule`` reproduces how NWS places a point: land zone polygons in the
        shapefiles reach past the coastline, so a point at sea can fall inside a forecast
        zone as well as a marine zone. A point inside a county is on land and gets the
        land layers; a point in no county is at sea and gets the marine layer only.
        """
        wanted = set(layers)
        # The county layer decides land versus sea, so it is consulted even
        # when the caller did not ask for county codes back.
        looked_up = wanted | {"county"} if land_sea_rule else wanted
        hits = self.resolve(lat, lon, radius_km=0.0, max_zones=10_000, layers=looked_up)
        if land_sea_rule:
            on_land = any(h.layer == "county" for h in hits)
            drop = {"marine"} if on_land else {"forecast", "fire", "county"}
            hits = [h for h in hits if h.layer not in drop]
        return [h for h in hits if h.layer in wanted]


def needs_recompute(
    lat: float,
    lon: float,
    last: tuple[float, float] | None,
    *,
    radius_km: float,
    hysteresis_pct: float = 50.0,
) -> bool:
    """True once the device has moved more than ``hysteresis_pct`` % of the radius since ``last``.

    Keeps a moving tracker from recomputing (and re-querying NWS) on every
    position update; the zone set only changes when the device has moved far
    enough to matter.
    """
    if last is None:
        return True
    return haversine_km(lat, lon, last[0], last[1]) > radius_km * hysteresis_pct / 100.0
