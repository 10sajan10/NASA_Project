"""Drought (KBDI) model — TiledProducer (ProducerV2) migration.

KBDI integrates forward in time WITHIN a single spatial tile (consecutive
days depend on each other), but spatial tiles are fully independent. The
engine's tile fan-out scheduler dispatches one process_tile call per
spatial tile; each call walks its own time axis sequentially.

Constructor accepts ``map_inches`` and ``kbdi_init`` so the same producer
can be re-used with different MAP / initial values across runs.
"""
from __future__ import annotations

from datetime import timedelta
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


class DroughtModel(TiledProducer):
    """Keetch-Byram Drought Index (0..800), daily."""

    name = "drought_v2"
    requires = (
        VarSpec(name="precip_mm", kind="time"),
        VarSpec(name="temp_c", kind="time"),
    )
    produces = (
        VarSpec(name="kbdi", kind="time", dtype="float32",
                units="0..800",
                merge_policy=MergePolicy.LAST_WRITER,
                description="Keetch-Byram Drought Index, daily"),
    )
    capabilities = ProducerCapabilities(
        tile_parallel=True,
        cost_hint=CostHint.CPU,
        memory_budget_mb=128,
        iterative=True)             # forward-in-time within a tile
    tile_size = 256

    def __init__(self, *, map_inches: float = 36.0,
                 kbdi_init: float = 100.0) -> None:
        self.map_inches = map_inches
        self.kbdi_init = kbdi_init
        self._t_slice: slice | None = None
        self._ts_h: list = []
        self._n_days: int = 0
        self._day0 = None

    # ---- engine hooks ---------------------------------------------------
    # Intentionally no is_satisfied(): scheduler routes through
    # cube.satisfies() which understands kind="time" + time-window
    # coverage; a naive cube.has() check here would shadow that.

    def init(self, cube, request: Request) -> None:
        pr_ts = cube.read_3d_times("precip_mm")
        t_ts = cube.read_3d_times("temp_c")
        if pr_ts != t_ts:
            raise RuntimeError("precip_mm and temp_c timesteps differ")
        if request.t_start is None or request.t_end is None:
            raise ValueError("drought_v2 requires t_start and t_end")
        keep = [i for i, t in enumerate(pr_ts)
                if request.t_start <= t < request.t_end]
        if not keep:
            raise RuntimeError("no overlap between weather and request")
        if keep != list(range(min(keep), max(keep) + 1)):
            raise RuntimeError("requested window is not contiguous")
        self._t_slice = slice(min(keep), max(keep) + 1)
        self._ts_h = [pr_ts[i] for i in keep]
        self._day0 = request.t_start
        self._n_days = max(1, (request.t_end.date()
                               - request.t_start.date()).days)

        daily_ts = [self._day0 + timedelta(days=d)
                    for d in range(self._n_days)]
        src = (f"KBDI integration, MAP={self.map_inches} in, "
               f"init={self.kbdi_init}; tile fan-out via engine")
        cube.init_time_tiled(
            "kbdi", ts=daily_ts, dtype="float32",
            source=src, native_res_m=cube.grid.pixel_m,
            units="0..800", producer=self.name,
            description="Keetch-Byram Drought Index, daily",
            chunk=(min(30, self._n_days), self.tile_size, self.tile_size))

    def process_tile(self, cube, request: Request,
                     tile: TileSpec) -> dict[str, Any]:
        if self._t_slice is None:
            raise RuntimeError("init() must run before process_tile")
        n_days = self._n_days
        pr_h = cube.read_chunk_time(
            "precip_mm", self._t_slice, tile.y, tile.x).astype(np.float32)
        T_h = cube.read_chunk_time(
            "temp_c", self._t_slice, tile.y, tile.x).astype(np.float32)
        h, w = pr_h.shape[1], pr_h.shape[2]

        pr_daily = np.zeros((n_days, h, w), dtype=np.float32)
        tmax_daily = np.full((n_days, h, w), -1e3, dtype=np.float32)
        for hi, t in enumerate(self._ts_h):
            d = (t.date() - self._day0.date()).days
            if d < 0 or d >= n_days:
                continue
            pr_daily[d] += pr_h[hi]
            np.maximum(tmax_daily[d], T_h[hi], out=tmax_daily[d])

        pr_in = pr_daily / 25.4
        tmax_f = tmax_daily * 9.0 / 5.0 + 32.0

        Q = np.full((h, w), self.kbdi_init, dtype=np.float32)
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
                  / (1.0 + 10.88 * np.exp(-0.0441 * self.map_inches)))
            df = np.maximum(df, 0.0)
            Q = np.clip(Q + df, 0.0, 800.0)
            kbdi_tile[d] = Q

        cube.write_chunk_time(
            "kbdi", slice(0, n_days), tile.y, tile.x, kbdi_tile)
        return {"n_days": n_days}

    def finalize(self, cube, request: Request) -> dict[str, int]:
        return {"kbdi": 1}
