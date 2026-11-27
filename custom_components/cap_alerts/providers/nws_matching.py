"""Which NWS alerts are within the radius of a point, decided locally (issue #316).

``alerts/active?point=lat,lon`` has NWS decide which alerts apply, which means
sending it the coordinates. The local mode asks about zones instead. This
module supplies the list of zones to ask about and, when the reply arrives,
places each alert in it relative to the point:

* **here**: the alert applies at the point. This group is what
  ``point=`` would have returned, and the parity script holds it to that.
* **nearby**: the alert does not apply at the point but lies within the
  radius. It is kept with its distance.
* **beyond the radius**: discarded.

An alert is discarded only when it can be shown to lie beyond the radius.
One that cannot be placed (it names a zone the dataset does not have, or
nothing in it can be read) is kept as nearby with no distance: an alert
shown with less detail is the cheaper mistake than one withheld.

**The zone list** is every zone within a circle, with no limit on their
number. The circle is not the user's: it is the enlarged circle around the
apparent centre that ``providers/nws_offset.py`` gives, so that the list says
nothing precise about the true position. ``request_codes`` therefore takes
the centre and radius it is given and must never be passed, or look up, the
true position.

**Applying at the point.** An alert that carries a polygon applies when the
point is inside the polygon. That test is exact and has no tolerance. An
alert with no polygon names zones. It applies when one of its zones counts
for the point, and "counts" is where the care goes:

**The tolerance band.** The dataset's boundaries were simplified, each zone
alone, so two neighbours can each pull a shared border toward themselves and
leave a sliver neither contains. A point in a sliver would match no zone and
lose every zone-only alert for the place it is really in. So a zone counts
when the point is within that zone's own simplification tolerance of it
(about 567 m; about 2.2 km in Alaska), not only when strictly inside. Since
the true boundary lies within the tolerance of the stored one, that closes
every sliver by construction. The price is that a point close to a border
also receives its neighbour's zone-only alerts. Measured on the April 2026
dataset at 4,000 random land points in each region, the band adds at least
one zone at 7.6% of them, in the lower 48 and in Alaska alike: a neighbouring
land zone at 7.3% and 6.0%, and a marine zone at 0.3% and 1.6%. An alert for
the zone next door is the cheaper mistake.

**Land and sea.** NWS places a point either on land or at sea: in a county,
the forecast, fire and county zones apply; in no county, the marine zone does.
The same band applies to that decision. Land zones count when a county is
within tolerance of the point. Marine zones count when the point is in no
county, or is within tolerance of the edge of the county it is in, since
either way it may really be on the water.

``strict_zones`` gives the unbanded answer, the one that reproduces ``point=``
exactly. The parity check uses both: nothing ``point=`` returns may be missing
from the banded result, and anything extra must be explained by the band.

**Distance.** A polygon alert's distance is from the point to the nearest
edge of its polygon. A zone-only alert's distance is to the nearest of the
zones it names, zero inside one. Distances come from the zone resolver, whose
flat projection is accurate to about 0.1% within 80 km and less so farther
out, so "within the radius" is exact to about one part in a hundred at 160
km. ``request_margin_km`` widens the request by enough that this never
leaves a zone out of the list.

No Home Assistant imports and no I/O: everything here is a function of its
arguments.
"""

from __future__ import annotations

from array import array
from dataclasses import dataclass
from math import degrees, radians, tan
from typing import Any, Final

from .gps import point_in_polygon
from .nws_zones import EARTH_RADIUS_KM, ZoneSet, distance_to_rings_km

ZoneKey = tuple[str, str]  # (layer, code)

LAND_LAYERS = frozenset({"forecast", "fire", "county"})

# The most zone codes to put in one ``alerts/active?zone=`` request. On 20
# November 2026 the service answered a request naming 426 zones and failed,
# with HTTP 500, on one naming 462. That is an internal error and not a
# documented limit, so the working figure is kept well below it.
REQUEST_LIMIT: Final = 300

_MAX_RADIUS_KM: Final = 100_000.0

# UGC prefixes of the marine areas. Their zone codes (ANZ338, LMZ846) are
# served under the API's forecast type but live on the dataset's marine layer.
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
_TYPE_TO_LAYER = {
    "forecast": "forecast",
    "public": "forecast",
    "county": "county",
    "fire": "fire",
    "marine": "marine",
}


def layer_for(code: str, url_type: str) -> str:
    """The dataset layer a zone code belongs to, given the type in its API URL."""
    if code[:2] in MARINE_AREAS:
        return "marine"
    fallback = "county" if len(code) == 6 and code[2] == "C" else "forecast"
    return _TYPE_TO_LAYER.get(url_type, fallback)


def affected_zones(properties: Any) -> set[ZoneKey]:
    """The (layer, code) of each zone an alert names.

    Read from ``affectedZones`` URLs such as ``.../zones/fire/COZ240``, because
    437 codes exist as both a fire and a forecast zone with different shapes
    and the bare UGC cannot say which is meant. Bare UGC is the fallback.

    The record comes from the network. Anything in it that is not of the
    expected form is passed over, and never raises: a record with no readable
    zone names no zone.
    """
    found: set[ZoneKey] = set()
    if not isinstance(properties, dict):
        return found
    urls = properties.get("affectedZones")
    for url in urls if isinstance(urls, list) else ():
        if not isinstance(url, str):
            continue
        parts = url.rstrip("/").split("/")
        if len(parts) >= 2 and parts[-2] in _TYPE_TO_LAYER and parts[-1]:
            found.add((layer_for(parts[-1], parts[-2]), parts[-1]))
    if not found:
        geocode = properties.get("geocode")
        codes = geocode.get("UGC") if isinstance(geocode, dict) else None
        for code in codes if isinstance(codes, list) else ():
            if isinstance(code, str) and code:
                found.add((layer_for(code, ""), code))
    return found


def _is_ring(value: Any) -> bool:
    """Whether ``value`` is a ring the polygon test can use: three or more
    ``[lon, lat]`` pairs of ordinary numbers."""
    if not isinstance(value, list) or len(value) < 3:
        return False
    for point in value:
        if not isinstance(point, (list, tuple)) or len(point) < 2:
            return False
        for coordinate in point[:2]:
            if isinstance(coordinate, bool) or not isinstance(coordinate, (int, float)):
                return False
            if not -360.0 <= coordinate <= 360.0:  # also rejects NaN and infinity
                return False
    return True


def _alert_shapes(geometry: Any) -> list[list[Any]] | None:
    """The polygons of an alert's geometry, each a list of rings, outline first.

    ``None`` when the alert has no polygon, or has one that cannot be read.
    """
    if not isinstance(geometry, dict):
        return None
    kind, coordinates = geometry.get("type"), geometry.get("coordinates")
    if kind == "Polygon":
        polygons = [coordinates]
    elif kind == "MultiPolygon" and isinstance(coordinates, list) and coordinates:
        polygons = coordinates
    else:
        return None
    shapes: list[list[Any]] = []
    for polygon in polygons:
        if not isinstance(polygon, list) or not polygon:
            return None
        if not all(_is_ring(ring) for ring in polygon):
            return None
        shapes.append(polygon)
    return shapes


def _inside(lat: float, lon: float, shapes: list[list[Any]]) -> bool:
    for rings in shapes:  # ring 0 is the outline, the rest are holes
        if point_in_polygon(lat, lon, rings[0]) and not any(
            point_in_polygon(lat, lon, hole) for hole in rings[1:]
        ):
            return True
    return False


def in_alert_polygon(lat: float, lon: float, geometry: Any) -> bool | None:
    """Whether the point is inside an alert's polygon.

    ``None`` when the alert has no polygon, or has one that cannot be read.
    The alert is then decided by its zones, as one issued without a polygon
    is. A polygon that cannot be read cannot rule the point out, and an alert
    kept on the strength of its zones is the cheaper mistake.
    """
    shapes = _alert_shapes(geometry)
    return None if shapes is None else _inside(lat, lon, shapes)


def _edge_distance_km(
    lat: float, lon: float, shapes: list[list[Any]], cutoff: float
) -> float:
    """From a point outside an alert's polygons to their nearest edge.

    Every ring is measured, holes included: a point in a hole is outside the
    alert, and the hole's edge is the nearest part of it. Infinity when every
    edge is beyond ``cutoff``.
    """
    rings = [
        array("d", [value for point in ring for value in point[:2]])
        for polygon in shapes
        for ring in polygon
    ]
    return distance_to_rings_km(lat, lon, rings, cutoff)


def strict_zones(zones: ZoneSet, lat: float, lon: float) -> frozenset[ZoneKey]:
    """The zones that contain the point, land or sea, with no band.

    This is what ``point=`` decides on, and what parity is measured against.
    """
    return frozenset((hit.layer, hit.code) for hit in zones.containing(lat, lon))


def matching_zones(zones: ZoneSet, lat: float, lon: float) -> frozenset[ZoneKey]:
    """The zones that count for the point: within tolerance, land-sea rule banded.

    Always a superset of ``strict_zones`` for the same point.

    Raises ``ValueError`` for a dataset with no county layer, as
    ``ZoneSet.containing`` does. Without counties no point is on land, so no
    forecast, fire or county zone would ever count, and every zone-only land
    alert would be dropped without any error to show for it.
    """
    if "county" not in zones.layers():
        raise ValueError("the land-sea rule needs a dataset with the county layer")
    hits = zones.resolve(lat, lon, radius_km=zones.max_tolerance_km, max_zones=10_000)
    within = [hit for hit in hits if hit.distance_km <= zones.tolerance_km(hit.code)]
    inside_county = [
        hit for hit in within if hit.layer == "county" and hit.distance_km == 0.0
    ]
    land = any(hit.layer == "county" for hit in within)
    sea = not inside_county or any(
        _near_own_edge(zones, lat, lon, hit.code) for hit in inside_county
    )
    return frozenset(
        (hit.layer, hit.code)
        for hit in within
        if (sea if hit.layer == "marine" else land)
    )


def _near_own_edge(zones: ZoneSet, lat: float, lon: float, county: str) -> bool:
    """Whether a point inside ``county`` is within tolerance of that county's edge."""
    zone = zones.get("county", county)
    if zone is None:  # pragma: no cover - the code came from this ZoneSet
        return False
    tolerance = zones.tolerance_km(county)
    return distance_to_rings_km(lat, lon, zone.rings, tolerance) <= tolerance


def _radius(value: Any, what: str = "radius_km") -> float:
    """A radius in km as a float, or ``ValueError``."""
    # The comparison refuses NaN and infinity. The ceiling is far above any
    # request the offset module can ask for (a radius of half the Earth's
    # circumference, enlarged), so that an absurd setting still gets an
    # answer: every zone there is.
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not 0.0 <= value <= _MAX_RADIUS_KM
    ):
        raise ValueError(
            f"{what} must be a number from 0 to {_MAX_RADIUS_KM:g}, not {value!r}"
        )
    return float(value)


def _position(lat: Any, lon: Any) -> tuple[float, float]:
    """A latitude and longitude as floats, or ``ValueError``.

    A position that is not a number would match no zone and place no alert,
    and the poll would come back empty with no error to show for it.
    """
    if (
        isinstance(lat, bool)
        or isinstance(lon, bool)
        or not isinstance(lat, (int, float))
        or not isinstance(lon, (int, float))
        or not -90.0 <= lat <= 90.0
        or not -180.0 <= lon <= 180.0
    ):
        raise ValueError(f"not a position: {lat!r}, {lon!r}")
    return float(lat), float(lon)


def request_margin_km(lat: float, radius_km: float) -> float:
    """How much wider than ``radius_km`` to ask, so that no zone is left out.

    The resolver measures in a flat projection centred on the point it is
    asked about. The same zone is therefore measured a little differently
    from the true position and from the apparent centre, and a zone that the
    one calls just inside the user's radius the other can call just outside
    the request. East-west distances are the ones that go wrong, by the ratio
    of the cosines of the two latitudes, which over a span of latitude ``d``
    is about ``tan(latitude) * d``. Half of that can fall on each of the two
    measurements, so the margin is the whole of it, taken at the far edge of
    the circle. Measured on the April 2026 dataset the largest shortfall was
    0.9% of the request radius (Alaska, 298 km); this allows 13% there and 5%
    at 40 degrees north.

    Close to a pole the projection gives no usable bound, and the margin is
    the radius itself.
    """
    lat, _lon = _position(lat, 0.0)
    radius = _radius(radius_km)
    span = radius / EARTH_RADIUS_KM
    edge = abs(lat) + 2.0 * degrees(span)
    if edge >= 85.0:
        return radius
    return min(radius, radius * tan(radians(edge)) * span)


def request_codes(
    zones: ZoneSet, lat: float, lon: float, radius_km: float
) -> list[str]:
    """The zone codes to ask ``alerts/active?zone=`` for, sorted, no repeats.

    Every zone within ``radius_km`` of the point given, whatever their
    number, and a little more: the dataset's largest tolerance, because a
    zone within tolerance of the true position counts for it even at a radius
    of zero and must be asked about, and a margin for the resolver's
    projection (``request_margin_km``). The point is the apparent centre and
    the radius the enlarged one, both from ``providers/nws_offset.py``. This
    function is given nothing else, so the list cannot depend on the true
    position.

    Sorted by code, not by distance. Nearest-first would say which zones the
    centre is closest to. Sorted, the list says only which zones.

    Raises ``ValueError`` for a position or a radius that is not a usable
    number.
    """
    lat, lon = _position(lat, lon)
    radius = _radius(radius_km) + zones.max_tolerance_km
    reach = radius + request_margin_km(lat, min(radius, _MAX_RADIUS_KM))
    hits = zones.resolve(lat, lon, radius_km=reach, max_zones=len(zones) + 1)
    return sorted({hit.code for hit in hits})


def split_codes(codes: list[str], limit: int = REQUEST_LIMIT) -> list[list[str]]:
    """The codes in as few lists of at most ``limit`` as will hold them.

    The lists are of nearly equal length and keep the order given. The caller
    sends one request for each, merges the replies and counts an alert once
    by its ID, since an alert that names zones in two lists comes back twice.
    """
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError(f"limit must be a whole number from 1 up, not {limit!r}")
    if not codes:
        return []
    parts = -(-len(codes) // limit)  # rounded up, in whole numbers
    size, extra = divmod(len(codes), parts)
    out: list[list[str]] = []
    start = 0
    for part in range(parts):
        end = start + size + (1 if part < extra else 0)
        out.append(codes[start:end])
        start = end
    return out


def alert_applies(
    feature: Any, lat: float, lon: float, counted: frozenset[ZoneKey]
) -> bool:
    """Whether one ``alerts/active`` feature applies to the point.

    ``counted`` is ``matching_zones`` (or ``strict_zones``) for the same point.
    A feature that is not a record at all applies to nothing. It never raises,
    so one malformed record cannot cost the poll its other alerts.
    """
    if not isinstance(feature, dict):
        return False
    inside = in_alert_polygon(lat, lon, feature.get("geometry"))
    if inside is not None:
        return inside
    return bool(affected_zones(feature.get("properties")) & counted)


def applicable_alerts(
    features: list[dict[str, Any]], lat: float, lon: float, counted: frozenset[ZoneKey]
) -> list[dict[str, Any]]:
    """The features that apply to the point, in the order given."""
    return [f for f in features if alert_applies(f, lat, lon, counted)]


@dataclass(frozen=True, slots=True)
class Placed:
    """One alert within the radius, and where it lies relative to the point."""

    feature: dict[str, Any]
    here: bool  # it applies at the point: what ``point=`` would return
    # Kilometres from the point: 0.0 when ``here``, and ``None`` when the
    # alert could not be measured and is kept for want of proof that it is
    # beyond the radius.
    distance_km: float | None


def nearby_zones(
    zones: ZoneSet, lat: float, lon: float, radius_km: float
) -> dict[ZoneKey, float]:
    """Every zone within the radius of the point, and its distance: 0.0 inside."""
    lat, lon = _position(lat, lon)
    hits = zones.resolve(
        lat, lon, radius_km=_radius(radius_km), max_zones=len(zones) + 1
    )
    return {(hit.layer, hit.code): hit.distance_km for hit in hits}


def place_alert(
    zones: ZoneSet,
    feature: Any,
    lat: float,
    lon: float,
    *,
    counted: frozenset[ZoneKey],
    nearby: dict[ZoneKey, float],
    radius_km: float,
) -> Placed | None:
    """Where one ``alerts/active`` feature lies, or ``None`` if beyond the radius.

    ``counted`` is ``matching_zones`` and ``nearby`` is ``nearby_zones`` for
    the same point and radius. ``here`` is decided exactly as
    ``alert_applies`` decides it. A feature that is not a record at all is
    nowhere. It never raises, so one malformed record cannot cost the poll
    its other alerts.
    """
    if not isinstance(feature, dict):
        return None
    shapes = _alert_shapes(feature.get("geometry"))
    if shapes is not None:
        if _inside(lat, lon, shapes):
            return Placed(feature, True, 0.0)
        away = _edge_distance_km(lat, lon, shapes, radius_km)
        return Placed(feature, False, away) if away <= radius_km else None
    named = affected_zones(feature.get("properties"))
    if named & counted:
        return Placed(feature, True, 0.0)
    reached = [nearby[key] for key in named if key in nearby]
    if reached:
        return Placed(feature, False, min(reached))
    if named and all(zones.get(layer, code) is not None for layer, code in named):
        return None  # every zone it names is known, and none is within the radius
    return Placed(feature, False, None)


def place_alerts(
    zones: ZoneSet, features: Any, lat: float, lon: float, radius_km: float
) -> list[Placed]:
    """The alerts within the radius of the point, in the order given.

    ``features`` is the merged ``features`` of the replies. Raises
    ``ValueError`` for a position or a radius that is not a usable number,
    and for a dataset with no county layer, as ``matching_zones`` does.
    """
    lat, lon = _position(lat, lon)
    radius = _radius(radius_km)
    counted = matching_zones(zones, lat, lon)
    nearby = nearby_zones(zones, lat, lon, radius)
    placed = []
    for feature in features if isinstance(features, list) else ():
        where = place_alert(
            zones, feature, lat, lon, counted=counted, nearby=nearby, radius_km=radius
        )
        if where is not None:
            placed.append(where)
    return placed
