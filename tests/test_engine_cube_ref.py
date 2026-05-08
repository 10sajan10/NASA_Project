"""Tests for cross-process cube handling.

The hard part of running producers across worker processes is that the
live Cube holds a long-lived DuckDB connection. CubeRef gives workers a
picklable handle they reopen locally.
"""
from __future__ import annotations

import pickle
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine import (
    CubeRef,
    Pipeline,
    PipelineRunner,
    ProducerRegistry,
    SerialBackend,
    is_cross_process_backend,
    make_backend,
    to_engine_registry,
)


# --------------------------------------------------- helpers / fixtures
def _real_cube(tmp_path: Path):
    """Build a real Cube backed by a tiny grid in tmp_path. Imports are
    deferred so test discovery doesn't pay the cost when this isn't run."""
    from cube.grid import SimulationGrid
    from cube.store import Cube
    grid = SimulationGrid.from_center_radius(-96.797, 32.776, 5_000.0, 500.0)
    return Cube(tmp_path, grid)


# Module-level so it's picklable for ProcessBackend.
class StaticDriverProducer:
    """Producer that writes a small constant static array via the cube."""
    name = "const_static"
    produces = ("dummy_static",)
    requires = ()

    def run(self, cube, request):
        H, W = cube.grid.shape
        arr = np.full((H, W), 42.0, dtype="float32")
        cube.write_static(
            "dummy_static", arr,
            source="test", native_res_m=float(cube.grid.pixel_m),
            units="", producer=self.name)
        return ["dummy_static"]


# --------------------------------------------------- pickle round-trip
def test_cube_ref_is_picklable():
    ref = CubeRef(root="/tmp/no_such_path")
    blob = pickle.dumps(ref)
    restored = pickle.loads(blob)
    assert restored == ref


def test_cube_ref_open_round_trip(tmp_path):
    cube = _real_cube(tmp_path)
    cube.close()
    ref = CubeRef.from_cube(cube)
    with ref.opened() as c2:
        assert c2.grid.shape == cube.grid.shape
        assert c2.grid.crs_epsg == cube.grid.crs_epsg


# --------------------------------------------------- backend dispatch
def test_is_cross_process_backend_classification():
    assert is_cross_process_backend(make_backend("process",
                                                  max_workers=1))
    serial = SerialBackend()
    assert not is_cross_process_backend(serial)
    thread = make_backend("thread", max_workers=1)
    try:
        assert not is_cross_process_backend(thread)
    finally:
        thread.shutdown()


# --------------------------------------------------- end-to-end
def test_pipeline_runner_uses_cube_ref_for_process_backend(tmp_path):
    """A producer running under ProcessBackend should reopen the cube via
    CubeRef and write a real Zarr store + DuckDB row that the parent can
    read back."""
    cube = _real_cube(tmp_path)
    try:
        reg = to_engine_registry([StaticDriverProducer()])
        pipeline = Pipeline.from_targets(["dummy_static"], registry=reg)
        backend = make_backend("process", max_workers=1)
        try:
            runner = PipelineRunner(reg, backend=backend, verbose=False)
            res = runner.run(cube, pipeline,
                             t_start=datetime(2036, 9, 15),
                             t_end=datetime(2036, 9, 16))
        finally:
            backend.shutdown()
        assert res.ok, [s.error for s in res.steps if s.status == "error"]
        # Reopen via the parent's cube object and verify the worker's write
        # is durable + visible.
        assert cube.has("dummy_static")
        arr = cube.read_static("dummy_static")
        assert arr.shape == cube.grid.shape
        assert float(arr.mean()) == pytest.approx(42.0)
    finally:
        cube.close()


def test_pipeline_runner_keeps_live_cube_for_in_process_backends(tmp_path):
    """Serial and Thread backends must NOT switch to CubeRef — the producer
    receives the live Cube object (faster, simpler)."""
    seen: list[type] = []

    class InspectCube:
        name = "inspect"
        produces = ("seen_marker",)
        requires = ()
        def run(self, cube, request):
            seen.append(type(cube).__name__)
            cube.write_static(
                "seen_marker",
                np.ones(cube.grid.shape, dtype="float32"),
                source="test",
                native_res_m=float(cube.grid.pixel_m),
                producer=self.name)
            return ["seen_marker"]

    cube = _real_cube(tmp_path)
    try:
        reg = to_engine_registry([InspectCube()])
        pipeline = Pipeline().add("inspect")
        runner = PipelineRunner(reg, backend=SerialBackend(), verbose=False)
        runner.run(cube, pipeline)
        assert seen and seen[0] == "Cube"
    finally:
        cube.close()
