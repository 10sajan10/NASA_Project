"""DataAdapter + ModelAdapter integration tests.

Demonstrates the plug-in shape end-to-end:

  1. DataAdapter declares input variable + resolution requirements
     -> engine searches cube via cube.satisfies(); reports missing
        when nothing satisfies the resolution
  2. ModelAdapter (subclass) stages inputs to a temp dir, calls a real
     subprocess that reads them + writes outputs, parses outputs back
  3. The whole thing runs through PipelineRunner; the cube
     ultimately holds the model's outputs.

A real Python subprocess is used as the stand-in 'binary' so we
exercise the actual stage-run-parse loop without any C/Fortran build.
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine import (
    DataAdapter,
    DataNeed,
    ModelAdapter,
    Pipeline,
    PipelineRunner,
    ProducerCapabilities,
    SerialBackend,
    VarSpec,
    to_engine_registry,
)


# ====================================================== helpers
def _real_cube(tmp_path: Path):
    from cube.grid import SimulationGrid
    from cube.store import Cube
    grid = SimulationGrid.from_center_radius(
        -96.797, 32.776, 5_000.0, 500.0)
    return Cube(tmp_path, grid)


# ====================================================== DataAdapter
class TestDataAdapter:
    """Tests for the data adapter (declare + search + fetch)."""

    def test_varspecs_carry_resolution_and_required(self):
        da = DataAdapter([
            DataNeed("ndvi", kind="static", max_native_res_m=30.0),
            DataNeed("dem",  kind="static", required=False),
        ])
        specs = {s.name: s for s in da.varspecs()}
        assert specs["ndvi"].max_native_res_m == 30.0
        assert specs["ndvi"].required is True
        assert specs["dem"].required is False

    def test_duplicate_name_raises(self):
        with pytest.raises(ValueError, match="duplicate"):
            DataAdapter([
                DataNeed("v"), DataNeed("v"),
            ])

    def test_search_reports_resolution_status(self, tmp_path):
        cube = _real_cube(tmp_path)
        try:
            # Write NDVI at 100m native resolution.
            cube.write_static(
                "ndvi", np.ones(cube.grid.shape, dtype="float32"),
                source="t", native_res_m=100.0, producer="t")

            # Adapter wants <=30m -> resolution NOT ok
            da_strict = DataAdapter([
                DataNeed("ndvi", max_native_res_m=30.0)])
            r = da_strict.search(cube)
            assert r["ndvi"]["present"] is True
            assert r["ndvi"]["resolution_ok"] is False
            assert r["ndvi"]["native_res_m"] == 100.0

            # Adapter wants <=200m -> resolution ok
            da_lax = DataAdapter([
                DataNeed("ndvi", max_native_res_m=200.0)])
            r2 = da_lax.search(cube)
            assert r2["ndvi"]["resolution_ok"] is True
        finally:
            cube.close()

    def test_missing_lists_unsatisfied_required_needs(self, tmp_path):
        cube = _real_cube(tmp_path)
        try:
            cube.write_static(
                "ndvi", np.ones(cube.grid.shape, dtype="float32"),
                source="t", native_res_m=100.0, producer="t")
            da = DataAdapter([
                DataNeed("ndvi", max_native_res_m=30.0),
                DataNeed("dem",  required=False),         # absent + optional
                DataNeed("temp_c", kind="time"),          # absent + required
            ])
            missing = da.missing(cube)
            # ndvi: present but too coarse -> missing
            # temp_c: absent + required -> missing
            # dem: absent but optional -> NOT missing
            assert set(missing) == {"ndvi", "temp_c"}
        finally:
            cube.close()

    def test_fetch_returns_static_arrays_and_time_tuples(self, tmp_path):
        cube = _real_cube(tmp_path)
        try:
            from datetime import datetime, timedelta
            cube.write_static(
                "ndvi", np.full(cube.grid.shape, 0.4, dtype="float32"),
                source="t", native_res_m=10.0, producer="t")
            ts = [datetime(2026, 1, 1) + timedelta(hours=i)
                  for i in range(3)]
            H, W = cube.grid.shape
            cube.write_3d(
                "temp_c", ts,
                np.full((3, H, W), 25.0, dtype="float32"),
                source="t", native_res_m=10.0, producer="t")

            da = DataAdapter([
                DataNeed("ndvi", kind="static"),
                DataNeed("temp_c", kind="time"),
            ])
            out = da.fetch(cube)
            assert out["ndvi"].shape == cube.grid.shape
            ts_out, arr = out["temp_c"]
            assert len(ts_out) == 3
            assert arr.shape == (3, H, W)
        finally:
            cube.close()

    def test_fetch_required_missing_raises(self, tmp_path):
        cube = _real_cube(tmp_path)
        try:
            da = DataAdapter([DataNeed("required_var", required=True)])
            with pytest.raises(RuntimeError, match="required"):
                da.fetch(cube)
        finally:
            cube.close()

    def test_fetch_optional_missing_returns_none(self, tmp_path):
        cube = _real_cube(tmp_path)
        try:
            da = DataAdapter([DataNeed("never_written", required=False)])
            out = da.fetch(cube)
            assert out["never_written"] is None
        finally:
            cube.close()


# ====================================================== fake subprocess
# A tiny "model binary": reads inputs/inputs.json + inputs/*.npy from
# the stage dir, computes input_a + input_b * 2, writes output.npy.
# Kept at module scope so it's importable + the path is stable.
_FAKE_BINARY_SOURCE = '''\
#!/usr/bin/env python3
import json, sys, numpy as np
from pathlib import Path
stage = Path(sys.argv[1])
inputs = json.loads((stage / "inputs.json").read_text())
a = np.load(stage / inputs["a"])
b = np.load(stage / inputs["b"])
out = (a + b * 2.0).astype("float32")
np.save(stage / "output.npy", out)
'''


@pytest.fixture
def fake_binary(tmp_path: Path) -> Path:
    """Write a tiny standalone Python script and chmod +x so the
    ModelAdapter can subprocess it like a real binary."""
    p = tmp_path / "fake_model.py"
    p.write_text(_FAKE_BINARY_SOURCE)
    p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return p


# ====================================================== ModelAdapter
class _FakeBinaryModel(ModelAdapter):
    """Plug-in model: declare two inputs at different required
    resolutions, run a subprocess that combines them, parse a single
    output ndarray."""
    name = "fake_binary_model"
    data_adapter = DataAdapter([
        DataNeed("var_a", kind="static", max_native_res_m=30.0),
        DataNeed("var_b", kind="static"),
    ])
    produces = (VarSpec("combined", kind="static"),)
    capabilities = ProducerCapabilities()

    def __init__(self, binary: Path):
        self.binary = binary

    def stage_inputs(self, grid, inputs, request, stage):
        np.save(stage / "var_a.npy", inputs["var_a"])
        np.save(stage / "var_b.npy", inputs["var_b"])
        (stage / "inputs.json").write_text(
            json.dumps({"a": "var_a.npy", "b": "var_b.npy"}))

    def run_model(self, stage, request):
        subprocess.run(
            [sys.executable, str(self.binary), str(stage)],
            check=True)
        return stage / "output.npy"

    def parse_outputs(self, output_path, grid):
        arr = np.load(output_path)
        return {"combined": arr}


class TestModelAdapter:
    """End-to-end tests for the model adapter pattern."""

    def test_run_via_pipeline_runner(self, tmp_path, fake_binary):
        cube = _real_cube(tmp_path)
        try:
            # Seed two inputs at 10m native res (well within the 30m limit
            # declared on var_a).
            cube.write_static(
                "var_a", np.full(cube.grid.shape, 3.0, dtype="float32"),
                source="t", native_res_m=10.0, producer="t")
            cube.write_static(
                "var_b", np.full(cube.grid.shape, 4.0, dtype="float32"),
                source="t", native_res_m=10.0, producer="t")

            model = _FakeBinaryModel(binary=fake_binary)
            reg = to_engine_registry([model])
            pipe = Pipeline.from_targets(["combined"], registry=reg)
            runner = PipelineRunner(reg, backend=SerialBackend(),
                                     verbose=False)
            res = runner.run(cube, pipe)
            assert res.ok, [s.error for s in res.steps if s.status == "error"]

            # Model computed 3 + 4*2 = 11 everywhere
            combined = cube.read_static("combined")
            assert combined.shape == cube.grid.shape
            assert float(combined.mean()) == pytest.approx(11.0)
        finally:
            cube.close()

    def test_re_run_skips_model_when_outputs_satisfied(self, tmp_path,
                                                       fake_binary):
        """Once 'combined' is in the cube, a second run is a no-op."""
        cube = _real_cube(tmp_path)
        try:
            cube.write_static(
                "var_a", np.full(cube.grid.shape, 1.0, dtype="float32"),
                source="t", native_res_m=10.0, producer="t")
            cube.write_static(
                "var_b", np.full(cube.grid.shape, 1.0, dtype="float32"),
                source="t", native_res_m=10.0, producer="t")
            model = _FakeBinaryModel(binary=fake_binary)
            reg = to_engine_registry([model])
            pipe = Pipeline.from_targets(["combined"], registry=reg)
            runner = PipelineRunner(reg, backend=SerialBackend(),
                                     verbose=False)
            res1 = runner.run(cube, pipe)
            assert res1.by_name()["fake_binary_model"].status == "ok"
            res2 = runner.run(cube, pipe)
            assert res2.by_name()["fake_binary_model"].status == "skipped"
        finally:
            cube.close()

    def test_dirty_propagation_through_model_adapter(self, tmp_path,
                                                     fake_binary):
        """Bump var_a; the model output is now stale; runner re-executes."""
        import time
        cube = _real_cube(tmp_path)
        try:
            cube.write_static(
                "var_a", np.full(cube.grid.shape, 1.0, dtype="float32"),
                source="t", native_res_m=10.0, producer="t")
            cube.write_static(
                "var_b", np.full(cube.grid.shape, 1.0, dtype="float32"),
                source="t", native_res_m=10.0, producer="t")
            model = _FakeBinaryModel(binary=fake_binary)
            reg = to_engine_registry([model])
            pipe = Pipeline.from_targets(["combined"], registry=reg)
            runner = PipelineRunner(reg, backend=SerialBackend(),
                                     verbose=False)
            runner.run(cube, pipe)
            # initial output: 1 + 1*2 = 3
            assert float(cube.read_static("combined").mean()) == 3.0

            # Re-fetch var_a with a new value -> model output stale
            time.sleep(0.05)
            cube.write_static(
                "var_a", np.full(cube.grid.shape, 10.0, dtype="float32"),
                source="t2", native_res_m=10.0, producer="t")
            runner.run(cube, pipe)
            # now: 10 + 1*2 = 12
            assert float(cube.read_static("combined").mean()) == 12.0
        finally:
            cube.close()

    def test_model_with_required_input_missing_raises(self, tmp_path,
                                                       fake_binary):
        """Missing required input: model fails fast with a clear error."""
        cube = _real_cube(tmp_path)
        try:
            # var_a is missing; var_b is present
            cube.write_static(
                "var_b", np.full(cube.grid.shape, 1.0, dtype="float32"),
                source="t", native_res_m=10.0, producer="t")
            model = _FakeBinaryModel(binary=fake_binary)
            reg = to_engine_registry([model])
            pipe = Pipeline.from_targets(["combined"], registry=reg)
            runner = PipelineRunner(reg, backend=SerialBackend(),
                                     verbose=False)
            res = runner.run(cube, pipe)
            assert not res.ok
            err = res.by_name()["fake_binary_model"].error or ""
            assert "var_a" in err
        finally:
            cube.close()

    def test_parse_outputs_must_match_declared(self, tmp_path, fake_binary):
        """If parse_outputs returns the wrong keys, the runner surfaces
        a contract-violation error."""

        class _MisbehavingModel(_FakeBinaryModel):
            def parse_outputs(self, output_path, grid):
                # Returns the WRONG key on purpose.
                arr = np.load(output_path)
                return {"surprise": arr}

        cube = _real_cube(tmp_path)
        try:
            cube.write_static(
                "var_a", np.full(cube.grid.shape, 1.0, dtype="float32"),
                source="t", native_res_m=10.0, producer="t")
            cube.write_static(
                "var_b", np.full(cube.grid.shape, 1.0, dtype="float32"),
                source="t", native_res_m=10.0, producer="t")
            reg = to_engine_registry([_MisbehavingModel(binary=fake_binary)])
            pipe = Pipeline.from_targets(["combined"], registry=reg)
            runner = PipelineRunner(reg, backend=SerialBackend(),
                                     verbose=False)
            res = runner.run(cube, pipe)
            assert not res.ok
            assert "parse_outputs" in res.by_name()[
                "fake_binary_model"].error
        finally:
            cube.close()
