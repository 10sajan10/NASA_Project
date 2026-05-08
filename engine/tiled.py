"""Tile-level fan-out: split one producer's spatial work across workers.

A tile-aware producer implements three hooks:

    init(cube, request)              # parent process, pre-allocate outputs
    process_tile(cube, request, tile) # worker, per-tile compute + write
    finalize(cube, request)          # parent process, optional cleanup

The runner detects tile-awareness (capability flag + presence of
`process_tile`) and fans `process_tile` calls across the configured
Backend. One producer that processes a 1000x1000 grid in 256x256 tiles
becomes 16 independent tasks the backend can run in parallel.

For cross-process backends each tile worker reopens the cube via CubeRef,
processes its slab, writes through `cube.write_chunk_*`, and closes.
Outputs must be pre-allocated in `init` so concurrent chunk writes
target disjoint regions of the same Zarr array.

Producers without `process_tile` keep going through the normal one-shot
`run()` path — tile fan-out is opt-in.
"""
from __future__ import annotations

from dataclasses import replace
from typing import Any, Iterator

from .contracts import ProducerCapabilities, Request, TileSpec
from .cube_ref import CubeRef


# ---------------------------------------------------------------- base
class TiledProducer:
    """Base class for tile-parallel producers.

    Subclass with `name`, `produces`, `requires`, set
    `capabilities.tile_parallel = True`, and override `process_tile`
    (mandatory) plus `init` / `finalize` (optional).
    """
    name: str = ""
    produces: tuple = ()
    requires: tuple = ()
    capabilities: ProducerCapabilities = ProducerCapabilities(tile_parallel=True)
    tile_size: int = 256

    # ---- parent-process hooks ----------------------------------------
    def init(self, cube, request: Request) -> None:
        """One-shot: pre-allocate output arrays. Default no-op."""

    def finalize(self, cube, request: Request) -> dict[str, int]:
        """One-shot cleanup. Returns {variable: version} for the runner.

        Default returns 0 for each declared output."""
        from .registry import producer_produces
        return {v: 0 for v in producer_produces(self)}

    # ---- worker-side hook --------------------------------------------
    def process_tile(self, cube, request: Request,
                     tile: TileSpec) -> dict[str, Any]:
        """Compute one tile and write it to the cube. Return value is
        unused but useful for testing (return per-tile output dict)."""
        raise NotImplementedError(
            f"{type(self).__name__} must implement process_tile")

    # ---- iteration default --------------------------------------------
    def tile_iter(self, cube, request: Request) -> Iterator[TileSpec]:
        """Yield spatial tiles. Default: cube's iter_spatial_tiles
        with `self.tile_size`. Override to customize (e.g. include
        time slabs)."""
        for y_sl, x_sl in cube.iter_spatial_tiles(tile=self.tile_size):
            yield TileSpec(y=y_sl, x=x_sl,
                           halo=self.capabilities.halo_cells)

    def tile_predicate(self, cube, request: Request,
                        tile: TileSpec) -> bool:
        """Active-set filter: return True if `tile` needs computation.

        Default: True (all tiles active). Producers with sparse work
        (fire spread fronts, hotspot detection, change-driven retraining)
        override this to skip cold tiles. The runner filters via this
        predicate before dispatching, so cost scales with the active
        set, not the full grid.

        Predicate runs in the parent process before fan-out, so it
        sees the live cube and any state the runner has already
        produced. Side effects are discouraged.
        """
        return True

    # ---- adapter to the runner's standard run() entrypoint -----------
    def run(self, cube, request: Request) -> dict[str, int]:
        """Default whole-grid invocation: init, sequential tile loop,
        finalize. Used when the runner doesn't dispatch tile-by-tile
        (e.g. backend is Serial or the producer wasn't recognized as
        tile-aware)."""
        self.init(cube, request)
        for tile in self.tile_iter(cube, request):
            tile_request = replace(request, tile=tile)
            self.process_tile(cube, tile_request, tile)
        return self.finalize(cube, request)


# ---------------------------------------------------------------- detection
def is_tile_aware(producer) -> bool:
    """A producer is tile-aware iff it declares tile_parallel and
    actually implements process_tile. Duck-typed so any class can opt in
    without inheriting TiledProducer."""
    cap = getattr(producer, "capabilities", None)
    if cap is None or not getattr(cap, "tile_parallel", False):
        return False
    method = getattr(producer, "process_tile", None)
    return callable(method)


# ---------------------------------------------------------------- workers
def _run_tile(producer, cube_or_ref, request: Request,
              tile: TileSpec) -> Any:
    """Worker entry: process one tile.

    Mirrors `_run_one` in scheduler.py for the cube-vs-ref handling.
    Stays at module scope so it's picklable for ProcessBackend / Dask.
    """
    tile_request = replace(request, tile=tile)
    if isinstance(cube_or_ref, CubeRef):
        with cube_or_ref.opened() as cube:
            return producer.process_tile(cube, tile_request, tile)
    return producer.process_tile(cube_or_ref, tile_request, tile)
