"""KBDI (Keetch-Byram Drought Index, 0-800) - tile-streamed.

KBDI must be integrated forward in time, so within a single spatial tile we
process the time axis sequentially. Across tiles the integration is fully
independent, so we walk the grid in (tile x tile) blocks and only keep that
tile's daily precip + temp series in RAM.

    daily ET (drought factor):
        df = ((800 - Q_prev) * (0.968 * exp(0.0486*T_F) - 8.30) * 0.001
              / (1 + 10.88 * exp(-0.0441 * MAP_in)))

    net precip carries through consecutive precip events (>= 0.20 in resets):
        R_net = max(0, R_today_in - max(0, 0.20 - R_carry_in))

    Q_today = clip(Q_prev + df - R_net*100, 0, 800)
"""
from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np

from cube.store import Cube


def _slice_window(ts_full: list[datetime], day0: datetime,
                  n_days: int) -> tuple[slice, list[datetime]]:
    t_start = day0
    t_end = day0 + timedelta(days=n_days)
    keep = [i for i, t in enumerate(ts_full) if t_start <= t < t_end]
    if not keep:
        raise RuntimeError("no overlap between cube weather and requested window")
    if keep != list(range(min(keep), max(keep) + 1)):
        raise RuntimeError("requested window is not contiguous in the cube")
    sl = slice(min(keep), max(keep) + 1)
    return sl, [ts_full[i] for i in keep]


def run(cube: Cube, day0: datetime, n_days: int,
        map_inches: float = 36.0,
        kbdi_init: float = 100.0,
        tile: int = 256) -> str:
    pr_ts_full = cube.read_3d_times("precip_mm")
    t_ts_full  = cube.read_3d_times("temp_c")
    if pr_ts_full != t_ts_full:
        raise RuntimeError("precip_mm and temp_c timesteps differ")

    t_slice, ts_h = _slice_window(pr_ts_full, day0, n_days)
    n_h = len(ts_h)
    if n_h % 24 != 0:
        # not a clean number of days - we still treat each calendar day in
        # the request window as one daily step.
        pass

    # daily output timestamps (start-of-day; matches the resolver's
    # `_time_range_is_covered` which requires min(times) <= t_start).
    daily_ts = [day0 + timedelta(days=d) for d in range(n_days)]
    src = (f"KBDI integration, MAP={map_inches} in, init={kbdi_init}; "
           "tile-streamed")
    cube.init_time_tiled(
        "kbdi", ts=daily_ts, dtype="float32",
        source=src, native_res_m=cube.grid.pixel_m,
        units="0..800", producer="drought_model",
        description="Keetch-Byram Drought Index, daily",
        chunk=(min(30, n_days), tile, tile))

    n_tiles = sum(1 for _ in cube.iter_spatial_tiles(tile=tile))
    print(f"      streaming KBDI over {n_tiles} {tile}x{tile} tiles "
          f"x {n_days} days")
    tile_no = 0
    for y_sl, x_sl in cube.iter_spatial_tiles(tile=tile):
        tile_no += 1

        # read this tile's hourly weather, reduce to daily totals/maxima
        pr_h = cube.read_chunk_time("precip_mm", t_slice, y_sl, x_sl).astype(np.float32)
        T_h  = cube.read_chunk_time("temp_c",    t_slice, y_sl, x_sl).astype(np.float32)
        h, w = pr_h.shape[1], pr_h.shape[2]
        pr_daily = np.zeros((n_days, h, w), dtype=np.float32)
        tmax_daily = np.full((n_days, h, w), -1e3, dtype=np.float32)
        for hi, t in enumerate(ts_h):
            d = (t.date() - day0.date()).days
            if d < 0 or d >= n_days:
                continue
            pr_daily[d] += pr_h[hi]
            np.maximum(tmax_daily[d], T_h[hi], out=tmax_daily[d])
        del pr_h, T_h
        pr_in = pr_daily / 25.4
        tmax_f = tmax_daily * 9.0 / 5.0 + 32.0

        # forward-integrate KBDI
        Q = np.full((h, w), kbdi_init, dtype=np.float32)
        R_carry = np.zeros((h, w), dtype=np.float32)
        kbdi_tile = np.empty((n_days, h, w), dtype=np.float32)
        for d in range(n_days):
            r = pr_in[d]
            net = np.maximum(0.0, r - np.maximum(0.0, 0.20 - R_carry))
            R_carry = np.where(r > 0, R_carry + r, 0.0)
            Q = np.clip(Q - net * 100.0, 0.0, 800.0)
            df = ((800.0 - Q)
                  * (0.968 * np.exp(0.0486 * tmax_f[d]) - 8.30)
                  * 0.001
                  / (1.0 + 10.88 * np.exp(-0.0441 * map_inches)))
            df = np.maximum(df, 0.0)
            Q = np.clip(Q + df, 0.0, 800.0)
            kbdi_tile[d] = Q
        cube.write_chunk_time("kbdi", slice(0, n_days), y_sl, x_sl, kbdi_tile)
        del Q, R_carry, kbdi_tile, pr_daily, tmax_daily, pr_in, tmax_f
    return "kbdi"
