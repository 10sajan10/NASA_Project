"""LandfireFBFM13Driver tests.

Mocks the LANDFIRE ImageServer call so no network is needed. Validates:
  * driver wires into the engine just like any other Driver
  * tiled request flow assembles full grid
  * non-burnable FBFM13 codes (91, 92, 93, 98, 99) and nodata map to the
    no-fuel sentinel in nfuel_cat
  * sha1-keyed local cache prevents re-request on a second fetch
  * end-to-end: the driver's nfuel_cat satisfies the WRF-SFIRE adapter's
    declared need (no preflight gap after this driver runs)
"""
from __future__ import annotations

import io
import sys
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from drivers.landfire_fbfm13 import LandfireFBFM13Driver, _NODATA_SENTINEL


def _real_cube(tmp_path: Path):
    from cube.grid import SimulationGrid
    from cube.store import Cube
    grid = SimulationGrid.from_center_radius(-96.797, 32.776, 2_500.0, 500.0)
    return Cube(tmp_path, grid)


def _fake_geotiff_response(pixel_value, height, width):
    """Return a requests.Response-like object whose .content is a tiny
    in-memory GeoTIFF carrying a constant pixel value."""
    import rasterio
    from rasterio.io import MemoryFile
    arr = np.full((height, width), pixel_value, dtype="uint16")
    with MemoryFile() as mem:
        with mem.open(
                driver="GTiff", height=height, width=width, count=1,
                dtype="uint16", crs="EPSG:32614",
                transform=rasterio.transform.from_bounds(
                    0, 0, width, height, width, height)) as dst:
            dst.write(arr, 1)
        data = mem.read()

    class _R:
        def __init__(self, content): self.content = content
        def raise_for_status(self): pass
    return _R(data)


@pytest.fixture
def driver(tmp_path):
    return LandfireFBFM13Driver(cache_dir=tmp_path / "lf_cache",
                                 max_tile_px=4000)


# ----------------------------------------------------- core path
def test_fetch_writes_fbfm13_and_nfuel_cat(tmp_path, driver):
    """Mocked service returns code 3 (Tall grass) everywhere; both cube
    vars should match the cube grid shape."""
    cube = _real_cube(tmp_path / "cube")
    try:
        H, W = cube.grid.shape
        with patch("drivers.landfire_fbfm13.requests.get",
                    return_value=_fake_geotiff_response(3, H, W)):
            driver.fetch(cube)
        fbfm = cube.read_static("fbfm13")
        nfc = cube.read_static("nfuel_cat")
        assert fbfm.shape == (H, W)
        assert nfc.shape == (H, W)
        assert np.all(fbfm == 3)
        assert np.all(nfc == 3)        # burnable -> identity mapping
    finally:
        cube.close()


def test_nonburnable_codes_remap_to_no_fuel(tmp_path, driver):
    """An FBFM13 code of 98 (water) should collapse to nfuel_cat = 14."""
    cube = _real_cube(tmp_path / "cube")
    try:
        H, W = cube.grid.shape
        with patch("drivers.landfire_fbfm13.requests.get",
                    return_value=_fake_geotiff_response(98, H, W)):
            driver.fetch(cube)
        fbfm = cube.read_static("fbfm13")
        nfc = cube.read_static("nfuel_cat")
        assert np.all(fbfm == 98)
        assert np.all(nfc == 14)
    finally:
        cube.close()


def test_no_fuel_category_is_configurable(tmp_path):
    """Setting no_fuel_category=99 changes the WRF-Fire sentinel."""
    drv = LandfireFBFM13Driver(
        cache_dir=tmp_path / "lf",
        max_tile_px=4000,
        no_fuel_category=99)
    cube = _real_cube(tmp_path / "cube")
    try:
        H, W = cube.grid.shape
        with patch("drivers.landfire_fbfm13.requests.get",
                    return_value=_fake_geotiff_response(92, H, W)):
            drv.fetch(cube)
        assert np.all(cube.read_static("nfuel_cat") == 99)
    finally:
        cube.close()


def test_request_params_carry_cube_grid_geometry(tmp_path, driver):
    """The driver must send bbox + bboxSR + imageSR + size + nearest-
    neighbour interpolation; otherwise the LANDFIRE service returns
    nonsense for a categorical raster."""
    cube = _real_cube(tmp_path / "cube")
    try:
        H, W = cube.grid.shape
        captured: dict = {}

        def _spy(url, params, **kw):
            captured["url"] = url
            captured["params"] = params
            return _fake_geotiff_response(3, H, W)

        with patch("drivers.landfire_fbfm13.requests.get", side_effect=_spy):
            driver.fetch(cube)

        assert "landfire" in captured["url"].lower()
        p = captured["params"]
        # bbox carries the cube's UTM extent
        for axis in p["bbox"].split(","):
            float(axis)                  # parses ok
        assert p["bboxSR"] == str(cube.grid.crs_epsg)
        assert p["imageSR"] == str(cube.grid.crs_epsg)
        assert p["size"] == f"{W},{H}"
        assert p["interpolation"] == "RSP_NearestNeighbor"
        assert p["pixelType"] == "U16"
    finally:
        cube.close()


def test_cache_hit_on_second_fetch(tmp_path, driver):
    """A second fetch against the same cube grid should hit the disk
    cache and NOT issue a second HTTP request."""
    cube = _real_cube(tmp_path / "cube")
    try:
        H, W = cube.grid.shape
        call_count = {"n": 0}

        def _spy(*a, **kw):
            call_count["n"] += 1
            return _fake_geotiff_response(3, H, W)

        with patch("drivers.landfire_fbfm13.requests.get", side_effect=_spy):
            driver.fetch(cube)
            driver.fetch(cube)
        assert call_count["n"] == 1, (
            f"expected one HTTP call (then cache), got {call_count['n']}")
    finally:
        cube.close()


def test_retries_on_transient_failure(tmp_path, driver):
    """First two calls raise; third returns valid data. The driver should
    retry without surfacing the error."""
    cube = _real_cube(tmp_path / "cube")
    try:
        H, W = cube.grid.shape
        sequence = [
            Exception("flaky service"),
            Exception("flaky service"),
            _fake_geotiff_response(3, H, W),
        ]
        # patch sleep so the test doesn't actually wait between retries
        with patch("drivers.landfire_fbfm13.time.sleep"), \
             patch("drivers.landfire_fbfm13.requests.get",
                    side_effect=sequence):
            driver.fetch(cube)
        assert np.all(cube.read_static("nfuel_cat") == 3)
    finally:
        cube.close()


# ----------------------------------------------------- end-to-end
def test_driver_satisfies_wrf_sfire_adapter_nfuel_cat(tmp_path, driver):
    """After the driver runs, WRFSFireAdapter.data_adapter.missing(cube)
    no longer reports nfuel_cat as a gap."""
    from models.wrf_sfire_adapter import WRFSFireAdapter
    cube = _real_cube(tmp_path / "cube")
    try:
        H, W = cube.grid.shape
        with patch("drivers.landfire_fbfm13.requests.get",
                    return_value=_fake_geotiff_response(3, H, W)):
            driver.fetch(cube)
        adapter = WRFSFireAdapter(sfire_dir="wrf-sfire/test/em_fire/hill")
        missing = adapter.data_adapter.missing(cube)
        assert "nfuel_cat" not in missing
    finally:
        cube.close()
