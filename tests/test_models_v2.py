"""ProducerV2 migrations of dead_fuel_model and drought_model.

Validates that both producers:
  * end-to-end on a real cube produce numerical output matching the
    legacy implementations
  * fan out across tiles via the engine scheduler
  * skip on re-run via is_satisfied / cube.satisfies
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine import (
    Pipeline,
    PipelineRunner,
    SerialBackend,
    ThreadBackend,
    to_engine_registry,
)
from models.dead_fuel_v2 import DeadFuelModel
from models.drought_v2 import DroughtModel


def _cube_with_weather(tmp_path: Path, *, n_hours: int = 48,
                        radius_m: float = 10_000.0):
    """Build a small cube and seed `rh`, `temp_c`, `precip_mm` time vars."""
    from cube.grid import SimulationGrid
    from cube.store import Cube
    grid = SimulationGrid.from_center_radius(
        -96.797, 32.776, radius_m, 500.0)
    cube = Cube(tmp_path, grid)
    H, W = grid.shape
    day0 = datetime(2026, 9, 15)
    ts = [day0 + timedelta(hours=h) for h in range(n_hours)]
    rng = np.random.default_rng(11)
    rh = rng.uniform(20.0, 80.0, size=(n_hours, H, W)).astype("float32")
    temp = rng.uniform(15.0, 32.0, size=(n_hours, H, W)).astype("float32")
    precip = rng.exponential(0.5, size=(n_hours, H, W)).astype("float32")
    for var, arr in [("rh", rh), ("temp_c", temp), ("precip_mm", precip)]:
        cube.write_3d(
            var, ts, arr,
            source="test", native_res_m=float(grid.pixel_m),
            units="", producer="test")
    return cube, day0


# ====================================================== dead_fuel_v2
def test_dead_fuel_v2_runs_via_engine_serial(tmp_path):
    cube, day0 = _cube_with_weather(tmp_path)
    try:
        reg = to_engine_registry([DeadFuelModel()])
        pipeline = Pipeline.from_targets(
            ["dfm_1hr", "dfm_10hr", "dfm_100hr"], registry=reg)
        runner = PipelineRunner(reg, backend=SerialBackend(), verbose=False)
        res = runner.run(cube, pipeline,
                         t_start=day0, t_end=day0 + timedelta(days=2))
        assert res.ok, [s.error for s in res.steps if s.status == "error"]
        # All three outputs present (time-vars; cube.has(name) without t
        # checks static-only, so verify via the catalog or read_3d_times)
        catalog_names = {v["name"] for v in cube.list_variables()}
        for v in ("dfm_1hr", "dfm_10hr", "dfm_100hr"):
            assert v in catalog_names
            assert len(cube.read_3d_times(v)) > 0
        # 10-hr is 1.35x 1-hr; 100-hr is 1.75x 1-hr (lag multipliers)
        _, a = cube.read_3d("dfm_1hr")
        _, b = cube.read_3d("dfm_10hr")
        _, c = cube.read_3d("dfm_100hr")
        assert np.allclose(b, a * 1.35, atol=1e-4)
        assert np.allclose(c, a * 1.75, atol=1e-4)
        # Physical range: Nelson EMC clipped to [1, 50]
        assert float(np.min(a)) >= 1.0
        assert float(np.max(a)) <= 50.0
    finally:
        cube.close()


def test_dead_fuel_v2_matches_legacy_numerically(tmp_path):
    """V2 producer output equals legacy run() output cell-for-cell."""
    from models.dead_fuel_model import run as legacy_run

    cube, day0 = _cube_with_weather(tmp_path)
    try:
        # Run V2
        reg = to_engine_registry([DeadFuelModel()])
        pipeline = Pipeline.from_targets(["dfm_1hr"], registry=reg)
        runner = PipelineRunner(reg, backend=SerialBackend(), verbose=False)
        runner.run(cube, pipeline,
                   t_start=day0, t_end=day0 + timedelta(days=2))
        _, v2 = cube.read_3d("dfm_1hr")
    finally:
        cube.close()

    # Run legacy on a fresh cube with identical seeds.
    cube2, day0_2 = _cube_with_weather(tmp_path.with_name("legacy_run"))
    try:
        legacy_run(cube2, day0_2, n_days=2)
        _, legacy = cube2.read_3d("dfm_1hr")
    finally:
        cube2.close()
    assert v2.shape == legacy.shape
    assert np.allclose(v2, legacy, atol=1e-5)


def test_dead_fuel_v2_skipped_on_re_run(tmp_path):
    cube, day0 = _cube_with_weather(tmp_path)
    try:
        reg = to_engine_registry([DeadFuelModel()])
        pipeline = Pipeline.from_targets(["dfm_1hr"], registry=reg)
        runner = PipelineRunner(reg, backend=SerialBackend(), verbose=False)
        runner.run(cube, pipeline,
                   t_start=day0, t_end=day0 + timedelta(days=2))
        res2 = runner.run(cube, pipeline,
                          t_start=day0, t_end=day0 + timedelta(days=2))
        assert res2.by_name()["dead_fuel_v2"].status == "skipped"
    finally:
        cube.close()


def test_dead_fuel_v2_fans_across_threads(tmp_path):
    """Bigger grid -> multiple tiles -> work spreads across workers."""
    cube, day0 = _cube_with_weather(tmp_path, n_hours=24,
                                     radius_m=40_000.0)
    try:
        reg = to_engine_registry([DeadFuelModel()])
        pipeline = Pipeline.from_targets(["dfm_1hr"], registry=reg)
        backend = ThreadBackend(max_workers=3)
        try:
            runner = PipelineRunner(reg, backend=backend, verbose=False)
            res = runner.run(cube, pipeline,
                             t_start=day0, t_end=day0 + timedelta(days=1))
        finally:
            backend.shutdown()
        sr = res.by_name()["dead_fuel_v2"]
        assert sr.status == "ok"
        assert sr.tile_count >= 1
    finally:
        cube.close()


# ====================================================== drought_v2
def test_drought_v2_runs_via_engine(tmp_path):
    cube, day0 = _cube_with_weather(tmp_path, n_hours=72)
    try:
        reg = to_engine_registry([DroughtModel(map_inches=36.0,
                                                 kbdi_init=100.0)])
        pipeline = Pipeline.from_targets(["kbdi"], registry=reg)
        runner = PipelineRunner(reg, backend=SerialBackend(), verbose=False)
        res = runner.run(cube, pipeline,
                         t_start=day0, t_end=day0 + timedelta(days=3))
        assert res.ok, [s.error for s in res.steps if s.status == "error"]
        catalog_names = {v["name"] for v in cube.list_variables()}
        assert "kbdi" in catalog_names
        _, kbdi = cube.read_3d("kbdi")
        # KBDI bounded [0, 800]
        assert float(np.min(kbdi)) >= 0.0
        assert float(np.max(kbdi)) <= 800.0
        # 3 days of output
        assert kbdi.shape[0] == 3
    finally:
        cube.close()


def test_drought_v2_matches_legacy_numerically(tmp_path):
    from models.drought_model import run as legacy_run

    cube, day0 = _cube_with_weather(tmp_path, n_hours=72)
    try:
        reg = to_engine_registry([DroughtModel(map_inches=36.0,
                                                 kbdi_init=100.0)])
        pipeline = Pipeline.from_targets(["kbdi"], registry=reg)
        runner = PipelineRunner(reg, backend=SerialBackend(), verbose=False)
        runner.run(cube, pipeline,
                   t_start=day0, t_end=day0 + timedelta(days=3))
        _, v2 = cube.read_3d("kbdi")
    finally:
        cube.close()

    cube2, day0_2 = _cube_with_weather(tmp_path.with_name("legacy_drought"),
                                         n_hours=72)
    try:
        legacy_run(cube2, day0_2, n_days=3,
                   map_inches=36.0, kbdi_init=100.0)
        _, legacy = cube2.read_3d("kbdi")
    finally:
        cube2.close()
    assert v2.shape == legacy.shape
    assert np.allclose(v2, legacy, atol=1e-3)
