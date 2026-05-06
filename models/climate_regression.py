"""Per-pixel climate regression + diurnal disaggregation.

Inputs (cube variables, time-varying historical stacks):
    era5_t2m_max_hist  [C]   daily maximum 2-m air temperature
    era5_t2m_min_hist  [C]   daily minimum 2-m air temperature
    era5_d2m_mean_hist [C]   daily mean dewpoint
    era5_u10_mean_hist [m/s] daily mean 10-m zonal wind
    era5_v10_mean_hist [m/s] daily mean 10-m meridional wind
    era5_pr_total_hist [mm]  daily total precipitation

For every pixel we fit

    y(year, doy) = β₀ + β₁(year - ȳ) + Σₖ (sin/cos harmonics)

via shared temporal_regression primitive, then predict daily values at the
scenario dates and disaggregate to the hourly cube cadence:

    T(h)  = sinusoidal swing between predicted T_min and T_max, peak 14:00
    Td    = predicted daily mean (slow variable)
    RH(h) = 100 · e_s(Td) / e_s(T(h))           Magnus equation
    U/V   = predicted daily mean held flat across the day
    speed = sqrt(U² + V²);  dir = compass bearing FROM
    P(h)  = total / 6 between 13:00-19:00, else 0   (afternoon convection)

Outputs the production-ready hourly cube variables that the fire model
already consumes:
    temp_c, rh, wind_speed_ms, wind_dir_deg, precip_mm
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


def _wind_dir_compass_from_uv(u: np.ndarray, v: np.ndarray) -> np.ndarray:
    """Wind FROM direction in compass degrees (CW from N).
    u (east), v (north) are wind TO components; FROM = (270 - atan2(v,u)) mod 360.
    """
    return (270.0 - np.rad2deg(np.arctan2(v, u))) % 360.0


def _afternoon_precip(daily_mm: np.ndarray, hour: int) -> np.ndarray:
    """Spread daily precip over a 6-hour afternoon window (13..18)."""
    if 13 <= hour < 19:
        return daily_mm / 6.0
    return np.zeros_like(daily_mm)


class ClimateRegression(BaseProducer):
    name = "climate_regression"
    produces = ["temp_c", "rh", "wind_speed_ms", "wind_dir_deg", "precip_mm"]
    requires = list(_HIST_VARS.values())
    kind = "model"
    can_run_parallel = False

    def __init__(self, n_harmonics: int = 2):
        self.n_harmonics = n_harmonics

    def _fit_predict_daily(self, cube: Cube,
                           target_dates: list[datetime]) -> dict[str, np.ndarray]:
        """Run the OLS fit for every climate field and return predictions
        at the requested daily timestamps."""
        out: dict[str, np.ndarray] = {}
        clip_map = {
            "tmax":  (-60.0, 60.0),
            "tmin":  (-60.0, 60.0),
            "td":    (-60.0, 50.0),
            "u":     (-60.0, 60.0),
            "v":     (-60.0, 60.0),
            "pr":    (0.0, 500.0),
        }
        for key, var in _HIST_VARS.items():
            ts, arr = cube.read_3d(var)
            if not ts or arr.size == 0:
                raise RuntimeError(f"{var} has no data; cannot fit climate regression")
            pred = fit_and_predict(
                ts_train=ts, arr=arr,
                ts_target=target_dates,
                n_harmonics=self.n_harmonics,
                clip=clip_map.get(key),
            )
            out[key] = pred.astype(np.float32)
        return out

    def run(self, cube: Cube, request: VariableRequest) -> list[str]:
        if request.t_start is None or request.t_end is None:
            raise ValueError("ClimateRegression requires a time range")
        n_hours = int((request.t_end - request.t_start).total_seconds() // 3600)
        if n_hours <= 0:
            raise ValueError("non-positive request horizon")
        n_days = (n_hours + 23) // 24
        day0 = request.t_start
        target_days = [day0 + timedelta(days=d) for d in range(n_days)]

        # ---- per-pixel daily predictions ---------------------------------
        daily = self._fit_predict_daily(cube, target_days)

        # ---- t_min must be <= t_max; enforce monotonicity to avoid Magnus blow-up
        tmax = daily["tmax"]; tmin = daily["tmin"]
        # if predictions cross, take the average and apply ±2 K guard
        bad = tmin > tmax
        if bad.any():
            mid = 0.5 * (tmax + tmin)
            tmax = np.where(bad, mid + 2.0, tmax)
            tmin = np.where(bad, mid - 2.0, tmin)
        td = np.minimum(daily["td"], tmin - 0.1)        # Td <= T_dry-bulb_min
        u  = daily["u"]
        v  = daily["v"]
        pr = np.maximum(daily["pr"], 0.0)

        # ---- hourly disaggregation ---------------------------------------
        H = cube.grid.height; W = cube.grid.width
        out_ts = [day0 + timedelta(hours=h) for h in range(n_hours)]
        T  = np.empty((n_hours, H, W), dtype=np.float32)
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
            ws_d = np.hypot(u[di], v[di])
            WS[hi] = ws_d
            WD[hi] = _wind_dir_compass_from_uv(u[di], v[di])
            PR[hi] = _afternoon_precip(pr[di], t_h.hour)

        src = (f"per-pixel climate regression (year + {self.n_harmonics} "
               "harmonics) on ARCO-ERA5 history; hourly RH via Magnus")
        nat = 25_000.0
        cube.write_3d("temp_c", out_ts, T, source=src, native_res_m=nat,
                      units="C", producer=self.name,
                      description="Air temperature, hourly diurnalised")
        cube.write_3d("rh", out_ts, RH, source=src, native_res_m=nat,
                      units="%", producer=self.name,
                      description="Relative humidity, Magnus(es(Td)/es(T))")
        cube.write_3d("wind_speed_ms", out_ts, WS, source=src,
                      native_res_m=nat, units="m/s", producer=self.name)
        cube.write_3d("wind_dir_deg", out_ts, WD, source=src,
                      native_res_m=nat, units="deg", producer=self.name,
                      description="Wind FROM direction, CW from N")
        cube.write_3d("precip_mm", out_ts, PR, source=src,
                      native_res_m=nat, units="mm", producer=self.name)
        return list(self.produces)
