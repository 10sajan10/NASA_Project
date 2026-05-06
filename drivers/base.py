"""Driver ABC + simple registry. A driver pulls data from one source and
writes it to the cube. The fusion engine dispatches drivers by variable name.
"""
from __future__ import annotations
from abc import ABC, abstractmethod
from datetime import datetime
from typing import ClassVar, Optional

from cube.store import Cube


class Driver(ABC):
    name: ClassVar[str]
    produces: ClassVar[list[str]]
    is_static: ClassVar[bool] = False

    @abstractmethod
    def fetch(self, cube: Cube,
              t_start: Optional[datetime] = None,
              t_end: Optional[datetime] = None) -> list[str]:
        """Fetch + write to cube. Returns variables that were (re)written."""
        ...


_REGISTRY: dict[str, Driver] = {}


def register(driver: Driver) -> Driver:
    _REGISTRY[driver.name] = driver
    return driver


def driver_for(variable: str) -> Driver:
    for d in _REGISTRY.values():
        if variable in d.produces:
            return d
    raise KeyError(f"no registered driver produces {variable!r}")


def all_drivers() -> dict[str, Driver]:
    return dict(_REGISTRY)


def reset_registry() -> None:
    _REGISTRY.clear()
