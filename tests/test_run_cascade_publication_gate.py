"""The launch gate that turns a 48-hour publication failure into a refusal.

`logs/20260708_005323_cascade_20190904T120000_targets.json` records
`arrival_s: array shape (253, 253) != grid (1001, 1001)` after
`elapsed_s: 174849.76`. The check was correct; it just ran at write time,
which is the last thing a run does. These tests drive the same decision at
launch.

The gate is model-agnostic on purpose: it reads whatever native grid a producer
declares and treats an undeclared producer as unestablished rather than
assuming the cube's own grid.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from contracts.types import GridDescriptor, SpatialScale
from cube.grid import SimulationGrid

ROOT = Path(__file__).resolve().parents[1]


def _cascade_module():
    spec = importlib.util.spec_from_file_location(
        "_run_cascade_under_test", ROOT / "scripts" / "run_cascade.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


CASCADE = _cascade_module()
TARGETS = ["arrival_s", "fire_area"]


def _cube_grid() -> SimulationGrid:
    return SimulationGrid(crs_epsg=32614, pixel_m=900.0, width=100,
                          height=100, x0=0.0, y1=90_000.0)


def _cube(grid=None):
    grid = grid or _cube_grid()
    return SimpleNamespace(grid=grid)


def _config(**kwargs):
    base = dict(pixel_m=900.0, fire_mesh_ratio=10, fire_pixel_m=90.0)
    base.update(kwargs)
    return SimpleNamespace(**base)


def _native(cell, *, crs="EPSG:32614", shape=(1000, 1000), origin=(0.0, 90_000.0)):
    return GridDescriptor(
        crs, ("easting", "northing"), shape,
        (repr(float(cell)), "0", repr(float(origin[0] + cell / 2)),
         "0", repr(-float(cell)), repr(float(origin[1] - cell / 2))),
        SpatialScale(repr(float(cell)), repr(float(cell)), "m"))


# -- the recorded failure, refused at launch ------------------------------


def test_an_undeclared_producer_is_reported_and_refused():
    """The recorded case: an array with a shape and no declared grid."""
    with pytest.raises(SystemExit, match="cannot publish its targets"):
        CASCADE._publication_gate(_cube(), _config(), TARGETS, strict=True)


def test_the_refusal_names_every_unplaceable_target(capsys):
    with pytest.raises(SystemExit):
        CASCADE._publication_gate(_cube(), _config(), TARGETS, strict=True)
    out = capsys.readouterr().out
    for name in TARGETS:
        assert name in out
    assert "declare no native grid" in out


def test_a_mismatched_crs_is_refused_at_launch():
    """WRF writes a domain-centred Lambert; a UTM cube cannot accept it."""
    lambert = _native(90.0, crs="WRF-LCC:lat_1=32.78:lon_0=-96.81")
    config = _config(producer_native_grids={name: lambert for name in TARGETS})
    with pytest.raises(SystemExit, match="cannot publish"):
        CASCADE._publication_gate(_cube(), config, TARGETS, strict=True)


# -- the placement that works --------------------------------------------


def test_an_exact_refinement_passes_the_gate(capsys):
    """900/90 = 10, aligned and whole-blocked, so publication is defined."""
    fine = _native(90.0, shape=(1000, 1000))
    config = _config(producer_native_grids={name: fine for name in TARGETS})
    CASCADE._publication_gate(_cube(), config, TARGETS, strict=True)
    assert "every declared publication places" in capsys.readouterr().out


def test_no_targets_is_vacuously_fine():
    CASCADE._publication_gate(_cube(), _config(), [], strict=True)


# -- the override --------------------------------------------------------


def test_the_override_warns_instead_of_refusing(capsys):
    CASCADE._publication_gate(_cube(), _config(), TARGETS, strict=False)
    out = capsys.readouterr().out
    assert "continuing anyway" in out
    assert "may fail" in out


def test_the_gate_allocates_no_arrays():
    """It is decidable from descriptors, which is why it can run at launch."""
    with pytest.raises(SystemExit):
        CASCADE._publication_gate(
            _cube(SimulationGrid(crs_epsg=32614, pixel_m=900.0, width=100_000,
                                 height=100_000, x0=0.0, y1=9e7)),
            _config(), TARGETS, strict=True)
