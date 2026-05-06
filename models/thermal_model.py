"""Build a 2D thermal-exposure raster from concentric isodose ring polygons.

Inputs:
  - 4 ring polygons (outermost = lowest fluence) for one city/band, EPSG:4326.
  - Ring fluence J_i [MJ/m^2] (from Acta Astronautica 216 (2024) Table 1).
  - Pulse duration t_pulse [s] used to map fluence -> instantaneous power flux
    (square-pulse assumption; for asteroid airbursts the true pulse is closer
    to a damped Gaussian over Y^(1/3)-scaled seconds, but the square-pulse
    average is what every wildfire-ignition lookup table uses).

Output: a metric (UTM) raster of fluence [MJ/m^2] and power [kW/m^2].

Method (purely radial, since rings are concentric circles by construction):
  1. Project rings + center to local UTM.
  2. Compute mean radius r_i of each ring.
  3. Interpolate log(J) linearly in r between adjacent rings (== exponential
     decay segments, the analytic form for a point source with Beer-Lambert
     atmospheric attenuation).
  4. Past the outermost ring: extend the (Severe -> Serious) tail.
  5. Inside the innermost ring: clamp to the innermost fluence (the dead zone
     is suppressed by the fire model, not by the thermal field).
"""
from __future__ import annotations
from dataclasses import dataclass

import numpy as np
from pyproj import CRS, Transformer
from shapely.geometry import Polygon
from shapely.ops import transform as shp_transform

from drivers.kml import CityDamage


@dataclass
class ThermalField:
    crs: CRS
    transform_affine: tuple   # rasterio Affine (a,b,c,d,e,f)
    fluence_mj_m2: np.ndarray
    power_kw_m2: np.ndarray
    center_xy: tuple[float, float]
    ring_radii_m: dict[str, float]
    pulse_seconds: float


def utm_crs_for_lonlat(lon: float, lat: float) -> CRS:
    zone = int((lon + 180) / 6) + 1
    epsg = 32600 + zone if lat >= 0 else 32700 + zone
    return CRS.from_epsg(epsg)


def _polygon_radius_m(poly: Polygon, cx: float, cy: float) -> float:
    xy = np.asarray(poly.exterior.coords)
    return float(np.mean(np.hypot(xy[:, 0] - cx, xy[:, 1] - cy)))


def build_thermal_field(damage: CityDamage,
                        pixel_m: float = 100.0,
                        pad_m: float = 30_000.0,
                        pulse_seconds: float = 10.0) -> ThermalField:
    sample = damage.rings[0].polygon.centroid
    dst_crs = utm_crs_for_lonlat(sample.x, sample.y)
    fwd = Transformer.from_crs(CRS.from_epsg(4326), dst_crs, always_xy=True).transform

    proj_polys = [shp_transform(fwd, r.polygon) for r in damage.rings]
    inner = proj_polys[-1]
    cx, cy = inner.centroid.x, inner.centroid.y

    radii_m = {r.name: _polygon_radius_m(p, cx, cy)
               for r, p in zip(damage.rings, proj_polys)}

    ordered = sorted(
        [(radii_m[r.name], r.fluence_mj_m2) for r in damage.rings])
    rs = np.array([o[0] for o in ordered])      # ascending radius
    js = np.array([o[1] for o in ordered])      # descending fluence
    log_js = np.log(js)

    # tail slope past outermost ring (using outermost two)
    dlogj_dr_tail = (log_js[-1] - log_js[-2]) / (rs[-1] - rs[-2])

    half = rs[-1] + pad_m
    x0, x1 = cx - half, cx + half
    y0, y1 = cy - half, cy + half
    nx = int(np.ceil((x1 - x0) / pixel_m))
    ny = int(np.ceil((y1 - y0) / pixel_m))
    xs = x0 + (np.arange(nx) + 0.5) * pixel_m
    ys = y1 - (np.arange(ny) + 0.5) * pixel_m
    XX, YY = np.meshgrid(xs, ys)
    R = np.hypot(XX - cx, YY - cy)

    log_j = np.interp(R, rs, log_js, left=log_js[0], right=np.nan)
    tail = R > rs[-1]
    log_j[tail] = log_js[-1] + dlogj_dr_tail * (R[tail] - rs[-1])
    inner_mask = R < rs[0]
    log_j[inner_mask] = log_js[0]

    fluence = np.exp(log_j).astype(np.float32)
    power = (fluence * 1000.0 / pulse_seconds).astype(np.float32)

    affine = (pixel_m, 0.0, x0, 0.0, -pixel_m, y1)
    return ThermalField(
        crs=dst_crs,
        transform_affine=affine,
        fluence_mj_m2=fluence,
        power_kw_m2=power,
        center_xy=(cx, cy),
        ring_radii_m=radii_m,
        pulse_seconds=pulse_seconds,
    )
