"""UniformFuelDriver tests."""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from drivers.uniform_fuel import UniformFuelDriver


def _real_cube(tmp_path: Path):
    from cube.grid import SimulationGrid
    from cube.store import Cube
    grid = SimulationGrid.from_center_radius(-96.797, 32.776, 2_500.0, 500.0)
    return Cube(tmp_path, grid)


def test_default_fills_constant_everywhere(tmp_path):
    cube = _real_cube(tmp_path)
    try:
        UniformFuelDriver(category=3).fetch(cube)
        arr = cube.read_static("nfuel_cat")
        assert arr.shape == cube.grid.shape
        assert np.all(arr == 3)
        assert arr.dtype.kind == "i"
    finally:
        cube.close()


def test_no_fuel_mask_stamps_alternate_category(tmp_path):
    cube = _real_cube(tmp_path)
    try:
        H, W = cube.grid.shape
        mask = np.zeros((H, W), dtype=bool)
        mask[0:3, 0:3] = True                # 3x3 corner is no-fuel
        UniformFuelDriver(category=3,
                          no_fuel_category=14,
                          no_fuel_mask=mask).fetch(cube)
        arr = cube.read_static("nfuel_cat")
        assert np.all(arr[0:3, 0:3] == 14)
        assert np.all(arr[3:, :] == 3)
    finally:
        cube.close()


def test_mask_shape_mismatch_raises(tmp_path):
    cube = _real_cube(tmp_path)
    try:
        bad_mask = np.zeros((2, 2), dtype=bool)
        with pytest.raises(ValueError, match="shape"):
            UniformFuelDriver(category=3,
                              no_fuel_mask=bad_mask).fetch(cube)
    finally:
        cube.close()


def test_invalid_category_raises():
    with pytest.raises(ValueError):
        UniformFuelDriver(category=-1)


def test_driver_satisfies_adapter_nfuel_cat_requirement(tmp_path):
    """End-to-end: a cube with UniformFuelDriver output now satisfies the
    WRF-SFIRE adapter's nfuel_cat declared need (no preflight gap)."""
    from models.wrf_sfire_adapter import WRFSFireAdapter
    cube = _real_cube(tmp_path)
    try:
        UniformFuelDriver(category=3).fetch(cube)
        adapter = WRFSFireAdapter(sfire_dir="wrf-sfire/test/em_fire/hill")
        # nfuel_cat satisfied; other required deps (ignition_t0, dem) still
        # missing because we didn't run their drivers here. Just assert
        # nfuel_cat is no longer in the missing list.
        missing = adapter.data_adapter.missing(cube)
        assert "nfuel_cat" not in missing
    finally:
        cube.close()
