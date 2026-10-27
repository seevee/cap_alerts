#!/usr/bin/env python3
"""Measure a stdlib EPSG:31287 → WGS84 inverse against pyproj (#300).

GeoSphere's Warn API returns MGI / Austria Lambert metres. This checks the
two halves a stdlib port would need: the Lambert Conformal Conic 2SP inverse
on Bessel 1841, and the MGI → WGS84 Helmert shift (EPSG:1618, "MGI to WGS 84
(3)", the transform pyproj picks first). It prints the worst error over a grid
covering Austria with and without the datum step, so the plan can decide
whether the shift is worth carrying.

pyproj is NOT a project dependency. Install it in a throwaway venv to run
this; without it the script prints the stdlib results only.
"""

from __future__ import annotations

import math

# EPSG:31287 parameters (pyproj: +proj=lcc +lat_0=47.5 +lon_0=13.3333333
# +lat_1=49 +lat_2=46 +x_0=400000 +y_0=400000 +ellps=bessel)
A = 6377397.155
INV_F = 299.1528128
LAT0, LON0, LAT1, LAT2 = 47.5, 13 + 1 / 3, 49.0, 46.0
X0 = Y0 = 400000.0

# EPSG:1618, position-vector convention: metres, arc-seconds, ppm
HELMERT = (577.326, 90.129, 463.919, 5.137, 1.474, 5.297, 2.4232)
WGS84_A, WGS84_INV_F = 6378137.0, 298.257223563


def _lcc_constants() -> tuple[float, float, float, float]:
    f = 1 / INV_F
    e = math.sqrt(2 * f - f * f)

    def m(phi: float) -> float:
        return math.cos(phi) / math.sqrt(1 - (e * math.sin(phi)) ** 2)

    def t(phi: float) -> float:
        s = e * math.sin(phi)
        return math.tan(math.pi / 4 - phi / 2) / ((1 - s) / (1 + s)) ** (e / 2)

    p0, p1, p2 = (math.radians(v) for v in (LAT0, LAT1, LAT2))
    n = (math.log(m(p1)) - math.log(m(p2))) / (math.log(t(p1)) - math.log(t(p2)))
    big_f = m(p1) / (n * t(p1) ** n)
    rho0 = A * big_f * t(p0) ** n
    return e, n, big_f, rho0


def lcc_inverse(x: float, y: float) -> tuple[float, float]:
    """Austria Lambert metres → MGI geographic degrees (lon, lat)."""
    e, n, big_f, rho0 = _lcc_constants()
    dx, dy = x - X0, rho0 - (y - Y0)
    rho = math.copysign(math.hypot(dx, dy), n)
    theta = math.atan2(dx, dy)
    t = (rho / (A * big_f)) ** (1 / n)
    phi = math.pi / 2 - 2 * math.atan(t)
    for _ in range(10):
        s = e * math.sin(phi)
        nxt = math.pi / 2 - 2 * math.atan(t * ((1 - s) / (1 + s)) ** (e / 2))
        if abs(nxt - phi) < 1e-12:
            phi = nxt
            break
        phi = nxt
    return math.degrees(theta / n) + LON0, math.degrees(phi)


def _to_ecef(lon: float, lat: float, a: float, inv_f: float) -> tuple[float, ...]:
    f = 1 / inv_f
    e2 = 2 * f - f * f
    la, ph = math.radians(lon), math.radians(lat)
    nu = a / math.sqrt(1 - e2 * math.sin(ph) ** 2)
    return (
        nu * math.cos(ph) * math.cos(la),
        nu * math.cos(ph) * math.sin(la),
        nu * (1 - e2) * math.sin(ph),
    )


def _from_ecef(
    x: float, y: float, z: float, a: float, inv_f: float
) -> tuple[float, float]:
    f = 1 / inv_f
    e2 = 2 * f - f * f
    p = math.hypot(x, y)
    ph = math.atan2(z, p * (1 - e2))
    for _ in range(10):
        nu = a / math.sqrt(1 - e2 * math.sin(ph) ** 2)
        ph = math.atan2(z + e2 * nu * math.sin(ph), p)
    return math.degrees(math.atan2(y, x)), math.degrees(ph)


def mgi_to_wgs84(lon: float, lat: float) -> tuple[float, float]:
    tx, ty, tz, rx, ry, rz, ds = HELMERT
    rx, ry, rz = (math.radians(v / 3600) for v in (rx, ry, rz))
    k = 1 + ds * 1e-6
    x, y, z = _to_ecef(lon, lat, A, INV_F)
    xw = tx + k * (x - rz * y + ry * z)
    yw = ty + k * (rz * x + y - rx * z)
    zw = tz + k * (-ry * x + rx * y + z)
    return _from_ecef(xw, yw, zw, WGS84_A, WGS84_INV_F)


def _metres(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    dy = (lat2 - lat1) * 111_320
    dx = (lon2 - lon1) * 111_320 * math.cos(math.radians(lat1))
    return math.hypot(dx, dy)


def main() -> None:
    # Spec's Schwechat ring vertex and the live Wien-Innere Stadt vertex.
    for label, (x, y) in {
        "schwechat": (640566, 472707),
        "innere_stadt": (625350, 482209),
    }.items():
        mgi = lcc_inverse(x, y)
        print(f"{label}: MGI {mgi[0]:.6f},{mgi[1]:.6f}  WGS84 {mgi_to_wgs84(*mgi)}")

    grid = [
        (x, y)
        for x in range(110_000, 700_001, 20_000)
        for y in range(270_000, 590_001, 20_000)
    ]
    try:
        import pyproj  # optional ground truth
    except ImportError:
        print("pyproj not installed, no comparison")
        return
    tr = pyproj.Transformer.from_crs(31287, 4326, always_xy=True)
    tr_mgi = pyproj.Transformer.from_crs(31287, 4312, always_xy=True)
    worst_lcc = worst_shift = worst_none = 0.0
    for x, y in grid:
        ref = tr.transform(x, y)
        ref_mgi = tr_mgi.transform(x, y)
        mgi = lcc_inverse(x, y)
        worst_lcc = max(worst_lcc, _metres(*ref_mgi, *mgi))
        worst_shift = max(worst_shift, _metres(*ref, *mgi_to_wgs84(*mgi)))
        worst_none = max(worst_none, _metres(*ref, *mgi))
    print(f"grid points: {len(grid)}")
    print(f"LCC inverse vs pyproj (MGI geographic): worst {worst_lcc:.4f} m")
    print(f"LCC + Helmert vs pyproj WGS84:          worst {worst_shift:.3f} m")
    print(f"LCC, no datum shift, vs pyproj WGS84:   worst {worst_none:.1f} m")


if __name__ == "__main__":
    main()
