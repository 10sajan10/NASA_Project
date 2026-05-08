"""Tests for the engine <-> legacy-fusion bridge.

Validates that:
  * fusion.ProducerRegistry can be wrapped by `to_engine_registry`
    without modifying the producers themselves
  * the satisfaction-skip in PipelineRunner honors fusion's per-variable
    `is_satisfied(cube, variable, request)` contract
  * run/skip/error paths each surface the correct StepResult.status
  * pipeline.run_full_via_engine drives an end-to-end run through the
    PipelineRunner over fusion-shaped producers
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine import (
    Pipeline,
    PipelineRunner,
    SerialBackend,
    ThreadBackend,
    to_engine_registry,
)


# --------------------------------------------------------------- fakes
@dataclass
class FusionShapedProducer:
    """Mimics the fusion.Producer protocol (legacy):

      * name : str
      * produces : list[str]
      * requires : list[str]
      * is_satisfied(cube, variable, request) -> bool   # per-variable
      * run(cube, request) -> list[str]                  # legacy return shape
    """
    name: str
    produces: list[str]
    requires: list[str] = field(default_factory=list)
    side_effect: Any = None
    _calls: list[Any] = field(default_factory=list)

    def is_satisfied(self, cube, variable, request) -> bool:
        if getattr(request, "force", False):
            return False
        return cube.has(variable)

    def run(self, cube, request) -> list[str]:
        self._calls.append((cube, request))
        if isinstance(self.side_effect, BaseException):
            raise self.side_effect
        for var in self.produces:
            cube.write(var)
        return list(self.produces)

    @property
    def call_count(self) -> int:
        return len(self._calls)


class StubLegacyRegistry:
    """Minimal stand-in for fusion.ProducerRegistry: just exposes
    `producers()` returning an iterable. The bridge depends only on this."""

    def __init__(self, producers):
        self._producers = list(producers)

    def producers(self):
        return list(self._producers)


class CubeStub:
    """Stub cube that only tracks which variables exist."""

    def __init__(self, present=()):
        self.present: set[str] = set(present)
        self.exported = False

    def has(self, var, t=None):
        return var in self.present

    def write(self, var):
        self.present.add(var)

    def export_catalog(self):
        self.exported = True


# --------------------------------------------------------------- tests
def test_bridge_accepts_fusion_registry_and_iterables():
    a = FusionShapedProducer("a", produces=["x"])
    b = FusionShapedProducer("b", produces=["y"], requires=["x"])

    eng_from_registry = to_engine_registry(StubLegacyRegistry([a, b]))
    eng_from_iter = to_engine_registry([a, b])

    for eng in (eng_from_registry, eng_from_iter):
        assert eng.has("a") and eng.has("b")
        assert eng.producer_for("x") is a
        assert eng.producer_for("y") is b


def test_runner_runs_fusion_shaped_producers_in_dep_order():
    a = FusionShapedProducer("a", produces=["x"])
    b = FusionShapedProducer("b", produces=["y"], requires=["x"])
    eng = to_engine_registry([a, b])
    pipeline = Pipeline.from_targets(["y"], registry=eng)
    cube = CubeStub()
    runner = PipelineRunner(eng, backend=SerialBackend(), verbose=False)
    res = runner.run(cube, pipeline,
                     t_start=datetime(2036, 9, 15),
                     t_end=datetime(2036, 9, 16))
    assert res.ok
    assert [s.name for s in res.steps] == ["a", "b"]
    assert all(s.status == "ok" for s in res.steps)
    assert "x" in cube.present and "y" in cube.present


def test_runner_skips_when_cube_already_satisfies_producer():
    a = FusionShapedProducer("a", produces=["x"])
    b = FusionShapedProducer("b", produces=["y"], requires=["x"])
    eng = to_engine_registry([a, b])
    pipeline = Pipeline.from_targets(["y"], registry=eng)
    # Pre-populate the cube; a should be skipped, b should still run.
    cube = CubeStub(present={"x"})
    runner = PipelineRunner(eng, backend=SerialBackend(), verbose=False)
    res = runner.run(cube, pipeline,
                     t_start=datetime(2036, 9, 15),
                     t_end=datetime(2036, 9, 16))
    by = res.by_name()
    assert by["a"].status == "skipped"
    assert by["b"].status == "ok"
    assert a.call_count == 0
    assert b.call_count == 1


def test_runner_force_overrides_skip():
    a = FusionShapedProducer("a", produces=["x"])
    eng = to_engine_registry([a])
    pipeline = Pipeline().add("a")
    cube = CubeStub(present={"x"})
    runner = PipelineRunner(eng, backend=SerialBackend(), verbose=False)
    res = runner.run(cube, pipeline,
                     t_start=datetime(2036, 9, 15),
                     t_end=datetime(2036, 9, 16),
                     force=True)
    assert res.by_name()["a"].status == "ok"
    assert a.call_count == 1


def test_runner_skip_and_run_mix_on_thread_backend():
    a = FusionShapedProducer("a", produces=["x"])
    b = FusionShapedProducer("b", produces=["y"])
    c = FusionShapedProducer("c", produces=["z"], requires=["x", "y"])
    eng = to_engine_registry([a, b, c])
    pipeline = Pipeline.from_targets(["z"], registry=eng)
    cube = CubeStub(present={"x"})           # a is satisfied; b and c run
    runner = PipelineRunner(eng,
                            backend=ThreadBackend(max_workers=2),
                            verbose=False)
    res = runner.run(cube, pipeline,
                     t_start=datetime(2036, 9, 15),
                     t_end=datetime(2036, 9, 16))
    by = res.by_name()
    assert by["a"].status == "skipped"
    assert by["b"].status == "ok"
    assert by["c"].status == "ok"


def test_request_n_days_compat_with_legacy_producers():
    """Legacy producers may call `request.n_days`; engine.Request must answer."""
    captured = {}

    class NDayConsumer(FusionShapedProducer):
        def run(self, cube, request):
            captured["n_days"] = request.n_days
            return super().run(cube, request)

    p = NDayConsumer("p", produces=["x"])
    eng = to_engine_registry([p])
    runner = PipelineRunner(eng, backend=SerialBackend(), verbose=False)
    runner.run(CubeStub(), Pipeline().add("p"),
               t_start=datetime(2036, 9, 15),
               t_end=datetime(2036, 9, 25))
    assert captured["n_days"] == 10


def test_run_full_via_engine_smoke_with_resolver_shaped_registry():
    """End-to-end smoke: build a fusion-shaped resolver-equivalent registry,
    run pipeline.run_full_via_engine. We patch run_full_via_engine's
    expectation that resolver.registry.get('fire_model') exists by mounting
    a producer named 'fire_model' that produces 'arrival_s'."""
    import pipeline as pipe
    from fusion.producers import ProducerRegistry as FusionRegistry
    from fusion.resolver import DependencyResolver

    layer0 = FusionShapedProducer("layer0", produces=["fbfm40"])
    weather = FusionShapedProducer("weather",
                                    produces=["temp_c", "rh"])
    fire = FusionShapedProducer("fire_model",
                                 produces=["arrival_s"],
                                 requires=["fbfm40", "temp_c", "rh"])

    reg = FusionRegistry()
    for p in (layer0, weather, fire):
        reg.register(p)
    resolver = DependencyResolver(reg)

    cube = CubeStub()

    # Fake a `cube.grid.pixel_m` etc. The function only calls
    # `cube.export_catalog()` after success, which CubeStub supports.
    pipe.run_full_via_engine(
        cube, day0=datetime(2036, 9, 15), n_days=3,
        resolver=resolver,
        backend_mode="serial",
        print_plan=False, verbose=False)

    assert cube.exported
    assert "arrival_s" in cube.present
    assert layer0.call_count == 1
    assert weather.call_count == 1
    assert fire.call_count == 1


def test_run_full_via_engine_raises_on_failure():
    import pipeline as pipe
    from fusion.producers import ProducerRegistry as FusionRegistry
    from fusion.resolver import DependencyResolver

    fire = FusionShapedProducer(
        "fire_model", produces=["arrival_s"],
        side_effect=RuntimeError("boom"))
    reg = FusionRegistry()
    reg.register(fire)
    resolver = DependencyResolver(reg)

    with pytest.raises(RuntimeError, match="engine run failed"):
        pipe.run_full_via_engine(
            CubeStub(), day0=datetime(2036, 9, 15), n_days=1,
            resolver=resolver,
            backend_mode="serial",
            print_plan=False, verbose=False)
