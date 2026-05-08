"""Disk-spill workspace: ndarray-shaped memory that overflows to disk.

Producers don't always know up front whether their working buffer fits in
RAM. `spill_array` returns a regular `np.ndarray` if it does, and a
`np.memmap` otherwise. Either way it walks like a numpy array, so the
producer code is identical.

Usage:

    from engine import workspace

    with workspace(scratch_dir="/scratch/$USER") as alloc:
        big = alloc((720, 4000, 4000), dtype="float32",
                    threshold_mb=2048)              # spills to disk if >2 GB
        big[...] = compute(...)
        # memmap files are deleted on context exit

Memmap files are page-cached by the OS; the kernel evicts cold pages back
to disk under memory pressure, which is exactly the "overflow to disk"
behavior we want without a custom paging layer.
"""
from __future__ import annotations

import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

import numpy as np


def _scratch_root(scratch_dir: Optional[str | Path]) -> Path:
    if scratch_dir is not None:
        p = Path(scratch_dir)
    else:
        p = Path(os.environ.get("CUBE_SCRATCH",
                                tempfile.gettempdir())) / "cube_spill"
    p.mkdir(parents=True, exist_ok=True)
    return p


def spill_array(shape: tuple[int, ...],
                dtype: Any = "float32",
                *,
                scratch_dir: Optional[str | Path] = None,
                threshold_mb: float = 1024.0,
                fill_value: Any = 0) -> np.ndarray:
    """Allocate an array, spilling to a memmap on disk past a size threshold.

    Parameters
    ----------
    shape : tuple of int
        Shape of the array.
    dtype : numpy dtype-like
        Element type.
    scratch_dir : path-like, optional
        Directory for spill files. Defaults to `$CUBE_SCRATCH/cube_spill`,
        or `$TMPDIR/cube_spill`. On HPC, point this at fast local scratch
        (e.g. `/scratch/local/$USER`) rather than parallel filesystem.
    threshold_mb : float
        If the requested array exceeds this many MB, allocate as memmap.
        Otherwise plain `np.ndarray`.
    fill_value : scalar
        Initial fill.

    Returns
    -------
    np.ndarray (or np.memmap, which is a subclass of ndarray). Memmap arrays
    carry a `_spill_path` attribute the workspace context uses for cleanup.
    """
    nbytes = int(np.prod(shape)) * np.dtype(dtype).itemsize
    if nbytes <= threshold_mb * 1024 * 1024:
        return np.full(shape, fill_value, dtype=dtype)

    root = _scratch_root(scratch_dir)
    fd, path = tempfile.mkstemp(prefix="spill_", suffix=".dat", dir=str(root))
    os.close(fd)
    arr = np.memmap(path, dtype=dtype, mode="w+", shape=shape)
    if fill_value != 0:
        arr[...] = fill_value
    # Stash the path on the array for the workspace cleanup loop. We're
    # explicitly attaching to a memmap object (subclass of ndarray) so this
    # attribute set is well-defined.
    arr._spill_path = path  # type: ignore[attr-defined]
    return arr


@contextmanager
def workspace(scratch_dir: Optional[str | Path] = None
              ) -> Iterator[Any]:
    """Context manager that yields an `alloc(shape, dtype, ...)` allocator
    and deletes any spill files it created on exit.

    Use one workspace per producer invocation so spill files have a clear
    lifetime and never leak.
    """
    spilled: list[str] = []

    def alloc(shape: tuple[int, ...],
              dtype: Any = "float32",
              *,
              threshold_mb: float = 1024.0,
              fill_value: Any = 0) -> np.ndarray:
        arr = spill_array(shape, dtype,
                          scratch_dir=scratch_dir,
                          threshold_mb=threshold_mb,
                          fill_value=fill_value)
        path = getattr(arr, "_spill_path", None)
        if path is not None:
            spilled.append(path)
        return arr

    try:
        yield alloc
    finally:
        for p in spilled:
            try:
                os.remove(p)
            except OSError:
                pass
