"""Fuel-dependent ignition and spread resistance layers.

This is a first-order WUI-aware screening model. It keeps LANDFIRE hard
barriers as true barriers, lets wildland fuels use Rothermel spread, and
allows urban cells to burn only when the incoming fire intensity clears a
high resistance threshold. The thresholds are intentionally stored in the
cube so they can be replaced by better calibrated WUI/structure models later
without changing the fire solver interface.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np

from cube.store import Cube
from fusion.producers import BaseProducer, VariableRequest


HARD_BARRIER_CODES = {92, 93, 98, 99}  # snow/ice, maintained ag, water, bare
URBAN_CODES = {91}
SURFACE_HARD = 0
SURFACE_WILDLAND = 1
SURFACE_URBAN = 2


def _time_mean_tiled(cube: Cube, var: str, day0: datetime, n_days: int,
                     tile: int = 256) -> np.ndarray:
    """Tile-streamed time-mean: never holds the full 3D array."""
    ts = cube.read_3d_times(var)
    t0 = day0
    t1 = day0 + timedelta(days=n_days)
    keep = [i for i, t in enumerate(ts) if t0 <= t < t1]
    if not keep:
        raise RuntimeError(f"{var}: no data in requested threshold window")
    if keep != list(range(min(keep), max(keep) + 1)):
        raise RuntimeError(f"{var}: requested window is not contiguous")
    sl = slice(min(keep), max(keep) + 1)

    H, W = cube.grid.shape
    out = np.empty((H, W), dtype=np.float32)
    for y_sl, x_sl in cube.iter_spatial_tiles(tile=tile):
        chunk = cube.read_chunk_time(var, sl, y_sl, x_sl)
        out[y_sl, x_sl] = chunk.mean(axis=0).astype(np.float32)
        del chunk
    return out


def _fuel_group(codes: np.ndarray) -> np.ndarray:
    """Return compact group ids used for threshold lookup."""
    group = np.zeros(codes.shape, dtype=np.uint8)
    group[(codes >= 101) & (codes <= 109)] = 1   # grass
    group[(codes >= 121) & (codes <= 124)] = 2   # grass-shrub
    group[(codes >= 141) & (codes <= 149)] = 3   # shrub
    group[(codes >= 161) & (codes <= 165)] = 4   # timber-understory
    group[(codes >= 181) & (codes <= 189)] = 5   # timber-litter
    group[(codes >= 201) & (codes <= 204)] = 6   # slash/blowdown
    return group


def _moisture_factor(dfm1_pct: np.ndarray, lfmc_pct: np.ndarray) -> np.ndarray:
    """Dimensionless resistance multiplier from fine/live moisture."""
    dead = np.clip((dfm1_pct - 5.0) / 18.0, 0.0, 2.0)
    live = np.clip((lfmc_pct - 60.0) / 190.0, 0.0, 1.5)
    return np.clip(0.75 + 0.55 * dead + 0.35 * live, 0.65, 2.25).astype(np.float32)


class FuelThresholdProducer(BaseProducer):
    name = "fuel_thresholds"
    produces = [
        "hard_barrier",
        "urban_mask",
        "surface_spread_class",
        "ignition_threshold_mj_m2",
        "spread_threshold_kw_m",
        "spread_rate_modifier",
    ]
    requires = ["fbfm40", "dfm_1hr", "lfmc_pct"]
    kind = "model"
    can_run_parallel = False

    def run(self, cube: Cube, request: VariableRequest) -> list[str]:
        if request.t_start is None:
            raise ValueError("fuel thresholds require t_start")

        fbfm = cube.read_static("fbfm40").astype(np.int32)
        dfm1 = _time_mean_tiled(cube, "dfm_1hr",
                                  request.t_start, request.n_days)
        lfmc = cube.read_static("lfmc_pct").astype(np.float32)
        group = _fuel_group(fbfm)

        hard = np.isin(fbfm, list(HARD_BARRIER_CODES))
        urban = np.isin(fbfm, list(URBAN_CODES))
        wildland = (group > 0) & ~hard & ~urban

        spread_class = np.full(fbfm.shape, SURFACE_HARD, dtype=np.uint8)
        spread_class[wildland] = SURFACE_WILDLAND
        spread_class[urban] = SURFACE_URBAN

        # MJ/m^2 thresholds for direct asteroid thermal ignition.
        ign_base = np.full(fbfm.shape, 1.0e9, dtype=np.float32)
        ign_base[group == 1] = 0.55
        ign_base[group == 2] = 0.65
        ign_base[group == 3] = 0.75
        ign_base[group == 4] = 0.85
        ign_base[group == 5] = 0.90
        ign_base[group == 6] = 0.80
        ign_base[urban] = 1.05

        # kW/m thresholds for neighbor-to-neighbor spread. These are screening
        # thresholds, not a building-code ignition standard.
        spread_base = np.full(fbfm.shape, 1.0e9, dtype=np.float32)
        spread_base[group == 1] = 45.0
        spread_base[group == 2] = 75.0
        spread_base[group == 3] = 130.0
        spread_base[group == 4] = 120.0
        spread_base[group == 5] = 170.0
        spread_base[group == 6] = 220.0
        spread_base[urban] = 700.0

        mf = _moisture_factor(dfm1.astype(np.float32), lfmc)
        ignition_threshold = np.where(hard, 1.0e9, ign_base * mf).astype(np.float32)
        spread_threshold = np.where(hard, 1.0e9, spread_base * mf).astype(np.float32)

        # Urban/maintained surfaces are discontinuous: if ignited, they spread
        # more slowly than wildland fuels until a richer urban model is added.
        spread_modifier = np.ones(fbfm.shape, dtype=np.float32)
        spread_modifier[urban] = 0.18
        spread_modifier[hard] = 0.0

        src = "fuel-threshold screening model v1"
        nat = float(cube.grid.pixel_m)
        cube.write_static("hard_barrier", hard.astype(np.uint8), source=src,
                          native_res_m=nat, units="bool",
                          producer=self.name,
                          description="1 = no surface fire spread into cell")
        cube.write_static("urban_mask", urban.astype(np.uint8), source=src,
                          native_res_m=nat, units="bool",
                          producer=self.name,
                          description="1 = urban/WUI cell with high spread threshold")
        cube.write_static("surface_spread_class", spread_class, source=src,
                          native_res_m=nat, units="0=barrier,1=wildland,2=urban",
                          producer=self.name,
                          description="Surface-spread class used by fire solver")
        cube.write_static("ignition_threshold_mj_m2", ignition_threshold,
                          source=src, native_res_m=nat, units="MJ/m^2",
                          producer=self.name,
                          description="Fuel/moisture threshold for thermal ignition")
        cube.write_static("spread_threshold_kw_m", spread_threshold,
                          source=src, native_res_m=nat, units="kW/m",
                          producer=self.name,
                          description="Target-cell intensity threshold for spread")
        cube.write_static("spread_rate_modifier", spread_modifier,
                          source=src, native_res_m=nat, units="fraction",
                          producer=self.name,
                          description="Multiplier applied to target spread rate")
        return list(self.produces)
