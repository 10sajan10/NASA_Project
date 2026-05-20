"""engine.log: project-wide logging facade tests.

Covers:
  * get_logger() maps module names into the `nasa_project.*` tree
  * configure() is idempotent (re-running cleans previous handlers)
  * file + console handlers are attached when configured
  * RunContext carries run_id and log_file path
  * RunResult.to_dict / save_json round-trip
  * PipelineRunner.result_dir + result_path persistence

Pure stdlib + a real cube, no network.
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine import (
    Pipeline,
    PipelineRunner,
    ProducerCapabilities,
    ProducerRegistry,
    ProducerV2,
    VarSpec,
)
from engine.log import (
    RunContext,
    configure as configure_logging,
    get_logger,
)
from engine.scheduler import RunResult, StepResult, TileMetric


# ============================================================ get_logger
def test_get_logger_namespaces_module_loggers():
    log = get_logger("engine.scheduler")
    assert log.name == "nasa_project.engine.scheduler"


def test_get_logger_passes_through_already_namespaced():
    log = get_logger("nasa_project.drivers.era5_wind")
    assert log.name == "nasa_project.drivers.era5_wind"


def test_get_logger_works_before_configure():
    """Module imports happen before configure() is called; calls must
    not error and must still produce a Logger object."""
    log = get_logger("engine.test_module")
    log.info("this should not raise")


# ============================================================ configure
def test_configure_attaches_console_and_file_handlers(tmp_path):
    ctx = configure_logging(log_dir=tmp_path)
    root = logging.getLogger("nasa_project")
    handler_types = {type(h).__name__ for h in root.handlers}
    assert "StreamHandler" in handler_types
    assert "RotatingFileHandler" in handler_types
    assert ctx.log_file is not None
    assert ctx.log_file.parent == tmp_path
    assert ctx.log_file.name.endswith(".log")


def test_configure_is_idempotent(tmp_path):
    configure_logging(log_dir=tmp_path, label="first")
    n_first = len(logging.getLogger("nasa_project").handlers)
    configure_logging(log_dir=tmp_path, label="second")
    n_second = len(logging.getLogger("nasa_project").handlers)
    # Second call must clear and re-add, not double-attach.
    assert n_first == n_second


def test_configure_quiet_suppresses_console(tmp_path):
    configure_logging(log_dir=tmp_path, quiet=True)
    root = logging.getLogger("nasa_project")
    types = [type(h).__name__ for h in root.handlers]
    assert "StreamHandler" not in types
    assert "RotatingFileHandler" in types


def test_configure_returns_run_context_with_label(tmp_path):
    ctx = configure_logging(log_dir=tmp_path, label="scenario_x")
    assert isinstance(ctx, RunContext)
    assert "scenario_x" in ctx.run_id
    # Path-safe label (no separators / specials)
    assert "/" not in ctx.run_id


def test_log_records_land_in_file(tmp_path):
    ctx = configure_logging(log_dir=tmp_path, level="DEBUG")
    log = get_logger("engine.test")
    log.info("hello-from-test")
    log.debug("debug-from-test")
    # Force handler flush
    for h in logging.getLogger("nasa_project").handlers:
        h.flush()
    text = ctx.log_file.read_text()
    assert "hello-from-test" in text
    assert "debug-from-test" in text


# ============================================================ to_dict
def test_tile_metric_to_dict():
    tm = TileMetric(index=3, elapsed_s=0.5, attempts=1, status="ok")
    d = tm.to_dict()
    assert d == {"index": 3, "elapsed_s": 0.5, "attempts": 1,
                  "status": "ok", "error": None}


def test_step_result_to_dict_carries_produced():
    sr = StepResult(
        name="foo", status="ok", elapsed_s=1.25,
        produced={"v1": 0, "v2": 1}, attempts=2)
    d = sr.to_dict()
    assert d["name"] == "foo"
    assert d["status"] == "ok"
    assert d["elapsed_s"] == 1.25
    assert d["produced"] == {"v1": 0, "v2": 1}
    assert d["attempts"] == 2
    assert d["tile_metrics"] == []


def test_run_result_to_dict_aggregates_counts():
    rr = RunResult(steps=[
        StepResult(name="a", status="ok", elapsed_s=0.1),
        StepResult(name="b", status="skipped", elapsed_s=0.0),
        StepResult(name="c", status="error", elapsed_s=0.5, error="boom"),
    ])
    d = rr.to_dict()
    assert d["ok"] is False
    assert d["n_steps"] == 3
    assert d["n_errors"] == 1
    assert d["n_skipped"] == 1
    assert d["total_elapsed_s"] == pytest.approx(0.6)
    assert len(d["steps"]) == 3


def test_run_result_save_json_round_trip(tmp_path):
    rr = RunResult(steps=[StepResult(name="x", status="ok", elapsed_s=0.42)])
    out = tmp_path / "subdir" / "run.json"
    rr.save_json(out)
    data = json.loads(out.read_text())
    assert data["ok"] is True
    assert data["steps"][0]["name"] == "x"
    assert out.parent.exists()


# ============================================================ runner persists
class _NoopProducer(ProducerV2):
    """Minimal producer that materialises one declared variable so the
    runner has something concrete to schedule. Writes directly via
    `run` so it can size the array off the live cube grid."""
    name = "noop"
    produces = (VarSpec("noop_var", kind="static", dtype="float32"),)
    requires = ()
    capabilities = ProducerCapabilities()

    def run(self, cube, request):
        import numpy as np
        H, W = cube.grid.shape
        cube.write_static(
            "noop_var", np.zeros((H, W), dtype="float32"),
            source="test", native_res_m=float(cube.grid.pixel_m),
            producer=self.name)
        return {"noop_var": 0}

    def compute(self, inputs, request):
        raise AssertionError("run() should bypass compute")


def _real_cube(tmp_path):
    from cube.grid import SimulationGrid
    from cube.store import Cube
    grid = SimulationGrid.from_center_radius(
        -96.797, 32.776, 500.0, 500.0)  # 2 x 2 cells
    return Cube(tmp_path, grid)


def test_runner_persists_runresult_when_result_dir_set(tmp_path):
    """`PipelineRunner(result_dir=...).run(...)` drops a JSON file."""
    cube = _real_cube(tmp_path / "cube")
    try:
        reg = ProducerRegistry()
        reg.register(_NoopProducer())
        results_dir = tmp_path / "results"
        runner = PipelineRunner(reg, verbose=False, result_dir=results_dir)
        rr = runner.run(
            cube,
            Pipeline.from_targets(["noop_var"], registry=reg))
        assert rr.ok
        dumps = list(results_dir.glob("runresult_*.json"))
        assert len(dumps) == 1, f"expected 1 dump, got {dumps}"
        data = json.loads(dumps[0].read_text())
        assert data["ok"] is True
        assert any(s["name"] == "noop" for s in data["steps"])
    finally:
        cube.close()


def test_runner_per_call_result_path_overrides_result_dir(tmp_path):
    cube = _real_cube(tmp_path / "cube")
    try:
        reg = ProducerRegistry()
        reg.register(_NoopProducer())
        target = tmp_path / "specific" / "my_run.json"
        runner = PipelineRunner(
            reg, verbose=False,
            result_dir=tmp_path / "default")
        runner.run(
            cube,
            Pipeline.from_targets(["noop_var"], registry=reg),
            result_path=target)
        assert target.exists()
        # The default `result_dir` should not have been used.
        assert not (tmp_path / "default").exists() or \
               not any((tmp_path / "default").iterdir())
    finally:
        cube.close()


def test_runner_result_path_false_disables_dump(tmp_path):
    cube = _real_cube(tmp_path / "cube")
    try:
        reg = ProducerRegistry()
        reg.register(_NoopProducer())
        results_dir = tmp_path / "results"
        runner = PipelineRunner(reg, verbose=False, result_dir=results_dir)
        runner.run(
            cube,
            Pipeline.from_targets(["noop_var"], registry=reg),
            result_path=False)
        assert not results_dir.exists() or \
               not any(results_dir.iterdir())
    finally:
        cube.close()


def test_runner_dumps_even_on_failure(tmp_path):
    """A failing step still gets a JSON record — the whole point of the
    persistence layer is post-mortem debugging."""
    class _FailingProducer(ProducerV2):
        name = "boom"
        produces = (VarSpec("boom_var", kind="static", dtype="float32"),)
        requires = ()
        capabilities = ProducerCapabilities()

        def compute(self, inputs, request):
            raise RuntimeError("intentional failure")

    cube = _real_cube(tmp_path / "cube")
    try:
        reg = ProducerRegistry()
        reg.register(_FailingProducer())
        results_dir = tmp_path / "results"
        runner = PipelineRunner(reg, verbose=False, result_dir=results_dir)
        rr = runner.run(
            cube,
            Pipeline.from_targets(["boom_var"], registry=reg))
        assert not rr.ok
        dumps = list(results_dir.glob("runresult_*.json"))
        assert len(dumps) == 1
        data = json.loads(dumps[0].read_text())
        assert data["n_errors"] == 1
        # Step's error string is preserved
        boom = [s for s in data["steps"] if s["name"] == "boom"][0]
        assert "intentional failure" in (boom["error"] or "")
    finally:
        cube.close()
