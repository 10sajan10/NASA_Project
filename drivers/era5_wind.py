"""ERA5WindDriver: 10 m wind from the ARCO-ERA5 public Zarr on GCS.

ARCO-ERA5 (Analysis-Ready Cloud-Optimised ERA5) is the public hourly
analysis from ECMWF, hosted on Google Cloud Storage with anonymous
access — no API key, no quota, no auth setup. We open the Zarr,
slice in time + space, bilinear-interp onto the cube cell centres,
derive speed and direction.

Produces (both hourly time series):

  * wind_speed_ms  m/s, magnitude of the 10 m wind vector
  * wind_dir_deg   meteorological convention — the direction the wind
                    is COMING FROM, measured clockwise from north.
                    (0 = north wind, 90 = east wind, etc.)

Output is bilinear-interpolated from ERA5's 0.25° grid (~28 km at the
equator) onto the cube's much finer grid. For typical fire-scenario
scales (~5–50 km) the wind field is effectively constant over the
domain, but the interpolation step makes the driver correct for
larger AOIs too.

Caching: the (time × cube grid) result is saved as a single npz under
``cache_dir`` keyed by a sha1 of cube geometry + time window. Re-runs
hit the cache before touching the network.
"""
from __future__ import annotations

import hashlib
import time as _time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import numpy as np

from cube.store import Cube
from drivers.base import Driver


ARCO_ERA5_URL = (
    "gs://gcp-public-data-arco-era5/ar/full_37-1h-0p25deg-chunk-1.zarr-v3"
)

# ARCO-ERA5 names. Some mirrors expose the short ECMWF names (u10/v10)
# instead of the long ones; constructor lets the caller override.
U10_VAR_DEFAULT = "10m_u_component_of_wind"
V10_VAR_DEFAULT = "10m_v_component_of_wind"

# Padding around the cube AOI before spatially slicing ARCO-ERA5, in
# degrees. Provides at least one ERA5 grid cell on every side so
# bilinear interpolation has corners to work with.
_SPATIAL_PAD_DEG = 0.5


def _wind_speed_dir(u: np.ndarray, v: np.ndarray
                    ) -> tuple[np.ndarray, np.ndarray]:
    """Speed (m/s) + meteorological direction (degrees the wind is
    COMING FROM, clockwise from north)."""
    speed = np.hypot(u, v).astype("float32")
    direction = (270.0 - np.degrees(np.arctan2(v, u))) % 360.0
    return speed, direction.astype("float32")


class ERA5WindDriver(Driver):
    name = "era5_wind"
    produces = ["wind_speed_ms", "wind_dir_deg"]
    is_static = False

    def __init__(self,
                 *,
                 zarr_url: str = ARCO_ERA5_URL,
                 cache_dir: str | Path = "data/raw/era5_wind",
                 u10_var: str = U10_VAR_DEFAULT,
                 v10_var: str = V10_VAR_DEFAULT,
                 retries: int = 4,
                 anon: bool = True) -> None:
        """
        zarr_url : path to an ARCO-ERA5 (or compatible) Zarr store.
        cache_dir : local directory for npz caches keyed by cube geometry
                     and time window.
        u10_var / v10_var : variable names of the 10 m wind components in
                             the Zarr. Defaults match the public ARCO-ERA5.
        retries : number of attempts to open the Zarr before giving up.
        anon : open the GCS store anonymously (the public ARCO bucket).
        """
        self.zarr_url = zarr_url
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.u10_var = u10_var
        self.v10_var = v10_var
        self.retries = max(1, int(retries))
        self.anon = bool(anon)

    # ---- ARCO open ------------------------------------------------------
    def _open_zarr(self):
        """Open the ARCO-ERA5 Zarr via gcsfs. Raises on failure; caller
        retries with backoff."""
        import gcsfs
        import xarray as xr
        token = "anon" if self.anon else None
        fs = gcsfs.GCSFileSystem(token=token)
        store = fs.get_mapper(self.zarr_url)
        return xr.open_zarr(store, consolidated=True, chunks={"time": 24})

    # ---- caching --------------------------------------------------------
    def _cache_path(self, cube: Cube,
                     t_start: datetime, t_end: datetime) -> Path:
        grid = cube.grid
        key = (
            f"{grid.crs_epsg}|{grid.width}|{grid.height}|"
            f"{grid.pixel_m}|{grid.x0:.3f}|{grid.y1:.3f}|"
            f"{t_start.isoformat()}|{t_end.isoformat()}"
        )
        digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:20]
        return self.cache_dir / f"era5_wind_{digest}.npz"

    # ---- engine entrypoint ---------------------------------------------
    def fetch(self, cube: Cube,
              t_start: Optional[datetime] = None,
              t_end: Optional[datetime] = None) -> list[str]:
        if t_start is None or t_end is None:
            raise ValueError(
                "ERA5WindDriver requires t_start and t_end")

        cache_path = self._cache_path(cube, t_start, t_end)
        if cache_path.exists():
            try:
                payload = np.load(cache_path, allow_pickle=False)
                ts_ns = payload["t_ns"]
                ts = [datetime.fromtimestamp(int(x) / 1e9, tz=timezone.utc)
                      .replace(tzinfo=None) for x in ts_ns]
                self._write_cube(cube, ts,
                                  payload["speed"], payload["direction"])
                return list(self.produces)
            except Exception:
                pass  # corrupt cache; fall through to refetch

        # Open the store (with retries).
        ds = None
        last_exc: Optional[BaseException] = None
        for attempt in range(self.retries):
            try:
                ds = self._open_zarr()
                break
            except Exception as e:
                last_exc = e
                if attempt < self.retries - 1:
                    _time.sleep(2 ** attempt)
        if ds is None:
            raise RuntimeError(
                f"could not open ARCO-ERA5 at {self.zarr_url}: {last_exc}")

        ts, speed, direction = self._fetch_and_interpolate(
            ds, cube, t_start, t_end)

        # Cache for next time.
        ts_ns = np.array([np.datetime64(t).astype("datetime64[ns]")
                          for t in ts]).astype("int64")
        np.savez(cache_path, t_ns=ts_ns, speed=speed, direction=direction)

        self._write_cube(cube, ts, speed, direction)
        return list(self.produces)

    # ---- helpers --------------------------------------------------------
    def _fetch_and_interpolate(self, ds, cube: Cube,
                                t_start: datetime, t_end: datetime
                                ) -> tuple[list[datetime],
                                           np.ndarray, np.ndarray]:
        """The actual ARCO-ERA5 slice + interp + speed/dir math.

        Kept separate from `fetch` so tests can call it with a synthetic
        in-memory Dataset without touching disk or network.
        """
        import xarray as xr
        from pyproj import Transformer

        # ---- time slice ----------------------------------------------
        t0 = t_start.replace(minute=0, second=0, microsecond=0)
        t1 = t_end.replace(minute=0, second=0, microsecond=0)
        if t1 <= t_start:
            t1 = t1 + timedelta(hours=1)
        sub = ds[[self.u10_var, self.v10_var]].sel(time=slice(t0, t1))

        # ---- cube cells -> lat/lon -----------------------------------
        transformer = Transformer.from_crs(
            cube.grid.crs, "EPSG:4326", always_xy=True)
        xs, ys = cube.grid.cell_centers_xy()
        XX, YY = np.meshgrid(xs, ys)
        lons, lats = transformer.transform(XX, YY)

        # ARCO-ERA5 longitudes are 0..360. Cube lons in EPSG:4326 are
        # -180..180. Convert before interp.
        lons_360 = np.where(lons < 0, lons + 360.0, lons)

        # ---- spatial pre-slice for speed -----------------------------
        # Without this, xarray.interp pulls the global grid for every
        # timestep — many GB. Slice a small window first.
        lat_min = float(lats.min()) - _SPATIAL_PAD_DEG
        lat_max = float(lats.max()) + _SPATIAL_PAD_DEG
        lon_min = float(lons_360.min()) - _SPATIAL_PAD_DEG
        lon_max = float(lons_360.max()) + _SPATIAL_PAD_DEG
        # ERA5 latitude axis runs 90..-90 (decreasing) — sortby first
        # so slice() works regardless of source ordering.
        sub = sub.sortby("latitude").sel(
            latitude=slice(lat_min, lat_max),
            longitude=slice(lon_min, lon_max))

        # ---- bilinear interp onto cube grid --------------------------
        lat_da = xr.DataArray(lats, dims=("y", "x"))
        lon_da = xr.DataArray(lons_360, dims=("y", "x"))
        u_interp = sub[self.u10_var].interp(
            latitude=lat_da, longitude=lon_da, method="linear")
        v_interp = sub[self.v10_var].interp(
            latitude=lat_da, longitude=lon_da, method="linear")

        u_arr = np.asarray(u_interp.values, dtype="float32")
        v_arr = np.asarray(v_interp.values, dtype="float32")
        speed, direction = _wind_speed_dir(u_arr, v_arr)

        ts_np = np.asarray(sub.time.values).astype("datetime64[ns]")
        ts = [datetime.fromtimestamp(int(t) / 1e9, tz=timezone.utc)
              .replace(tzinfo=None)
              for t in ts_np.astype("int64")]
        return ts, speed, direction

    def _write_cube(self, cube: Cube, ts: list[datetime],
                    speed: np.ndarray, direction: np.ndarray) -> None:
        src = f"ARCO-ERA5({self.zarr_url})"
        # ARCO-ERA5 0.25 deg ~ 28 km at the equator. Record as the native
        # resolution so cube.satisfies / max_native_res_m sees this
        # source as coarse compared to typical cube grids.
        nat = 0.25 * 111_000.0
        cube.write_3d(
            "wind_speed_ms", ts, speed,
            source=src, native_res_m=nat, units="m/s",
            producer=self.name,
            description="10 m wind speed from ARCO-ERA5 "
                        "(bilinear-interp to cube grid)")
        cube.write_3d(
            "wind_dir_deg", ts, direction,
            source=src, native_res_m=nat, units="deg",
            producer=self.name,
            description="10 m wind direction (meteorological: where the "
                        "wind comes from, 0=N, 90=E, 180=S, 270=W)")
