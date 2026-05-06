"""Per-pixel climate regression + diurnal disaggregation, **tile-streamed**.

For every cell we fit
    y(year, doy) = β₀ + β₁(year - ȳ) + Σₖ (sin/cos harmonics)
on the historical ARCO-ERA5 stack, predict daily values at the scenario dates,
and disaggregate to hourly cube cadence with the Magnus equation supplying
RH(t, hour) thermodynamically from daily mean dewpoint and hourly diurnal T.

Memory model
------------
The hourly outputs at full grid scale (e.g. 720 hours x 1000 x 1000 x 4 bytes
x 5 vars = 13 GB) cannot fit in RAM. Instead we:

  1. pre-allocate the five hourly Zarr stores with chunks (24h, 128, 128).
  2. iterate over 128 x 128 spatial tiles. For each tile:
       a. read history tile from era5_*_hist (small: 336 days x 128 x 128 x 6).
       b. fit per-pixel regression on this tile (vectorised lstsq).
       c. predict daily values for the scenario horizon.
       d. disaggregate to hourly per tile.
       e. write the (n_hours, 128, 128) tile to each of the 5 Zarr stores.
       f. release tile arrays before the next tile is touched.

Peak memory per tile is ~ 60 MB at 128 x 128, ~ 15 MB at 64 x 64.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np

from cube.store import Cube
from fusion.producers import BaseProducer, VariableRequest
from models.magnus import (
    diurnal_temperature,
    relative_humidity_pct,
)
from models.temporal_regression import fit_and_predict


_HIST_VARS = {
    "tmax":  "era5_t2m_max_hist",
    "tmin":  "era5_t2m_min_hist",
    "td":    "era5_d2m_mean_hist",
    "u":     "era5_u10_mean_hist",
    "v":     "era5_v10_mean_hist",
    "pr":    "era5_pr_total_hist",
}

_CLIP = {
    "tmax":  (-60.0, 60.0),
    "tmin":  (-60.0, 60.0),
    "td":    (-60.0, 50.0),
    "u":     (-60.0, 60.0),
    "v":     (-60.0, 60.0),
    "pr":    (0.0, 500.0),
}

_OUT_VARS = ["temp_c", "rh", "wind_speed_ms", "wind_dir_deg", "precip_mm"]


def _wind_dir_compass_from_uv(u: np.ndarray, v: np.ndarray) -> np.ndarray:
    return (270.0 - np.rad2deg(np.arctan2(v, u))) % 360.0


def _afternoon_precip_factor(hour: int) -> float:
    return (1.0 / 6.0) if 13 <= hour < 19 else 0.0


class ClimateRegression(BaseProducer):
    name = "climate_regression"
    produces = list(_OUT_VARS)
    requires = list(_HIST_VARS.values())
    kind = "model"
    can_run_parallel = False

    def __init__(self, n_harmonics: int = 2, tile: int = 128):
        self.n_harmonics = n_harmonics
        self.tile = tile

    def run(self, cube: Cube, request: VariableRequest) -> list[str]:
        if request.t_start is None or request.t_end is None:
            raise ValueError("ClimateRegression requires a time range")
        n_hours = int((request.t_end - request.t_start).total_seconds() // 3600)
        if n_hours <= 0:
            raise ValueError("non-positive request horizon")
        n_days = (n_hours + 23) // 24
        day0 = request.t_start
        target_days = [day0 + timedelta(days=d) for d in range(n_days)]
        out_ts = [day0 + timedelta(hours=h) for h in range(n_hours)]
        H, W = cube.grid.shape

        # historical timesteps (read once, all training is on the same axis)
        hist_ts = cube.read_3d_times(_HIST_VARS["tmax"])

        # pre-allocate hourly Zarr stores; chunks aligned to our spatial tile
        src = (f"per-pixel climate regression (year + {self.n_harmonics} "
               "harmonics) on ARCO-ERA5 history; hourly RH via Magnus; "
               f"streamed at {self.tile}x{self.tile} tiles")
        nat = 25_000.0
        attrs = {
            "temp_c": ("C", "Air temperature, hourly diurnalised"),
            "rh":     ("%", "Relative humidity from Magnus(es(Td)/es(T))"),
            "wind_speed_ms": ("m/s", "10-m wind speed, daily mean held flat"),
            "wind_dir_deg":  ("deg", "Wind FROM direction (CW from N)"),
            "precip_mm":     ("mm", "Hourly precip from afternoon-distributed "
                              "daily total"),
        }
        for var in _OUT_VARS:
            units, descr = attrs[var]
            cube.init_time_tiled(
                var, ts=out_ts, dtype="float32",
                source=src, native_res_m=nat, units=units,
                producer=self.name, description=descr,
                chunk=(min(24, n_hours), self.tile, self.tile))

        # read tile shape used as a numpy buffer; reused across tiles to avoid
        # repeated allocations.
        n_tiles = sum(1 for _ in cube.iter_spatial_tiles(tile=self.tile))
        print(f"      streaming climate regression over {n_tiles} "
              f"{self.tile}x{self.tile} tiles")
        tile_no = 0
        for y_sl, x_sl in cube.iter_spatial_tiles(tile=self.tile):
            tile_no += 1
            self._process_tile(cube, hist_ts, y_sl, x_sl,
                               target_days, out_ts, day0, n_days, n_hours,
                               tile_no, n_tiles)
        return list(self.produces)

    # ----------------------------------------------------------------
    def _process_tile(self, cube: Cube, hist_ts: list[datetime],
                      y_sl: slice, x_sl: slice,
                      target_days: list[datetime],
                      out_ts: list[datetime],
                      day0: datetime, n_days: int, n_hours: int,
                      tile_no: int, n_tiles: int) -> None:
        if tile_no % max(1, n_tiles // 10) == 0 or tile_no == 1:
            print(f"      tile {tile_no}/{n_tiles}  rows={y_sl.start}:{y_sl.stop}"
                  f"  cols={x_sl.start}:{x_sl.stop}")

        # 1) read this tile's history for each variable
        daily_pred: dict[str, np.ndarray] = {}
        for key, hist_var in _HIST_VARS.items():
            hist_tile = cube.read_chunk_time(
                hist_var, slice(0, len(hist_ts)), y_sl, x_sl)
            pred = fit_and_predict(
                ts_train=hist_ts, arr=hist_tile,
                ts_target=target_days,
                n_harmonics=self.n_harmonics,
                clip=_CLIP[key],
            )
            daily_pred[key] = pred.astype(np.float32)
            del hist_tile

        # 2) enforce physical constraints (T_min<=T_max, Td<=T_min)
        tmax = daily_pred["tmax"]; tmin = daily_pred["tmin"]
        bad = tmin > tmax
        if bad.any():
            mid = 0.5 * (tmax + tmin)
            tmax = np.where(bad, mid + 2.0, tmax)
            tmin = np.where(bad, mid - 2.0, tmin)
        td = np.minimum(daily_pred["td"], tmin - 0.1)
        u  = daily_pred["u"]; v = daily_pred["v"]
        pr = np.maximum(daily_pred["pr"], 0.0)

        # 3) disaggregate to hourly within this tile (small, fits in RAM)
        h = y_sl.stop - y_sl.start; w = x_sl.stop - x_sl.start
        T  = np.empty((n_hours, h, w), dtype=np.float32)
        RH = np.empty_like(T)
        WS = np.empty_like(T)
        WD = np.empty_like(T)
        PR = np.empty_like(T)
        for hi, t_h in enumerate(out_ts):
            di = (t_h.date() - day0.date()).days
            if di >= n_days:
                di = n_days - 1
            t_hourly = diurnal_temperature(tmax[di], tmin[di], hour=t_h.hour)
            T[hi]  = t_hourly
            RH[hi] = relative_humidity_pct(t_hourly, td[di])
            WS[hi] = np.hypot(u[di], v[di])
            WD[hi] = _wind_dir_compass_from_uv(u[di], v[di])
            PR[hi] = pr[di] * _afternoon_precip_factor(t_h.hour)

        # 4) stream tile to each of the five Zarr stores
        cube.write_chunk_time("temp_c",        slice(0, n_hours), y_sl, x_sl, T)
        cube.write_chunk_time("rh",            slice(0, n_hours), y_sl, x_sl, RH)
        cube.write_chunk_time("wind_speed_ms", slice(0, n_hours), y_sl, x_sl, WS)
        cube.write_chunk_time("wind_dir_deg",  slice(0, n_hours), y_sl, x_sl, WD)
        cube.write_chunk_time("precip_mm",     slice(0, n_hours), y_sl, x_sl, PR)
        del T, RH, WS, WD, PR, daily_pred
