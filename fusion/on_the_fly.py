"""On-the-fly data fusion.

`get_static` / `get_timestep` look up a variable in the cube; if it's missing,
the dispatcher invokes the registered driver, which fetches the data, writes
it to the cube, and the read retries.

Models call only these two functions — they never know whether a value came
from a cached tile or from a fresh download.
"""
from __future__ import annotations
from datetime import datetime
from typing import Optional

import numpy as np

from cube.store import Cube
from drivers.base import driver_for


def get_static(cube: Cube, variable: str) -> np.ndarray:
    if not cube.has(variable):
        d = driver_for(variable)
        d.fetch(cube)
        if not cube.has(variable):
            raise RuntimeError(
                f"driver {d.name!r} ran but {variable!r} still missing")
    return cube.read_static(variable)


def get_timestep(cube: Cube, variable: str, t: datetime) -> np.ndarray:
    if not cube.has(variable, t):
        d = driver_for(variable)
        d.fetch(cube, t_start=t, t_end=t)
        if not cube.has(variable, t):
            raise RuntimeError(
                f"driver {d.name!r} ran but {variable!r} @ {t} still missing")
    return cube.read_timestep(variable, t)


def ensure_range(cube: Cube, variable: str,
                 t_start: datetime, t_end: datetime) -> None:
    """Make sure all timesteps in [t_start, t_end] are present.
    Driver decides timestep cadence. We just trigger one fetch over the range.
    """
    d = driver_for(variable)
    d.fetch(cube, t_start=t_start, t_end=t_end)
