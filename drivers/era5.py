"""ERA5 historical weather adapter.

This adapter reads a local ERA5 NetCDF/Zarr dataset and writes historical
weather stacks to the cube. The future weather predictor is intentionally a
separate model, so this driver remains a source adapter only.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import xarray as xr
from pyproj import Transformer
from scipy.interpolate import RegularGridInterpolator

from cube.store import Cube
from drivers.base import Driver


class ERA5HistoryDriver(Driver):
    name = "era5_history"
    produces = [
        "era5_temp_c_hist",
        "era5_rh_hist",
        "era5_wind_speed_ms_hist",
        "era5_wind_dir_deg_hist",
        "era5_precip_mm_hist",
    ]
    is_static = False

    def __init__(self, source_path: str | Path | None = None,
                 years_back: int = 20,
                 day_window: int = 21):
        self.source_path = Path(source_path) if source_path else None
        self.years_back = years_back
        self.day_window = day_window

    def _open(self) -> xr.Dataset:
        if self.source_path is None:
            raise RuntimeError(
                "ERA5 source path is required. Provide a local NetCDF/Zarr "
                "with time/lat/lon variables, or use --weather synthetic/cmip6.")
        if not self.source_path.exists():
            raise FileNotFoundError(self.source_path)
        if self.source_path.suffix == ".zarr" or self.source_path.is_dir():
            return xr.open_zarr(self.source_path)
        return xr.open_dataset(self.source_path)

    @staticmethod
    def _var(ds: xr.Dataset, *names: str) -> xr.DataArray | None:
        for name in names:
            if name in ds:
                return ds[name]
        return None

    @staticmethod
    def _rh_from_dewpoint(t_c: xr.DataArray, td_c: xr.DataArray) -> xr.DataArray:
        es_t = 6.112 * np.exp(17.62 * t_c / (243.12 + t_c))
        es_d = 6.112 * np.exp(17.62 * td_c / (243.12 + td_c))
        return (100.0 * es_d / es_t).clip(0, 100)

    def _standardize(self, ds: xr.Dataset) -> xr.Dataset:
        t = self._var(ds, "temp_c", "t2m", "temperature")
        rh = self._var(ds, "rh", "r", "relative_humidity")
        d2m = self._var(ds, "d2m", "dewpoint")
        u = self._var(ds, "u10", "u")
        v = self._var(ds, "v10", "v")
        ws = self._var(ds, "wind_speed_ms", "si10", "wind_speed")
        wd = self._var(ds, "wind_dir_deg", "wind_direction")
        pr = self._var(ds, "precip_mm", "tp", "precip", "precipitation")

        if t is None:
            raise KeyError("ERA5 dataset needs temp_c or t2m")
        t_c = t - 273.15 if float(t.mean()) > 100 else t

        if rh is None:
            if d2m is None:
                raise KeyError("ERA5 dataset needs rh/r or d2m dewpoint")
            td_c = d2m - 273.15 if float(d2m.mean()) > 100 else d2m
            rh = self._rh_from_dewpoint(t_c, td_c)

        if ws is None or wd is None:
            if u is None or v is None:
                raise KeyError("ERA5 dataset needs wind_speed/wind_dir or u10/v10")
            ws = np.hypot(u, v)
            wd = (270.0 - np.rad2deg(np.arctan2(v, u))) % 360.0

        if pr is None:
            pr = xr.zeros_like(t_c)
        pr_mm = pr * 1000.0 if float(pr.max()) < 10.0 else pr

        return xr.Dataset({
            "era5_temp_c_hist": t_c.astype("float32"),
            "era5_rh_hist": rh.clip(0, 100).astype("float32"),
            "era5_wind_speed_ms_hist": ws.astype("float32"),
            "era5_wind_dir_deg_hist": wd.astype("float32"),
            "era5_precip_mm_hist": pr_mm.astype("float32"),
        })

    def _select_history(self, ds: xr.Dataset,
                        t_start: datetime, t_end: datetime) -> xr.Dataset:
        times = pd.to_datetime(ds.time.values)
        if len(times) == 0:
            raise RuntimeError("ERA5 dataset has no time values")
        target_doys = set(range(t_start.timetuple().tm_yday,
                                t_end.timetuple().tm_yday + 1))
        latest_year = min(t_start.year - 1, int(times.year.max()))
        earliest_year = latest_year - self.years_back + 1
        years = (times.year >= earliest_year) & (times.year <= latest_year)
        doy = times.dayofyear
        season = np.zeros(len(times), dtype=bool)
        for d in target_doys:
            dist = np.minimum(np.abs(doy - d), 366 - np.abs(doy - d))
            season |= dist <= self.day_window
        selected = years & season
        if not selected.any():
            raise RuntimeError("ERA5 dataset has no records in requested season")
        return ds.isel(time=np.where(selected)[0])

    def _interp_to_grid(self, da: xr.DataArray, grid) -> np.ndarray:
        xs, ys = grid.cell_centers_xy()
        XX, YY = np.meshgrid(xs, ys)
        inv = Transformer.from_crs(grid.crs, 4326, always_xy=True).transform
        LON, LAT = inv(XX.ravel(), YY.ravel())
        lon_q = (np.asarray(LON) + 360.0) % 360.0
        lat_q = np.asarray(LAT)

        lat_name = "lat" if "lat" in da.coords else "latitude"
        lon_name = "lon" if "lon" in da.coords else "longitude"
        lat = da[lat_name].values
        lon = da[lon_name].values
        lon = lon % 360.0
        data = da.values
        if lat[0] > lat[-1]:
            lat = lat[::-1]
            data = data[:, ::-1, :]
        order = np.argsort(lon)
        lon = lon[order]
        data = data[:, :, order]

        pts = np.stack([lat_q, lon_q], axis=-1)
        out = np.empty((data.shape[0], grid.height, grid.width), dtype=np.float32)
        for k in range(data.shape[0]):
            f = RegularGridInterpolator((lat, lon), data[k],
                                        bounds_error=False, fill_value=np.nan)
            out[k] = f(pts).reshape(grid.height, grid.width).astype(np.float32)
        return out

    def fetch(self, cube: Cube,
              t_start: Optional[datetime] = None,
              t_end: Optional[datetime] = None) -> list[str]:
        if t_start is None or t_end is None:
            raise ValueError("ERA5HistoryDriver requires t_start and t_end")
        ds = self._select_history(self._standardize(self._open()), t_start, t_end)
        ts = [pd.Timestamp(t).to_pydatetime().replace(tzinfo=None)
              for t in ds.time.values]
        src = f"ERA5 local history: {self.source_path}"
        for variable in self.produces:
            arr = self._interp_to_grid(ds[variable], cube.grid)
            cube.write_3d(variable, ts, arr, source=src, native_res_m=31_000.0,
                          units=self._units(variable), producer=self.name,
                          description=f"Historical ERA5 {variable}")
        return list(self.produces)

    @staticmethod
    def _units(variable: str) -> str:
        if "temp" in variable:
            return "C"
        if "rh" in variable:
            return "%"
        if "wind_speed" in variable:
            return "m/s"
        if "wind_dir" in variable:
            return "deg"
        return "mm"
