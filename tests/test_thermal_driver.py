"""ThermalDriver tests.

After the time-field fix, `ignition_t0` must:
  * be float32 (so NaN encoding works)
  * carry `0.0` inside the Critical -> Unsurvivable annulus
  * carry NaN everywhere else (so the WRF-SFIRE adapter's
    np.isfinite check correctly drops un-ignited cells)
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from drivers.thermal import ThermalDriver


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DALLAS_KML = PROJECT_ROOT / "Dallas.kml"


def _real_cube(tmp_path: Path):
    from cube.grid import SimulationGrid
    from cube.store import Cube
    # Dallas center; ~50 km radius so the AOI spans every PDC ring.
    grid = SimulationGrid.from_center_radius(
        -96.797, 32.776, 50_000.0, 1_000.0)
    return Cube(tmp_path, grid)


@pytest.mark.skipif(not DALLAS_KML.exists(),
                     reason="Dallas.kml required for thermal-driver smoke test")
def test_ignition_t0_is_float32_with_nan_sentinels(tmp_path):
    cube = _real_cube(tmp_path / "cube")
    try:
        ThermalDriver(kml_path=str(DALLAS_KML)).fetch(cube)
        ign = cube.read_static("ignition_t0")
        assert ign.dtype == np.float32, (
            "ignition_t0 must be float32 so NaN can act as the "
            "un-ignited sentinel")
        assert np.isnan(ign).any(), (
            "expected NaN sentinels outside the asteroid annulus")
        assert np.isfinite(ign).any(), (
            "expected at least one finite cell inside the annulus")
    finally:
        cube.close()


@pytest.mark.skipif(not DALLAS_KML.exists(),
                     reason="Dallas.kml required for thermal-driver smoke test")
def test_ignition_t0_finite_cells_are_zero(tmp_path):
    """The pulse front arrives effectively instantaneously: every cell
    in the annulus has the same ignition time (0.0 s)."""
    cube = _real_cube(tmp_path / "cube")
    try:
        ThermalDriver(kml_path=str(DALLAS_KML)).fetch(cube)
        ign = cube.read_static("ignition_t0")
        inside = ign[np.isfinite(ign)]
        assert inside.size > 0
        assert np.allclose(inside, 0.0), (
            "all in-annulus cells should be pre-ignited at t=0")
    finally:
        cube.close()


@pytest.mark.skipif(not DALLAS_KML.exists(),
                     reason="Dallas.kml required for thermal-driver smoke test")
def test_burnable_mask_excludes_only_unsurvivable_zone(tmp_path):
    """`burnable` stays a uint8 bool mask (separate from ignition_t0).
    Only the innermost (Unsurvivable) ring is non-burnable."""
    cube = _real_cube(tmp_path / "cube")
    try:
        ThermalDriver(kml_path=str(DALLAS_KML)).fetch(cube)
        burn = cube.read_static("burnable")
        assert set(np.unique(burn).tolist()) <= {0, 1}
        # Most of the AOI is outside the Unsurvivable ring -> burnable.
        assert burn.mean() > 0.5
    finally:
        cube.close()
