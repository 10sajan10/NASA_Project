"""Cross-process safe cube handle.

The live `cube.store.Cube` keeps a long-running DuckDB connection. DuckDB
connections aren't reliably picklable, so a Cube can't be sent to a worker
process. `CubeRef` is a tiny picklable handle containing just the cube root
path; the worker reopens the cube in-process and closes it when done.

Used transparently by `PipelineRunner` for cross-process backends. In-process
backends (Serial, Thread) keep using the live Cube object directly.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


@dataclass(frozen=True)
class CubeRef:
    """Picklable handle to a Cube on disk.

    The grid is loaded from `<root>/grid.json` (which `Cube.__init__` always
    writes), so the ref is fully self-contained — workers don't need to
    receive the grid object separately.
    """
    root: str

    @classmethod
    def from_cube(cls, cube) -> "CubeRef":
        return cls(root=str(cube.root))

    def open(self):
        """Open a fresh Cube in the current process. Caller is responsible
        for closing it (or use `opened()` as a context manager)."""
        from cube.grid import SimulationGrid
        from cube.store import Cube
        grid = SimulationGrid.load(Path(self.root) / "grid.json")
        return Cube(self.root, grid)

    @contextmanager
    def opened(self) -> Iterator:
        """Context manager: open, yield, close."""
        cube = self.open()
        try:
            yield cube
        finally:
            try:
                cube.close()
            except Exception:
                pass


def is_cross_process_backend(backend) -> bool:
    """True if the backend executes tasks in a separate OS process or node.

    For these backends the runner must hand workers a CubeRef rather than
    a live Cube object. Identified by name to avoid importing optional
    dependencies (dask) just for the type check.
    """
    name = getattr(backend, "name", "") or ""
    return name in ("process", "dask", "dask-local") or name.startswith("dask")
