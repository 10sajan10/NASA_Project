"""Cube: per-variable Zarr stores indexed by a DuckDB catalog.

Static variables are written as 2-D (y, x); time variables are 3-D (t, y, x)
with append along t. All variables share the canonical SimulationGrid.
"""
from __future__ import annotations
from datetime import datetime
from pathlib import Path
from typing import Optional, Iterable

import numpy as np
import xarray as xr

from .catalog import Catalog, TileRecord
from .grid import SimulationGrid


class Cube:
    def __init__(self, root: Path | str, grid: SimulationGrid):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "cube").mkdir(exist_ok=True)
        self.grid = grid
        self.catalog = Catalog(self.root / "catalog.duckdb")
        # save grid for reproducibility
        grid.save(self.root / "grid.json")

    def _zarr_path(self, variable: str) -> Path:
        return self.root / "cube" / f"{variable}.zarr"

    # ---------- writes ----------
    def write_static(self, variable: str, array: np.ndarray, *,
                     source: str, native_res_m: float,
                     units: str = "", producer: str = "",
                     description: str = "") -> None:
        if array.shape != self.grid.shape:
            raise ValueError(
                f"{variable}: array shape {array.shape} != grid {self.grid.shape}")
        xs, ys = self.grid.cell_centers_xy()
        ds = xr.Dataset(
            {variable: (("y", "x"), array)},
            coords={"y": ys, "x": xs},
            attrs={"crs_epsg": int(self.grid.crs_epsg),
                   "pixel_m": float(self.grid.pixel_m),
                   "source": source,
                   "native_res_m": float(native_res_m),
                   "units": units},
        )
        ds.to_zarr(self._zarr_path(variable), mode="w")
        self.catalog.register_variable(variable, kind="static",
                                        units=units, dtype=str(array.dtype),
                                        description=description,
                                        producer=producer)
        self.catalog.add_tile(TileRecord(variable=variable, t=None,
                                         source=source,
                                         native_res_m=native_res_m))

    def write_timestep(self, variable: str, t: datetime,
                       array: np.ndarray, *,
                       source: str, native_res_m: float,
                       units: str = "", producer: str = "",
                       description: str = "") -> None:
        if array.shape != self.grid.shape:
            raise ValueError(
                f"{variable}: array shape {array.shape} != grid {self.grid.shape}")
        if self.catalog.has(variable, t):
            return  # idempotent
        xs, ys = self.grid.cell_centers_xy()
        t64 = np.array([np.datetime64(t)], dtype="datetime64[ns]")
        ds = xr.Dataset(
            {variable: (("t", "y", "x"), array[np.newaxis])},
            coords={"t": t64, "y": ys, "x": xs},
            attrs={"crs_epsg": int(self.grid.crs_epsg),
                   "pixel_m": float(self.grid.pixel_m),
                   "source": source,
                   "native_res_m": float(native_res_m),
                   "units": units},
        )
        path = self._zarr_path(variable)
        if path.exists():
            ds.to_zarr(path, mode="a", append_dim="t")
        else:
            ds.to_zarr(path, mode="w")
        self.catalog.register_variable(variable, kind="time",
                                        units=units, dtype=str(array.dtype),
                                        description=description,
                                        producer=producer)
        self.catalog.add_tile(TileRecord(variable=variable, t=t,
                                         source=source,
                                         native_res_m=native_res_m))

    def write_3d(self, variable: str, ts: list[datetime],
                 cube_array: np.ndarray, *, source: str,
                 native_res_m: float, units: str = "",
                 producer: str = "", description: str = "") -> None:
        """Bulk-write a (T, Y, X) array. Replaces any existing variable."""
        if cube_array.shape[1:] != self.grid.shape:
            raise ValueError(
                f"{variable}: array shape {cube_array.shape[1:]} != grid")
        xs, ys = self.grid.cell_centers_xy()
        t64 = np.array([np.datetime64(t) for t in ts], dtype="datetime64[ns]")
        ds = xr.Dataset(
            {variable: (("t", "y", "x"), cube_array)},
            coords={"t": t64, "y": ys, "x": xs},
            attrs={"crs_epsg": int(self.grid.crs_epsg),
                   "pixel_m": float(self.grid.pixel_m),
                   "source": source,
                   "native_res_m": float(native_res_m),
                   "units": units},
        )
        ds.to_zarr(self._zarr_path(variable), mode="w")
        self.catalog.register_variable(variable, kind="time",
                                        units=units, dtype=str(cube_array.dtype),
                                        description=description,
                                        producer=producer)
        for t in ts:
            self.catalog.add_tile(TileRecord(variable=variable, t=t,
                                              source=source,
                                              native_res_m=native_res_m))

    # ---------- reads ----------
    def read_static(self, variable: str) -> np.ndarray:
        ds = xr.open_zarr(self._zarr_path(variable))
        return ds[variable].values

    def read_timestep(self, variable: str, t: datetime) -> np.ndarray:
        ds = xr.open_zarr(self._zarr_path(variable))
        return ds[variable].sel(t=np.datetime64(t), method="nearest").values

    def read_3d(self, variable: str) -> tuple[list[datetime], np.ndarray]:
        ds = xr.open_zarr(self._zarr_path(variable))
        ts = [t.astype("datetime64[us]").item() for t in ds.t.values]
        return ts, ds[variable].values

    # ---------- introspection ----------
    def has(self, variable: str, t: Optional[datetime] = None) -> bool:
        return self.catalog.has(variable, t)

    def list_variables(self) -> list[dict]:
        return self.catalog.list_variables()

    def export_catalog(self) -> None:
        self.catalog.export_json_catalog(self.root / "catalog.json")

    def close(self) -> None:
        self.catalog.close()
