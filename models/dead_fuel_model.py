"""DeadFuelModel: 1-h, 10-h, 100-h dead fuel moisture per timestep using
Nelson (1984) equilibrium-moisture-content lookup, with fixed lag multipliers
for the heavier classes.

EMC formula (Nelson 1984, simplified Simard form used in NFDRS):
    if RH < 10:    EMC = 0.03229 + 0.281073*RH - 0.000578*RH*T_c
    elif RH < 50:  EMC = 2.22749 + 0.160107*RH - 0.014784*T_c
    else:          EMC = 21.0606 + 0.005565*RH^2 - 0.00035*RH*T_c
                          - 0.483199*RH

Lag class moistures:
    1-hr   = EMC
    10-hr  = EMC * 1.35
    100-hr = EMC * 1.75

Reads hourly `rh` and `temp_c` from cube; writes `dfm_1hr`, `dfm_10hr`,
`dfm_100hr` at the same timesteps.
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


def run(cube: Cube, day0: datetime, n_days: int) -> list[str]:
    """Compute dead-fuel moistures for every hour in [day0, day0+n_days)."""
    ts_rh, rh_3d = cube.read_3d("rh")
    ts_t,  t_3d  = cube.read_3d("temp_c")
    if ts_rh != ts_t:
        raise ValueError("rh and temp_c timesteps differ; re-run CMIP6Driver")
    t_start = day0
    t_end = day0 + timedelta(days=n_days)
    keep = [i for i, t in enumerate(ts_rh) if t_start <= t < t_end]
    if not keep:
        raise RuntimeError("no overlap between cube weather and requested window")

    ts = [ts_rh[i] for i in keep]
    rh = rh_3d[keep].astype(np.float32)
    T  = t_3d[keep].astype(np.float32)
    emc = _nelson_emc(rh, T)

    src = "Nelson1984 EMC + lag multipliers"
    nat = float(cube.grid.pixel_m)
    cube.write_3d("dfm_1hr",   ts, emc,
                  source=src, native_res_m=nat, units="%",
                  producer="dead_fuel_model")
    cube.write_3d("dfm_10hr",  ts, (emc * 1.35).astype(np.float32),
                  source=src, native_res_m=nat, units="%",
                  producer="dead_fuel_model")
    cube.write_3d("dfm_100hr", ts, (emc * 1.75).astype(np.float32),
                  source=src, native_res_m=nat, units="%",
                  producer="dead_fuel_model")
    return ["dfm_1hr", "dfm_10hr", "dfm_100hr"]
