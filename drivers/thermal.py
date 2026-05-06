"""ThermalDriver: parse PDC thermal-damage KML and write a static thermal
exposure field (fluence + power + ignition_t0 + burnable mask) to the cube.

The four ring polygons are concentric isodose curves with fluence values from
Acta Astronautica 216 (2024) 468-487, Table 1. Between rings we interpolate
log(J) linearly in radius (== exponential decay segments). Past the outermost
ring we extend the (Severe -> Serious) tail; inside the innermost ring we
clamp to the innermost fluence.
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np
from pyproj import Transformer
from shapely.ops import transform as shp_transform

from cube.store import Cube
from drivers.base import Driver, register
from drivers.kml import load_city_damage, DAMAGE_FLUENCE_MJ_M2, CityDamage


@dataclass
class _RingMetrics:
    radii_m: dict[str, float]
    center_xy: tuple[float, float]


class ThermalDriver(Driver):
    name = "thermal"
    produces = ["thermal_fluence", "thermal_power",
                "ignition_t0", "burnable"]
    is_static = True

    def __init__(self, kml_path: str | Path,
                 city: str = "Dallas TX USA",
                 band: str = "Mean",
                 pulse_seconds: float = 10.0):
        self.kml_path = str(kml_path)
        self.city = city
        self.band = band
        self.pulse_seconds = pulse_seconds
        self._last_metrics: Optional[_RingMetrics] = None

    def fetch(self, cube: Cube,
              t_start: Optional[datetime] = None,
              t_end: Optional[datetime] = None) -> list[str]:
        damage = load_city_damage(self.kml_path, city=self.city, band=self.band)
        fluence, power, ig_t0, burnable, metrics = self._build_field(cube, damage)
        self._last_metrics = metrics

        src = f"PDC2023_KMZ::{damage.city}::{damage.band}"
        nat = float(cube.grid.pixel_m)
        cube.write_static("thermal_fluence", fluence,
                          source=src, native_res_m=nat, units="MJ/m^2",
                          producer=self.name,
                          description="Asteroid airburst thermal fluence, "
                          "log-linear interpolation of PDC2023 isodose rings")
        cube.write_static("thermal_power", power,
                          source=src, native_res_m=nat, units="kW/m^2",
                          producer=self.name,
                          description=f"Power flux assuming {self.pulse_seconds}-s "
                          "square pulse")
        cube.write_static("ignition_t0", ig_t0,
                          source=src, native_res_m=nat, units="bool",
                          producer=self.name,
                          description="t=0 burning annulus: Critical -> Unsurvivable")
        cube.write_static("burnable", burnable,
                          source=src, native_res_m=nat, units="bool",
                          producer=self.name,
                          description="True outside Unsurvivable dead zone")
        return list(self.produces)

    def _build_field(self, cube: Cube, damage: CityDamage):
        grid = cube.grid
        fwd = Transformer.from_crs(4326, grid.crs, always_xy=True).transform
        rings_proj = [shp_transform(fwd, r.polygon) for r in damage.rings]
        inner = rings_proj[-1]
        cx, cy = float(inner.centroid.x), float(inner.centroid.y)

        ordered = []
        radii_m: dict[str, float] = {}
        for r, p in zip(damage.rings, rings_proj):
            xy_arr = np.asarray(p.exterior.coords)
            rad = float(np.mean(np.hypot(xy_arr[:, 0] - cx, xy_arr[:, 1] - cy)))
            radii_m[r.name] = rad
            ordered.append((rad, r.fluence_mj_m2))
        ordered.sort()
        rs = np.array([o[0] for o in ordered])
        js = np.array([o[1] for o in ordered])
        log_js = np.log(js)
        dlog_dr_tail = (log_js[-1] - log_js[-2]) / (rs[-1] - rs[-2])

        xs, ys = grid.cell_centers_xy()
        XX, YY = np.meshgrid(xs, ys)
        R = np.hypot(XX - cx, YY - cy)

        log_j = np.interp(R, rs, log_js, left=log_js[0], right=np.nan)
        tail = R > rs[-1]
        log_j[tail] = log_js[-1] + dlog_dr_tail * (R[tail] - rs[-1])
        log_j[R < rs[0]] = log_js[0]

        fluence = np.exp(log_j).astype(np.float32)
        power = (fluence * 1000.0 / self.pulse_seconds).astype(np.float32)

        j_crit = DAMAGE_FLUENCE_MJ_M2["Critical Burn (clothing)"]
        j_unsurv = DAMAGE_FLUENCE_MJ_M2["Unsurvivable Burn (structures)"]
        ig_t0 = ((fluence >= j_crit) & (fluence < j_unsurv)).astype(np.uint8)
        burnable = (fluence < j_unsurv).astype(np.uint8)

        return fluence, power, ig_t0, burnable, _RingMetrics(
            radii_m=radii_m, center_xy=(cx, cy))
