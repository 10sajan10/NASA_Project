"""Merge-policy semantics for cube writes.

A `VarSpec` declares one of:

  * LAST_WRITER  - default; later write replaces earlier (weather, indices)
  * MONOTONE_MIN - take cell-wise minimum (arrival time, time-to-event)
  * MONOTONE_MAX - take cell-wise maximum (peak intensity, burned area)
  * ACCUMULATE   - sum (counters, integrated quantities)

ProducerV2's default `update()` routes static + 3D writes through `merge`
so the policy is honored automatically. Tile producers writing chunked
output use `write_chunk_static_with_policy` / `write_chunk_time_with_policy`.

Concurrency note: tile-level read-modify-write is NOT atomic when tiles are
processed by multiple workers writing the same chunk. This is safe in
practice because tile producers own disjoint chunks (each worker's tile
target is exclusive). If a future caller breaks that invariant, they need
a barrier or single-worker mode for the merge step.
"""
from __future__ import annotations

from typing import Optional

import numpy as np

from .contracts import MergePolicy


def merge(existing: Optional[np.ndarray], incoming: np.ndarray,
          policy: MergePolicy) -> np.ndarray:
    """Combine an incoming write with whatever's already in the cube.

    NaNs in `existing` are treated as "no value" — `fmin`/`fmax` ignore them
    so the first write through a monotone policy is just `incoming`.
    `ACCUMULATE` treats existing NaN as zero.
    """
    if existing is None or policy == MergePolicy.LAST_WRITER:
        return incoming
    if policy == MergePolicy.MONOTONE_MIN:
        return np.fmin(existing, incoming)
    if policy == MergePolicy.MONOTONE_MAX:
        return np.fmax(existing, incoming)
    if policy == MergePolicy.ACCUMULATE:
        return np.add(np.nan_to_num(existing, nan=0.0), incoming)
    if policy == MergePolicy.UNION:
        # Sets-as-arrays: not meaningful for raster data; fall back to
        # last-writer rather than silently corrupting.
        return incoming
    raise ValueError(f"unsupported merge policy: {policy!r}")


def write_chunk_static_with_policy(cube, variable: str,
                                    y_slice: slice, x_slice: slice,
                                    data: np.ndarray,
                                    policy: MergePolicy) -> None:
    """Tile-level static write with merge policy.

    For LAST_WRITER (the common case), this is identical to
    `cube.write_chunk_static`. Other policies do a read-merge-write of
    just the target chunk, so cost is O(chunk) not O(grid)."""
    if policy == MergePolicy.LAST_WRITER:
        cube.write_chunk_static(variable, y_slice, x_slice, data)
        return
    existing = cube.read_chunk_static(variable, y_slice, x_slice)
    merged = merge(existing, data, policy)
    cube.write_chunk_static(variable, y_slice, x_slice, merged)


def write_chunk_time_with_policy(cube, variable: str,
                                  t_slice: slice,
                                  y_slice: slice, x_slice: slice,
                                  data: np.ndarray,
                                  policy: MergePolicy) -> None:
    """Tile-level time-3D write with merge policy."""
    if policy == MergePolicy.LAST_WRITER:
        cube.write_chunk_time(variable, t_slice, y_slice, x_slice, data)
        return
    existing = cube.read_chunk_time(variable, t_slice, y_slice, x_slice)
    merged = merge(existing, data, policy)
    cube.write_chunk_time(variable, t_slice, y_slice, x_slice, merged)
