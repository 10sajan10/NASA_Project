"""Smoke tests + contract tests for the engine/ foundation.

Runnable as:
    python -m pytest tests/test_engine_foundation.py -v
or as a self-check:
    python tests/test_engine_foundation.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import numpy as np

# Allow running this file directly without `pip install -e .`
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine import (
    CostHint,
    FakeCube,
    MergePolicy,
    ProducerCapabilities,
    ProducerV2,
    Request,
    RunManifest,
    SerialBackend,
    ThreadBackend,
    VarSpec,
    check_producer,
    load_config,
    make_backend,
    merge_overrides,
    spill_array,
    workspace,
)


# ============================================================== contracts
def test_varspec_and_capabilities():
    v = VarSpec(name="ndvi", kind="static",
                merge_policy=MergePolicy.LAST_WRITER)
    assert v.name == "ndvi"
    cap = ProducerCapabilities(halo_cells=2, boundary_coupled=True)
    assert cap.halo_cells == 2 and cap.boundary_coupled


def test_capabilities_invariant_boundary_requires_halo():
    import pytest
    with pytest.raises(ValueError):
        ProducerCapabilities(halo_cells=0, boundary_coupled=True)


# A minimal pure producer for end-to-end exercise.
class _AddOne(ProducerV2):
    name = "add_one"
    requires = (VarSpec("input_x", kind="static"),)
    produces = (VarSpec("output_y", kind="static",
                        merge_policy=MergePolicy.LAST_WRITER),)
    capabilities = ProducerCapabilities(cost_hint=CostHint.CPU)

    def extract(self, cube, request: Request):
        return {"input_x": cube.read_static("input_x")}

    def compute(self, inputs, request: Request):
        return {"output_y": inputs["input_x"] + 1.0}


def _seed_addone(cube: FakeCube) -> None:
    cube.seed_static("input_x", np.ones((8, 8), dtype="float32"))


def test_producer_contract_passes_for_addone():
    errs = check_producer(_AddOne(), seed=_seed_addone)
    assert errs == [], errs


def test_producer_contract_catches_undeclared_write():
    class _Bad(ProducerV2):
        name = "bad"
        requires = (VarSpec("input_x"),)
        produces = (VarSpec("output_y"),)
        capabilities = ProducerCapabilities()
        def extract(self, cube, request):
            return {"input_x": cube.read_static("input_x")}
        def compute(self, inputs, request):
            # Returns an undeclared key.
            return {"surprise": inputs["input_x"]}
    errs = check_producer(_Bad(), seed=_seed_addone)
    assert errs and "compute returned" in errs[0]


# ============================================================== backends
def _square(x: int) -> int:
    return x * x


def test_serial_backend_map_and_submit():
    b = SerialBackend()
    assert b.map(_square, [1, 2, 3]) == [1, 4, 9]
    fut = b.submit(_square, 5)
    assert fut.result() == 25
    b.shutdown()


def test_thread_backend_runs_via_factory():
    with make_backend("thread", max_workers=2) as b:
        assert b.map(_square, [1, 2, 3, 4]) == [1, 4, 9, 16]


def test_process_backend_smoke():
    # Skip if running under coverage tooling that can't pickle, etc.
    try:
        with make_backend("process", max_workers=2) as b:
            assert sorted(b.map(_square, [1, 2, 3])) == [1, 4, 9]
    except Exception as e:  # pragma: no cover
        import pytest
        pytest.skip(f"process backend unavailable in this env: {e}")


def test_make_backend_rejects_unknown_mode():
    import pytest
    with pytest.raises(ValueError):
        make_backend("nonsense")


# ============================================================== spill
def test_spill_array_in_memory_for_small_alloc():
    arr = spill_array((4, 4), dtype="float32", threshold_mb=1.0)
    assert isinstance(arr, np.ndarray)
    assert not isinstance(arr, np.memmap)


def test_spill_array_overflows_to_memmap_when_large():
    # 4 MB > threshold_mb=1
    with tempfile.TemporaryDirectory() as td:
        arr = spill_array((1024, 1024), dtype="float32",
                          scratch_dir=td, threshold_mb=1.0)
        assert isinstance(arr, np.memmap)
        assert os.path.exists(arr._spill_path)
        # Cleanup explicitly since we didn't use the workspace ctx.
        del arr
        for fn in os.listdir(td):
            os.remove(os.path.join(td, fn))


def test_workspace_cleans_up_spill_files():
    with tempfile.TemporaryDirectory() as td:
        with workspace(scratch_dir=td) as alloc:
            big = alloc((1024, 1024), dtype="float32", threshold_mb=1.0)
            assert isinstance(big, np.memmap)
            path = big._spill_path
            assert os.path.exists(path)
        # On exit the spill file is removed.
        assert not os.path.exists(path)


# ============================================================== manifest
def test_run_manifest_records_and_finalizes(tmp_path):
    db = tmp_path / "catalog.duckdb"
    cfg = {"city": "Dallas", "days": 30, "weather": "era5"}
    with RunManifest.create(db, cfg) as m:
        m.record_input("kml", __file__)  # any real file works
        m.record_output("ndvi", version=1, producer="dummy")
    s = m.summary()
    assert s["status"] == "ok"
    assert s["config_hash"]
    assert s["ended_at"] is not None


def test_run_manifest_marks_error_on_exception(tmp_path):
    db = tmp_path / "catalog.duckdb"
    try:
        with RunManifest.create(db, {"x": 1}) as m:
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert m.summary()["status"] == "error"


# ============================================================== config
def test_config_load_json(tmp_path):
    p = tmp_path / "c.json"
    p.write_text('{"days": 30, "city": "Dallas"}')
    cfg = load_config(p)
    assert cfg == {"days": 30, "city": "Dallas"}


def test_config_overrides_skip_none():
    base = {"city": "Dallas", "days": 30, "weather": {"src": "era5"}}
    overrides = {"days": None, "weather": {"src": "synthetic"}}
    out = merge_overrides(base, overrides)
    assert out == {"city": "Dallas", "days": 30,
                   "weather": {"src": "synthetic"}}


# ============================================================== runner
if __name__ == "__main__":
    # Minimal in-process runner so we can sanity-check without pytest.
    import traceback
    failures = 0
    g = dict(globals())
    for name, fn in sorted(g.items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            # Provide a tmp_path stand-in for tests that need one.
            if "tmp_path" in fn.__code__.co_varnames:
                with tempfile.TemporaryDirectory() as td:
                    fn(Path(td))
            else:
                fn()
            print(f"  ok   {name}")
        except Exception:
            failures += 1
            print(f"  FAIL {name}")
            traceback.print_exc()
    print(f"\n{failures} failures")
    sys.exit(1 if failures else 0)
