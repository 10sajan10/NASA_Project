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

    # --------------------------------------------------------------------
    # tile-streamed interpolation. ERA5 source is small (~7x7 cells); the
    # blow-up happens when we materialise (T_days, H_grid, W_grid) at full
    # grid scale. Instead we build a spatial interpolator per timestep and
    # call it for one simulation tile at a time, so peak memory scales with
    # the tile size, not the full grid.
    @staticmethod
    def _build_query_lonlat(grid, y_sl, x_sl
                            ) -> tuple[np.ndarray, np.ndarray]:
        xs, ys = grid.cell_centers_xy()
        xs_t = xs[x_sl]; ys_t = ys[y_sl]
        XX, YY = np.meshgrid(xs_t, ys_t)
        inv = Transformer.from_crs(grid.crs, 4326, always_xy=True).transform
        LON, LAT = inv(XX.ravel(), YY.ravel())
        return (np.asarray(LAT).reshape(XX.shape),
                np.asarray(LON).reshape(XX.shape))

    @staticmethod
    def _interp_tile(daily_da: xr.DataArray, grid, y_sl, x_sl
                     ) -> np.ndarray:
        """Return (T_days, h_tile, w_tile) for one variable at one tile."""
        lat_q, lon_q = ARCOERA5HistoryDriver._build_query_lonlat(
            grid, y_sl, x_sl)
        if float(daily_da.longitude.max()) > 180:
            lon_q = (lon_q + 360.0) % 360.0

        lat_v = daily_da.latitude.values
        lon_v = daily_da.longitude.values
        if lat_v[0] > lat_v[-1]:
            lat_v = lat_v[::-1]
            arr = daily_da.values[..., ::-1, :]
        else:
            arr = daily_da.values
        order = np.argsort(lon_v)
        lon_v = lon_v[order]
        arr = arr[..., :, order]

        T = arr.shape[0]
        h = y_sl.stop - y_sl.start; w = x_sl.stop - x_sl.start
        out = np.empty((T, h, w), dtype=np.float32)
        pts = np.stack([lat_q.ravel(), lon_q.ravel()], axis=-1)
        for k in range(T):
            f = RegularGridInterpolator(
                (lat_v, lon_v), arr[k],
                bounds_error=False, fill_value=np.nan)
            out[k] = f(pts).reshape(h, w).astype(np.float32)
        return out

    # ----------------------------------------------------------------- run
    def fetch(self, cube: Cube,
              t_start: Optional[datetime] = None,
              t_end: Optional[datetime] = None) -> list[str]:
        print("      opening ARCO-ERA5 (Google Cloud, anonymous read)")
        ds = self._open_arco()
        keep = [v for v in _VARNAMES.values() if v in ds]
        missing = set(_VARNAMES.values()) - set(keep)
        if missing:
            raise RuntimeError(
                f"ARCO-ERA5 missing variables {missing}; bucket may have moved")
        ds = ds[keep]

        ds = self._slice_bbox(ds, cube.grid.lonlat_bbox())
        windows = self._yearly_windows(ds)
        total_h = sum(w.sizes["time"] for w in windows)
        print(f"      sliced to {ds.sizes.get('latitude','?')} x "
              f"{ds.sizes.get('longitude','?')} cells, "
              f"{total_h} hourly samples across {len(windows)} years")

        # daily aggregation per yearly window (small in source resolution)
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

        ts = [pd.Timestamp(t).to_pydatetime().replace(tzinfo=None)
              for t in daily.time.values]
        n_days = len(ts)

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
               f"+/- {self.day_window} d; tile-streamed")

        # pre-allocate chunked Zarr stores so the (n_days, H, W) array per
        # variable never lives in RAM
        tile = 128
        for out_var, (key, units) in out_var_map.items():
            cube.init_time_tiled(
                out_var, ts=ts, dtype="float32",
                source=src, native_res_m=25_000.0, units=units,
                producer=self.name,
                description=f"Historical ARCO-ERA5 daily {key}",
                chunk=(min(64, n_days), tile, tile))

        n_tiles = sum(1 for _ in cube.iter_spatial_tiles(tile=tile))
        print(f"      streaming interpolation to {cube.grid.shape} grid "
              f"in {n_tiles} {tile}x{tile} tiles")
        tile_no = 0
        for y_sl, x_sl in cube.iter_spatial_tiles(tile=tile):
            tile_no += 1
            if tile_no % max(1, n_tiles // 10) == 0 or tile_no == 1:
                print(f"      tile {tile_no}/{n_tiles}")
            for out_var, (key, _units) in out_var_map.items():
                tile_arr = self._interp_tile(daily[key], cube.grid, y_sl, x_sl)
                cube.write_chunk_time(out_var, slice(0, n_days), y_sl, x_sl,
                                       tile_arr)
                del tile_arr
        return list(self.produces)
