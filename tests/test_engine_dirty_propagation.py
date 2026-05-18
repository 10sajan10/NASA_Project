"""Dirty propagation tests.

When an upstream variable's underlying data is re-fetched, downstream
outputs computed from it must be treated as stale (not satisfied) so the
producer re-runs instead of serving cached output.
"""
from __future__ import annotations

import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine import (
    Pipeline,
    PipelineRunner,
    ProducerCapabilities,
    ProducerV2,
    SerialBackend,
    VarSpec,
    to_engine_registry,
)


def _real_cube(tmp_path: Path):
    from cube.grid import SimulationGrid
    from cube.store import Cube
    grid = SimulationGrid.from_center_radius(-96.797, 32.776, 5_000.0, 500.0)
    return Cube(tmp_path, grid)


# ----------------------------------------------------- catalog primitive
def test_is_output_stale_no_inputs_returns_false(tmp_path):
    cube = _real_cube(tmp_path)
    try:
        assert cube.is_output_stale("anything", []) is False
    finally:
        cube.close()


def test_is_output_stale_absent_output_returns_false(tmp_path):
    cube = _real_cube(tmp_path)
    try:
        # input present, output absent
        cube.write_static("ndvi", np.ones(cube.grid.shape, dtype="float32"),
                          source="t", native_res_m=10.0, producer="t")
        assert cube.is_output_stale("lfmc_pct", ["ndvi"]) is False
    finally:
        cube.close()


def test_is_output_stale_fresh_output_returns_false(tmp_path):
    cube = _real_cube(tmp_path)
    try:
        # input written, output written AFTER -> not stale
        cube.write_static("ndvi", np.ones(cube.grid.shape, dtype="float32"),
                          source="t", native_res_m=10.0, producer="t")
        time.sleep(0.01)
        cube.write_static("lfmc_pct",
                           np.full(cube.grid.shape, 150.0, dtype="float32"),
                           source="t", native_res_m=10.0, producer="t")
        assert cube.is_output_stale("lfmc_pct", ["ndvi"]) is False
    finally:
        cube.close()


def test_is_output_stale_when_input_bumped(tmp_path):
    cube = _real_cube(tmp_path)
    try:
        # output written, then input re-written -> stale
        cube.write_static("ndvi", np.ones(cube.grid.shape, dtype="float32"),
                          source="t", native_res_m=10.0, producer="t")
        time.sleep(0.01)
        cube.write_static("lfmc_pct",
                           np.full(cube.grid.shape, 150.0, dtype="float32"),
                           source="t", native_res_m=10.0, producer="t")
        time.sleep(0.05)
        cube.write_static("ndvi",
                           np.full(cube.grid.shape, 0.5, dtype="float32"),
                           source="t2", native_res_m=10.0, producer="t")
        assert cube.is_output_stale("lfmc_pct", ["ndvi"]) is True
    finally:
        cube.close()


# ----------------------------------------------------- runner integration
class _Doubler(ProducerV2):
    name = "doubler"
    requires = (VarSpec("base", kind="static"),)
    produces = (VarSpec("out", kind="static"),)
    capabilities = ProducerCapabilities()

    def __init__(self):
        self.call_count = 0

    def extract(self, cube, request):
        return {"base": cube.read_static("base")}

    def compute(self, inputs, request):
        self.call_count += 1
        return {"out": inputs["base"] * 2.0}


def test_runner_skips_when_output_fresh(tmp_path):
    cube = _real_cube(tmp_path)
    try:
        cube.write_static("base",
                          np.ones(cube.grid.shape, dtype="float32"),
                          source="t", native_res_m=10.0, producer="t")
        prod = _Doubler()
        reg = to_engine_registry([prod])
        pipeline = Pipeline.from_targets(["out"], registry=reg)
        runner = PipelineRunner(reg, backend=SerialBackend(), verbose=False)
        runner.run(cube, pipeline)
        assert prod.call_count == 1
        # Second run: output fresh -> skipped.
        runner.run(cube, pipeline)
        assert prod.call_count == 1
    finally:
        cube.close()


def test_runner_reruns_when_input_bumped(tmp_path):
    """The whole point: bump 'base' -> 'out' is stale -> doubler re-runs."""
    cube = _real_cube(tmp_path)
    try:
        cube.write_static("base",
                          np.ones(cube.grid.shape, dtype="float32"),
                          source="t", native_res_m=10.0, producer="t")
        prod = _Doubler()
        reg = to_engine_registry([prod])
        pipeline = Pipeline.from_targets(["out"], registry=reg)
        runner = PipelineRunner(reg, backend=SerialBackend(), verbose=False)
        runner.run(cube, pipeline)
        assert prod.call_count == 1

        # Re-write the upstream input.
        time.sleep(0.05)
        cube.write_static("base",
                          np.full(cube.grid.shape, 7.0, dtype="float32"),
                          source="t2", native_res_m=10.0, producer="t")

        res = runner.run(cube, pipeline)
        # Doubler should have run again because 'out' is now stale.
        assert prod.call_count == 2
        assert res.by_name()["doubler"].status == "ok"
        # And the new output reflects the new input.
        assert float(cube.read_static("out").mean()) == pytest.approx(14.0)
    finally:
        cube.close()
