"""Cube: per-variable Zarr stores indexed by a DuckDB catalog.

Two write APIs are provided:
  * `write_static` / `write_3d`        - bulk; the full array lives in RAM
  * `init_static_tiled` + `write_chunk_static` (and the time variants)
    pre-allocate a chunked Zarr and stream the array in spatial / temporal
    tiles. Memory scales with the chunk size, not the full grid.

Producers that materialise large arrays (ERA5 interp, climate regression,
hourly dead-fuel, KBDI, fire frames) use the chunked path so a 1000 x 1000
grid x 720 hourly steps no longer needs to fit in RAM.
"""
from __future__ import annotations
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterator, Optional

import numpy as np
import xarray as xr
import zarr

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

    # ------------------------------------------------------------------
    # legacy bulk writes (full array in memory)
    # ------------------------------------------------------------------
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
            return
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

    # ------------------------------------------------------------------
    # chunked / tiled writes  (memory scales with chunk size)
    # ------------------------------------------------------------------
    def _open_root_group(self, variable: str, mode: str = "r+"
                         ) -> zarr.Group:
        return zarr.open_group(str(self._zarr_path(variable)), mode=mode)

    def _open_var_array(self, variable: str, mode: str = "r+"
                        ) -> zarr.Array:
        g = self._open_root_group(variable, mode=mode)
        return g[variable]

    def init_static_tiled(self, variable: str, *,
                          dtype="float32",
                          fill_value: float | int = np.nan,
                          source: str, native_res_m: float,
                          units: str = "", producer: str = "",
                          description: str = "",
                          chunk: tuple[int, int] = (256, 256),
                          overwrite: bool = True) -> zarr.Array:
        """Pre-allocate a chunked (H, W) Zarr store.

        Empty chunks are not persisted (Zarr's sparse-chunk default), so this
        is cheap on disk. Subsequent `write_chunk_static` calls fill chunks one
        at a time, freeing memory between chunks.
        """
        H, W = self.grid.shape
        xs, ys = self.grid.cell_centers_xy()
        path = self._zarr_path(variable)
        if path.exists():
            if not overwrite:
                return self._open_var_array(variable, mode="r+")
            shutil.rmtree(path)

        chunk = (min(chunk[0], H), min(chunk[1], W))
        g = zarr.open_group(str(path), mode="w")
        z_var = g.create_array(
            name=variable, shape=(H, W), chunks=chunk,
            dtype=dtype, fill_value=fill_value,
            dimension_names=("y", "x"))
        z_var.attrs.update({
            "crs_epsg": int(self.grid.crs_epsg),
            "pixel_m": float(self.grid.pixel_m),
            "source": source,
            "native_res_m": float(native_res_m),
            "units": units,
        })
        z_y = g.create_array(name="y", shape=(H,), chunks=(H,),
                              dtype="float64", dimension_names=("y",))
        z_y[:] = ys
        z_x = g.create_array(name="x", shape=(W,), chunks=(W,),
                              dtype="float64", dimension_names=("x",))
        z_x[:] = xs

        self.catalog.register_variable(variable, kind="static",
                                        units=units, dtype=str(dtype),
                                        description=description,
                                        producer=producer)
        self.catalog.add_tile(TileRecord(variable=variable, t=None,
                                          source=source,
                                          native_res_m=native_res_m))
        return z_var

    def init_time_tiled(self, variable: str, *,
                        ts: list[datetime],
                        dtype="float32",
                        fill_value: float | int = np.nan,
                        source: str, native_res_m: float,
                        units: str = "", producer: str = "",
                        description: str = "",
                        chunk: tuple[int, int, int] = (24, 128, 128),
                        overwrite: bool = True) -> zarr.Array:
        H, W = self.grid.shape
        T = len(ts)
        xs, ys = self.grid.cell_centers_xy()
        path = self._zarr_path(variable)
        if path.exists():
            if not overwrite:
                return self._open_var_array(variable, mode="r+")
            shutil.rmtree(path)

        chunk = (min(chunk[0], T), min(chunk[1], H), min(chunk[2], W))
        g = zarr.open_group(str(path), mode="w")
        z_var = g.create_array(
            name=variable, shape=(T, H, W), chunks=chunk,
            dtype=dtype, fill_value=fill_value,
            dimension_names=("t", "y", "x"))
        z_var.attrs.update({
            "crs_epsg": int(self.grid.crs_epsg),
            "pixel_m": float(self.grid.pixel_m),
            "source": source,
            "native_res_m": float(native_res_m),
            "units": units,
        })
        # store time as int64 nanoseconds since epoch + CF units, the convention
        # xarray decodes back to datetime64.
        t_ns = np.array([np.datetime64(t).astype("datetime64[ns]")
                         for t in ts]).astype("int64")
        z_t = g.create_array(name="t", shape=(T,), chunks=(T,),
                              dtype="int64", dimension_names=("t",))
        z_t[:] = t_ns
        z_t.attrs["units"] = "nanoseconds since 1970-01-01"
        z_t.attrs["calendar"] = "proleptic_gregorian"

        z_y = g.create_array(name="y", shape=(H,), chunks=(H,),
                              dtype="float64", dimension_names=("y",))
        z_y[:] = ys
        z_x = g.create_array(name="x", shape=(W,), chunks=(W,),
                              dtype="float64", dimension_names=("x",))
        z_x[:] = xs

        self.catalog.register_variable(variable, kind="time",
                                        units=units, dtype=str(dtype),
                                        description=description,
                                        producer=producer)
        for t in ts:
            self.catalog.add_tile(TileRecord(variable=variable, t=t,
                                              source=source,
                                              native_res_m=native_res_m))
        return z_var

    def write_chunk_static(self, variable: str,
                           y_slice: slice, x_slice: slice,
                           data: np.ndarray) -> None:
        z = self._open_var_array(variable)
        z[y_slice, x_slice] = data.astype(z.dtype, copy=False)

    def write_chunk_time(self, variable: str,
                         t_slice: slice, y_slice: slice, x_slice: slice,
                         data: np.ndarray) -> None:
        z = self._open_var_array(variable)
        z[t_slice, y_slice, x_slice] = data.astype(z.dtype, copy=False)

    def read_chunk_static(self, variable: str,
                          y_slice: slice, x_slice: slice) -> np.ndarray:
        z = self._open_var_array(variable, mode="r")
        return np.asarray(z[y_slice, x_slice])

    def read_chunk_time(self, variable: str,
                        t_slice: slice, y_slice: slice, x_slice: slice
                        ) -> np.ndarray:
        z = self._open_var_array(variable, mode="r")
        return np.asarray(z[t_slice, y_slice, x_slice])

    def iter_spatial_tiles(self, tile: int = 128
                           ) -> Iterator[tuple[slice, slice]]:
        """Walk the grid in (tile x tile) spatial blocks. Last row/col may
        be smaller. Used by every producer that streams chunk-by-chunk."""
        H, W = self.grid.shape
        for y0 in range(0, H, tile):
            y1 = min(y0 + tile, H)
            for x0 in range(0, W, tile):
                x1 = min(x0 + tile, W)
                yield slice(y0, y1), slice(x0, x1)

    # ------------------------------------------------------------------
    # halo I/O: read a tile padded with neighbouring cells; write back only
    # the inner (non-halo) region. Boundary-coupled producers (fire spread,
    # diffusion, convolutional stencils) need this to compute correctly at
    # tile edges without seam artifacts.
    # ------------------------------------------------------------------
    def _halo_bounds(self, y_slice: slice, x_slice: slice,
                     halo: int) -> tuple[slice, slice, slice, slice]:
        """Compute halo-expanded outer slice and the inner slice WITHIN the
        halo'd array that corresponds to the original (y_slice, x_slice).

        Returns (outer_y, outer_x, inner_y_in_arr, inner_x_in_arr).
        Halo is clipped at grid edges so cells outside the grid aren't
        invented; the inner slice still points at the original tile,
        possibly closer to the array edge."""
        if halo < 0:
            raise ValueError("halo must be non-negative")
        H, W = self.grid.shape
        y0 = max(0, y_slice.start - halo)
        y1 = min(H, y_slice.stop + halo)
        x0 = max(0, x_slice.start - halo)
        x1 = min(W, x_slice.stop + halo)
        outer_y = slice(y0, y1)
        outer_x = slice(x0, x1)
        inner_y = slice(y_slice.start - y0, y_slice.stop - y0)
        inner_x = slice(x_slice.start - x0, x_slice.stop - x0)
        return outer_y, outer_x, inner_y, inner_x

    def read_chunk_static_with_halo(self, variable: str,
                                     y_slice: slice, x_slice: slice,
                                     halo: int
                                     ) -> tuple[np.ndarray,
                                                 tuple[slice, slice]]:
        """Read (y_slice, x_slice) padded with `halo` cells on each side.

        Returns (data, inner) where `data` is the halo'd ndarray (slightly
        smaller than 2*halo+tile if the tile sits against the grid edge)
        and `inner = (inner_y_in_arr, inner_x_in_arr)` is the slice
        within `data` that maps back to the original tile bounds. After
        the producer computes over `data`, it writes `data[inner]`
        back via `write_chunk_static`.
        """
        outer_y, outer_x, inner_y, inner_x = self._halo_bounds(
            y_slice, x_slice, halo)
        data = self.read_chunk_static(variable, outer_y, outer_x)
        return data, (inner_y, inner_x)

    def read_chunk_time_with_halo(self, variable: str,
                                   t_slice: slice,
                                   y_slice: slice, x_slice: slice,
                                   halo: int
                                   ) -> tuple[np.ndarray,
                                              tuple[slice, slice, slice]]:
        """Time-3D version. Returns (data, (full_t, inner_y, inner_x))."""
        outer_y, outer_x, inner_y, inner_x = self._halo_bounds(
            y_slice, x_slice, halo)
        data = self.read_chunk_time(variable, t_slice, outer_y, outer_x)
        full_t = slice(0, data.shape[0])
        return data, (full_t, inner_y, inner_x)

    def write_inner_static(self, variable: str,
                            y_slice: slice, x_slice: slice,
                            halo_data: np.ndarray,
                            halo: int) -> None:
        """Write only the inner (non-halo) region of `halo_data` to the
        target tile. Caller computed over a halo'd extent; this drops the
        halo before persisting so neighbouring tiles' inner regions
        aren't clobbered by overlapping halos."""
        _, _, inner_y, inner_x = self._halo_bounds(y_slice, x_slice, halo)
        inner = halo_data[inner_y, inner_x]
        self.write_chunk_static(variable, y_slice, x_slice, inner)

    def write_inner_time(self, variable: str,
                          t_slice: slice,
                          y_slice: slice, x_slice: slice,
                          halo_data: np.ndarray,
                          halo: int) -> None:
        """Time-3D version of write_inner_static."""
        _, _, inner_y, inner_x = self._halo_bounds(y_slice, x_slice, halo)
        inner = halo_data[:, inner_y, inner_x]
        self.write_chunk_time(variable, t_slice, y_slice, x_slice, inner)

    # ------------------------------------------------------------------
    # reads
    # ------------------------------------------------------------------
    def read_static(self, variable: str) -> np.ndarray:
        ds = xr.open_zarr(self._zarr_path(variable), consolidated=False)
        return ds[variable].values

    def read_timestep(self, variable: str, t: datetime) -> np.ndarray:
        ds = xr.open_zarr(self._zarr_path(variable), consolidated=False)
        return ds[variable].sel(t=np.datetime64(t), method="nearest").values

    def read_3d(self, variable: str) -> tuple[list[datetime], np.ndarray]:
        ds = xr.open_zarr(self._zarr_path(variable), consolidated=False)
        ts = [t.astype("datetime64[us]").item() for t in ds.t.values]
        return ts, ds[variable].values

    def read_3d_times(self, variable: str) -> list[datetime]:
        """Return only the time axis (cheap; doesn't load data)."""
        ds = xr.open_zarr(self._zarr_path(variable), consolidated=False)
        return [t.astype("datetime64[us]").item() for t in ds.t.values]

    # ------------------------------------------------------------------
    # introspection
    # ------------------------------------------------------------------
    def has(self, variable: str, t: Optional[datetime] = None) -> bool:
        return self.catalog.has(variable, t)

    def is_output_stale(self, output_var: str,
                         required_vars) -> bool:
        """True if any required input has been written more recently than
        `output_var`. Used by the engine scheduler to invalidate
        cached outputs whose upstream variables have been re-fetched."""
        names = [r if isinstance(r, str) else str(getattr(r, "name", r))
                 for r in (required_vars or ())]
        return self.catalog.is_output_stale(output_var, names)

    def satisfies(self, spec, request=None) -> bool:
        """Return True if this cube already satisfies a variable request.

        `spec` may be a variable name or any VarSpec-like object with
        `name`, `kind`, and optional `max_native_res_m` attributes. The cube
        owns the cache/reuse decision: producers and schedulers can ask this
        one question instead of reimplementing static/time/resolution checks.
        """
        if isinstance(spec, str):
            name = spec
            kind = None
            max_native_res_m = None
        else:
            name = str(getattr(spec, "name"))
            kind = getattr(spec, "kind", None)
            max_native_res_m = getattr(spec, "max_native_res_m", None)

        meta = self.catalog.get_variable(name)
        if meta is None:
            return False
        stored_kind = meta.get("kind")
        effective_kind = kind or stored_kind
        if kind is not None and stored_kind != kind:
            return False

        if max_native_res_m is not None:
            native = self.catalog.native_resolution_m(name)
            if native is None or native > float(max_native_res_m):
                return False

        if effective_kind == "static":
            return self.catalog.has(name)

        if effective_kind == "time":
            times = self.catalog.list_times(name)
            if not times:
                return False
            t_start = getattr(request, "t_start", None)
            t_end = getattr(request, "t_end", None)
            if t_start is None:
                return True
            if t_end is None:
                return self.catalog.has(name, t_start)
            # Treat t_end as exclusive. Cadence is producer-owned metadata, so
            # daily and hourly products both satisfy the same date window when
            # they cover the requested start and final requested date.
            last_needed = t_end - timedelta(microseconds=1)
            return (min(times) <= t_start
                    and max(times).date() >= last_needed.date())

        # Unknown kinds are intentionally not guessed.
        return False

    def list_variables(self) -> list[dict]:
        return self.catalog.list_variables()

    def export_catalog(self) -> None:
        self.catalog.export_json_catalog(self.root / "catalog.json")

    def close(self) -> None:
        self.catalog.close()
