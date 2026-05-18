"""Tests for the generic adapter boundary.

These tests model the scalable cascade shape:

    model1_output <- Model 1 Adapter
      requires model2_output + data1_var
    model2_output <- Model 2 Adapter
      requires data2_var
    data*_var <- Data Driver Adapters
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine import (
    DataDriverAdapter,
    ModelFunctionAdapter,
    Pipeline,
    PipelineRunner,
    Request,
    SerialBackend,
    VarSpec,
    to_engine_registry,
)


def _real_cube(tmp_path: Path):
    from cube.grid import SimulationGrid
    from cube.store import Cube
    grid = SimulationGrid.from_center_radius(-96.797, 32.776, 2_000.0, 500.0)
    return Cube(tmp_path, grid)


class ConstantStaticDriver:
    is_static = True

    def __init__(self, name: str, variable: str, value: float):
        self.name = name
        self.produces = [variable]
        self.variable = variable
        self.value = value
        self.calls = 0

    def fetch(self, cube, t_start=None, t_end=None):
        self.calls += 1
        arr = np.full(cube.grid.shape, self.value, dtype="float32")
        cube.write_static(
            self.variable, arr,
            source=f"constant:{self.value}",
            native_res_m=float(cube.grid.pixel_m),
            units="", producer=self.name)
        return [self.variable]


def test_data_driver_adapter_runs_and_reuses_cube_variable(tmp_path):
    cube = _real_cube(tmp_path)
    try:
        driver = ConstantStaticDriver("data1", "data1_var", 7.0)
        reg = to_engine_registry([DataDriverAdapter(driver)])
        pipeline = Pipeline.from_targets(["data1_var"], registry=reg)
        runner = PipelineRunner(reg, backend=SerialBackend(), verbose=False)

        first = runner.run(cube, pipeline)
        second = runner.run(cube, pipeline)

        assert first.by_name()["data1"].status == "ok"
        assert second.by_name()["data1"].status == "skipped"
        assert driver.calls == 1
        assert float(cube.read_static("data1_var").mean()) == 7.0
    finally:
        cube.close()


def test_model_and_data_adapters_form_dependency_cascade(tmp_path):
    cube = _real_cube(tmp_path)
    try:
        data1 = ConstantStaticDriver("data1", "data1_var", 1.0)
        data2 = ConstantStaticDriver("data2", "data2_var", 2.0)

        def run_model2(cube, request):
            out = cube.read_static("data2_var") + 10.0
            cube.write_static(
                "model2_output", out, source="model2",
                native_res_m=float(cube.grid.pixel_m),
                units="", producer="model2")
            return "model2_output"

        def run_model1(cube, request):
            out = cube.read_static("model2_output") + cube.read_static("data1_var")
            cube.write_static(
                "model1_output", out, source="model1",
                native_res_m=float(cube.grid.pixel_m),
                units="", producer="model1")
            return "model1_output"

        reg = to_engine_registry([
            DataDriverAdapter(data1),
            DataDriverAdapter(data2),
            ModelFunctionAdapter(
                name="model2",
                requires=[VarSpec("data2_var")],
                produces=[VarSpec("model2_output")],
                func=run_model2),
            ModelFunctionAdapter(
                name="model1",
                requires=[VarSpec("model2_output"), VarSpec("data1_var")],
                produces=[VarSpec("model1_output")],
                func=run_model1),
        ])
        pipeline = Pipeline.from_targets(["model1_output"], registry=reg)
        runner = PipelineRunner(reg, backend=SerialBackend(), verbose=False)

        result = runner.run(cube, pipeline)
        rerun = runner.run(cube, pipeline)

        assert result.ok
        assert [s.name for s in result.steps] == ["data1", "data2", "model2", "model1"]
        assert float(cube.read_static("model1_output").mean()) == 13.0
        assert all(s.status == "skipped" for s in rerun.steps)
        assert data1.calls == 1
        assert data2.calls == 1
    finally:
        cube.close()


def test_cube_satisfies_static_time_and_resolution_contracts(tmp_path):
    cube = _real_cube(tmp_path)
    try:
        cube.write_static(
            "static_ok", np.ones(cube.grid.shape, dtype="float32"),
            source="test", native_res_m=30.0, units="", producer="test")
        assert cube.satisfies(VarSpec("static_ok", max_native_res_m=30.0))
        assert not cube.satisfies(VarSpec("static_ok", max_native_res_m=10.0))

        t0 = datetime(2036, 9, 15)
        ts = [t0 + timedelta(hours=h) for h in range(48)]
        arr = np.ones((len(ts), *cube.grid.shape), dtype="float32")
        cube.write_3d(
            "time_ok", ts, arr,
            source="test", native_res_m=100.0, units="", producer="test")
        assert cube.satisfies(
            VarSpec("time_ok", kind="time"),
            Request(t_start=t0, t_end=t0 + timedelta(days=2)))
        assert not cube.satisfies(
            VarSpec("time_ok", kind="time"),
            Request(t_start=t0, t_end=t0 + timedelta(days=3)))
    finally:
        cube.close()
