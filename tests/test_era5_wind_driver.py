"""ERA5WindDriver tests.

Builds an in-memory synthetic xarray Dataset matching the ARCO-ERA5
schema and patches `_open_zarr` to return it. No network, no gcsfs.
Validates time slicing, spatial interp onto cube grid, wind-speed and
meteorological-direction math, caching, and end-to-end satisfaction
of the WRF-SFIRE adapter's declared optional wind needs.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest
import xarray as xr

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from drivers.era5_wind import (
    ERA5WindDriver,
    U10_VAR_DEFAULT,
    V10_VAR_DEFAULT,
    _wind_speed_dir,
)


# ============================================================ fixtures
def _real_cube(tmp_path: Path, radius_m: float = 2_500.0):
    from cube.grid import SimulationGrid
    from cube.store import Cube
    grid = SimulationGrid.from_center_radius(
        -96.797, 32.776, radius_m, 500.0)
    return Cube(tmp_path, grid)


def _fake_arco_dataset(
        lat_grid_step: float = 0.25,
        u_value: float = 5.0,
        v_value: float = 0.0,
        hours: int = 6,
        t0: datetime = datetime(2026, 9, 15)) -> xr.Dataset:
    """Synthetic ARCO-ERA5-shaped Dataset over a small window around
    Dallas (32.776 N, -96.797 E -> 263.203 in 0..360)."""
    lat = np.arange(32.0, 34.0 + 1e-9, lat_grid_step)[::-1]      # 90..-90 dir
    lon = np.arange(262.0, 265.0 + 1e-9, lat_grid_step)
    time = np.array([np.datetime64(t0 + timedelta(hours=i), "ns")
                     for i in range(hours)])
    shape = (len(time), len(lat), len(lon))
    u = np.full(shape, u_value, dtype="float32")
    v = np.full(shape, v_value, dtype="float32")
    return xr.Dataset(
        {
            U10_VAR_DEFAULT: (("time", "latitude", "longitude"), u),
            V10_VAR_DEFAULT: (("time", "latitude", "longitude"), v),
        },
        coords={"time": time, "latitude": lat, "longitude": lon},
    )


@pytest.fixture
def driver(tmp_path):
    return ERA5WindDriver(cache_dir=tmp_path / "era5_cache")


# ============================================================ math
def test_wind_speed_dir_north_wind():
    """North wind: u=0, v=-1 -> dir=0 (wind from N)."""
    s, d = _wind_speed_dir(np.array([0.0]), np.array([-1.0]))
    assert s.item() == pytest.approx(1.0)
    assert d.item() == pytest.approx(0.0)


def test_wind_speed_dir_east_wind():
    """East wind (blowing from E to W): u=-1, v=0 -> dir=90."""
    s, d = _wind_speed_dir(np.array([-1.0]), np.array([0.0]))
    assert s.item() == pytest.approx(1.0)
    assert d.item() == pytest.approx(90.0)


def test_wind_speed_dir_west_wind():
    """West wind (blowing from W to E): u=+1, v=0 -> dir=270."""
    s, d = _wind_speed_dir(np.array([1.0]), np.array([0.0]))
    assert d.item() == pytest.approx(270.0)


def test_wind_speed_dir_south_wind():
    """South wind: u=0, v=+1 -> dir=180."""
    s, d = _wind_speed_dir(np.array([0.0]), np.array([1.0]))
    assert d.item() == pytest.approx(180.0)


# ============================================================ fetch flow
def test_fetch_writes_speed_and_direction(driver, tmp_path):
    """Driver opens (mocked) ARCO, slices the time window, interpolates,
    and writes wind_speed_ms + wind_dir_deg to the cube."""
    cube = _real_cube(tmp_path / "cube")
    try:
        fake = _fake_arco_dataset(u_value=3.0, v_value=4.0, hours=4)
        t0 = datetime(2026, 9, 15)
        with patch.object(ERA5WindDriver, "_open_zarr", return_value=fake):
            driver.fetch(cube, t_start=t0,
                          t_end=t0 + timedelta(hours=3))
        catalog_names = {v["name"] for v in cube.list_variables()}
        assert {"wind_speed_ms", "wind_dir_deg"} <= catalog_names

        ts, speed = cube.read_3d("wind_speed_ms")
        _, direction = cube.read_3d("wind_dir_deg")
        H, W = cube.grid.shape
        # 4 hours = 4 time slices
        assert speed.shape == (4, H, W)
        # |u=3, v=4| -> speed = 5 everywhere
        assert np.allclose(speed, 5.0, atol=1e-3)
        # u=3 v=4 -> dir = (270 - atan2(4, 3)*180/pi) % 360
        # = (270 - 53.13) % 360 ~ 216.87
        assert np.allclose(direction, 216.87, atol=0.5)
    finally:
        cube.close()


def test_time_window_carries_through(driver, tmp_path):
    """A 24-hour window should give exactly 24 hourly time slices."""
    cube = _real_cube(tmp_path / "cube")
    try:
        fake = _fake_arco_dataset(u_value=1.0, v_value=0.0, hours=48)
        t0 = datetime(2026, 9, 15)
        with patch.object(ERA5WindDriver, "_open_zarr", return_value=fake):
            driver.fetch(cube, t_start=t0,
                          t_end=t0 + timedelta(hours=23))
        ts, _ = cube.read_3d("wind_speed_ms")
        assert len(ts) == 24
    finally:
        cube.close()


def test_cache_hit_skips_open_zarr(driver, tmp_path):
    """Second fetch with identical (cube, t_start, t_end) MUST use the
    npz cache and NOT call _open_zarr."""
    cube = _real_cube(tmp_path / "cube")
    try:
        fake = _fake_arco_dataset(hours=3)
        t0 = datetime(2026, 9, 15)
        with patch.object(ERA5WindDriver, "_open_zarr",
                           return_value=fake) as m:
            driver.fetch(cube, t_start=t0,
                          t_end=t0 + timedelta(hours=2))
            n_first = m.call_count
            driver.fetch(cube, t_start=t0,
                          t_end=t0 + timedelta(hours=2))
            n_second = m.call_count
        assert n_first == 1
        assert n_second == 1, (
            f"second fetch should hit cache; _open_zarr called "
            f"{n_second} times total")
    finally:
        cube.close()


def test_retries_on_open_failure(tmp_path):
    """Two failures then success — driver retries and writes the cube."""
    driver = ERA5WindDriver(cache_dir=tmp_path / "cache", retries=3)
    cube = _real_cube(tmp_path / "cube")
    try:
        fake = _fake_arco_dataset(hours=3)
        call_seq = [Exception("transient"), Exception("transient"), fake]

        def _open():
            r = call_seq.pop(0)
            if isinstance(r, Exception):
                raise r
            return r

        t0 = datetime(2026, 9, 15)
        with patch("drivers.era5_wind._time.sleep"), \
             patch.object(ERA5WindDriver, "_open_zarr", side_effect=_open):
            driver.fetch(cube, t_start=t0,
                          t_end=t0 + timedelta(hours=2))
        catalog = {v["name"] for v in cube.list_variables()}
        assert "wind_speed_ms" in catalog
    finally:
        cube.close()


def test_missing_t_start_or_end_raises(driver, tmp_path):
    cube = _real_cube(tmp_path / "cube")
    try:
        with pytest.raises(ValueError, match="t_start and t_end"):
            driver.fetch(cube)
    finally:
        cube.close()


# ============================================================ end-to-end
def test_driver_satisfies_wrf_sfire_adapter_wind_needs(driver, tmp_path):
    """After the ERA5 driver runs, both optional wind needs are satisfied —
    DataAdapter.search reports them present + resolution_ok=True (ARCO-ERA5's
    ~28 km native resolution is broader than any constraint the adapter
    sets today, which is None)."""
    from models.wrf_sfire_adapter import WRFSFireAdapter
    cube = _real_cube(tmp_path / "cube")
    try:
        fake = _fake_arco_dataset(hours=3)
        t0 = datetime(2026, 9, 15)
        with patch.object(ERA5WindDriver, "_open_zarr", return_value=fake):
            driver.fetch(cube, t_start=t0,
                          t_end=t0 + timedelta(hours=2))
        adapter = WRFSFireAdapter(sfire_dir="wrf-sfire/test/em_fire/hill")
        report = adapter.data_adapter.search(cube)
        assert report["wind_speed_ms"]["present"]
        assert report["wind_dir_deg"]["present"]
    finally:
        cube.close()
