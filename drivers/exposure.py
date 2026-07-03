"""SyntheticExposureDriver: parametric population + built-asset rasters.

An explicit no-data placeholder (same philosophy as UniformFuelDriver):
population density decays exponentially from the grid centre — a crude
monocentric-city model — and per-cell asset value is population times a
per-capita replacement value. This keeps the consequence chain runnable
end-to-end without network access or licensed data.

Replace with a WorldPop/GPW driver (population_density) and a
HAZUS-style building-inventory driver (asset_value_usd) for real
estimates; the card in models/catalog.py is trust-tier "experimental"
so the planner will prefer a real source the moment one is registered.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

import numpy as np

from cube.store import Cube
from drivers.base import Driver


class SyntheticExposureDriver(Driver):
    name = "exposure"
    produces = ["population_density", "asset_value_usd"]
    is_static = True

    def __init__(self, *,
                 peak_density_km2: float = 1500.0,
                 decay_km: float = 15.0,
                 asset_usd_per_capita: float = 250_000.0) -> None:
        """
        peak_density_km2 : population density at the city centre.
        decay_km : e-folding distance of the density falloff.
        asset_usd_per_capita : built-asset replacement value per person.
        """
        self.peak_density_km2 = float(peak_density_km2)
        self.decay_km = float(decay_km)
        self.asset_usd_per_capita = float(asset_usd_per_capita)

    def fetch(self, cube: Cube,
              t_start: Optional[datetime] = None,
              t_end: Optional[datetime] = None) -> list[str]:
        grid = cube.grid
        H, W = grid.shape
        ii, jj = np.indices((H, W), dtype="float64")
        r_km = np.hypot(ii - (H - 1) / 2.0,
                        jj - (W - 1) / 2.0) * grid.pixel_m / 1000.0

        density = (self.peak_density_km2
                   * np.exp(-r_km / self.decay_km)).astype("float32")
        cube.write_static("population_density", density,
                          source="synthetic_exposure",
                          native_res_m=float(grid.pixel_m),
                          units="people/km^2", producer=self.name,
                          description="Monocentric exponential-decay "
                                      "population model (placeholder)")

        cell_km2 = (grid.pixel_m / 1000.0) ** 2
        assets = (density * cell_km2
                  * self.asset_usd_per_capita).astype("float32")
        cube.write_static("asset_value_usd", assets,
                          source="synthetic_exposure",
                          native_res_m=float(grid.pixel_m),
                          units="USD/cell", producer=self.name,
                          description="Per-cell built-asset value = "
                                      "population x per-capita value "
                                      "(placeholder)")
        return list(self.produces)
