"""Per-pixel multivariate regression for satellite indices, **tile-streamed**.

For every pixel we fit
    index(year, doy) = β₀ + β₁(year - ȳ)
                     + Σₖ (sin/cos harmonics)
on the historical Landsat stack and predict at the scenario date.

Memory model
------------
The historical stack (n_scenes x H x W) and the regression intermediates can
exceed several GB at the simulation grids the fire model wants. We avoid
that by:
  1. pre-allocating a chunked (H, W) Zarr store for each predicted index
  2. iterating spatial tiles. For each tile:
       a. read the tile's history slice (n_scenes x tile x tile - small)
       b. fit per-pixel regression on this tile (vectorised)
       c. predict at the scenario date
       d. write the tile to the output Zarr
       e. release tile arrays before the next iteration
"""
from __future__ import annotations

from datetime import datetime

import numpy as np

from cube.store import Cube
from fusion.producers import BaseProducer, VariableRequest
from models.temporal_regression import fit_and_predict


_SOURCES: dict[str, tuple[str, tuple[float, float], float]] = {
    "ndvi": ("landsat_ndvi_hist", (-1.0, 1.0), 0.20),
    "ndwi": ("landsat_ndwi_hist", (-1.0, 1.0), 0.00),
    "nbr":  ("landsat_nbr_hist",  (-1.0, 1.0), 0.20),
}


class SatelliteIndexRegression(BaseProducer):
    name = "satellite_index_regression"
    produces = list(_SOURCES.keys())
    requires = [v[0] for v in _SOURCES.values()]
    kind = "model"
    can_run_parallel = False

    def __init__(self, target_date: datetime,
                 n_harmonics: int = 2,
                 tile: int = 256):
        self.target_date = target_date
        self.n_harmonics = n_harmonics
        self.tile = tile

    def run(self, cube: Cube, request: VariableRequest) -> list[str]:
        H, W = cube.grid.shape
        # historical timestamps shared by all three indices
        hist_ts = cube.read_3d_times(self.requires[0])
        n_scenes = len(hist_ts)
        if n_scenes == 0:
            raise RuntimeError("Landsat history is empty; cannot fit")

        # pre-allocate output Zarrs
        for out_var, (hist_var, _clip, _default) in _SOURCES.items():
            cube.init_static_tiled(
                out_var, dtype="float32",
                source=(f"per-pixel multivariate regression on {hist_var} "
                        f"(year + {self.n_harmonics} harmonics); "
                        f"target = {self.target_date.date()}"),
                native_res_m=30.0, units="dimensionless",
                producer=self.name,
                description=(f"Predicted future {out_var.upper()} from a "
                             "per-cell multivariate regression of historical "
                             "Landsat samples"),
                chunk=(self.tile, self.tile))

        n_tiles = sum(1 for _ in cube.iter_spatial_tiles(tile=self.tile))
        print(f"      streaming satellite-index regression over {n_tiles} "
              f"{self.tile}x{self.tile} tiles")
        tile_no = 0
        for y_sl, x_sl in cube.iter_spatial_tiles(tile=self.tile):
            tile_no += 1
            if tile_no % max(1, n_tiles // 10) == 0 or tile_no == 1:
                print(f"      tile {tile_no}/{n_tiles}")
            for out_var, (hist_var, clip_range, default) in _SOURCES.items():
                hist_tile = cube.read_chunk_time(
                    hist_var, slice(0, n_scenes), y_sl, x_sl)
                pred = fit_and_predict(
                    ts_train=hist_ts, arr=hist_tile,
                    ts_target=[self.target_date],
                    n_harmonics=self.n_harmonics,
                    clip=clip_range,
                )[0]
                if not np.all(np.isfinite(pred)):
                    med = (float(np.nanmedian(pred))
                           if np.isfinite(pred).any() else default)
                    pred = np.where(np.isfinite(pred), pred,
                                    med).astype(np.float32)
                cube.write_chunk_static(out_var, y_sl, x_sl, pred)
                del hist_tile, pred
        return list(self.produces)
