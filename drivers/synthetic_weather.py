"""SyntheticWeatherDriver: spatially-uniform constant weather for testing.

Useful as a fallback when CMIP6 / ARCO-ERA5 fetches are unreachable, or for
sensitivity bounding. **Tile-streamed**: pre-allocates chunked Zarr stores
and walks the grid in 256x256 spatial blocks so a 1000x1000 grid x 720 hours
no longer materialises a 13 GB tensor in RAM.
"""
from __future__ import annotations
from datetime import datetime, timedelta
from typing import Optional

import numpy as np

from cube.store import Cube
from drivers.base import Driver


class SyntheticWeatherDriver(Driver):
    name = "synthetic_weather"
    produces = ["temp_c", "rh", "wind_speed_ms", "wind_dir_deg", "precip_mm"]

    def __init__(self,
                 temp_c: float = 28.0,
                 rh_pct: float = 35.0,
                 wind_ms: float = 6.0,
                 wind_dir_deg: float = 180.0,
                 daily_precip_mm: float = 0.0,
                 tile: int = 256):
        self.temp_c = temp_c
        self.rh_pct = rh_pct
        self.wind_ms = wind_ms
        self.wind_dir_deg = wind_dir_deg
        self.daily_precip_mm = daily_precip_mm
        self.tile = tile

    def fetch(self, cube: Cube,
              t_start: Optional[datetime] = None,
              t_end: Optional[datetime] = None) -> list[str]:
        if t_start is None or t_end is None:
            raise ValueError("synthetic_weather requires t_start and t_end")
        H, W = cube.grid.shape
        n_days = max(1, (t_end.date() - t_start.date()).days + 1)
        ts = [t_start + timedelta(days=d, hours=h)
              for d in range(n_days) for h in range(24)]
        n = len(ts)

        # diurnal series (constant across all cells, computed once)
        hour_of_day = np.array([t.hour for t in ts], dtype=np.float32)
        T_series = self.temp_c + 6.0 * np.cos(
            2 * np.pi * (hour_of_day - 14) / 24).astype(np.float32)
        RH_series = np.clip(
            self.rh_pct + (self.temp_c - T_series) * 1.5, 5, 100
        ).astype(np.float32)
        precip_factor = np.where(
            (hour_of_day >= 13) & (hour_of_day < 19),
            self.daily_precip_mm / 6.0, 0.0
        ).astype(np.float32)

        src = (f"synthetic constants (T={self.temp_c}C, RH={self.rh_pct}%, "
               f"U={self.wind_ms}m/s, dir={self.wind_dir_deg}deg, "
               f"P={self.daily_precip_mm}mm/d); tile-streamed")
        nat = float(cube.grid.pixel_m)
        chunk_t = min(24, n)
        for var, units in [("temp_c", "C"), ("rh", "%"),
                            ("wind_speed_ms", "m/s"),
                            ("wind_dir_deg", "deg"),
                            ("precip_mm", "mm")]:
            cube.init_time_tiled(
                var, ts=ts, dtype="float32",
                source=src, native_res_m=nat, units=units,
                producer=self.name, chunk=(chunk_t, self.tile, self.tile))

        n_tiles = sum(1 for _ in cube.iter_spatial_tiles(tile=self.tile))
        for y_sl, x_sl in cube.iter_spatial_tiles(tile=self.tile):
            h = y_sl.stop - y_sl.start; w = x_sl.stop - x_sl.start
            T_t  = np.broadcast_to(T_series[:, None, None],  (n, h, w)).copy()
            RH_t = np.broadcast_to(RH_series[:, None, None], (n, h, w)).copy()
            WS_t = np.full((n, h, w), self.wind_ms, dtype=np.float32)
            WD_t = np.full((n, h, w), self.wind_dir_deg, dtype=np.float32)
            PR_t = np.broadcast_to(precip_factor[:, None, None],
                                    (n, h, w)).copy()
            cube.write_chunk_time("temp_c",        slice(0, n), y_sl, x_sl, T_t)
            cube.write_chunk_time("rh",            slice(0, n), y_sl, x_sl, RH_t)
            cube.write_chunk_time("wind_speed_ms", slice(0, n), y_sl, x_sl, WS_t)
            cube.write_chunk_time("wind_dir_deg",  slice(0, n), y_sl, x_sl, WD_t)
            cube.write_chunk_time("precip_mm",     slice(0, n), y_sl, x_sl, PR_t)
            del T_t, RH_t, WS_t, WD_t, PR_t
        return list(self.produces)
