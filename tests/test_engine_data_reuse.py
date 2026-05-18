"""Data reuse across models — fetch once, consume many.

Concrete demonstration of the pattern you asked about:

    Model A → checks cube/cache
            → downloads the data (if missing)
            → stores in cube

    Model B → checks cube/cache
            → uses existing data
            → NO download

Three scenarios are validated end-to-end on a real Cube:

  1. Two models share a fetcher in the SAME run -> fetcher runs ONCE.
  2. Run 1 populates the cube; Run 2 adds a new downstream model;
     the fetcher is SKIPPED because the data is already on disk.
  3. Run 2's downstream model produces correct output from the
     cached data (no re-download required).
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine import (
    Pipeline,
    PipelineRunner,
    ProducerCapabilities,
    ProducerV2,
    SerialBackend,
    VarSpec,
    to_engine_registry,
)


# ----------------------------------------------------- producers
def _real_cube(tmp_path: Path):
    """Build a real on-disk Cube — durable across PipelineRunner runs."""
    from cube.grid import SimulationGrid
    from cube.store import Cube
    grid = SimulationGrid.from_center_radius(
        -96.797, 32.776, 5_000.0, 500.0)
    return Cube(tmp_path, grid)


class RemoteFetcher(ProducerV2):
    """Simulates an expensive remote fetch — any external data acquisition
    you might think of (a scene download, an archive pull, a tile fetch).
    We count invocations to prove the fetch happens only when needed."""
    name = "remote_fetcher"
    requires = ()
    produces = (VarSpec("remote_data", kind="static"),)
    capabilities = ProducerCapabilities()
    # Class-level counter persists across instances so any new fetcher
    # we register sees the same count. Tests reset before each run.
    fetch_count: int = 0

    def extract(self, cube, request):
        # Read the grid shape from the cube so the synthetic 'fetch'
        # produces an array matching the cube's geometry. A real fetcher
        # would reproject onto cube.grid here.
        return {"shape": cube.grid.shape}

    def compute(self, inputs, request):
        type(self).fetch_count += 1
        # Imagine an HTTP fetch + reproject ran here.
        return {"remote_data": np.full(inputs["shape"], 1.5,
                                        dtype="float32")}


class ModelA(ProducerV2):
    """Multiplies the shared data by 2."""
    name = "model_a"
    requires = (VarSpec("remote_data", kind="static"),)
    produces = (VarSpec("model_a_out", kind="static"),)
    capabilities = ProducerCapabilities()

    def extract(self, cube, request):
        return {"remote_data": cube.read_static("remote_data")}

    def compute(self, inputs, request):
        return {"model_a_out": inputs["remote_data"] * 2.0}


class ModelB(ProducerV2):
    """Adds 10 to the shared data."""
    name = "model_b"
    requires = (VarSpec("remote_data", kind="static"),)
    produces = (VarSpec("model_b_out", kind="static"),)
    capabilities = ProducerCapabilities()

    def extract(self, cube, request):
        return {"remote_data": cube.read_static("remote_data")}

    def compute(self, inputs, request):
        return {"model_b_out": inputs["remote_data"] + 10.0}


# ============================================================= test 1
def test_two_models_in_one_run_share_one_fetch(tmp_path):
    """Scenario 1: model_a AND model_b both declare requires=['remote_data'].
    The registry maps the variable to ONE producer (RemoteFetcher), so
    the dependency graph fans out from a single upstream node. The
    fetcher runs exactly once even though two downstream models consume
    its output."""
    RemoteFetcher.fetch_count = 0
    cube = _real_cube(tmp_path)
    try:
        reg = to_engine_registry([RemoteFetcher(), ModelA(), ModelB()])
        # Ask for BOTH downstream outputs in a single run.
        pipeline = Pipeline.from_targets(
            ["model_a_out", "model_b_out"], registry=reg)
        runner = PipelineRunner(reg, backend=SerialBackend(), verbose=False)
        res = runner.run(cube, pipeline)
        assert res.ok

        # *** THE POINT ***  the fetcher ran ONCE despite serving two models.
        assert RemoteFetcher.fetch_count == 1, (
            f"expected exactly 1 fetch; got {RemoteFetcher.fetch_count}")

        # Both downstream models produced correct output from the shared
        # fetch.
        assert float(cube.read_static("model_a_out").mean()) == 3.0
        assert float(cube.read_static("model_b_out").mean()) == 11.5

        # And the runner topology confirms a single layer of fetcher
        # feeding two parallel consumers.
        layers = pipeline.topological_layers()
        assert layers[0] == ("remote_fetcher",)
        assert set(layers[1]) == {"model_a", "model_b"}
    finally:
        cube.close()


# ============================================================= test 2
def test_second_run_on_same_cube_skips_fetch(tmp_path):
    """Scenario 2: Run 1 populates the cube with remote_data + model_a_out.
    Run 2 on the SAME cube root asks for a DIFFERENT model (model_b),
    which also requires remote_data. The fetcher must NOT re-download —
    cube.satisfies sees remote_data is already cached and the runner
    skips that step."""
    RemoteFetcher.fetch_count = 0
    cube = _real_cube(tmp_path)
    try:
        # ---- Run 1 ---------------------------------------------------
        reg1 = to_engine_registry([RemoteFetcher(), ModelA()])
        pipe1 = Pipeline.from_targets(["model_a_out"], registry=reg1)
        runner = PipelineRunner(reg1, backend=SerialBackend(), verbose=False)
        res1 = runner.run(cube, pipe1)
        assert res1.ok
        assert RemoteFetcher.fetch_count == 1
        assert res1.by_name()["remote_fetcher"].status == "ok"

        # ---- Run 2: same cube, NEW downstream model (model_b) --------
        reg2 = to_engine_registry([RemoteFetcher(), ModelB()])
        pipe2 = Pipeline.from_targets(["model_b_out"], registry=reg2)
        runner2 = PipelineRunner(reg2, backend=SerialBackend(),
                                 verbose=False)
        res2 = runner2.run(cube, pipe2)
        assert res2.ok

        # *** THE POINT ***  fetch_count is STILL 1 — no re-download.
        assert RemoteFetcher.fetch_count == 1, (
            f"fetcher should have been skipped on run 2; "
            f"fetch_count={RemoteFetcher.fetch_count}")

        # And the runner explicitly reports the skip.
        assert res2.by_name()["remote_fetcher"].status == "skipped"

        # model_b consumed the cached data correctly.
        assert float(cube.read_static("model_b_out").mean()) == 11.5
    finally:
        cube.close()


# ============================================================= test 3
def test_three_models_added_incrementally(tmp_path):
    """Scenario 3: realistic incremental workflow. Each successive run
    adds a new downstream model. The fetcher's count never goes above 1."""
    RemoteFetcher.fetch_count = 0
    cube = _real_cube(tmp_path)
    try:
        # Run 1: fetcher only (e.g. "let me cache the data first")
        reg = to_engine_registry([RemoteFetcher()])
        runner = PipelineRunner(reg, backend=SerialBackend(), verbose=False)
        runner.run(cube, Pipeline().add("remote_fetcher"))
        assert RemoteFetcher.fetch_count == 1

        # Run 2: add model_a
        reg = to_engine_registry([RemoteFetcher(), ModelA()])
        runner = PipelineRunner(reg, backend=SerialBackend(), verbose=False)
        res = runner.run(cube, Pipeline.from_targets(
            ["model_a_out"], registry=reg))
        assert res.by_name()["remote_fetcher"].status == "skipped"
        assert RemoteFetcher.fetch_count == 1

        # Run 3: add model_b on top
        reg = to_engine_registry([RemoteFetcher(), ModelB()])
        runner = PipelineRunner(reg, backend=SerialBackend(), verbose=False)
        res = runner.run(cube, Pipeline.from_targets(
            ["model_b_out"], registry=reg))
        assert res.by_name()["remote_fetcher"].status == "skipped"
        assert RemoteFetcher.fetch_count == 1  # STILL 1

        # All three are now in the cube (static vars, cube.has works).
        names = {v["name"] for v in cube.list_variables()}
        assert {"remote_data", "model_a_out", "model_b_out"} <= names
    finally:
        cube.close()
