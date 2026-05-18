"""Tests for to_adapter_registry: legacy fusion producers get auto-promoted
to engine adapters so they pick up cube.satisfies + merge benefits without
manual rewrites.
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine import (
    DataDriverAdapter,
    ModelFunctionAdapter,
    Pipeline,
    PipelineRunner,
    ProducerV2,
    SerialBackend,
    VarSpec,
    to_adapter_registry,
    to_engine_registry,
)


# ----------------------------------------------------- driver shape
class _FakeDriver:
    name = "fake_driver"
    produces = ["fakevar"]
    is_static = True

    def fetch(self, cube, t_start=None, t_end=None):
        cube.write_static(
            "fakevar",
            np.ones(cube.grid.shape, dtype="float32"),
            source="fake", native_res_m=float(cube.grid.pixel_m),
            producer=self.name)
        return ["fakevar"]


class _FakeDriverProducer:
    """fusion.DriverProducer-shaped: holds a .driver + duck-types the rest."""
    def __init__(self, driver):
        self.driver = driver
        self.name = driver.name
        self.produces = list(driver.produces)
        self.requires: list[str] = []
        self.time_end_mode = "as_requested"
        self.time_check_mode = "as_requested"

    def is_satisfied(self, cube, variable, request):
        return cube.has(variable)

    def run(self, cube, request):
        return self.driver.fetch(cube)


class _FakeFunctionProducer:
    """fusion.FunctionProducer-shaped: holds a .func(cube, request)."""
    def __init__(self, name, produces, func):
        self.name = name
        self.produces = list(produces)
        self.requires: list[str] = []
        self.func = func

    def run(self, cube, request):
        return self.func(cube, request)


# ----------------------------------------------------- wrapping
def test_driver_producer_wrapped_as_data_adapter():
    legacy = _FakeDriverProducer(_FakeDriver())
    reg = to_adapter_registry([legacy])
    adapted = reg.get("fake_driver")
    assert isinstance(adapted, DataDriverAdapter)
    # Underlying driver preserved
    assert adapted.driver is legacy.driver
    # Variable mapping intact
    assert reg.has_variable("fakevar")


def test_function_producer_wrapped_as_model_adapter():
    func = lambda cube, request: ["x"]
    legacy = _FakeFunctionProducer("legacy_fn", ["x"], func)
    reg = to_adapter_registry([legacy])
    adapted = reg.get("legacy_fn")
    assert isinstance(adapted, ModelFunctionAdapter)
    assert adapted.func is func


def test_producer_v2_passes_through_unchanged():
    """An object that already extends ProducerV2 is NOT re-wrapped."""
    class _NativeV2(ProducerV2):
        name = "native"
        requires = ()
        produces = (VarSpec("nv", kind="static"),)
        def compute(self, inputs, request): return {"nv": None}
    p = _NativeV2()
    reg = to_adapter_registry([p])
    assert reg.get("native") is p


def test_to_engine_registry_does_NOT_wrap():
    """The plain bridge stays transparent — backwards compat."""
    legacy = _FakeDriverProducer(_FakeDriver())
    reg = to_engine_registry([legacy])
    assert reg.get("fake_driver") is legacy


# ----------------------------------------------------- end-to-end behavior
def _real_cube(tmp_path: Path):
    from cube.grid import SimulationGrid
    from cube.store import Cube
    grid = SimulationGrid.from_center_radius(-96.797, 32.776, 5_000.0, 500.0)
    return Cube(tmp_path, grid)


def test_wrapped_driver_writes_and_skip_on_re_run(tmp_path):
    """The wrapped legacy driver still writes, and re-running through the
    runner skips it via cube.satisfies (resolution-aware skip)."""
    cube = _real_cube(tmp_path)
    try:
        legacy = _FakeDriverProducer(_FakeDriver())
        reg = to_adapter_registry([legacy])
        pipeline = Pipeline().add("fake_driver")
        runner = PipelineRunner(reg, backend=SerialBackend(), verbose=False)

        res = runner.run(cube, pipeline)
        assert res.ok
        assert cube.has("fakevar")
        assert res.by_name()["fake_driver"].status == "ok"

        # Second run: cube already populated, adapter's is_satisfied
        # routes through cube.satisfies and skips.
        res2 = runner.run(cube, pipeline)
        assert res2.by_name()["fake_driver"].status == "skipped"
    finally:
        cube.close()


def test_wrapped_function_producer_runs(tmp_path):
    cube = _real_cube(tmp_path)
    try:
        def _compute(cube, request):
            cube.write_static(
                "fn_out", np.full(cube.grid.shape, 3.0, dtype="float32"),
                source="t", native_res_m=float(cube.grid.pixel_m),
                producer="fn")
            return ["fn_out"]

        legacy = _FakeFunctionProducer("fn", ["fn_out"], _compute)
        reg = to_adapter_registry([legacy])
        pipeline = Pipeline().add("fn")
        runner = PipelineRunner(reg, backend=SerialBackend(), verbose=False)
        res = runner.run(cube, pipeline)
        assert res.ok
        assert float(cube.read_static("fn_out").mean()) == 3.0
    finally:
        cube.close()
