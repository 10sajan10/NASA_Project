"""End-to-end asteroid consequence cascade through the real engine.

The Phase-3 integration test: an economic-loss target backward-chains
impact_scaling -> blast_damage -> econ_loss (+ the exposure driver)
from the default catalog, runs on a real cube, and produces physically
sane rasters. No WRF, no network — the whole chain is pure Python.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agentic import ComputeBudget, MetaCatalog, plan
from engine import Pipeline, PipelineRunner, SerialBackend
from models.catalog import ScenarioConfig, default_catalog, make_context
from tests.test_agentic import DALLAS

CHAIN = {"exposure", "impact_scaling", "blast_damage", "econ_loss"}


def _cube(tmp_path):
    from cube.grid import SimulationGrid
    from cube.store import Cube
    grid = SimulationGrid.from_center_radius(-96.809, 32.780,
                                             20_000.0, 1_000.0)
    return Cube(tmp_path / "cube", grid)


def _context(energy_mt=5.0):
    cfg = ScenarioConfig(kml=Path("unused.kml"), energy_mt=energy_mt)
    return make_context(cfg, install_root=".", templates_dir=".")


def test_economic_intent_binds_consequence_chain():
    mc = MetaCatalog()
    default_catalog().seed_metacatalog(mc)
    p = plan(DALLAS, mc, budget=ComputeBudget(cores=4, wall_s=3600))
    assert set(p.producers) == CHAIN
    assert p.bindings["economic_loss_usd"].producer == "econ_loss"
    assert p.bindings["blast_overpressure_pa"].producer == "impact_scaling"


def test_cascade_runs_end_to_end(tmp_path):
    cube = _cube(tmp_path)
    try:
        reg = default_catalog().build_registry(_context(), only=CHAIN)
        pipeline = Pipeline.from_targets(["economic_loss_usd"],
                                         registry=reg)
        runner = PipelineRunner(reg, backend=SerialBackend(),
                                verbose=False)
        results = runner.run(cube, pipeline)
        assert all(r.status == "ok" for r in results.by_name().values())

        damage = cube.read_static("building_damage_frac")
        loss = cube.read_static("economic_loss_usd")
        exposed = cube.read_static("population_exposure")
        pressure = cube.read_static("blast_overpressure_pa")

        assert np.all(damage >= 0.0) and np.all(damage <= 1.0)
        assert np.all(loss >= 0.0) and np.all(exposed >= 0.0)

        # physics is monotone: centre is hit hardest
        H, W = cube.grid.shape
        ci, cj = H // 2, W // 2
        assert pressure[ci, cj] > pressure[0, 0]
        assert damage[ci, cj] > damage[0, 0]
        assert loss[ci, cj] > loss[0, 0]
        # 5 Mt at ground zero is devastating; the 20 km edge is not
        assert damage[ci, cj] > 0.9
        assert damage[0, 0] < 0.5
        assert float(loss.sum()) > 0.0
    finally:
        cube.close()


def test_energy_scales_the_footprint(tmp_path):
    def peak_damage_at_edge(energy_mt, sub):
        cube = _cube(tmp_path / sub)
        try:
            reg = default_catalog().build_registry(
                _context(energy_mt), only=CHAIN)
            pipeline = Pipeline.from_targets(["building_damage_frac"],
                                             registry=reg)
            PipelineRunner(reg, backend=SerialBackend(),
                           verbose=False).run(cube, pipeline)
            return float(cube.read_static("building_damage_frac")[0, 0])
        finally:
            cube.close()

    assert peak_damage_at_edge(50.0, "big") > \
           peak_damage_at_edge(0.5, "small")
