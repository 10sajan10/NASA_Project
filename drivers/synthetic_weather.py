"""SyntheticWeatherDriver: spatially-uniform constant weather for testing.

Useful as a fallback when CMIP6 / HRRR fetches are unreachable, or to bound
sensitivity tests. Writes the same five hourly variables a real climate
driver would produce.
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
                 daily_precip_mm: float = 0.0):
        self.temp_c = temp_c
        self.rh_pct = rh_pct
        self.wind_ms = wind_ms
        self.wind_dir_deg = wind_dir_deg
        self.daily_precip_mm = daily_precip_mm

    def fetch(self, cube: Cube,
              t_start: Optional[datetime] = None,
              t_end: Optional[datetime] = None) -> list[str]:
        if t_start is None or t_end is None:
            raise ValueError("synthetic_weather requires t_start and t_end")
        H, W = cube.grid.shape
        n_days = max(1, (t_end.date() - t_start.date()).days + 1)
        ts = []
        for d in range(n_days):
            for h in range(24):
                ts.append(t_start + timedelta(days=d, hours=h))
        n = len(ts)
        T  = np.full((n, H, W), self.temp_c, dtype=np.float32)
        # add diurnal swing of +-6 C around mean
        for i, t in enumerate(ts):
            T[i] += 6.0 * np.cos(2 * np.pi * (t.hour - 14) / 24)
        RH = np.clip(self.rh_pct + (self.temp_c - T) * 1.5, 5, 100).astype(np.float32)
        WS = np.full((n, H, W), self.wind_ms, dtype=np.float32)
        WD = np.full((n, H, W), self.wind_dir_deg, dtype=np.float32)
        PR = np.zeros((n, H, W), dtype=np.float32)
        # split daily precip evenly across hours 13..18 if any
        if self.daily_precip_mm > 0:
            for i, t in enumerate(ts):
                if 13 <= t.hour < 19:
                    PR[i] = self.daily_precip_mm / 6.0

        src = (f"synthetic constants (T={self.temp_c}C, RH={self.rh_pct}%, "
               f"U={self.wind_ms}m/s, dir={self.wind_dir_deg}deg, "
               f"P={self.daily_precip_mm}mm/d)")
        nat = float(cube.grid.pixel_m)
        cube.write_3d("temp_c", ts, T, source=src, native_res_m=nat, units="C",
                      producer=self.name)
        cube.write_3d("rh", ts, RH, source=src, native_res_m=nat, units="%",
                      producer=self.name)
        cube.write_3d("wind_speed_ms", ts, WS, source=src, native_res_m=nat,
                      units="m/s", producer=self.name)
        cube.write_3d("wind_dir_deg", ts, WD, source=src, native_res_m=nat,
                      units="deg", producer=self.name)
        cube.write_3d("precip_mm", ts, PR, source=src, native_res_m=nat,
                      units="mm", producer=self.name)
        return list(self.produces)
