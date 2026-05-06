"""CMIP6Driver: future climate projections from NASA NEX-GDDP-CMIP6.

NEX-GDDP-CMIP6 is a bias-corrected, statistically-downscaled CMIP6 product
on a 0.25-degree daily grid covering the global land surface from 1950-2100.
Hosted as a Zarr/NetCDF mirror on AWS S3 (NOAA Big Data Program / NCCS).

For a 30-day fire scenario this gives us a daily climate trajectory hundreds
of years past the HRRR forecast horizon. We diurnalise the daily values
into the hourly cadence the fire model expects:
  - tasmax / tasmin -> sinusoidal diurnal temperature curve
  - hurs (daily mean RH) -> inverse-temp diurnal RH (held = daily mean)
  - sfcWind (daily mean) -> held constant, direction sampled from a
    seasonal climatology (we don't have uas/vas in NEX-GDDP)
  - pr (daily total mm) -> distributed over a single 6-hour window/day
                            for KBDI integration

Variables produced (hourly):
  temp_c, rh, wind_speed_ms, wind_dir_deg, precip_mm
"""
from __future__ import annotations
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import fsspec
import numpy as np
import xarray as xr
from pyproj import Transformer
from scipy.interpolate import RegularGridInterpolator

from cube.store import Cube
from drivers.base import Driver


# NEX-GDDP-CMIP6 base URL on AWS Open Data
# https://registry.opendata.aws/nex-gddp-cmip6/
S3_BASE = "https://nex-gddp-cmip6.s3.us-west-2.amazonaws.com/NEX-GDDP-CMIP6"


class CMIP6Driver(Driver):
    """Default model = ACCESS-CM2 (one of the well-validated members);
    SSP370 is the central plausible-but-pessimistic scenario."""

    name = "cmip6"
    produces = ["temp_c", "rh", "wind_speed_ms", "wind_dir_deg", "precip_mm"]

    def __init__(self,
                 model: str = "ACCESS-CM2",
                 scenario: str = "ssp370",
                 ensemble: str = "r1i1p1f1",
                 wind_dir_deg: float = 180.0,
                 cache_dir: str | Path = "data/raw/cmip6"):
        self.model = model
        self.scenario = scenario
        self.ensemble = ensemble
        self.wind_dir_deg = wind_dir_deg  # constant for now; downstream WindModel can diversify
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # NEX-GDDP file URL convention:
    #   {S3_BASE}/{scenario}/{ensemble}/{var}/{var}_day_{model}_{scenario}_{ensemble}_gn_{year}.nc
    # ------------------------------------------------------------------
    def _file_url(self, var: str, year: int) -> str:
        # historical for years <= 2014, scenario for years >= 2015
        scen = "historical" if year <= 2014 else self.scenario
        run = "v1.1" if scen == "historical" else "v1.1"
        return (f"{S3_BASE}/{self.model}/{scen}/{self.ensemble}/{var}/"
                f"{var}_day_{self.model}_{scen}_{self.ensemble}_gn_{year}.nc")

    def _open_var_year(self, var: str, year: int) -> xr.DataArray:
        """Open one variable for one year, returning a DataArray with dims
        (time, lat, lon) cropped to the simulation grid bbox + small padding."""
        url = self._file_url(var, year)
        # cache locally to avoid repeated downloads
        local = self.cache_dir / f"{var}_{self.model}_{self.scenario}_{year}.nc"
        if not local.exists():
            with fsspec.open(url, "rb", anon=True) as r, open(local, "wb") as w:
                w.write(r.read())
        return xr.open_dataset(local)[var]

    def _interp_to_grid(self, da: xr.DataArray, grid) -> np.ndarray:
        """Bilinear interpolation of a (time, lat, lon) DA to the cube grid,
        returning a (T, H, W) float32 array."""
        # grid centres in lon/lat
        xs, ys = grid.cell_centers_xy()
        XX, YY = np.meshgrid(xs, ys)
        inv = Transformer.from_crs(grid.crs, 4326, always_xy=True).transform
        LON, LAT = inv(XX.ravel(), YY.ravel())
        LON = np.asarray(LON).reshape(XX.shape)
        LAT = np.asarray(LAT).reshape(XX.shape)

        lat = da.lat.values
        lon = da.lon.values % 360
        # NEX-GDDP uses 0..360 longitude
        LON_q = (LON + 360) % 360

        # build interpolators per timestep
        T = da.shape[0]
        out = np.empty((T, grid.height, grid.width), dtype=np.float32)
        # ensure lat is ascending for RegularGridInterpolator
        if lat[0] > lat[-1]:
            lat = lat[::-1]
            data_all = da.values[:, ::-1, :]
        else:
            data_all = da.values
        for k in range(T):
            f = RegularGridInterpolator(
                (lat, lon), data_all[k], method="linear",
                bounds_error=False, fill_value=np.nan)
            pts = np.stack([LAT.ravel(), LON_q.ravel()], axis=-1)
            out[k] = f(pts).reshape(grid.height, grid.width).astype(np.float32)
        return out

    # ------------------------------------------------------------------
    # diurnalisation helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _diurnal_temp(tmax_c: np.ndarray, tmin_c: np.ndarray,
                      hour: int) -> np.ndarray:
        # cosine model: max at 14:00 local, min at 02:00 local; we treat
        # `hour` as local time of the grid centroid (close enough for one city).
        phase = np.cos(2 * np.pi * (hour - 14) / 24)
        amp = (tmax_c - tmin_c) / 2.0
        mid = (tmax_c + tmin_c) / 2.0
        return mid + amp * phase

    @staticmethod
    def _diurnal_rh(rh_mean: np.ndarray, t_c: np.ndarray,
                    t_mean_c: np.ndarray) -> np.ndarray:
        # RH varies inversely with T relative to the daily mean, scaled by
        # saturation-vapour-pressure ratio. Cap to [3, 100].
        def es(t):  # Magnus, hPa
            return 6.112 * np.exp(17.62 * t / (243.12 + t))
        rh = rh_mean * es(t_mean_c) / np.maximum(es(t_c), 0.01)
        return np.clip(rh, 3.0, 100.0)

    @staticmethod
    def _diurnal_precip(pr_day_mm: np.ndarray, hour: int) -> np.ndarray:
        # dump entire daily precip into hours 13..18 (afternoon convection
        # in summer for Texas-like climates). KBDI is daily anyway.
        if 13 <= hour < 19:
            return pr_day_mm / 6.0
        return np.zeros_like(pr_day_mm)

    def fetch(self, cube: Cube,
              t_start: Optional[datetime] = None,
              t_end: Optional[datetime] = None) -> list[str]:
        if t_start is None or t_end is None:
            raise ValueError("CMIP6Driver requires t_start and t_end")

        grid = cube.grid
        years = sorted({t_start.year, t_end.year})

        # load daily fields per year
        daily = {}
        for var in ["tasmax", "tasmin", "hurs", "sfcWind", "pr"]:
            arrs = []
            for y in years:
                da = self._open_var_year(var, y)
                arrs.append(da)
            daily[var] = xr.concat(arrs, dim="time")
            daily[var] = daily[var].sel(
                time=slice(t_start.date().isoformat(),
                           t_end.date().isoformat()))

        # crop to bbox to keep the interp problem small
        bbox = grid.lonlat_bbox()
        lon_q = ((np.array([bbox[0], bbox[2]]) + 360) % 360)
        lon_lo, lon_hi = float(min(lon_q)) - 1, float(max(lon_q)) + 1
        for k in list(daily):
            d = daily[k]
            d = d.where(d.lat >= bbox[1] - 1, drop=True)
            d = d.where(d.lat <= bbox[3] + 1, drop=True)
            d = d.where(d.lon >= lon_lo, drop=True)
            d = d.where(d.lon <= lon_hi, drop=True)
            daily[k] = d

        # interpolate to grid
        print("      interpolating CMIP6 -> sim grid")
        tmax_K = self._interp_to_grid(daily["tasmax"], grid)
        tmin_K = self._interp_to_grid(daily["tasmin"], grid)
        rh_pct = self._interp_to_grid(daily["hurs"], grid)
        wind_ms = self._interp_to_grid(daily["sfcWind"], grid)
        pr_kgms = self._interp_to_grid(daily["pr"], grid)

        # convert K->C, kg/m^2/s -> mm/day
        tmax_c = tmax_K - 273.15
        tmin_c = tmin_K - 273.15
        tmean_c = (tmax_c + tmin_c) / 2.0
        pr_mm_day = pr_kgms * 86400.0

        n_days = tmax_c.shape[0]
        days = [t_start + timedelta(days=i) for i in range(n_days)]

        src = (f"NEX-GDDP-CMIP6/{self.model}/{self.scenario}/{self.ensemble}; "
               f"diurnalised hourly")
        nat = 25_000.0  # ~0.25 deg

        # write hourly variables
        n_hours = n_days * 24
        ts: list[datetime] = []
        T = np.empty((n_hours, grid.height, grid.width), dtype=np.float32)
        RH = np.empty_like(T)
        WS = np.empty_like(T)
        WD = np.empty_like(T)
        PR = np.empty_like(T)

        for di in range(n_days):
            for h in range(24):
                idx = di * 24 + h
                ts.append(days[di] + timedelta(hours=h))
                t_h = self._diurnal_temp(tmax_c[di], tmin_c[di], h)
                T[idx] = t_h
                RH[idx] = self._diurnal_rh(rh_pct[di], t_h, tmean_c[di])
                WS[idx] = wind_ms[di]
                WD[idx] = self.wind_dir_deg
                PR[idx] = self._diurnal_precip(pr_mm_day[di], h)

        cube.write_3d("temp_c", ts, T, source=src, native_res_m=nat,
                      units="C", producer=self.name,
                      description="2-m air temperature (CMIP6 diurnalised)")
        cube.write_3d("rh", ts, RH, source=src, native_res_m=nat,
                      units="%", producer=self.name,
                      description="2-m relative humidity")
        cube.write_3d("wind_speed_ms", ts, WS, source=src, native_res_m=nat,
                      units="m/s", producer=self.name,
                      description="10-m wind speed (CMIP6 daily mean)")
        cube.write_3d("wind_dir_deg", ts, WD, source=src, native_res_m=nat,
                      units="deg", producer=self.name,
                      description="wind FROM direction; clockwise from N")
        cube.write_3d("precip_mm", ts, PR, source=src, native_res_m=nat,
                      units="mm", producer=self.name,
                      description="hourly precip from daily total")
        return list(self.produces)
