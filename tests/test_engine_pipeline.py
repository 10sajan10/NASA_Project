"""Tests for the orchestration layer: registry + Pipeline DSL + Runner.

Model-agnostic by design: tests use synthetic mini-producers that just
record their invocations.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine import (
    Pipeline,
    PipelineRunner,
    ProducerRegistry,
    SerialBackend,
    ThreadBackend,
    Trigger,
    make_backend,
    parallel,
)


# ============================================================== fixtures
@dataclass
class MiniProducer:
    """Tiny producer-shaped object for orchestration tests.

    Only declares the bare contract the engine needs: name, produces,
    requires, run(cube, request). No models, no real I/O.
    """
    name: str
    produces: tuple[str, ...]
    requires: tuple[str, ...] = ()
    side_effect: Any = None
    _calls: list[Any] = field(default_factory=list)

    def run(self, cube, request):
        self._calls.append((cube, request))
        if self.side_effect is not None:
            if callable(self.side_effect):
                return self.side_effect(cube, request)
            if isinstance(self.side_effect, BaseException):
                raise self.side_effect
            return self.side_effect
        return {v: 1 for v in self.produces}

    @property
    def call_count(self) -> int:
        return len(self._calls)


class CubeStub:
    """Mutable bag standing in for a Cube in pipeline tests."""

    def __init__(self):
        self.state: dict[str, Any] = {}

    def has(self, name):
        return name in self.state


# ============================================================== registry
def test_registry_register_lookup_unregister():
    reg = ProducerRegistry()
    p = MiniProducer("a", produces=("x",))
    reg.register(p)
    assert "a" in reg
    assert reg.get("a") is p
    assert reg.producer_for("x") is p
    assert reg.has_variable("x")
    reg.unregister("a")
    assert "a" not in reg
    assert not reg.has_variable("x")


def test_registry_rejects_duplicate_variable():
    reg = ProducerRegistry()
    reg.register(MiniProducer("a", produces=("x",)))
    with pytest.raises(ValueError):
        reg.register(MiniProducer("b", produces=("x",)))


def test_registry_replace_swaps_owner():
    reg = ProducerRegistry()
    reg.register(MiniProducer("a", produces=("x",)))
    reg.register(MiniProducer("b", produces=("x",)), replace=True)
    assert reg.producer_for("x").name == "b"


# ============================================================== chain DSL
def test_chain_simple_serial():
    p = Pipeline.chain(["a", "b", "c"])
    layers = p.topological_layers()
    assert layers == [("a",), ("b",), ("c",)]


def test_chain_with_parallel_layer():
    # a -> {b, c (parallel)} -> d
    p = Pipeline.chain(["a", parallel("b", "c"), "d"])
    layers = p.topological_layers()
    assert layers[0] == ("a",)
    assert set(layers[1]) == {"b", "c"}
    assert layers[2] == ("d",)


# ============================================================== DAG DSL
def test_dag_add_with_after():
    p = (Pipeline()
         .add("a")
         .add("b", after="a")
         .add("c", after=["a"])
         .add("d", after=["b", "c"]))
    layers = p.topological_layers()
    assert layers[0] == ("a",)
    assert set(layers[1]) == {"b", "c"}
    assert layers[2] == ("d",)


def test_dag_merge_after_on_re_add():
    p = (Pipeline()
         .add("a").add("b").add("z", after="a")
         .add("z", after="b"))   # union: {a, b}
    z = next(n for n in p.nodes() if n.name == "z")
    assert set(z.after) == {"a", "b"}


def test_dag_unknown_dep_raises():
    p = Pipeline().add("a", after="ghost")
    with pytest.raises(RuntimeError, match="unknown nodes"):
        p.topological_layers()


def test_dag_cycle_raises():
    p = (Pipeline()
         .add("a", after="b")
         .add("b", after="a"))
    with pytest.raises(RuntimeError, match="cycle"):
        p.topological_layers()


# ============================================================== from_targets
def test_from_targets_infers_dag_from_registry():
    reg = ProducerRegistry()
    reg.register(MiniProducer("layer0", produces=("fbfm40",)))
    reg.register(MiniProducer("ndvi_model", produces=("ndvi",)))
    reg.register(MiniProducer("lfmc",
                              produces=("lfmc",), requires=("ndvi",)))
    reg.register(MiniProducer("fire",
                              produces=("arrival_s",),
                              requires=("fbfm40", "lfmc")))
    p = Pipeline.from_targets(["arrival_s"], registry=reg)
    layers = p.topological_layers()
    # layer 0: layer0 + ndvi_model (no deps)
    assert set(layers[0]) == {"layer0", "ndvi_model"}
    # layer 1: lfmc (after ndvi_model)
    assert layers[1] == ("lfmc",)
    # layer 2: fire (after layer0 + lfmc)
    assert layers[2] == ("fire",)


# ============================================================== runner
def test_runner_serial_executes_in_order():
    reg = ProducerRegistry()
    a = MiniProducer("a", produces=("x",))
    b = MiniProducer("b", produces=("y",))
    c = MiniProducer("c", produces=("z",))
    for p in (a, b, c):
        reg.register(p)
    pipe = Pipeline.chain(["a", "b", "c"])
    r = PipelineRunner(reg, backend=SerialBackend(), verbose=False)
    res = r.run(CubeStub(), pipe)
    assert res.ok
    assert [s.name for s in res.steps] == ["a", "b", "c"]
    assert all(p.call_count == 1 for p in (a, b, c))
    assert res.by_name()["b"].produced == {"y": 1}


def test_runner_parallel_layer_thread_backend():
    reg = ProducerRegistry()
    for n in ("a", "b1", "b2", "c"):
        reg.register(MiniProducer(n, produces=(f"v_{n}",)))
    pipe = Pipeline.chain(["a", parallel("b1", "b2"), "c"])
    r = PipelineRunner(reg, backend=make_backend("thread", max_workers=2),
                       verbose=False)
    res = r.run(CubeStub(), pipe)
    assert res.ok
    names_in_order = [s.name for s in res.steps]
    # a always first; c always last; b1/b2 in the middle (any order).
    assert names_in_order[0] == "a"
    assert names_in_order[-1] == "c"
    assert set(names_in_order[1:3]) == {"b1", "b2"}


def test_runner_failure_blocks_dependents_unreachable():
    reg = ProducerRegistry()
    reg.register(MiniProducer("a", produces=("x",),
                              side_effect=RuntimeError("boom")))
    reg.register(MiniProducer("b", produces=("y",), requires=("x",)))
    pipe = (Pipeline()
            .add("a")
            .add("b", after="a"))
    r = PipelineRunner(reg, backend=SerialBackend(), verbose=False)
    res = r.run(CubeStub(), pipe)
    assert not res.ok
    by = res.by_name()
    assert by["a"].status == "error"
    # b should be reported as error with the upstream-failed message
    assert by["b"].status == "error"
    assert "upstream" in by["b"].error


def test_runner_fail_fast_stops_immediately():
    reg = ProducerRegistry()
    reg.register(MiniProducer("a", produces=("x",),
                              side_effect=RuntimeError("nope")))
    reg.register(MiniProducer("b", produces=("y",)))
    pipe = Pipeline.chain(["a", "b"])
    r = PipelineRunner(reg, backend=SerialBackend(),
                       verbose=False, fail_fast=True)
    res = r.run(CubeStub(), pipe)
    assert not res.ok
    assert [s.name for s in res.steps] == ["a"]   # b never ran


# ============================================================== triggers
def test_trigger_fires_and_runs_target():
    reg = ProducerRegistry()
    a = MiniProducer("a", produces=("x",))
    b = MiniProducer("b", produces=("y",))
    expand = MiniProducer("expand", produces=("z",))
    for p in (a, b, expand):
        reg.register(p)
    pipe = (Pipeline.chain(["a", "b"])
            .on_complete("b", run="expand", when=lambda cube: True))
    r = PipelineRunner(reg, backend=SerialBackend(), verbose=False)
    res = r.run(CubeStub(), pipe)
    assert res.ok
    assert "expand" in res.triggered
    assert [s.name for s in res.steps] == ["a", "b", "expand"]
    assert expand.call_count == 1


def test_trigger_does_not_fire_when_predicate_false():
    reg = ProducerRegistry()
    for n in ("a", "expand"):
        reg.register(MiniProducer(n, produces=(f"v_{n}",)))
    pipe = (Pipeline.chain(["a"])
            .on_complete("a", run="expand", when=lambda cube: False))
    r = PipelineRunner(reg, backend=SerialBackend(), verbose=False)
    res = r.run(CubeStub(), pipe)
    assert "expand" not in res.triggered
    assert [s.name for s in res.steps] == ["a"]


def test_trigger_respects_cube_state():
    """A trigger predicate inspects cube state set by the source step."""
    reg = ProducerRegistry()

    def source_side_effect(cube, request):
        cube.state["edge_reached"] = True
        return {"x": 1}

    reg.register(MiniProducer("source", produces=("x",),
                              side_effect=source_side_effect))
    reg.register(MiniProducer("expand", produces=("y",)))
    pipe = (Pipeline.chain(["source"])
            .on_complete("source", run="expand",
                         when=lambda cube: cube.state.get("edge_reached")))
    r = PipelineRunner(reg, backend=SerialBackend(), verbose=False)
    res = r.run(CubeStub(), pipe)
    assert "expand" in res.triggered


def test_trigger_only_fires_after_source_success():
    reg = ProducerRegistry()
    reg.register(MiniProducer("source", produces=("x",),
                              side_effect=RuntimeError("fail")))
    reg.register(MiniProducer("expand", produces=("y",)))
    pipe = (Pipeline.chain(["source"])
            .on_complete("source", run="expand", when=lambda c: True))
    r = PipelineRunner(reg, backend=SerialBackend(), verbose=False)
    res = r.run(CubeStub(), pipe)
    assert res.by_name()["source"].status == "error"
    assert "expand" not in res.triggered


# ============================================================== explain / dot
def test_explain_renders_layers_and_triggers():
    p = (Pipeline.chain(["a", parallel("b", "c"), "d"])
         .on_complete("d", run="e", when=lambda c: True))
    text = p.explain()
    assert "L0" in text and "L1" in text and "L2" in text
    assert "parallel" in text
    assert "trigger" in text


def test_to_dot_emits_graphviz():
    p = (Pipeline()
         .add("a").add("b", after="a")
         .on_complete("b", run="c", when=lambda c: True))
    dot = p.to_dot()
    assert dot.startswith("digraph")
    assert '"a" -> "b"' in dot
    assert '"b" -> "c"' in dot
    assert "dashed" in dot
