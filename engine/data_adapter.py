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
from datetime import datetime
from typing import Any, Iterable, Optional

from .contracts import MergePolicy, Request, VarSpec


class NoDataAvailable(RuntimeError):
    """Raised by `DataAdapter.resolve` when a required variable cannot be
    satisfied: the cube has no data for the requested window AND no
    producer is registered (or the registered producer ran and still
    didn't satisfy the spec).

    Carries the variable name + requested window so callers can build
    a meaningful error message at any layer.
    """

    def __init__(self, variable: str,
                 t_start: Optional[datetime] = None,
                 t_end: Optional[datetime] = None,
                 reason: str = ""):
        self.variable = variable
        self.t_start = t_start
        self.t_end = t_end
        self.reason = reason
        window = f"[{t_start}, {t_end}]" if t_start or t_end else "<no time window>"
        msg = (f"no data to retrieve variable {variable!r} for {window}"
               + (f": {reason}" if reason else ""))
        super().__init__(msg)


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

    # ----------------------------------------------------- resolve
    def resolve(self, cube, registry, request=None) -> dict[str, dict[str, Any]]:
        """Time-aware variable resolution.

        For each declared need, in order:

          1. Ask the cube: does it already satisfy the spec for this
             `request` (variable + time window + resolution cap)?
             Yes -> mark satisfied via ``"cube"`` and move on.
          2. Otherwise, look up a producer in `registry` that can
             generate the variable. If found, run it and re-check the
             cube. Mark satisfied via ``"producer:<name>"`` on success.
          3. Otherwise (no cube hit AND no producer, OR producer ran
             but cube still doesn't satisfy):
                - if the need is required -> raise NoDataAvailable
                - if the need is optional -> record ``error="no_data"``
                  and continue

        Returns a per-need report:
            { name: {satisfied: bool, via: str|None, error: str|None,
                     native_res_m: float|None} }

        This method is the system's "where does data come from?" entry
        point. Both data drivers (fetch external bytes) and models
        (compute from other variables) plug in via the same registry;
        `resolve` doesn't care which kind a producer is — it just runs
        whatever satisfies the variable.
        """
        report: dict[str, dict[str, Any]] = {}
        for n in self.needs:
            spec = self._spec_for(n)
            entry: dict[str, Any] = {
                "satisfied": False, "via": None,
                "error": None, "native_res_m": None,
            }

            if self._cube_satisfies(cube, spec, request):
                entry["satisfied"] = True
                entry["via"] = "cube"
                entry["native_res_m"] = self._native_res(cube, n.name)
                report[n.name] = entry
                continue

            producer = self._lookup_producer(registry, n.name)
            if producer is None:
                entry["error"] = "no_producer"
                if n.required:
                    raise NoDataAvailable(
                        n.name,
                        getattr(request, "t_start", None),
                        getattr(request, "t_end", None),
                        reason="cube has none and no producer registered")
                report[n.name] = entry
                continue

            try:
                self._invoke_producer(producer, cube, request)
            except Exception as exc:
                entry["error"] = f"producer_failed: {exc}"
                if n.required:
                    raise NoDataAvailable(
                        n.name,
                        getattr(request, "t_start", None),
                        getattr(request, "t_end", None),
                        reason=f"producer "
                               f"{getattr(producer, 'name', '?')!r} "
                               f"raised: {exc}") from exc
                report[n.name] = entry
                continue

            if self._cube_satisfies(cube, spec, request):
                entry["satisfied"] = True
                entry["via"] = f"producer:{getattr(producer, 'name', '?')}"
                entry["native_res_m"] = self._native_res(cube, n.name)
            else:
                entry["error"] = "producer_ran_but_not_satisfied"
                if n.required:
                    raise NoDataAvailable(
                        n.name,
                        getattr(request, "t_start", None),
                        getattr(request, "t_end", None),
                        reason=f"producer "
                               f"{getattr(producer, 'name', '?')!r} ran "
                               f"but cube still does not satisfy spec")
            report[n.name] = entry
        return report

    # ----------------------------------------------------- fetch
    def fetch(self, cube, request=None,
              *,
              target_pixel_m: Optional[float] = None,
              target_times: Optional[list[datetime]] = None,
              spatial_method: str = "bilinear",
              temporal_method: str = "linear") -> dict[str, Any]:
        """Read every declared need from the cube, with optional
        spatial / temporal resampling.

        Returns a dict keyed by need name:
          * static need  -> ndarray
          * time need    -> (timestamps_list, ndarray)
          * optional & absent -> None

        Resampling (model-agnostic, off by default):

          * `target_pixel_m` : if given AND differs from a variable's
                                catalog native resolution, the array is
                                resampled to the requested pixel size
                                using `engine.resample.resample_spatial`.
          * `target_times`   : if given, time-kind variables have their
                                time axis re-projected onto these
                                timestamps via
                                `engine.resample.resample_temporal`.
          * `spatial_method` / `temporal_method` are forwarded to the
            resampling functions; the defaults suit continuous fields.

        The cube remains the single source of truth — resampling is
        purely a read-time projection so consumers don't all have to
        reimplement the same interp/aggregation logic.
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
                arr = cube.read_static(n.name)
                arr = self._maybe_resample_spatial(
                    cube, n.name, arr, target_pixel_m, spatial_method)
                out[n.name] = arr
            elif n.kind == "time":
                t_start = getattr(request, "t_start", None)
                t_end = getattr(request, "t_end", None)
                if (t_start is not None or t_end is not None) and \
                        hasattr(cube, "read_3d_window"):
                    ts, arr = cube.read_3d_window(n.name, t_start, t_end)
                else:
                    ts, arr = cube.read_3d(n.name)
                if target_times is not None and ts:
                    from .resample import resample_temporal
                    arr = resample_temporal(
                        ts, arr, target_times, method=temporal_method)
                    ts = list(target_times)
                arr = self._maybe_resample_spatial(
                    cube, n.name, arr, target_pixel_m, spatial_method)
                out[n.name] = (ts, arr)
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

    def _spec_for(self, n: DataNeed) -> VarSpec:
        """Inline-build a VarSpec for a single need (used by resolve)."""
        return VarSpec(
            name=n.name,
            kind=n.kind,
            dtype=n.dtype,
            units=n.units,
            merge_policy=MergePolicy.LAST_WRITER,
            description=n.description,
            max_native_res_m=n.max_native_res_m,
            required=n.required,
        )

    @staticmethod
    def _cube_satisfies(cube, spec: VarSpec, request) -> bool:
        try:
            return bool(cube.satisfies(spec, request))
        except Exception:
            return False

    @staticmethod
    def _native_res(cube, name: str) -> Optional[float]:
        try:
            return cube.catalog.native_resolution_m(name)
        except Exception:
            return None

    @staticmethod
    def _lookup_producer(registry, variable: str):
        """Find a producer that can produce `variable`. Duck-typed:
        any registry exposing producer_for / has_variable / get works.
        Returns None if no producer is registered for the variable."""
        if registry is None:
            return None
        try:
            if hasattr(registry, "has_variable") and \
                    not registry.has_variable(variable):
                return None
            return registry.producer_for(variable)
        except Exception:
            return None

    @staticmethod
    def _invoke_producer(producer, cube, request) -> None:
        """Call a producer to materialise its output into the cube.

        Duck-typed for both ProducerV2 (``run(cube, request)``) and the
        Driver protocol (``fetch(cube, t_start=..., t_end=...)``). The
        DataAdapter doesn't care which shape is registered as long as
        one of these entry points exists.
        """
        if hasattr(producer, "run"):
            # Ensure we always pass a Request, even if caller passed None.
            req = request if request is not None else Request()
            producer.run(cube, req)
            return
        if hasattr(producer, "fetch"):
            t_start = getattr(request, "t_start", None)
            t_end = getattr(request, "t_end", None)
            producer.fetch(cube, t_start=t_start, t_end=t_end)
            return
        raise RuntimeError(
            f"producer {getattr(producer, 'name', producer)!r} exposes "
            "neither `run(cube, request)` nor `fetch(cube, t_start=..., "
            "t_end=...)`")

    @staticmethod
    def _maybe_resample_spatial(cube, name: str, arr,
                                 target_pixel_m: Optional[float],
                                 method: str):
        """Bridge to engine.resample.resample_spatial driven by the
        catalog's native_res_m for this variable. No-ops if the target
        equals the source or the catalog can't supply a source size."""
        if target_pixel_m is None:
            return arr
        src = DataAdapter._native_res(cube, name)
        if src is None or src <= 0:
            return arr
        if abs(float(src) - float(target_pixel_m)) < 1e-9:
            return arr
        from .resample import resample_spatial
        return resample_spatial(arr, float(src), float(target_pixel_m),
                                 method=method)
