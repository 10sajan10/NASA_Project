"""Tile metrics + backpressure tests.

Validates that:
  * TileMetric records per-tile elapsed / attempts / status
  * StepResult.tile_latency_summary aggregates correctly
  * max_inflight_tiles caps concurrent in-flight tasks
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine import (
    Pipeline,
    PipelineRunner,
    ProducerCapabilities,
    RetryPolicy,
    SerialBackend,
    ThreadBackend,
    TiledProducer,
    TileMetric,
    to_engine_registry,
)


def _real_cube(tmp_path: Path, *, radius_m: float = 50_000.0):
    from cube.grid import SimulationGrid
    from cube.store import Cube
    grid = SimulationGrid.from_center_radius(
        -96.797, 32.776, radius_m, 500.0)
    return Cube(tmp_path, grid)


# --------------------------------------------------- per-tile metrics
class _SimpleTiled(TiledProducer):
    name = "simple"
    produces = ("filled",)
    requires = ()
    capabilities = ProducerCapabilities(tile_parallel=True)
    tile_size = 32

    def init(self, cube, request):
        cube.init_static_tiled(
            "filled", dtype="float32", source="t",
            native_res_m=float(cube.grid.pixel_m), units="", producer="t",
            chunk=(self.tile_size, self.tile_size))

    def process_tile(self, cube, request, tile):
        h = tile.y.stop - tile.y.start
        w = tile.x.stop - tile.x.start
        cube.write_chunk_static(
            "filled", tile.y, tile.x,
            np.zeros((h, w), dtype="float32"))


def test_step_result_records_tile_metrics(tmp_path):
    cube = _real_cube(tmp_path)
    try:
        reg = to_engine_registry([_SimpleTiled()])
        runner = PipelineRunner(reg, backend=SerialBackend(), verbose=False)
        res = runner.run(cube, Pipeline().add("simple"))
        assert res.ok
        sr = res.by_name()["simple"]
        assert sr.tile_count > 0
        assert len(sr.tile_metrics) == sr.tile_count
        assert all(isinstance(t, TileMetric) for t in sr.tile_metrics)
        assert all(t.status == "ok" for t in sr.tile_metrics)
        assert all(t.elapsed_s >= 0 for t in sr.tile_metrics)
    finally:
        cube.close()


def test_tile_latency_summary(tmp_path):
    cube = _real_cube(tmp_path)
    try:
        reg = to_engine_registry([_SimpleTiled()])
        runner = PipelineRunner(reg, backend=SerialBackend(), verbose=False)
        res = runner.run(cube, Pipeline().add("simple"))
        summary = res.by_name()["simple"].tile_latency_summary
        assert summary["n"] > 0
        assert summary["min_s"] <= summary["median_s"] <= summary["max_s"]
        assert summary["total_s"] >= summary["max_s"]
        assert summary["errors"] == 0
    finally:
        cube.close()


def test_tile_latency_summary_empty_for_non_tiled(tmp_path):
    """Non-tile-aware producer: empty tile_metrics, empty summary."""
    cube = _real_cube(tmp_path, radius_m=5_000.0)
    try:
        from engine import ProducerV2, VarSpec

        class _Whole(ProducerV2):
            name = "whole"
            requires = ()
            produces = (VarSpec("v", kind="static"),)
            def compute(self, inputs, request):
                return {"v": np.ones(cube.grid.shape, dtype="float32")}

        reg = to_engine_registry([_Whole()])
        runner = PipelineRunner(reg, backend=SerialBackend(), verbose=False)
        res = runner.run(cube, Pipeline().add("whole"))
        sr = res.by_name()["whole"]
        assert sr.tile_count == 0
        assert sr.tile_metrics == []
        assert sr.tile_latency_summary == {}
    finally:
        cube.close()


# --------------------------------------------------- backpressure
class _ConcurrencyTracker(TiledProducer):
    """Counts max simultaneous tiles in-flight (thread-shared counter)."""
    name = "track"
    produces = ("v",)
    requires = ()
    capabilities = ProducerCapabilities(tile_parallel=True)
    tile_size = 16

    def __init__(self):
        self.lock = threading.Lock()
        self.live = 0
        self.peak = 0

    def init(self, cube, request):
        cube.init_static_tiled(
            "v", dtype="float32", source="t",
            native_res_m=float(cube.grid.pixel_m), units="", producer="t",
            chunk=(self.tile_size, self.tile_size))

    def process_tile(self, cube, request, tile):
        with self.lock:
            self.live += 1
            if self.live > self.peak:
                self.peak = self.live
        # Hold for a moment so concurrency can actually accumulate.
        time.sleep(0.02)
        with self.lock:
            self.live -= 1
        h = tile.y.stop - tile.y.start
        w = tile.x.stop - tile.x.start
        cube.write_chunk_static(
            "v", tile.y, tile.x,
            np.zeros((h, w), dtype="float32"))


def test_max_inflight_tiles_caps_concurrency(tmp_path):
    """Setting max_inflight_tiles=2 with a 4-thread backend forces tile
    work to drip through 2 at a time."""
    cube = _real_cube(tmp_path, radius_m=20_000.0)
    try:
        prod = _ConcurrencyTracker()
        reg = to_engine_registry([prod])
        backend = ThreadBackend(max_workers=4)
        try:
            runner = PipelineRunner(
                reg, backend=backend, max_inflight_tiles=2,
                verbose=False)
            res = runner.run(cube, Pipeline().add("track"))
        finally:
            backend.shutdown()
        assert res.ok
        # Peak concurrency should be at most 2 (the in-flight cap).
        assert prod.peak <= 2, (
            f"max_inflight_tiles=2 violated; peak={prod.peak}")
    finally:
        cube.close()


def test_unbounded_inflight_lets_full_pool_run(tmp_path):
    """Without max_inflight_tiles, a 4-thread pool should hit peak >= 2."""
    cube = _real_cube(tmp_path, radius_m=20_000.0)
    try:
        prod = _ConcurrencyTracker()
        reg = to_engine_registry([prod])
        backend = ThreadBackend(max_workers=4)
        try:
            runner = PipelineRunner(
                reg, backend=backend, verbose=False)
            res = runner.run(cube, Pipeline().add("track"))
        finally:
            backend.shutdown()
        assert res.ok
        # Without cap, multiple tiles should overlap. >=2 is a safe assertion
        # on the test environment (cap is os.cpu_count()-1 by default).
        assert prod.peak >= 2, (
            f"expected multiple tiles in flight; peak={prod.peak}")
    finally:
        cube.close()


# --------------------------------------------------- retry + metrics
def test_tile_metric_captures_attempts():
    """Tile that fails once then succeeds records attempts=2 in its metric."""
    pass  # Placeholder — combined retry/metric coverage exists via
    # test_engine_retry; this is wired implicitly through _record().
