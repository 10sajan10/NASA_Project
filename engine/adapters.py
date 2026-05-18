"""Producer adapters for data drivers and model functions.

The engine's scalable boundary is intentionally small: a producer declares
the cube variables it requires and produces, then implements ``run``. These
adapters make that boundary explicit for the two common migration cases:

* data drivers that already expose ``fetch(cube, t_start, t_end)``
* model functions that already read/write the cube directly

Both become ProducerV2-shaped objects, so the planner and scheduler can treat
data access, model execution, and external-format conversion uniformly.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any, Callable, Iterable, Optional

from .contracts import (
    CostHint,
    MergePolicy,
    ProducerCapabilities,
    ProducerV2,
    Request,
    VarSpec,
)


def _varspecs(items: Iterable[str | VarSpec],
              *,
              default_kind: str = "static") -> tuple[VarSpec, ...]:
    specs: list[VarSpec] = []
    for item in items or ():
        if isinstance(item, VarSpec):
            specs.append(item)
        elif hasattr(item, "name") and hasattr(item, "kind"):
            specs.append(VarSpec(
                name=str(item.name),
                kind=str(item.kind),
                dtype=str(getattr(item, "dtype", "float32")),
                units=str(getattr(item, "units", "")),
                merge_policy=getattr(
                    item, "merge_policy", MergePolicy.LAST_WRITER),
                description=str(getattr(item, "description", "")),
                max_native_res_m=getattr(item, "max_native_res_m", None),
                required=bool(getattr(item, "required", True))))
        else:
            specs.append(VarSpec(str(item), kind=default_kind))
    return tuple(specs)


def _names(items: Iterable[str | VarSpec]) -> tuple[str, ...]:
    out: list[str] = []
    for item in items or ():
        out.append(item if isinstance(item, str) else str(item.name))
    return tuple(out)


def _latest_version(cube, variable: str) -> int:
    try:
        row = cube.catalog.con.execute(
            "SELECT MAX(version) FROM tiles WHERE variable=?",
            [variable]).fetchone()
        return int(row[0]) if row and row[0] is not None else 0
    except Exception:
        return 0


def _coerce_versions(out: Any, cube, declared: Iterable[str | VarSpec]
                     ) -> dict[str, int]:
    declared_names = _names(declared)
    if out is None:
        names = declared_names
    elif isinstance(out, dict):
        return {str(k): int(v) for k, v in out.items()}
    elif isinstance(out, str):
        names = (out,)
    else:
        names = tuple(str(v) for v in out)
    return {name: _latest_version(cube, name) for name in names}


class DataDriverAdapter(ProducerV2):
    """Wrap a data driver as a generic ProducerV2.

    The wrapped driver owns provider-specific access and reprojection logic.
    The engine only sees declared cube variables and the run result.
    """

    adapter_type = "data"
    capabilities = ProducerCapabilities(
        tile_parallel=False,
        cost_hint=CostHint.IO,
        memory_budget_mb=512)

    def __init__(self,
                 driver,
                 *,
                 name: Optional[str] = None,
                 produces: Optional[Iterable[str | VarSpec]] = None,
                 requires: Iterable[str | VarSpec] = (),
                 time_end_mode: str = "as_requested",
                 time_check_mode: str = "as_requested") -> None:
        self.driver = driver
        self.name = name or driver.name
        default_kind = "static" if getattr(driver, "is_static", False) else "time"
        self.produces = _varspecs(
            produces if produces is not None else driver.produces,
            default_kind=default_kind)
        self.requires = _varspecs(requires)
        self.time_end_mode = time_end_mode
        self.time_check_mode = time_check_mode

    def compute(self, inputs: dict[str, Any],
                request: Request) -> dict[str, Any]:
        raise NotImplementedError(
            "DataDriverAdapter runs the wrapped driver's fetch() directly")

    def _driver_end(self, request: Request):
        if request.t_end is None:
            return None
        if self.time_end_mode == "inclusive_day":
            return request.t_end - timedelta(days=1)
        return request.t_end

    def _satisfies_var(self, cube, variable: str, request: Request) -> bool:
        if self.time_check_mode == "any":
            try:
                return cube.has(variable) or bool(cube.catalog.list_times(variable))
            except Exception:
                return False
        spec = next((s for s in self.produces if s.name == variable), variable)
        if hasattr(cube, "satisfies"):
            return bool(cube.satisfies(spec, request))
        return bool(cube.has(variable))

    def is_satisfied(self, cube, variable: str,
                     request: Request) -> bool:
        if request is not None and getattr(request, "force", False):
            return False
        return self._satisfies_var(cube, variable, request)

    def run(self, cube, request: Request) -> dict[str, int]:
        if getattr(self.driver, "is_static", False):
            out = self.driver.fetch(cube)
        else:
            out = self.driver.fetch(cube, t_start=request.t_start,
                                    t_end=self._driver_end(request))
        return _coerce_versions(out, cube, self.produces)


class ModelFunctionAdapter(ProducerV2):
    """Wrap an existing cube-reading/writing model function.

    New models should usually subclass ProducerV2 directly. This adapter is a
    bridge for legacy model functions while the codebase migrates toward the
    generic contract.
    """

    adapter_type = "model"
    capabilities = ProducerCapabilities(
        tile_parallel=False,
        cost_hint=CostHint.CPU)

    def __init__(self,
                 *,
                 name: str,
                 produces: Iterable[str | VarSpec],
                 requires: Iterable[str | VarSpec],
                 func: Callable[[Any, Request], Any],
                 capabilities: Optional[ProducerCapabilities] = None) -> None:
        self.name = name
        self.produces = _varspecs(produces)
        self.requires = _varspecs(requires)
        self.func = func
        if capabilities is not None:
            self.capabilities = capabilities

    def compute(self, inputs: dict[str, Any],
                request: Request) -> dict[str, Any]:
        raise NotImplementedError(
            "ModelFunctionAdapter runs the wrapped function directly")

    def run(self, cube, request: Request) -> dict[str, int]:
        out = self.func(cube, request)
        return _coerce_versions(out, cube, self.produces)
