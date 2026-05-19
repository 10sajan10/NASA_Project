"""DataAdapter: declare what data a producer needs, search the cube for
it (resolution-aware), and fetch it in one call.

A `DataAdapter` is a bundle of `DataNeed`s. Each need names a cube
variable and (optionally) a maximum acceptable native resolution. The
engine already has the machinery to resolve these:

  * VarSpec carries `max_native_res_m` and `required`
  * `cube.satisfies(spec)` returns False when cached data is too coarse
  * The scheduler then re-runs the upstream producer until the cube
    holds data fine enough for the consumer.

This module gives consumers a clean, declarative way to say "I need X
at <= 30m" without writing per-variable cube I/O boilerplate. Used by
`ModelAdapter` (and any other ProducerV2 that wants the pattern).

Example
-------
    from engine import DataAdapter, DataNeed

    needs = DataAdapter([
        DataNeed("ndvi",          kind="static", max_native_res_m=30.0),
        DataNeed("wind_speed_ms", kind="time"),
        DataNeed("dem",           kind="static", max_native_res_m=10.0,
                  required=False),                 # optional input
    ])

    # In a ProducerV2:
    #   requires = needs.varspecs()
    #   def extract(self, cube, request):
    #       return needs.fetch(cube, request)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from .contracts import MergePolicy, VarSpec


@dataclass(frozen=True)
class DataNeed:
    """One declared input dependency."""
    name: str
    kind: str = "static"                          # "static" | "time"
    max_native_res_m: Optional[float] = None
    units: str = ""
    dtype: str = "float32"
    description: str = ""
    required: bool = True


class DataAdapter:
    """Bundle of input needs + cube fetch.

    Two responsibilities:
      1. Surface declared inputs as `VarSpec`s so the engine can drive
         dep resolution (DAG construction, cube.satisfies skip, etc.)
      2. Pull the resolved data out of the cube at run time, in a
         uniform shape, with optional inputs handled gracefully.

    The cube is the source of truth for what's present and at what
    resolution. This class does not fetch from external sources — that's
    the upstream producers' job. By the time `fetch()` runs, the engine
    has already ensured every required need is present at the requested
    resolution (or raised an error).
    """

    def __init__(self, needs: Iterable[DataNeed]):
        needs = list(needs)
        seen: dict[str, DataNeed] = {}
        for n in needs:
            if n.name in seen:
                raise ValueError(
                    f"duplicate DataNeed name: {n.name!r}")
            seen[n.name] = n
        self.needs: tuple[DataNeed, ...] = tuple(needs)

    # ----------------------------------------------------- declarations
    def varspecs(self) -> tuple[VarSpec, ...]:
        """Return VarSpecs the engine's contract layer understands."""
        return tuple(
            VarSpec(
                name=n.name,
                kind=n.kind,
                dtype=n.dtype,
                units=n.units,
                merge_policy=MergePolicy.LAST_WRITER,
                description=n.description,
                max_native_res_m=n.max_native_res_m,
                required=n.required,
            )
            for n in self.needs
        )

    def names(self) -> tuple[str, ...]:
        return tuple(n.name for n in self.needs)

    def need(self, name: str) -> DataNeed:
        for n in self.needs:
            if n.name == name:
                return n
        raise KeyError(name)

    # ----------------------------------------------------- search / check
    def search(self, cube) -> dict[str, dict[str, Any]]:
        """Report which needs the cube currently satisfies and at what
        resolution. Used for diagnostics; the engine itself uses
        cube.satisfies() during scheduling."""
        report: dict[str, dict[str, Any]] = {}
        for n in self.needs:
            entry: dict[str, Any] = {"present": False,
                                      "resolution_ok": False,
                                      "native_res_m": None}
            if cube.has(n.name) or self._has_time(cube, n.name):
                entry["present"] = True
                if hasattr(cube, "catalog"):
                    native = cube.catalog.native_resolution_m(n.name)
                    entry["native_res_m"] = native
                    if n.max_native_res_m is None or (
                            native is not None
                            and native <= n.max_native_res_m):
                        entry["resolution_ok"] = True
            report[n.name] = entry
        return report

    def missing(self, cube, request=None) -> list[str]:
        """Names the cube does NOT currently satisfy at the declared
        resolution. Useful for fast preflight before running a model."""
        out: list[str] = []
        for spec in self.varspecs():
            if not spec.required:
                continue
            try:
                ok = bool(cube.satisfies(spec, request))
            except Exception:
                ok = False
            if not ok:
                out.append(spec.name)
        return out

    # ----------------------------------------------------- fetch
    def fetch(self, cube, request=None) -> dict[str, Any]:
        """Read every declared need from the cube.

        Returns a dict keyed by need name. Static needs map to ndarray.
        Time needs map to (timestamps_list, ndarray). Optional needs
        that aren't present map to None.

        The engine's scheduler runs before this is called, so all
        required inputs should already be present at the declared
        resolution. If a *required* need is unexpectedly absent at
        fetch time we raise loudly rather than silently return None.
        """
        out: dict[str, Any] = {}
        for n in self.needs:
            present = cube.has(n.name) or self._has_time(cube, n.name)
            if not present:
                if n.required:
                    raise RuntimeError(
                        f"DataAdapter.fetch: required {n.name!r} "
                        "missing from cube; the engine should have "
                        "resolved it before this call")
                out[n.name] = None
                continue
            if n.kind == "static":
                out[n.name] = cube.read_static(n.name)
            elif n.kind == "time":
                out[n.name] = cube.read_3d(n.name)
            else:
                raise ValueError(f"unknown kind {n.kind!r} for {n.name!r}")
        return out

    # ----------------------------------------------------- helpers
    @staticmethod
    def _has_time(cube, name: str) -> bool:
        """cube.has(name) returns True only for static vars; for time
        vars we check the catalog directly."""
        try:
            return bool(cube.catalog.list_times(name))
        except Exception:
            return False
