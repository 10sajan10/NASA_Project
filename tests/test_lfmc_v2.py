"""Validation: the migrated lfmc_v2.LfmcModel passes the engine contract
test kit AND produces correct numerical output end-to-end on a real cube.

This is the first real model migrated to ProducerV2. Its passing here
proves the contract bites against actual code, not just synthetic
fixtures.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine import (
    Pipeline,
    PipelineRunner,
    SerialBackend,
    check_producer,
    to_engine_registry,
)
from models.lfmc_v2 import LfmcModel


# ---------------------------------------------------- contract kit
def _seed_ndwi(cube):
    """Seed the FakeCube with a synthetic NDWI field."""
    rng = np.random.default_rng(42)
    cube.seed_static("ndwi",
                     rng.uniform(-0.2, 0.6, size=(8, 8)).astype("float32"))


def test_lfmc_v2_passes_engine_contract():
    """capabilities + IO isolation + idempotency."""
    errs = check_producer(LfmcModel(), seed=_seed_ndwi)
    assert errs == [], errs


def test_lfmc_v2_returns_only_declared_outputs():
    """compute() must not leak undeclared keys."""
    p = LfmcModel()
    inputs = {"ndwi": np.full((4, 4), 0.3, dtype="float32")}
    out = p.compute(inputs, request=None)
    assert set(out.keys()) == {"lfmc_pct"}


# ---------------------------------------------------- numerical correctness
def test_lfmc_v2_implements_yebra_regression():
    p = LfmcModel()
    ndwi = np.array([[-0.1, 0.0, 0.3, 0.5]], dtype="float32")
    lfmc = p.compute({"ndwi": ndwi}, request=None)["lfmc_pct"]
    expected = np.clip(125.0 + 288.0 * ndwi, 30.0, 250.0).astype("float32")
    assert np.allclose(lfmc, expected, atol=1e-3)


def test_lfmc_v2_fills_nan_with_median():
    p = LfmcModel()
    ndwi = np.array([[0.1, np.nan, 0.3]], dtype="float32")
    out = p.compute({"ndwi": ndwi}, request=None)["lfmc_pct"]
    # NaN cell should be replaced with finite value, not propagated
    assert np.all(np.isfinite(out))


def test_lfmc_v2_fallback_when_all_input_is_nan():
    """If the whole NDWI mosaic is NaN, output is uniform fallback (100%)."""
    p = LfmcModel()
    ndwi = np.full((4, 4), np.nan, dtype="float32")
    out = p.compute({"ndwi": ndwi}, request=None)["lfmc_pct"]
    assert float(out.mean()) == pytest.approx(100.0)


def test_lfmc_v2_clips_to_physical_range():
    p = LfmcModel()
    # Force values past both clip bounds.
    ndwi = np.array([[-1.0, 1.0]], dtype="float32")
    out = p.compute({"ndwi": ndwi}, request=None)["lfmc_pct"]
    assert float(out.min()) >= 30.0
    assert float(out.max()) <= 250.0


# ---------------------------------------------------- end-to-end on real cube
def _real_cube(tmp_path: Path):
    from cube.grid import SimulationGrid
    from cube.store import Cube
    grid = SimulationGrid.from_center_radius(-96.797, 32.776, 5_000.0, 500.0)
    return Cube(tmp_path, grid)


def test_lfmc_v2_runs_via_pipeline_runner_on_real_cube(tmp_path):
    """Plug the producer into the engine, write LFMC to a real cube,
    read it back, verify shape + value range."""
    cube = _real_cube(tmp_path)
    try:
        # Seed NDWI with a realistic-ish field.
        rng = np.random.default_rng(7)
        ndwi = rng.uniform(-0.1, 0.4, size=cube.grid.shape).astype("float32")
        cube.write_static(
            "ndwi", ndwi, source="test", native_res_m=10.0, units="",
            producer="test")

        reg = to_engine_registry([LfmcModel()])
        pipeline = Pipeline.from_targets(["lfmc_pct"], registry=reg)
        runner = PipelineRunner(reg, backend=SerialBackend(), verbose=False)
        res = runner.run(cube, pipeline)
        assert res.ok, [s.error for s in res.steps if s.status == "error"]

        assert cube.has("lfmc_pct")
        lfmc = cube.read_static("lfmc_pct")
        assert lfmc.shape == cube.grid.shape
        assert float(lfmc.min()) >= 30.0
        assert float(lfmc.max()) <= 250.0
        # Mean LFMC for NDWI~0.15 should be roughly 125 + 288*0.15 = 168
        assert 80.0 < float(lfmc.mean()) < 220.0
    finally:
        cube.close()


def test_lfmc_v2_skipped_on_re_run(tmp_path):
    """is_satisfied honored by the runner: second run is a no-op."""
    cube = _real_cube(tmp_path)
    try:
        ndwi = np.full(cube.grid.shape, 0.2, dtype="float32")
        cube.write_static("ndwi", ndwi, source="test",
                          native_res_m=10.0, producer="test")
        reg = to_engine_registry([LfmcModel()])
        pipeline = Pipeline.from_targets(["lfmc_pct"], registry=reg)
        runner = PipelineRunner(reg, backend=SerialBackend(), verbose=False)
        runner.run(cube, pipeline)
        # Re-run should report skipped
        res2 = runner.run(cube, pipeline)
        assert res2.by_name()["lfmc_v2"].status == "skipped"
    finally:
        cube.close()
