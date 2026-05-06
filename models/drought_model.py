"""KBDI (Keetch-Byram Drought Index, 0-800).

Forward integration in time, daily cadence. Reads daily aggregated precip
(from hourly `precip_mm`) and daily max temp (from hourly `temp_c`). Uses
the standard formulation:

    daily ET (drought factor):
        df = ((800 - Q_prev) * (0.968 * exp(0.0486*T_F) - 8.30) * dt
              / (1 + 10.88 * exp(-0.0441*MAP_in)))
        with dt = 0.001 (per Keetch-Byram tables, encodes 1 day)

    net precip:
        R_net = max(0, R_today_in - max(0, 0.20 - R_carry_in))
        where R_carry tracks consecutive precip events (>= 0.20 in resets it).

    Q_today = clip(Q_prev + df - R_net*100, 0, 800)

We treat MAP (mean annual precip in inches) as a per-grid scalar; for a
~200 km grid it's well-approximated by a single value (Dallas ~36 in).

Reads: precip_mm, temp_c (hourly).  Writes: kbdi (daily).
"""
from __future__ import annotations
from datetime import datetime, timedelta

import numpy as np

from cube.store import Cube


def _hourly_to_daily(cube: Cube, var: str, day0: datetime, n_days: int,
                     reducer: str) -> np.ndarray:
    ts, arr = cube.read_3d(var)
    keep_idx_per_day: list[list[int]] = [[] for _ in range(n_days)]
    for i, t in enumerate(ts):
        rel = t - day0
        d = rel.days
        if 0 <= d < n_days:
            keep_idx_per_day[d].append(i)
    out = np.empty((n_days, cube.grid.height, cube.grid.width), dtype=np.float32)
    for d, idxs in enumerate(keep_idx_per_day):
        if not idxs:
            raise RuntimeError(
                f"{var}: missing hourly data for day {day0 + timedelta(days=d)}")
        sub = arr[idxs]
        if reducer == "sum":
            out[d] = sub.sum(axis=0)
        elif reducer == "max":
            out[d] = sub.max(axis=0)
        elif reducer == "mean":
            out[d] = sub.mean(axis=0)
        else:
            raise ValueError(reducer)
    return out


def run(cube: Cube, day0: datetime, n_days: int,
        map_inches: float = 36.0,
        kbdi_init: float = 100.0) -> str:
    """Integrate KBDI for `n_days` starting at `day0`.

    Returns the variable name written ("kbdi"). Hourly precip is summed
    per day; hourly temp_c is daily-maxed and converted to F.
    """
    pr_mm = _hourly_to_daily(cube, "precip_mm", day0, n_days, "sum")
    tmax_c = _hourly_to_daily(cube, "temp_c", day0, n_days, "max")

    pr_in = pr_mm / 25.4
    tmax_f = tmax_c * 9.0 / 5.0 + 32.0

    Q = np.full(cube.grid.shape, kbdi_init, dtype=np.float32)
    R_carry = np.zeros(cube.grid.shape, dtype=np.float32)
    src = f"KBDI integration, MAP={map_inches}in, init={kbdi_init}"

    for d in range(n_days):
        r = pr_in[d]
        net = np.maximum(0.0, r - np.maximum(0.0, 0.20 - R_carry))
        R_carry = np.where(r > 0, R_carry + r, 0.0)
        Q = Q - net * 100.0  # net precip reduces drought (in 100ths)
        Q = np.clip(Q, 0.0, 800.0)
        df = ((800.0 - Q) * (0.968 * np.exp(0.0486 * tmax_f[d]) - 8.30)
              * 0.001
              / (1.0 + 10.88 * np.exp(-0.0441 * map_inches)))
        df = np.maximum(df, 0.0)
        Q = np.clip(Q + df, 0.0, 800.0)
        cube.write_timestep("kbdi", day0 + timedelta(days=d),
                            Q.astype(np.float32),
                            source=src, native_res_m=cube.grid.pixel_m,
                            units="0..800", producer="drought_model",
                            description="Keetch-Byram Drought Index, daily")
    return "kbdi"
