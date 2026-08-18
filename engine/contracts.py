"""ProducerV2 contract: the orchestration substrate everything else rides on.

A producer declares:

  * `produces`     - VarSpecs it materialises, each with a merge policy
  * `requires`     - VarSpecs it reads
  * `capabilities` - halo, tile-parallelism, iterativeness, cost class

and provides three hooks:

  * `extract(cube, request)         -> inputs dict`
  * `compute(inputs, request)       -> outputs dict`         # pure-ish, worker-safe
  * `update(cube, outputs, request) -> {var: version}`       # writes through cube

The default `run` chains the three. Custom producers can override `run`
directly when extract/compute/update is awkward (e.g. drivers that stream
external data and never materialise inputs).

The contract is intentionally model-agnostic. The scheduler inspects the
declared metadata, never the variable semantics.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Optional


import hashlib
from pathlib import Path


class MergePolicy(str, Enum):
    """How concurrent / overlapping writes to a variable combine.

    Declared per-variable on the VarSpec, not globally. The scheduler picks
    the policy when collapsing tile writes from parallel workers.
    """
    LAST_WRITER = "last_writer"      # default; later write wins (weather, indices)
    MONOTONE_MIN = "monotone_min"    # arrival time, time-to-event
    MONOTONE_MAX = "monotone_max"    # peak intensity, burned area, fluence
    ACCUMULATE = "accumulate"        # counters, integrated quantities
    UNION = "union"                  # event sets


class CostHint(str, Enum):
    """Coarse routing hint for the scheduler. Drives pool selection."""
    IO = "io"     # network / disk-bound -> threads
    CPU = "cpu"   # numpy / numerics     -> processes
    GPU = "gpu"   # device-bound         -> dedicated worker


@dataclass(frozen=True)
class VarSpec:
    """Declared variable contract."""
    name: str
    kind: str = "static"             # "static" | "time"
    dtype: str = "float32"
    units: str = ""
    merge_policy: MergePolicy = MergePolicy.LAST_WRITER
    description: str = ""
    max_native_res_m: Optional[float] = None
    required: bool = True


@dataclass(frozen=True)
class ProducerCapabilities:
    """Static, declared scheduler hints. Inspected before invocation."""
    halo_cells: int = 0
    tile_parallel: bool = True
    boundary_coupled: bool = False
    iterative: bool = False
    requires_barrier: bool = False
    cost_hint: CostHint = CostHint.CPU
    memory_budget_mb: int = 1024     # advisory; scheduler may pack workers

    def __post_init__(self):
        if self.boundary_coupled and self.halo_cells == 0:
            raise ValueError(
                "boundary_coupled producer must declare halo_cells > 0")
        if self.halo_cells < 0:
            raise ValueError("halo_cells must be non-negative")
        if self.memory_budget_mb <= 0:
            raise ValueError("memory_budget_mb must be positive")


@dataclass(frozen=True)
class TileSpec:
    """A spatial+temporal work unit. `t` is None for static producers."""
    y: slice
    x: slice
    t: Optional[slice] = None
    halo: int = 0

    def with_halo(self, height: int, width: int) -> "TileSpec":
        h = self.halo
        if h == 0:
            return self
        return TileSpec(
            y=slice(max(0, self.y.start - h), min(height, self.y.stop + h)),
            x=slice(max(0, self.x.start - h), min(width, self.x.stop + h)),
            t=self.t,
            halo=0,
        )


@dataclass
class Request:
    """Invocation envelope. `tile=None` means whole-grid invocation."""
    t_start: Optional[datetime] = None
    t_end: Optional[datetime] = None
    force: bool = False
    tile: Optional[TileSpec] = None
    context: dict[str, Any] = field(default_factory=dict)

    @property
    def n_days(self) -> int:
        """Inclusive day count between t_start and t_end. Mirrors the legacy
        legacy per-variable producer protocol so older code can run unchanged
        through the engine runner."""
        if self.t_start is None or self.t_end is None:
            raise ValueError("time range is required for n_days")
        days = (self.t_end.date() - self.t_start.date()).days
        return max(1, days)


class ProducerV2(ABC):
    """Generic adapter base.

    Subclasses set the class attributes `name`, `produces`, `requires`,
    `capabilities`, then either:

      (a) override `compute()` (and optionally `extract` / `update`), letting
          the default `run()` chain them, or
      (b) override `run()` directly when the three-step split is unnatural
          (e.g. drivers that stream data from a remote service).
    """

    name: str = ""
    produces: tuple[VarSpec, ...] = ()
    requires: tuple[VarSpec, ...] = ()
    capabilities: ProducerCapabilities = ProducerCapabilities()

    # --- public entrypoint ------------------------------------------------
    def run(self, cube, request: Request) -> dict[str, int]:
        """Default invocation: extract -> compute -> update. Returns
        `{variable_name: new_version}` for each produced variable.

        Custom producers may override this directly; the scheduler only
        cares about the (name, produces, requires, capabilities) contract
        plus this entrypoint."""
        inputs = self.extract(cube, request)
        outputs = self.compute(inputs, request)
        return self.update(cube, outputs, request)

    # --- default three-step pipeline --------------------------------------
    def extract(self, cube, request: Request) -> dict[str, Any]:
        """Read declared inputs. Default: empty (drivers and zero-input
        producers). Override to return `{var.name: array}`."""
        return {}

    @abstractmethod
    def compute(self, inputs: dict[str, Any],
                request: Request) -> dict[str, Any]:
        """Pure-ish transform. Returns `{var.name: array}` for every var
        in `self.produces`. Must NOT touch the cube; runs on workers."""
        raise NotImplementedError

    def update(self, cube, outputs: dict[str, Any],
               request: Request) -> dict[str, int]:
        """Write outputs back through the cube, honoring each VarSpec's
        declared merge_policy. Producers with custom write paths override
        this."""
        # Local import: avoids a circular reference (merge -> contracts).
        from .merge import merge as _merge

        produced_names = {v.name for v in self.produces}
        missing = produced_names - set(outputs.keys())
        if missing:
            raise RuntimeError(
                f"{self.name}: compute did not return {sorted(missing)}; "
                f"declared produces={[v.name for v in self.produces]}")
        unexpected = set(outputs.keys()) - produced_names
        if unexpected:
            raise RuntimeError(
                f"{self.name}: compute returned undeclared vars "
                f"{sorted(unexpected)}")

        versions: dict[str, int] = {}
        for spec in self.produces:
            arr = outputs[spec.name]
            policy = spec.merge_policy
            # Local import: cube.entries reaches the typed contracts package,
            # which imports back into engine.runtime, so a module-level import
            # here would be circular.
            from cube.entries import DatasetRef
            if isinstance(arr, DatasetRef):
                # The producer emitted a file, not an array.  Catalog it where
                # it already lives, on its own grid, rather than resampling it
                # onto the cube's.
                versions[spec.name] = self._catalog_dataset(cube, spec, arr)
                continue
            if spec.kind == "static":
                if (policy is not MergePolicy.LAST_WRITER
                        and cube.has(spec.name)):
                    arr = _merge(cube.read_static(spec.name), arr, policy)
                cube.write_static(
                    spec.name, arr,
                    source=f"producer:{self.name}",
                    native_res_m=float(cube.grid.pixel_m),
                    units=spec.units,
                    producer=self.name,
                    description=spec.description)
            elif spec.kind == "time":
                ts = request.context.get("t_axis") or []
                if (policy is not MergePolicy.LAST_WRITER
                        and cube.has(spec.name)):
                    _, existing = cube.read_3d(spec.name)
                    arr = _merge(existing, arr, policy)
                cube.write_3d(
                    spec.name, ts, arr,
                    source=f"producer:{self.name}",
                    native_res_m=float(cube.grid.pixel_m),
                    units=spec.units,
                    producer=self.name,
                    description=spec.description)
            else:
                raise ValueError(f"unknown var kind {spec.kind!r}")
            versions[spec.name] = self._latest_version(cube, spec.name)
        return versions

    def _catalog_dataset(self, cube, spec, ref) -> int:
        """Record a produced file in the cube catalog, unconverted."""
        from cube.entries import CubeEntry

        located = Path(ref.path)
        digest = hashlib.sha256()
        with located.open("rb") as stream:
            for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
                digest.update(block)
        detail = dict(ref.detail)
        detail.setdefault("units", spec.units)
        detail.setdefault("description", spec.description)
        entry = CubeEntry.create(
            concept=spec.name, kind=spec.kind, producer=self.name,
            content_sha256=digest.hexdigest(), grid=ref.grid,
            location=str(located.resolve()), media_type=ref.media_type,
            detail=detail, inputs=tuple(ref.inputs))
        cube.catalog.register_dataset(entry, located)
        return 0

    @staticmethod
    def _latest_version(cube, variable: str) -> int:
        """Best-effort lookup of the variable's current version row.

        Catalog already stores a `version` column on tiles; default to 0
        if the cube doesn't expose it."""
        try:
            rows = cube.catalog.con.execute(
                "SELECT MAX(version) FROM tiles WHERE variable=?",
                [variable]).fetchone()
            return int(rows[0]) if rows and rows[0] is not None else 0
        except Exception:
            return 0
