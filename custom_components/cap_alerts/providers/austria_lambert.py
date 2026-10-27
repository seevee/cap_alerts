"""MGI / Austria Lambert (EPSG:31287) → WGS84, without pyproj (issue #300).

GeoSphere Austria's Warn API answers in projected metres, and every consumer of
``CAPAlert.geometry`` expects GeoJSON longitude/latitude. Two steps get there:

1. The Lambert Conformal Conic 2SP inverse on the Bessel 1841 ellipsoid, which
   yields MGI geographic coordinates. Exact to the float: 0.0000 m worst case
   against pyproj over a 510-point grid covering Austria.
2. The MGI → WGS84 datum shift, EPSG:1618 ("MGI to WGS 84 (3)", the transform
   pyproj picks first), a seven-parameter Helmert through geocentric XYZ.
   Skipping it leaves the result up to 121 m off; with it the worst case is
   0.74 m, under the transform's own stated 1.5 m accuracy.

The measurement is ``scripts/geosphere_projection_probe.py``. One fixed CRS is
all this module does, which is why it is forty lines of arithmetic rather than
a dependency.
"""

from __future__ import annotations

import math
from typing import Any

# EPSG:31287: +proj=lcc +lat_0=47.5 +lon_0=13.3333333 +lat_1=49 +lat_2=46
# +x_0=400000 +y_0=400000 +ellps=bessel
_BESSEL_A = 6377397.155
_BESSEL_INV_F = 299.1528128
_LON0 = 13 + 1 / 3
_X0 = _Y0 = 400000.0

# EPSG:1618, position-vector convention: metres, arc-seconds, ppm.
_HELMERT = (577.326, 90.129, 463.919, 5.137, 1.474, 5.297, 2.4232)
_WGS84_A = 6378137.0
_WGS84_INV_F = 298.257223563


def _eccentricity(inv_f: float) -> float:
    f = 1 / inv_f
    return math.sqrt(2 * f - f * f)


_E = _eccentricity(_BESSEL_INV_F)


def _m(phi: float) -> float:
    return math.cos(phi) / math.sqrt(1 - (_E * math.sin(phi)) ** 2)


def _t(phi: float) -> float:
    s = _E * math.sin(phi)
    return float(math.tan(math.pi / 4 - phi / 2) / ((1 - s) / (1 + s)) ** (_E / 2))


_PHI0, _PHI1, _PHI2 = (math.radians(v) for v in (47.5, 49.0, 46.0))
_N = (math.log(_m(_PHI1)) - math.log(_m(_PHI2))) / (
    math.log(_t(_PHI1)) - math.log(_t(_PHI2))
)
_F = _m(_PHI1) / (_N * _t(_PHI1) ** _N)
_RHO0 = _BESSEL_A * _F * _t(_PHI0) ** _N


def _lcc_inverse(x: float, y: float) -> tuple[float, float]:
    """Austria Lambert metres → MGI geographic radians ``(lon, lat)``."""
    dx, dy = x - _X0, _RHO0 - (y - _Y0)
    rho = math.copysign(math.hypot(dx, dy), _N)
    t = (rho / (_BESSEL_A * _F)) ** (1 / _N)
    phi = math.pi / 2 - 2 * math.atan(t)
    # Converges in four or five rounds anywhere in Austria; ten is the bound.
    for _ in range(10):
        s = _E * math.sin(phi)
        nxt = math.pi / 2 - 2 * math.atan(t * ((1 - s) / (1 + s)) ** (_E / 2))
        converged = abs(nxt - phi) < 1e-12
        phi = nxt
        if converged:
            break
    return math.atan2(dx, dy) / _N + math.radians(_LON0), phi


def _to_ecef(lon: float, lat: float, a: float, e2: float) -> tuple[float, ...]:
    nu = a / math.sqrt(1 - e2 * math.sin(lat) ** 2)
    return (
        nu * math.cos(lat) * math.cos(lon),
        nu * math.cos(lat) * math.sin(lon),
        nu * (1 - e2) * math.sin(lat),
    )


def _from_ecef(
    x: float, y: float, z: float, a: float, e2: float
) -> tuple[float, float]:
    p = math.hypot(x, y)
    lat = math.atan2(z, p * (1 - e2))
    for _ in range(10):
        nu = a / math.sqrt(1 - e2 * math.sin(lat) ** 2)
        lat = math.atan2(z + e2 * nu * math.sin(lat), p)
    return math.atan2(y, x), lat


def to_wgs84(x: float, y: float) -> tuple[float, float]:
    """One EPSG:31287 position → WGS84 ``(lon, lat)`` degrees."""
    lon, lat = _lcc_inverse(x, y)
    tx, ty, tz, rx, ry, rz, ds = _HELMERT
    rx, ry, rz = (math.radians(v / 3600) for v in (rx, ry, rz))
    k = 1 + ds * 1e-6
    bx, by, bz = _to_ecef(lon, lat, _BESSEL_A, _E**2)
    wx = tx + k * (bx - rz * by + ry * bz)
    wy = ty + k * (rz * bx + by - rx * bz)
    wz = tz + k * (-ry * bx + rx * by + bz)
    lon, lat = _from_ecef(wx, wy, wz, _WGS84_A, _eccentricity(_WGS84_INV_F) ** 2)
    return math.degrees(lon), math.degrees(lat)


def reproject_coordinates(coordinates: Any) -> Any:
    """Reproject a GeoJSON ``coordinates`` array of any nesting depth.

    The nesting is kept as published, so a MultiPolygon's holes stay holes.
    A position is the innermost list of numbers; anything after x and y
    (an elevation) is dropped, since nothing downstream reads one.
    """
    if (
        isinstance(coordinates, list)
        and len(coordinates) >= 2
        and all(isinstance(v, (int, float)) for v in coordinates[:2])
    ):
        lon, lat = to_wgs84(float(coordinates[0]), float(coordinates[1]))
        return [round(lon, 6), round(lat, 6)]
    if isinstance(coordinates, list):
        return [reproject_coordinates(c) for c in coordinates]
    raise ValueError(f"not a GeoJSON coordinate array: {coordinates!r}")
