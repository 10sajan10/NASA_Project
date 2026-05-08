"""Merge-policy tests.

Validates that VarSpec.merge_policy is actually enforced on writes (not
just declared) for both ProducerV2.update() (whole-array writes) and
the tile-write helpers (chunked writes).
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine import (
    MergePolicy,
    ProducerCapabilities,
    ProducerV2,
    Request,
    VarSpec,
    merge_arrays,
    write_chunk_static_with_policy,
    write_chunk_time_with_policy,
)


# ---------------------------------------------------- pure merge function
def test_merge_last_writer_replaces():
    a = np.array([1.0, 2.0, 3.0])
    b = np.array([10.0, 20.0, 30.0])
    out = merge_arrays(a, b, MergePolicy.LAST_WRITER)
    assert np.array_equal(out, b)


def test_merge_monotone_min():
    a = np.array([5.0, np.nan, 9.0])
    b = np.array([3.0, 7.0, 11.0])
    out = merge_arrays(a, b, MergePolicy.MONOTONE_MIN)
    # NaN treated as 'no value' — fmin picks the non-NaN side
    assert np.array_equal(out, np.array([3.0, 7.0, 9.0]))


def test_merge_monotone_max():
    a = np.array([5.0, 7.0, np.nan])
    b = np.array([3.0, 8.0, 11.0])
    out = merge_arrays(a, b, MergePolicy.MONOTONE_MAX)
    assert np.array_equal(out, np.array([5.0, 8.0, 11.0]))


def test_merge_accumulate_with_nan_baseline():
    a = np.array([np.nan, 2.0, 3.0])
    b = np.array([1.0, 4.0, 5.0])
    out = merge_arrays(a, b, MergePolicy.ACCUMULATE)
    assert np.array_equal(out, np.array([1.0, 6.0, 8.0]))


def test_merge_existing_none_returns_incoming():
    b = np.array([1.0, 2.0, 3.0])
    for policy in MergePolicy:
        out = merge_arrays(None, b, policy)
        assert np.array_equal(out, b)


# ---------------------------------------------------- ProducerV2 update
def _real_cube(tmp_path: Path):
    from cube.grid import SimulationGrid
    from cube.store import Cube
    grid = SimulationGrid.from_center_radius(-96.797, 32.776, 5_000.0, 500.0)
    return Cube(tmp_path, grid)


class _ConstStaticProducer(ProducerV2):
    """Writes a constant value to one declared variable. The shape is
    pulled from the cube grid in extract() so the producer adapts to
    whatever grid the test built."""
    requires = ()

    def __init__(self, name: str, var: str, value: float,
                 policy: MergePolicy):
        self.name = name
        self.value = value
        self.produces = (VarSpec(name=var, kind="static",
                                 merge_policy=policy),)
        self.capabilities = ProducerCapabilities()

    def extract(self, cube, request):
        return {"shape": cube.grid.shape}

    def compute(self, inputs, request):
        target = self.produces[0].name
        return {target: np.full(inputs["shape"], self.value,
                                 dtype="float32")}


def test_producer_update_honors_monotone_min(tmp_path):
    cube = _real_cube(tmp_path)
    try:
        p1 = _ConstStaticProducer("a", "v_min", 5.0, MergePolicy.MONOTONE_MIN)
        # Run twice with different values; lower should win.
        p1.run(cube, Request())
        p2 = _ConstStaticProducer("b", "v_min", 3.0, MergePolicy.MONOTONE_MIN)
        # NB: registry would refuse two producers for the same var, but the
        # contract check is at the cube write boundary, not the registry —
        # we exercise the merge directly.
        p2.run(cube, Request())
        arr = cube.read_static("v_min")
        assert float(arr[0, 0]) == 3.0
    finally:
        cube.close()


def test_producer_update_honors_monotone_max(tmp_path):
    cube = _real_cube(tmp_path)
    try:
        p1 = _ConstStaticProducer("a", "v_max", 2.0, MergePolicy.MONOTONE_MAX)
        p1.run(cube, Request())
        p2 = _ConstStaticProducer("b", "v_max", 9.0, MergePolicy.MONOTONE_MAX)
        p2.run(cube, Request())
        # And a third write below the max — should NOT lower the value.
        p3 = _ConstStaticProducer("c", "v_max", 4.0, MergePolicy.MONOTONE_MAX)
        p3.run(cube, Request())
        arr = cube.read_static("v_max")
        assert float(arr[0, 0]) == 9.0
    finally:
        cube.close()


def test_producer_update_honors_accumulate(tmp_path):
    cube = _real_cube(tmp_path)
    try:
        for v in (1.0, 2.0, 3.0):
            _ConstStaticProducer(f"a{v}", "v_acc", v,
                                  MergePolicy.ACCUMULATE).run(
                cube, Request())
        arr = cube.read_static("v_acc")
        assert float(arr[0, 0]) == pytest.approx(6.0)
    finally:
        cube.close()


def test_producer_update_last_writer_replaces(tmp_path):
    cube = _real_cube(tmp_path)
    try:
        _ConstStaticProducer("a", "v_lw", 7.0, MergePolicy.LAST_WRITER).run(
            cube, Request())
        _ConstStaticProducer("b", "v_lw", 1.0, MergePolicy.LAST_WRITER).run(
            cube, Request())
        arr = cube.read_static("v_lw")
        assert float(arr[0, 0]) == 1.0
    finally:
        cube.close()


# ---------------------------------------------------- chunk-write helpers
def test_chunk_write_static_helper_applies_min(tmp_path):
    cube = _real_cube(tmp_path)
    try:
        H, W = cube.grid.shape
        h = min(8, H); w = min(8, W)
        cube.init_static_tiled(
            "v", dtype="float32", source="t",
            native_res_m=float(cube.grid.pixel_m), units="",
            producer="t", chunk=(h, w), fill_value=np.nan)
        # First chunk write
        write_chunk_static_with_policy(
            cube, "v", slice(0, h), slice(0, w),
            np.full((h, w), 5.0, dtype="float32"),
            MergePolicy.MONOTONE_MIN)
        # Lower second write should win
        write_chunk_static_with_policy(
            cube, "v", slice(0, h), slice(0, w),
            np.full((h, w), 3.0, dtype="float32"),
            MergePolicy.MONOTONE_MIN)
        # Higher third write should NOT raise the value
        write_chunk_static_with_policy(
            cube, "v", slice(0, h), slice(0, w),
            np.full((h, w), 8.0, dtype="float32"),
            MergePolicy.MONOTONE_MIN)
        arr = cube.read_chunk_static("v", slice(0, h), slice(0, w))
        assert float(arr.min()) == 3.0
        assert float(arr.max()) == 3.0
    finally:
        cube.close()


def test_chunk_write_static_helper_last_writer_is_pure_write(tmp_path):
    """LAST_WRITER must NOT do a read-merge-write (it's the fast path)."""
    cube = _real_cube(tmp_path)
    try:
        H, W = cube.grid.shape
        h = min(8, H); w = min(8, W)
        cube.init_static_tiled(
            "v", dtype="float32", source="t",
            native_res_m=float(cube.grid.pixel_m), units="",
            producer="t", chunk=(h, w))
        write_chunk_static_with_policy(
            cube, "v", slice(0, h), slice(0, w),
            np.full((h, w), 1.0, dtype="float32"),
            MergePolicy.LAST_WRITER)
        write_chunk_static_with_policy(
            cube, "v", slice(0, h), slice(0, w),
            np.full((h, w), 2.0, dtype="float32"),
            MergePolicy.LAST_WRITER)
        arr = cube.read_chunk_static("v", slice(0, h), slice(0, w))
        assert float(arr[0, 0]) == 2.0
    finally:
        cube.close()
