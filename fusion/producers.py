"""Producer interfaces for data/model adapters.

A producer is anything that can materialize one or more cube variables. Data
adapters fetch external data; model adapters derive or predict variables from
other variables. The resolver only cares about `produces`, `requires`, and
`run`, which keeps the graph extensible.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Iterable, Optional, Protocol

from cube.store import Cube
from drivers.base import Driver


@dataclass
class VariableRequest:
    """Request for one or more variables over an optional time range.

    `t_start` is inclusive. `t_end` is exclusive. Static variables ignore the
    time range, while time-varying producers use it to decide the output
    horizon.
    """

    t_start: Optional[datetime] = None
    t_end: Optional[datetime] = None
    force: bool = False
    context: dict[str, Any] = field(default_factory=dict)

    @property
    def n_days(self) -> int:
        if self.t_start is None or self.t_end is None:
            raise ValueError("time range is required for n_days")
        days = (self.t_end.date() - self.t_start.date()).days
        return max(1, days)


class Producer(Protocol):
    name: str
    produces: list[str]
    requires: list[str]
    kind: str
    can_run_parallel: bool

    def run(self, cube: Cube, request: VariableRequest) -> list[str]:
        ...

    def is_satisfied(self, cube: Cube, variable: str,
                     request: VariableRequest) -> bool:
        ...


def _time_range_is_covered(cube: Cube, variable: str,
                           request: VariableRequest) -> bool:
    if request.t_start is None:
        return cube.has(variable)
    if request.t_end is None:
        return cube.has(variable, request.t_start)
    times = cube.catalog.list_times(variable)
    if not times:
        return False
    # `t_end` is exclusive. We only require coverage through the final
    # requested date because cube variables may be daily (KBDI) or hourly
    # (weather/fire), and cadence is producer-owned metadata.
    last_needed = request.t_end - timedelta(microseconds=1)
    return min(times) <= request.t_start and max(times).date() >= last_needed.date()


class BaseProducer:
    name: str = ""
    produces: list[str] = []
    requires: list[str] = []
    kind: str = "model"
    can_run_parallel: bool = False

    def run(self, cube: Cube, request: VariableRequest) -> list[str]:
        raise NotImplementedError

    def is_satisfied(self, cube: Cube, variable: str,
                     request: VariableRequest) -> bool:
        if request.force:
            return False
        if cube.has(variable):
            return True
        return _time_range_is_covered(cube, variable, request)


class DriverProducer(BaseProducer):
    """Wrap an existing Driver in the producer interface.

    `time_check_mode` controls when this producer is considered satisfied:
      - "as_requested"  (default) — requested t_start..t_end must be covered
      - "any"           — satisfied if any tile exists for the variable
                          (use for historical adapters whose timestamps are
                          intentionally in the past)
    `time_end_mode` controls how the requested t_end is mapped to the driver's
    fetch call:
      - "as_requested"  (default) — pass through unchanged
      - "inclusive_day" — convert exclusive day-end to inclusive last day
    """

    def __init__(self, driver: Driver, *,
                 requires: Optional[Iterable[str]] = None,
                 time_end_mode: str = "as_requested",
                 time_check_mode: str = "as_requested"):
        self.driver = driver
        self.name = driver.name
        self.produces = list(driver.produces)
        self.requires = list(requires or [])
        self.kind = "data"
        self.can_run_parallel = False
        self.time_end_mode = time_end_mode
        self.time_check_mode = time_check_mode

    def _driver_end(self, request: VariableRequest) -> Optional[datetime]:
        if request.t_end is None:
            return None
        if self.time_end_mode == "inclusive_day":
            from datetime import timedelta

            return request.t_end - timedelta(days=1)
        return request.t_end

    def is_satisfied(self, cube: Cube, variable: str,
                     request: VariableRequest) -> bool:
        if request.force:
            return False
        if cube.has(variable):
            return True
        if self.time_check_mode == "any":
            return bool(cube.catalog.list_times(variable))
        return _time_range_is_covered(cube, variable, request)

    def run(self, cube: Cube, request: VariableRequest) -> list[str]:
        if getattr(self.driver, "is_static", False):
            return self.driver.fetch(cube)
        return self.driver.fetch(cube, t_start=request.t_start,
                                 t_end=self._driver_end(request))


class FunctionProducer(BaseProducer):
    """Wrap a model function in the producer interface."""

    def __init__(self, *, name: str, produces: Iterable[str],
                 requires: Iterable[str],
                 func: Callable[[Cube, VariableRequest], list[str] | str],
                 kind: str = "model", can_run_parallel: bool = False):
        self.name = name
        self.produces = list(produces)
        self.requires = list(requires)
        self.func = func
        self.kind = kind
        self.can_run_parallel = can_run_parallel

    def run(self, cube: Cube, request: VariableRequest) -> list[str]:
        out = self.func(cube, request)
        if isinstance(out, str):
            return [out]
        return list(out)


class ProducerRegistry:
    """Map variables to the producer that materializes them."""

    def __init__(self):
        self._by_name: dict[str, Producer] = {}
        self._by_variable: dict[str, Producer] = {}

    def register(self, producer: Producer, *, replace: bool = False) -> Producer:
        if producer.name in self._by_name and not replace:
            raise ValueError(f"producer already registered: {producer.name}")
        for variable in producer.produces:
            if variable in self._by_variable and not replace:
                old = self._by_variable[variable].name
                raise ValueError(
                    f"{variable!r} already produced by {old!r}; "
                    f"cannot register {producer.name!r}")
        self._by_name[producer.name] = producer
        for variable in producer.produces:
            self._by_variable[variable] = producer
        return producer

    def producer_for(self, variable: str) -> Producer:
        try:
            return self._by_variable[variable]
        except KeyError as exc:
            raise KeyError(f"no producer registered for {variable!r}") from exc

    def get(self, name: str) -> Producer:
        return self._by_name[name]

    def producers(self) -> list[Producer]:
        return list(self._by_name.values())

    def variables(self) -> dict[str, str]:
        return {var: prod.name for var, prod in sorted(self._by_variable.items())}
