"""Future weather prediction from ERA5 historical analog climatology."""
from __future__ import annotations

from datetime import timedelta

import numpy as np

from cube.store import Cube
from fusion.producers import BaseProducer, VariableRequest


def _slice_for_target(ts, target, day_window: int) -> np.ndarray:
    out = np.zeros(len(ts), dtype=bool)
    target_doy = target.timetuple().tm_yday
    for i, t in enumerate(ts):
        if t.hour != target.hour:
            continue
        doy = t.timetuple().tm_yday
        dist = min(abs(doy - target_doy), 366 - abs(doy - target_doy))
        out[i] = dist <= day_window
    return out


def _mean_or_all(arr: np.ndarray, keep: np.ndarray) -> np.ndarray:
    sub = arr[keep]
    if sub.size == 0:
        sub = arr
    with np.errstate(invalid="ignore"):
        out = np.nanmean(sub, axis=0)
    if np.isnan(out).any():
        fill = np.nanmean(arr, axis=0)
        out = np.where(np.isfinite(out), out, fill)
    return np.nan_to_num(out).astype(np.float32)


class ERA5WeatherPredictor(BaseProducer):
    name = "era5_weather_predictor"
    produces = ["temp_c", "rh", "wind_speed_ms", "wind_dir_deg", "precip_mm"]
    requires = [
        "era5_temp_c_hist",
        "era5_rh_hist",
        "era5_wind_speed_ms_hist",
        "era5_wind_dir_deg_hist",
        "era5_precip_mm_hist",
    ]
    kind = "model"
    can_run_parallel = False

    def __init__(self, day_window: int = 7):
        self.day_window = day_window

    def run(self, cube: Cube, request: VariableRequest) -> list[str]:
        if request.t_start is None or request.t_end is None:
            raise ValueError("ERA5WeatherPredictor requires a time range")
        n_hours = int((request.t_end - request.t_start).total_seconds() // 3600)
        if n_hours <= 0:
            raise ValueError("weather request must cover at least one hour")
        out_ts = [request.t_start + timedelta(hours=i) for i in range(n_hours)]

        ts, temp = cube.read_3d("era5_temp_c_hist")
        _, rh = cube.read_3d("era5_rh_hist")
        _, ws = cube.read_3d("era5_wind_speed_ms_hist")
        _, wd = cube.read_3d("era5_wind_dir_deg_hist")
        _, pr = cube.read_3d("era5_precip_mm_hist")

        shape = (n_hours, cube.grid.height, cube.grid.width)
        T = np.empty(shape, dtype=np.float32)
        RH = np.empty(shape, dtype=np.float32)
        WS = np.empty(shape, dtype=np.float32)
        WD = np.empty(shape, dtype=np.float32)
        PR = np.empty(shape, dtype=np.float32)

        for i, target in enumerate(out_ts):
            keep = _slice_for_target(ts, target, self.day_window)
            T[i] = _mean_or_all(temp, keep)
            RH[i] = np.clip(_mean_or_all(rh, keep), 0, 100)
            WS[i] = np.maximum(_mean_or_all(ws, keep), 0)
            PR[i] = np.maximum(_mean_or_all(pr, keep), 0)

            dirs = wd[keep] if keep.any() else wd
            ang = np.deg2rad(dirs)
            sin_m = np.nanmean(np.sin(ang), axis=0)
            cos_m = np.nanmean(np.cos(ang), axis=0)
            WD[i] = (np.rad2deg(np.arctan2(sin_m, cos_m)) % 360).astype(np.float32)

        src = f"ERA5 historical analog predictor (+/-{self.day_window}d, same hour)"
        cube.write_3d("temp_c", out_ts, T, source=src, native_res_m=31_000.0,
                      units="C", producer=self.name)
        cube.write_3d("rh", out_ts, RH, source=src, native_res_m=31_000.0,
                      units="%", producer=self.name)
        cube.write_3d("wind_speed_ms", out_ts, WS, source=src,
                      native_res_m=31_000.0, units="m/s", producer=self.name)
        cube.write_3d("wind_dir_deg", out_ts, WD, source=src,
                      native_res_m=31_000.0, units="deg", producer=self.name)
        cube.write_3d("precip_mm", out_ts, PR, source=src,
                      native_res_m=31_000.0, units="mm", producer=self.name)
        return list(self.produces)
