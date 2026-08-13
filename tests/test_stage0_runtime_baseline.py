"""Executable record of Stage-0 runner limitations and safety guards."""
from __future__ import annotations

import threading
import time

import pytest

from engine import (
    Pipeline,
    PipelineRunner,
    ProducerCapabilities,
    ProducerRegistry,
    SerialBackend,
    ThreadBackend,
    TileSpec,
    TiledProducer,
)


class _Cube:
    def __init__(self):
        self.state = {}
        self.grid = type("Grid", (), {"shape": (2, 2)})()

    def has(self, name):
        return name in self.state


class _Producer:
    deterministic = True
    idempotent = True

    def __init__(self, name, output, action=None, marker="original"):
        self.name = name
        self.produces = (output,)
        self.requires = ()
        self.action = action
        self.marker = marker

    def run(self, cube, request):
        if self.action:
            self.action(cube)
        cube.state[self.produces[0]] = self.marker
        return {self.produces[0]: 1}


def test_bound_pipeline_is_not_rewired_by_registry_mutation():
    registry = ProducerRegistry()
    original = _Producer("source", "wind", marker="original")
    replacement = _Producer("source", "wind", marker="replacement")
    registry.register(original)
    bound = Pipeline.chain(["source"]).bind(registry)
    registry.register(replacement, replace=True)

    cube = _Cube()
    result = PipelineRunner(
        registry, backend=SerialBackend(), verbose=False).run(cube, bound)

    assert result.ok
    assert cube.state["wind"] == "original"
    assert result.manifest["plan_id"] == bound.plan_id
    assert result.manifest["components"][0]["implementation_sha256"]
    assert result.manifest["components"][0]["configuration_sha256"]


def test_component_mutation_after_binding_is_rejected():
    registry = ProducerRegistry()
    producer = _Producer("source", "wind", marker="original")
    registry.register(producer)
    bound = Pipeline.chain(["source"]).bind(registry)
    producer.marker = "mutated"

    with pytest.raises(RuntimeError, match="changed after plan binding"):
        PipelineRunner(registry, verbose=False).run(_Cube(), bound)


def test_runtime_trigger_expansion_is_disabled_by_default():
    registry = ProducerRegistry()
    registry.register(_Producer("source", "x"))
    registry.register(_Producer("branch", "y"))
    pipeline = Pipeline.chain(["source"]).on_complete(
        "source", run="branch", when=lambda cube: True)

    with pytest.raises(RuntimeError, match="runtime triggers are disabled"):
        PipelineRunner(registry, verbose=False).run(_Cube(), pipeline)


@pytest.mark.xfail(
    strict=True,
    reason="legacy runner waits for a complete topological layer",
)
def test_independent_completion_releases_dependent_without_layer_barrier():
    timeline = {}
    lock = threading.Lock()

    def record(label):
        with lock:
            timeline[label] = time.monotonic()

    def fast(_cube):
        record("fast_done")

    def slow(_cube):
        time.sleep(0.15)
        record("slow_done")

    def dependent(_cube):
        record("dependent_started")

    registry = ProducerRegistry()
    registry.register(_Producer("fast", "fast_out", fast))
    registry.register(_Producer("slow", "slow_out", slow))
    registry.register(_Producer("dependent", "result", dependent))
    pipeline = (Pipeline("barrier-baseline")
                .add("fast").add("slow")
                .add("dependent", after="fast"))

    result = PipelineRunner(
        registry, backend=ThreadBackend(max_workers=2), verbose=False,
    ).run(_Cube(), pipeline)

    assert result.ok
    assert timeline["dependent_started"] < timeline["slow_done"]


class _StreamingTiles(TiledProducer):
    name = "streaming_tiles"
    produces = ("tile_output",)
    requires = ()
    capabilities = ProducerCapabilities(tile_parallel=True)

    def __init__(self):
        self.processed = 0

    def tile_iter(self, cube, request):
        yield TileSpec(slice(0, 1), slice(0, 1))
        if self.processed == 0:
            raise AssertionError("tile iterator was exhausted before work began")
        yield TileSpec(slice(1, 2), slice(1, 2))

    def process_tile(self, cube, request, tile):
        self.processed += 1


@pytest.mark.xfail(
    strict=True,
    reason="legacy tiled runner materializes the complete iterator twice",
)
def test_tile_admission_is_lazy_and_bounded():
    producer = _StreamingTiles()
    registry = ProducerRegistry()
    registry.register(producer)
    result = PipelineRunner(
        registry, backend=SerialBackend(), verbose=False,
        max_inflight_tiles=1,
    ).run(_Cube(), Pipeline.chain([producer.name]))

    assert result.ok
    assert producer.processed == 2


class _PartialPublisher(TiledProducer):
    name = "partial_publisher"
    produces = ("partial",)
    requires = ()
    capabilities = ProducerCapabilities(tile_parallel=True)

    def init(self, cube, request):
        # Mirrors current Cube.init_* behavior: output becomes discoverable
        # before all partitions validate and commit.
        cube.state["partial"] = "preallocated"

    def tile_iter(self, cube, request):
        yield TileSpec(slice(0, 1), slice(0, 1))

    def process_tile(self, cube, request, tile):
        raise RuntimeError("worker lost")


@pytest.mark.xfail(
    strict=True,
    reason="legacy tiled output is published before validation/finalization",
)
def test_failed_tiled_attempt_does_not_publish_partial_artifact():
    producer = _PartialPublisher()
    registry = ProducerRegistry()
    registry.register(producer)
    cube = _Cube()

    result = PipelineRunner(
        registry, backend=SerialBackend(), verbose=False,
    ).run(cube, Pipeline.chain([producer.name]))

    assert not result.ok
    assert not cube.has("partial")


@pytest.mark.xfail(
    strict=True,
    reason="legacy StepResult has no attempt identity or fencing token",
)
def test_duplicate_or_late_completion_has_fenced_attempt_identity():
    registry = ProducerRegistry()
    registry.register(_Producer("source", "x"))
    result = PipelineRunner(
        registry, backend=SerialBackend(), verbose=False,
    ).run(_Cube(), Pipeline.chain(["source"]))

    step = result.steps[0]
    assert getattr(step, "attempt_id")
    assert getattr(step, "fencing_token")
