"""ARCO-ERA5 historical-weather adapter.

Reads the analysis-ready cloud-optimised ERA5 archive on Google Cloud Storage
(anonymous public access, no credentials needed) and writes per-pixel daily
historical stacks to the cube. The downstream `climate_regression` model fits
a per-cell multivariate temporal regression on these stacks and predicts the
scenario-date hourly weather, with RH derived from the Magnus equation.

ARCO-ERA5
---------
- Bucket : gs://gcp-public-data-arco-era5
- Path   : ar/full_37-1h-0p25deg-chunk-1.zarr-v3   (hourly, 0.25 deg)
- Years  : 1940-present
- Cite   : Carver, R. W., & Merose, A. (2023). ARCO-ERA5: An analysis-ready
           cloud-optimized reanalysis dataset. AMS Annual Meeting, 2023.

For each historical year inside the look-back window we slice ±day_window days
around the same month-day as the scenario date, aggregate to daily statistics
(max/min/mean/sum), interpolate to the simulation grid, and store as
time-varying cube variables.

Outputs
-------
    era5_t2m_max_hist  [C]   daily max 2-m temperature
    era5_t2m_min_hist  [C]   daily min 2-m temperature
    era5_d2m_mean_hist [C]   daily mean 2-m dewpoint
    era5_u10_mean_hist [m/s] daily mean 10-m u wind
    era5_v10_mean_hist [m/s] daily mean 10-m v wind
    era5_pr_total_hist [mm]  daily total precipitation
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import gcsfs
import numpy as np
import pandas as pd
import xarray as xr
from pyproj import Transformer
from scipy.interpolate import RegularGridInterpolator

from cube.store import Cube
from drivers.base import Driver


ARCO_ERA5_ZARR = (
    "gcp-public-data-arco-era5/ar/full_37-1h-0p25deg-chunk-1.zarr-v3")

# ARCO-ERA5 variable names (ECMWF long-form keys)
_VARNAMES = {
    "t2m":  "2m_temperature",
    "d2m":  "2m_dewpoint_temperature",
    "u10":  "10m_u_component_of_wind",
    "v10":  "10m_v_component_of_wind",
    "tp":   "total_precipitation",
}


class ARCOERA5HistoryDriver(Driver):
    name = "era5_history"
    produces = [
        "era5_t2m_max_hist",
        "era5_t2m_min_hist",
        "era5_d2m_mean_hist",
        "era5_u10_mean_hist",
        "era5_v10_mean_hist",
        "era5_pr_total_hist",
    ]
    is_static = False

    def __init__(self, *,
                 target_date: datetime,
                 years_back: int = 12,
                 day_window: int = 21,
                 latest_year: Optional[int] = None,
                 cache_dir: str | Path = "data/raw/era5"):
        self.target_date = target_date
        self.years_back = years_back
        self.day_window = day_window
        self.latest_year = latest_year
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    # -------------------------------------------------------------- helpers
    def _open_arco(self) -> xr.Dataset:
        fs = gcsfs.GCSFileSystem(token="anon")
        mapper = fs.get_mapper(ARCO_ERA5_ZARR)
        # consolidated metadata is on for the public ARCO bucket
        return xr.open_zarr(mapper, consolidated=True,
                            chunks={"time": 24})

    def _history_years(self) -> list[int]:
        latest = self.latest_year if self.latest_year is not None \
            else min(datetime.now(timezone.utc).year - 1, self.target_date.year - 1)
        latest = max(latest, 1980)
        first = max(1980, latest - self.years_back + 1)
        return list(range(first, latest + 1))

    def _date_in_year(self, year: int) -> datetime:
        day = self.target_date.day
        if self.target_date.month == 2 and day == 29:
            day = 28
        return datetime(year, self.target_date.month, day)

    def _yearly_windows(self, ds: xr.Dataset) -> list[xr.Dataset]:
        """List of ±day_window-around-target slices, one per historical year.

        We deliberately keep these as separate Datasets (rather than concat)
        so that downstream daily aggregation can be applied to each window
        in turn; a single resample('1D') on a concat would synthesize NaN
        rows for every missing inter-window day.
        """
        windows = []
        for year in self._history_years():
            center = self._date_in_year(year)
            t0 = center - timedelta(days=self.day_window)
            t1 = center + timedelta(days=self.day_window)
            sub = ds.sel(time=slice(np.datetime64(t0), np.datetime64(t1)))
            if sub.sizes.get("time", 0) > 0:
                windows.append(sub)
        if not windows:
            raise RuntimeError("ARCO-ERA5: no time slices found for window")
        return windows

    @staticmethod
    def _slice_bbox(ds: xr.Dataset, bbox_lonlat: tuple[float, float, float, float]
                    ) -> xr.Dataset:
        w, s, e, n = bbox_lonlat
        # ARCO-ERA5 longitudes are 0..360, latitudes 90..-90 (descending)
        lon_max = float(ds.longitude.max())
        if lon_max > 180:
            w = (w + 360.0) % 360.0
            e = (e + 360.0) % 360.0
        # pad ~1 cell (0.25 deg)
        return ds.sel(longitude=slice(w - 0.5, e + 0.5),
                      latitude=slice(n + 0.5, s - 0.5))

    @staticmethod
    def _interp_to_grid(da: xr.DataArray, grid) -> np.ndarray:
        xs, ys = grid.cell_centers_xy()
        XX, YY = np.meshgrid(xs, ys)
        inv = Transformer.from_crs(grid.crs, 4326, always_xy=True).transform
        LON, LAT = inv(XX.ravel(), YY.ravel())
        lat_q = np.asarray(LAT).reshape(XX.shape)
        lon_q = np.asarray(LON).reshape(XX.shape)
        # match the dataset's longitude convention
        if float(da.longitude.max()) > 180:
            lon_q = (lon_q + 360.0) % 360.0

        lat_v = da.latitude.values
        lon_v = da.longitude.values
        if lat_v[0] > lat_v[-1]:
            lat_v = lat_v[::-1]
            arr = da.values[..., ::-1, :]
        else:
            arr = da.values
        order = np.argsort(lon_v)
        lon_v = lon_v[order]
        arr = arr[..., :, order]

        T = arr.shape[0] if arr.ndim == 3 else 1
        if arr.ndim == 2:
            arr = arr[None]
        out = np.empty((T, grid.height, grid.width), dtype=np.float32)
        pts = np.stack([lat_q.ravel(), lon_q.ravel()], axis=-1)
        for k in range(T):
            f = RegularGridInterpolator(
                (lat_v, lon_v), arr[k],
                bounds_error=False, fill_value=np.nan)
            out[k] = f(pts).reshape(grid.height, grid.width).astype(np.float32)
        return out

    # ----------------------------------------------------------------- run
    def fetch(self, cube: Cube,
              t_start: Optional[datetime] = None,
              t_end: Optional[datetime] = None) -> list[str]:
        print("      opening ARCO-ERA5 (Google Cloud, anonymous read)")
        ds = self._open_arco()

        # only keep variables we need (cheap; no I/O yet)
        keep = [v for v in _VARNAMES.values() if v in ds]
        missing = set(_VARNAMES.values()) - set(keep)
        if missing:
            raise RuntimeError(
                f"ARCO-ERA5 missing variables {missing}; bucket may have moved")
        ds = ds[keep]

        # spatial slice (lazy), then per-year window slicing
        ds = self._slice_bbox(ds, cube.grid.lonlat_bbox())
        windows = self._yearly_windows(ds)
        total_h = sum(w.sizes["time"] for w in windows)
        print(f"      sliced to {ds.sizes.get('latitude','?')} x "
              f"{ds.sizes.get('longitude','?')} cells, "
              f"{total_h} hourly samples across {len(windows)} years")

        # daily aggregation: do it per yearly window, then concat. This avoids
        # the resample('1D') NaN-gap pathology when the time index is
        # discontinuous.
        per_window: list[xr.Dataset] = []
        for w in windows:
            t2m = w[_VARNAMES["t2m"]]
            d2m = w[_VARNAMES["d2m"]]
            u10 = w[_VARNAMES["u10"]]
            v10 = w[_VARNAMES["v10"]]
            tp  = w[_VARNAMES["tp"]]
            per_window.append(xr.Dataset({
                "t2m_max":  t2m.resample(time="1D").max() - 273.15,
                "t2m_min":  t2m.resample(time="1D").min() - 273.15,
                "d2m_mean": d2m.resample(time="1D").mean() - 273.15,
                "u10_mean": u10.resample(time="1D").mean(),
                "v10_mean": v10.resample(time="1D").mean(),
                "pr_total": (tp * 1000.0).resample(time="1D").sum(),  # m -> mm
            }))
        daily = xr.concat(per_window, dim="time").compute()
        print(f"      computed daily aggregations: {daily.sizes['time']} days")
        print(f"      interpolating to {cube.grid.shape} sim grid")

        ts = [pd.Timestamp(t).to_pydatetime().replace(tzinfo=None)
              for t in daily.time.values]

        out_var_map = {
            "era5_t2m_max_hist":  ("t2m_max",  "C"),
            "era5_t2m_min_hist":  ("t2m_min",  "C"),
            "era5_d2m_mean_hist": ("d2m_mean", "C"),
            "era5_u10_mean_hist": ("u10_mean", "m/s"),
            "era5_v10_mean_hist": ("v10_mean", "m/s"),
            "era5_pr_total_hist": ("pr_total", "mm"),
        }

        src = (f"ARCO-ERA5 ({ARCO_ERA5_ZARR}); "
               f"target {self.target_date.date()}; "
               f"years {self._history_years()[0]}-{self._history_years()[-1]}; "
               f"+/- {self.day_window} d")
        for out_var, (key, units) in out_var_map.items():
            arr = self._interp_to_grid(daily[key], cube.grid)
            cube.write_3d(out_var, ts, arr,
                          source=src, native_res_m=25_000.0,
                          units=units, producer=self.name,
                          description=f"Historical ARCO-ERA5 daily {key}")
        return list(self.produces)
