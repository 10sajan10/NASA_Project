"""DeadFuelModel: 1-h, 10-h, 100-h dead fuel moisture per timestep using
Nelson (1984) equilibrium-moisture-content lookup, with fixed lag multipliers
for the heavier classes. **Tile-streamed** so the (T_hours, H, W) outputs
never live in RAM at full grid scale.

EMC formula (NFDRS Simard form):
    if RH < 10:    EMC = 0.03229 + 0.281073*RH - 0.000578*RH*T_c
    elif RH < 50:  EMC = 2.22749 + 0.160107*RH - 0.014784*T_c
    else:          EMC = 21.0606 + 0.005565*RH^2 - 0.00035*RH*T_c
                          - 0.483199*RH

Lag class moistures:
    1-hr   = EMC
    10-hr  = EMC * 1.35
    100-hr = EMC * 1.75
"""
from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np

from cube.store import Cube


def _nelson_emc(rh: np.ndarray, t_c: np.ndarray) -> np.ndarray:
    rh = np.clip(rh, 0.1, 100.0)
    out = np.empty_like(rh, dtype=np.float32)
    a = rh < 10.0
    b = (rh >= 10.0) & (rh < 50.0)
    c = rh >= 50.0
    out[a] = 0.03229 + 0.281073 * rh[a] - 0.000578 * rh[a] * t_c[a]
    out[b] = 2.22749 + 0.160107 * rh[b] - 0.014784 * t_c[b]
    out[c] = (21.0606 + 0.005565 * rh[c] ** 2 - 0.00035 * rh[c] * t_c[c]
              - 0.483199 * rh[c])
    return np.clip(out, 1.0, 50.0)


def run(cube: Cube, day0: datetime, n_days: int,
        tile: int = 256) -> list[str]:
    """Compute dead-fuel moistures for every hour in [day0, day0+n_days).

    Streams in (24h x tile x tile) chunks, so a 1000 x 1000 grid x 720 hours
    only ever holds ~24 x 256 x 256 x 4 bytes ~ 6 MB per variable in RAM.
    """
    rh_ts_full = cube.read_3d_times("rh")
    t_ts_full  = cube.read_3d_times("temp_c")
    if rh_ts_full != t_ts_full:
        raise ValueError("rh and temp_c timesteps differ; re-run climate driver")

    t_start = day0
    t_end = day0 + timedelta(days=n_days)
    keep = [i for i, t in enumerate(rh_ts_full) if t_start <= t < t_end]
    if not keep:
        raise RuntimeError("no overlap between cube weather and requested window")
    if keep != list(range(min(keep), max(keep) + 1)):
        raise RuntimeError("requested window is not contiguous in the cube")
    t_slice = slice(min(keep), max(keep) + 1)
    ts = [rh_ts_full[i] for i in keep]
    n_t = len(ts)

    src = "Nelson1984 EMC + lag multipliers; tile-streamed"
    nat = float(cube.grid.pixel_m)
    chunk_t = min(24, n_t)
    for var, lag in [("dfm_1hr",  1.00),
                     ("dfm_10hr", 1.35),
                     ("dfm_100hr", 1.75)]:
        cube.init_time_tiled(
            var, ts=ts, dtype="float32",
            source=src, native_res_m=nat, units="%", producer="dead_fuel_model",
            chunk=(chunk_t, tile, tile))

    # Walk (time-chunk) x (spatial-tile)
    H, W = cube.grid.shape
    n_tiles = sum(1 for _ in cube.iter_spatial_tiles(tile=tile))
    print(f"      streaming Nelson EMC over {n_tiles} {tile}x{tile} tiles "
          f"x {n_t} hours")
    tile_no = 0
    for y_sl, x_sl in cube.iter_spatial_tiles(tile=tile):
        tile_no += 1
        # process the whole time axis for this tile in one go (small)
        rh = cube.read_chunk_time("rh",     t_slice, y_sl, x_sl).astype(np.float32)
        T  = cube.read_chunk_time("temp_c", t_slice, y_sl, x_sl).astype(np.float32)
        emc = _nelson_emc(rh, T)
        cube.write_chunk_time("dfm_1hr",   slice(0, n_t), y_sl, x_sl, emc)
        cube.write_chunk_time("dfm_10hr",  slice(0, n_t), y_sl, x_sl, emc * 1.35)
        cube.write_chunk_time("dfm_100hr", slice(0, n_t), y_sl, x_sl, emc * 1.75)
        del rh, T, emc
    return ["dfm_1hr", "dfm_10hr", "dfm_100hr"]
