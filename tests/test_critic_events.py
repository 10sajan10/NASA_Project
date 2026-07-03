"""Critic verification/replan loop and the event bus."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agentic import ComputeBudget, MetaCatalog, plan
from agentic.critic import critique, replan
from agentic.events import EventWatcher, handle_event, normalize_event
from agentic.planner import PlanError
from tests.test_agentic import DALLAS, toy_metacat


def _cube(tmp_path):
    from cube.grid import SimulationGrid
    from cube.store import Cube
    grid = SimulationGrid.from_center_radius(-96.809, 32.780,
                                             5_000.0, 1_000.0)
    return Cube(tmp_path / "cube", grid)


def _write(cube, var, arr, units=""):
    cube.write_static(var, arr.astype("float32"), source="test",
                      native_res_m=float(cube.grid.pixel_m), units=units)


def _dallas_plan(metacat=None):
    return plan(DALLAS, metacat or toy_metacat(),
                budget=ComputeBudget(cores=4, wall_s=3600)).to_dict()


# ---------------------------------------------------------------- critic
def test_critique_passes_on_good_outputs(tmp_path):
    cube = _cube(tmp_path)
    try:
        p = _dallas_plan()
        shape = cube.grid.shape
        _write(cube, "economic_loss_usd", np.full(shape, 1e6), "USD")
        report = critique(p, cube)
        assert report.ok and not report.findings
        assert report.coverage["economic_loss_usd"] == 1.0
    finally:
        cube.close()


def test_critique_flags_missing_and_insane_outputs(tmp_path):
    cube = _cube(tmp_path)
    try:
        p = _dallas_plan()
        # never materialised -> error naming the bound producer
        report = critique(p, cube)
        assert not report.ok
        assert report.findings[0].severity == "error"
        assert "econ_io" in report.replan_excludes

        # negative loss -> sanity-bound error
        _write(cube, "economic_loss_usd",
               np.full(cube.grid.shape, -5.0), "USD")
        report = critique(p, cube)
        assert not report.ok
        assert any("outside sane range" in f.message
                   for f in report.findings)

        # mostly-NaN raster -> coverage warning
        arr = np.full(cube.grid.shape, np.nan)
        arr[0, 0] = 1.0
        _write(cube, "economic_loss_usd", arr, "USD")
        report = critique(p, cube)
        assert any("footprint has data" in f.message
                   for f in report.findings)
    finally:
        cube.close()


def test_replan_binds_next_best_candidate(tmp_path):
    mc = toy_metacat()
    p = _dallas_plan(mc)
    assert p["bindings"]["economic_loss_usd"]["producer"] == "econ_io"

    cube = _cube(tmp_path)
    try:
        report = critique(p, cube)          # nothing materialised
        new_plan = replan(p, report, mc)
        # econ_io failed -> the alternative economy model binds
        assert new_plan.bindings["economic_loss_usd"].producer == "econ_quick"
    finally:
        cube.close()


def test_replan_raises_when_no_alternative_exists(tmp_path):
    mc = toy_metacat()
    p = _dallas_plan(mc)
    cube = _cube(tmp_path)
    try:
        report = critique(p, cube)
        # exclude BOTH economy models -> honestly unplannable
        from agentic.critic import CritiqueReport
        report = CritiqueReport(ok=False, findings=report.findings,
                                replan_excludes=frozenset(
                                    {"econ_io", "econ_quick"}))
        with pytest.raises(PlanError):
            replan(p, report, mc)
    finally:
        cube.close()


# ------------------------------------------------------------- event bus
def test_normalize_event_aliases():
    ev = normalize_event({"kind": "asteroid_impact",
                          "latitude": 32.78, "longitude": -96.81,
                          "energy_mt": 5.0, "radius_km": 50,
                          "duration_h": 24, "intent": "economic"})
    assert ev.lat == 32.78 and ev.lon == -96.81
    assert ev.magnitude == {"energy_mt": 5.0}
    assert ev.radius_m == 50_000.0 and ev.duration_s == 86_400.0

    with pytest.raises(ValueError):
        normalize_event({"kind": "asteroid_impact", "lat": 1.0})


def test_handle_event_plans_without_executing():
    report = handle_event({"kind": "asteroid_impact", "lat": 32.78,
                           "lon": -96.81, "energy_mt": 5.0,
                           "intent": "economic"}, toy_metacat())
    assert report["ok"]
    assert report["plan"]["bindings"]["economic_loss_usd"]["producer"] \
        == "econ_io"
    assert "execution" not in report

    bad = handle_event({"kind": "asteroid_impact", "lat": 48.85,
                        "lon": 2.35, "intent": "economic",
                        "energy_mt": 5.0}, toy_metacat())
    assert not bad["ok"] and bad["variable"] == "asset_value_usd"


def test_event_watcher_processes_inbox(tmp_path):
    inbox, outbox = tmp_path / "inbox", tmp_path / "out"
    watcher = EventWatcher(inbox=inbox, outbox=outbox,
                           metacat=toy_metacat())

    (inbox / "dallas.json").parent.mkdir(parents=True, exist_ok=True)
    (inbox / "dallas.json").write_text(json.dumps(
        {"kind": "asteroid_impact", "lat": 32.78, "lon": -96.81,
         "energy_mt": 5.0, "intent": "economic"}))
    (inbox / "broken.json").write_text("{not json")

    reports = watcher.poll_once()
    assert len(reports) == 2
    assert not list(inbox.glob("*.json"))          # archived

    good = json.loads((outbox / "dallas.report.json").read_text())
    assert good["ok"] and good["plan"]["targets"] == ["economic_loss_usd"]
    bad = json.loads((outbox / "broken.report.json").read_text())
    assert not bad["ok"] and "unparseable" in bad["error"]

    assert watcher.poll_once() == []               # inbox is drained
