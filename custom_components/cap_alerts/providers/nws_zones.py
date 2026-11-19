"""Resolve a point and radius to nearby NWS zone codes, entirely locally.

Pure Python, standard library only, no Home Assistant imports. Written for a
GPS or tracker scope that queries ``alerts/active?zone=`` with the returned
codes instead of sending ``point=lat,lon`` (issue #316).

Data: a file produced by ``scripts/build_zone_dataset.py`` from NWS's public
zone and county shapefiles, one zone per line (see ``ZoneSet.from_lines``). It
is read once into a :class:`ZoneSet`, which keeps every ring as a flat
``array('f')`` of ``lon, lat`` pairs: about 5 MB resident for the national
four-layer set, a small fraction of what lists of tuples take.

All distances are kilometres, as in Home Assistant core; convert for display
at the config boundary only.
"""

from __future__ import annotations

import hashlib
import json
import re
from array import array
from collections.abc import Iterable
from dataclasses import dataclass
from math import asin, cos, pi, radians, sin, sqrt
from typing import Any

EARTH_RADIUS_KM = 6371.0088
KM_PER_DEGREE = EARTH_RADIUS_KM * pi / 180.0  # along a meridian

# The dataset layout this module reads (see ``ZoneSet.from_lines``).
DATASET_FORMAT = 2

# Ceilings on what a dataset may contain. The file is trusted only after its
# hash has been checked, but the parser does not lean on that: it assumes the
# bytes could be anything, and refuses whatever does not look like a zone
# dataset of plausible size. Each ceiling is several times the real figure
# (April 2026: 13.8 MB, 11,651 zones, 664,766 vertices), so a genuine dataset
# has room to grow and a hostile one cannot ask for unbounded memory or time.
# The check script holds a build to half of every ceiling.
MAX_DATASET_BYTES = 64 * 1024 * 1024  # uncompressed
MAX_LINE_BYTES = 1024 * 1024
MAX_ZONES = 100_000
MAX_VERTICES = 4_000_000
MAX_RINGS_PER_ZONE = 2_000
MAX_SOURCES = 32
MAX_TAIL_STATES = 60
# Simplification tolerance, in degrees. Matching widens every zone by this
# much, so a dataset claiming a huge one would make every zone match every
# point. A tenth of a degree is 11 km, five times the coarsest in use.
MAX_EPSILON_DEG = 0.1

LAYERS = frozenset({"forecast", "county", "fire", "marine"})
_CODE = re.compile(r"[A-Z]{2}[CZ][0-9]{3}")
_STATE = re.compile(r"[A-Z]{2}")
_SHA256 = re.compile(r"[0-9a-f]{64}")


class ZoneDataError(ValueError):
    """The dataset is cut short, damaged, or not in the layout this module reads."""


def _reject_constant(name: str) -> float:
    # ``json`` accepts NaN and Infinity unless told otherwise. A NaN coordinate
    # compares false against everything and would quietly break containment.
    raise ZoneDataError(f"{name} is not a number a dataset may contain")


def _parse(line: bytes | str, where: str) -> Any:
    """One JSON value, or ``ZoneDataError``. Never any other exception."""
    try:
        return json.loads(line, parse_constant=_reject_constant)
    except ZoneDataError:
        raise
    except (ValueError, RecursionError) as err:
        # ValueError covers malformed JSON, bad UTF-8 and integers too long to
        # convert; RecursionError is what deeply nested brackets produce.
        raise ZoneDataError(f"{where} is not JSON") from err


def _number(value: Any, low: float, high: float, what: str) -> float:
    """A finite real number within ``low..high``. Booleans are not numbers here."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ZoneDataError(f"{what} is not a number")
    # Compared before it is converted: an integer of a few hundred digits is
    # valid JSON, and ``float()`` raises OverflowError on it. NaN and the
    # infinities fail the comparison too.
    if not low <= value <= high:
        raise ZoneDataError(f"{what} is out of range")
    return float(value)


def _count(value: Any, ceiling: int, what: str) -> int:
    """A whole number from 0 to ``ceiling``."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ZoneDataError(f"{what} is not a whole number")
    if not 0 <= value <= ceiling:
        raise ZoneDataError(f"{what} is out of range")
    return value


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
    """Turns dataset records into zones one at a time, checking each as it goes.

    Each record's coordinate lists become a compact array as soon as the
    record arrives and are then dropped, so reading a file line by line never
    holds more than one zone in its parsed form. Collecting the records first
    and converting them afterwards put the whole file in memory as Python
    lists, and the peak was the same 100 MB as parsing a single document.

    Nothing is taken on trust. Every field is checked for type and range, the
    counts are held under the module's ceilings, and the stored bounding box
    must be exactly the extent of the rings: the box is what decides whether
    a zone is even looked at, so a wrong one would hide the zone.
    """

    def __init__(self) -> None:
        self.count = 0
        self._vertices = 0
        self._zones: list[Zone] = []
        self._by_key: dict[tuple[str, str], Zone] = {}
        self._seen: set[tuple[str, str]] = set()
        self._aliases: list[tuple[str, str]] = []

    def add(self, record: Any) -> None:
        if not isinstance(record, dict):
            raise ZoneDataError("a zone record is not an object")
        self.count += 1
        if self.count > MAX_ZONES:
            raise ZoneDataError(f"more than {MAX_ZONES} zones")
        code, layer = record.get("code"), record.get("layer")
        if not isinstance(code, str) or not _CODE.fullmatch(code):
            raise ZoneDataError("a zone code is not of the form AAZ999")
        if not isinstance(layer, str) or layer not in LAYERS:
            raise ZoneDataError(f"{code}: unknown layer")
        if (layer, code) in self._seen:
            raise ZoneDataError(f"{layer} {code} appears twice")
        self._seen.add((layer, code))
        # A fire zone identical to the forecast zone of the same code. It can
        # come before that zone in the file, so it waits for the end.
        if "alias" in record:
            if record["alias"] != "forecast" or layer != "fire":
                raise ZoneDataError(
                    f"{code}: only a fire zone may alias a forecast zone"
                )
            self._aliases.append((code, layer))
            return
        rings = self._rings(code, record.get("rings"))
        bbox = self._bbox(code, record.get("bbox"), rings)
        zone = Zone(code, layer, bbox, rings)
        self._zones.append(zone)
        self._by_key[(layer, code)] = zone

    def _rings(self, code: str, value: Any) -> tuple[array[float], ...]:
        if not isinstance(value, list) or not 1 <= len(value) <= MAX_RINGS_PER_ZONE:
            raise ZoneDataError(f"{code}: rings missing, empty or too many")
        rings = []
        for ring in value:
            if not isinstance(ring, list) or len(ring) < 3:
                raise ZoneDataError(f"{code}: a ring has fewer than three points")
            self._vertices += len(ring)
            if self._vertices > MAX_VERTICES:
                raise ZoneDataError(f"more than {MAX_VERTICES} vertices")
            # Checked a ring at a time with builtins, not a point at a time:
            # half a million points pass through here on every load.
            if not all(type(point) is list and len(point) == 2 for point in ring):
                raise ZoneDataError(f"{code}: a point is not a [lon, lat] pair")
            lons = [point[0] for point in ring]
            lats = [point[1] for point in ring]
            # ``type(...) in``, not ``isinstance``: True and False are ints.
            if not all(type(v) in (float, int) for v in lons + lats):
                raise ZoneDataError(f"{code}: a coordinate is not a number")
            if min(lons) < -180.0 or max(lons) > 180.0:
                raise ZoneDataError(f"{code}: a longitude is out of range")
            if min(lats) < -90.0 or max(lats) > 90.0:
                raise ZoneDataError(f"{code}: a latitude is out of range")
            flat = array("f", (v for point in ring for v in point))
            rings.append(flat)
        return tuple(rings)

    @staticmethod
    def _bbox(
        code: str, value: Any, rings: tuple[array[float], ...]
    ) -> tuple[float, float, float, float]:
        if not isinstance(value, list) or len(value) != 4:
            raise ZoneDataError(f"{code}: bbox is not four numbers")
        x0 = _number(value[0], -180.0, 180.0, f"{code}: bbox")
        y0 = _number(value[1], -90.0, 90.0, f"{code}: bbox")
        x1 = _number(value[2], -180.0, 180.0, f"{code}: bbox")
        y1 = _number(value[3], -90.0, 90.0, f"{code}: bbox")
        lons = [v for ring in rings for v in ring[0::2]]
        lats = [v for ring in rings for v in ring[1::2]]
        extent = (min(lons), min(lats), max(lons), max(lats))
        # The rings are held as float32, so compare at that precision.
        stored = tuple(array("f", (x0, y0, x1, y1)))
        if stored != extent:
            raise ZoneDataError(f"{code}: bbox does not match the extent of its rings")
        # The float32 values, the ones the rings hold. The float64 originals
        # can sit a fraction of a metre inside the ring's own extreme vertex,
        # and the box is what prunes a zone before its rings are looked at.
        return (stored[0], stored[1], stored[2], stored[3])

    def finish(self) -> list[Zone]:
        for code, layer in self._aliases:
            src = self._by_key.get(("forecast", code))
            if src is None:
                raise ZoneDataError(
                    f"{layer} {code} aliases a forecast zone that is absent"
                )
            # The same array objects, no copy.
            self._zones.append(Zone(code, layer, src.bbox, src.rings))
        return self._zones


def _rounding(meta: Any) -> float:
    """One step of the rounding the stored coordinates were given, in degrees.

    The builder rounds every vertex to ``decimals`` places after simplifying,
    which can move a boundary a little further than the simplification
    tolerance alone allows. Zero when the dataset does not say.
    """
    decimals = meta.get("decimals")
    if decimals is None:
        return 0.0
    return 10.0 ** -_count(decimals, 9, "decimals")


def _tolerances(meta: Any) -> tuple[float, float | None, frozenset[str]]:
    """The simplification tolerances a dataset declares, checked and bounded."""
    epsilon = _number(meta.get("epsilon_deg", 0.0), 0.0, MAX_EPSILON_DEG, "epsilon_deg")
    tail = meta.get("tail_epsilon_deg")
    tail_epsilon = (
        None
        if tail is None
        else _number(tail, 0.0, MAX_EPSILON_DEG, "tail_epsilon_deg")
    )
    states = meta.get("tail_states") or []
    if not isinstance(states, list) or len(states) > MAX_TAIL_STATES:
        raise ZoneDataError("tail_states is not a short list")
    for state in states:
        if not isinstance(state, str) or not _STATE.fullmatch(state):
            raise ZoneDataError(
                "tail_states holds something that is not a state prefix"
            )
    return epsilon, tail_epsilon, frozenset(states)


def _sources(meta: Any) -> tuple[dict[str, Any], ...]:
    sources = meta.get("sources") or []
    if not isinstance(sources, list) or len(sources) > MAX_SOURCES:
        raise ZoneDataError("sources is not a short list")
    if not all(isinstance(source, dict) for source in sources):
        raise ZoneDataError("a source entry is not an object")
    return tuple(sources)


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
        rounding_deg: float = 0.0,
    ) -> None:
        self._zones: tuple[Zone, ...] = tuple(zones)
        self._index: dict[tuple[str, str], Zone] = {
            (z.layer, z.code): z for z in self._zones
        }
        self._layers: frozenset[str] = frozenset(z.layer for z in self._zones)
        # One entry per shapefile the dataset was built from: file, md5, layer.
        self.sources: tuple[dict[str, Any], ...] = tuple(sources)
        # How far simplification was allowed to move a boundary, in degrees:
        # ``epsilon_deg`` everywhere, ``tail_epsilon_deg`` for codes that begin
        # with one of ``tail_states`` (Alaska, whose zones are far larger).
        self.epsilon_deg = epsilon_deg
        self.tail_epsilon_deg = tail_epsilon_deg
        # One step of the rounding applied to stored vertices, in degrees.
        self.rounding_deg = rounding_deg
        self.tail_states: frozenset[str] = frozenset(tail_states)

    # -- construction ----------------------------------------------------

    @classmethod
    def from_lines(cls, lines: Iterable[bytes]) -> ZoneSet:
        """Read the dataset ``scripts/build_zone_dataset.py`` writes, a line at a time.

        Three kinds of line, each one JSON object::

            {"format": 2, "zones": 11651, "lines": 11653, "meta": {...}}     first
            {"code": "NYZ072", "layer": "forecast", "bbox": [...], "rings": [...]}
            {"eof": true, "zones": 11651, "lines": 11653, "sha256": "..."}   last

        One zone per line keeps the peak memory near the resident size: parsing
        the same data as a single document builds every coordinate pair as a
        Python list first, 98 MB at the peak against 7 MB this way.

        The price of lines is that a file cut short is still a run of valid
        lines. So the header says how many zones and lines to expect, and the
        last line repeats both and carries the SHA-256 of every byte before it.
        A missing last line, a count that disagrees, a hash that does not match
        or anything after the last line raises :class:`ZoneDataError`; a
        partial set is never returned.

        Beyond that, nothing in the file is taken on trust. Every line is
        held to a length ceiling and the whole to a size ceiling as it is
        read; every field is checked for type and range (codes, layers,
        coordinates, counts, the declared tolerances); NaN and Infinity are
        refused; each stored bounding box must equal the extent of its rings;
        no zone may appear twice; and whatever is wrong, the only exception
        that leaves this method is :class:`ZoneDataError`.

        Pass an open binary file (``gzip.open(path, "rb")``). Do this in an
        executor: it is CPU-bound for a few hundred milliseconds.
        """
        digest = hashlib.sha256()
        build = _ZoneBuilder()
        header: dict[str, Any] | None = None
        footer: dict[str, Any] | None = None
        seen = 0
        size = 0
        for line in lines:
            # Sizes first, before the line is looked at: these two ceilings are
            # what bound the memory and time a hostile file can demand.
            if len(line) > MAX_LINE_BYTES:
                raise ZoneDataError(f"a line is longer than {MAX_LINE_BYTES} bytes")
            size += len(line)
            if size > MAX_DATASET_BYTES:
                raise ZoneDataError(
                    f"the dataset is larger than {MAX_DATASET_BYTES} bytes"
                )
            if not line.strip():
                continue
            if footer is not None:
                raise ZoneDataError("data after the end-of-file line")
            seen += 1
            record = _parse(line, f"line {seen}")
            if header is None:
                header = cls._header(record)
            elif isinstance(record, dict) and record.get("eof") is True:
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
            if not header[name] == footer.get(name) == found:
                raise ZoneDataError(
                    f"{name}: header says {header[name]}, "
                    f"end-of-file line says {footer.get(name)!r}, found {found}"
                )
        checksum = footer.get("sha256")
        if not isinstance(checksum, str) or not _SHA256.fullmatch(checksum):
            raise ZoneDataError("the end-of-file line carries no SHA-256")
        if checksum != digest.hexdigest():
            raise ZoneDataError("checksum mismatch: the dataset is damaged")
        return cls._built(build, header["meta"])

    @staticmethod
    def _header(record: Any) -> dict[str, Any]:
        """The first line, checked: the format, both counts and the manifest."""
        if not isinstance(record, dict) or record.get("format") != DATASET_FORMAT:
            raise ZoneDataError(f"not a format {DATASET_FORMAT} zone dataset")
        zones = _count(record.get("zones"), MAX_ZONES, "header zones")
        lines = _count(record.get("lines"), MAX_ZONES + 2, "header lines")
        # One line per zone, plus this one and the end-of-file line.
        if lines != zones + 2:
            raise ZoneDataError("header lines is not zones plus two")
        meta = record.get("meta", {})
        if not isinstance(meta, dict):
            raise ZoneDataError("header meta is not an object")
        return {"zones": zones, "lines": lines, "meta": meta}

    @classmethod
    def from_json(cls, data: str | bytes) -> ZoneSet:
        """Parse the single-document layout: ``{"meta": {...}, "zones": [...]}``.

        The first builds were written this way. Kept for the parity scripts
        and for reading those files; the shipped dataset uses ``from_lines``.
        It parses the whole document at once, so it has none of the size
        ceilings ``from_lines`` enforces while reading, and is not for a file
        that arrived over a network. Every record still gets the same checks.
        """
        doc = _parse(data, "the document")
        if not isinstance(doc, dict) or not isinstance(doc.get("zones"), list):
            raise ZoneDataError("not a zone dataset document")
        meta = doc.get("meta", {})
        if not isinstance(meta, dict):
            raise ZoneDataError("meta is not an object")
        build = _ZoneBuilder()
        for record in doc["zones"]:
            build.add(record)
        return cls._built(build, meta)

    @classmethod
    def _built(cls, build: _ZoneBuilder, meta: dict[str, Any]) -> ZoneSet:
        epsilon, tail_epsilon, tail_states = _tolerances(meta)
        return cls(
            build.finish(),
            sources=_sources(meta),
            epsilon_deg=epsilon,
            tail_epsilon_deg=tail_epsilon,
            tail_states=tail_states,
            rounding_deg=_rounding(meta),
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
        return self._with_rounding(degrees or 0.0) * KM_PER_DEGREE

    def _with_rounding(self, degrees: float) -> float:
        """A tolerance plus one step of the stored rounding.

        Simplification keeps every original vertex within the tolerance of
        the simplified line, and the vertices of that line are then rounded
        and stored at reduced precision, which moves it by up to a step more.
        Without the step the bound would be the simplification's, not the
        file's. A dataset that records no tolerance has none.
        """
        return degrees + self.rounding_deg if degrees > 0.0 else 0.0

    @property
    def max_tolerance_km(self) -> float:
        """The largest ``tolerance_km`` any zone in the set can have."""
        largest = max(self.epsilon_deg, self.tail_epsilon_deg or 0.0)
        return self._with_rounding(largest) * KM_PER_DEGREE

    def __len__(self) -> int:
        return len(self._zones)

    def layers(self) -> set[str]:
        return set(self._layers)

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
        if land_sea_rule and "county" not in self._layers:
            # Without counties every point would read as at sea and lose its
            # land zones, with nothing to say why.
            raise ValueError("the land-sea rule needs a dataset with the county layer")
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
