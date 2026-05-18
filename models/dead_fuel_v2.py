"""DeadFuel model — TiledProducer (ProducerV2) migration.

Same Nelson (1984) EMC + lag multipliers as the legacy
``models/dead_fuel_model.py``. Spatial tiles are independent and each
processes its full time slab in one go; perfect fit for the engine's
tile fan-out scheduler.

The legacy `dead_fuel_model.run(cube, day0, n_days)` keeps working for
existing callers. This is an additive migration that lets the engine
dispatch tiles across workers + record per-tile metrics + retry on
transient I/O.
"""
from __future__ import annotations

from typing import Any

import numpy as np

from engine.contracts import (
    CostHint,
    MergePolicy,
    ProducerCapabilities,
    Request,
    TileSpec,
    VarSpec,
)
from engine.tiled import TiledProducer

# Reuse the legacy Nelson EMC kernel — it's pure numpy and stable.
from models.dead_fuel_model import _nelson_emc


_LAGS = (("dfm_1hr", 1.00),
          ("dfm_10hr", 1.35),
          ("dfm_100hr", 1.75))


class DeadFuelModel(TiledProducer):
    """1-h / 10-h / 100-h dead-fuel moisture via Nelson EMC."""

    name = "dead_fuel_v2"
    requires = (
        VarSpec(name="rh", kind="time"),
        VarSpec(name="temp_c", kind="time"),
    )
    produces = tuple(
        VarSpec(name=var, kind="time", dtype="float32", units="%",
                merge_policy=MergePolicy.LAST_WRITER,
                description=f"Dead fuel moisture (Nelson 1984 EMC * "
                             f"{lag} lag)")
        for var, lag in _LAGS
    )
    capabilities = ProducerCapabilities(
        tile_parallel=True,
        cost_hint=CostHint.CPU,
        memory_budget_mb=128)
    tile_size = 256

    def __init__(self) -> None:
        # Populated by init() in the parent process so workers see them.
        self._t_slice: slice | None = None
        self._n_t: int = 0

    # ---- engine hooks ---------------------------------------------------
    # Intentionally no is_satisfied(): scheduler's cube.satisfies()
    # fallback handles time-variable cache checks correctly, including
    # time-window coverage. Defining a per-variable is_satisfied here
    # would shadow that with a naive cube.has() check that doesn't
    # understand the kind="time" semantics.

    def init(self, cube, request: Request) -> None:
        rh_ts = cube.read_3d_times("rh")
        t_ts = cube.read_3d_times("temp_c")
        if rh_ts != t_ts:
            raise ValueError(
                "rh and temp_c timesteps differ; re-run weather first")
        if request.t_start is None or request.t_end is None:
            raise ValueError("dead_fuel_v2 requires t_start and t_end")
        keep = [i for i, t in enumerate(rh_ts)
                if request.t_start <= t < request.t_end]
        if not keep:
            raise RuntimeError(
                "no overlap between cube weather and requested window")
        if keep != list(range(min(keep), max(keep) + 1)):
            raise RuntimeError(
                "requested window is not contiguous in the cube")
        self._t_slice = slice(min(keep), max(keep) + 1)
        ts = [rh_ts[i] for i in keep]
        self._n_t = len(ts)

        chunk_t = min(24, self._n_t)
        src = "Nelson1984 EMC + lag multipliers; tile fan-out via engine"
        nat = float(cube.grid.pixel_m)
        for var, _ in _LAGS:
            cube.init_time_tiled(
                var, ts=ts, dtype="float32",
                source=src, native_res_m=nat, units="%",
                producer=self.name,
                chunk=(chunk_t, self.tile_size, self.tile_size))

    def process_tile(self, cube, request: Request,
                     tile: TileSpec) -> dict[str, Any]:
        if self._t_slice is None:
            raise RuntimeError(
                "init() must run before process_tile (engine bug?)")
        rh = cube.read_chunk_time(
            "rh", self._t_slice, tile.y, tile.x).astype(np.float32)
        T = cube.read_chunk_time(
            "temp_c", self._t_slice, tile.y, tile.x).astype(np.float32)
        emc = _nelson_emc(rh, T)
        cube.write_chunk_time("dfm_1hr",
                              slice(0, self._n_t), tile.y, tile.x, emc)
        cube.write_chunk_time("dfm_10hr",
                              slice(0, self._n_t), tile.y, tile.x,
                              emc * 1.35)
        cube.write_chunk_time("dfm_100hr",
                              slice(0, self._n_t), tile.y, tile.x,
                              emc * 1.75)
        return {"n_hours": self._n_t}

    def finalize(self, cube, request: Request) -> dict[str, int]:
        return {v.name: 1 for v in self.produces}
