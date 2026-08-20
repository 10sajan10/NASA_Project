"""A producer declaring its native grid from configuration, before it runs.

The publication preflight can only decide placement for producers that declare
a grid. WRF fixes its projection and nest geometry in configuration, so the
declaration is available at launch rather than after the run.

The declaration is not evidence. `read_wrf_georeference` verifies real output
against its own XLONG/XLAT afterwards and remains the authority; these tests
check that the two agree, which is what makes the declaration trustworthy.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from models.wrf_config import WRFScenario
from models.wrf_georeference import (
    WrfGeoreferenceError,
    crs_token,
    native_grid_from_scenario,
    read_wrf_georeference,
)

WRFOUT = Path("wrf-sfire-stack/WRF-SFIRE/test/em_real")


def _scenario(**kwargs):
    base = dict(
        center_lon=-96.81, center_lat=32.78,
        start=datetime(2019, 9, 4, 12, tzinfo=timezone.utc),
        extent_km=1000.0, resolutions_m=[9000.0, 3000.0, 1000.0],
        nest_fraction=0.5, fire_mesh_ratio=10, duration_days=1.0)
    base.update(kwargs)
    return WRFScenario.from_simple(**base)


# -- the declaration ------------------------------------------------------


def test_the_fire_grid_is_the_innermost_nest_refined():
    scenario = _scenario()
    inner = scenario.domains[-1]
    grid = native_grid_from_scenario(scenario, fire=True)
    assert tuple(grid.shape) == (inner.ny * inner.sr, inner.nx * inner.sr)
    assert float(grid.affine[0]) == inner.dx_m / inner.sr
    assert float(grid.affine[4]) > 0


def test_the_atmospheric_grid_is_the_innermost_mass_grid():
    scenario = _scenario()
    inner = scenario.domains[-1]
    grid = native_grid_from_scenario(scenario, fire=False)
    assert tuple(grid.shape) == (inner.ny, inner.nx)
    assert float(grid.affine[0]) == inner.dx_m
    assert float(grid.affine[4]) > 0


def test_the_fire_mesh_is_a_clean_refinement_of_its_own_nest():
    """900 m / 10 = 90 m, so the two declared grids place onto each other."""
    from contracts.placement import PlacementStatus, assess_placement

    scenario = _scenario()
    fine = native_grid_from_scenario(scenario, fire=True)
    coarse = native_grid_from_scenario(scenario, fire=False)
    assessment = assess_placement(fine, coarse)
    assert assessment.status is PlacementStatus.INTEGER_REFINEMENT
    assert assessment.placeable
    assert (assessment.refinement_x, assessment.refinement_y) == (
        scenario.domains[-1].sr, scenario.domains[-1].sr)


def test_every_nest_shares_the_scenario_projection():
    scenario = _scenario()
    assert native_grid_from_scenario(scenario, fire=True).crs == \
        native_grid_from_scenario(scenario, fire=False).crs


# -- agreement with real output -------------------------------------------


def test_the_declaration_is_close_to_real_output_but_never_equal_to_it():
    """WPS snaps the domain centre, so configuration cannot be exact.

    The scenario requests a centre of (-96.81, 32.78); the run WPS actually
    produced is centred at (-96.808891, 32.779987). That is 103.8 m in
    longitude and 1.4 m in latitude -- more than one 90 m fire cell.

    So a configuration-derived grid is a preflight estimate and must never be
    used as the georeference for publication. `read_wrf_georeference`, which
    verifies against the file's own XLONG/XLAT, stays the authority. This test
    pins both halves: close enough to preflight with, never close enough to
    substitute.
    """
    path = WRFOUT / "wrfout_d03_2019-09-04_12:00:00"
    if not path.exists():
        pytest.skip(f"{path} absent (gitignored, 131 MB)")
    observed = read_wrf_georeference(path).atmospheric.crs
    declared = native_grid_from_scenario(_scenario(), fire=False).crs
    assert declared != observed, (
        "if these ever match exactly, WPS stopped adjusting the centre and "
        "this test should be replaced by an equality check")

    def _params(token):
        return {k: float(v) for k, _, v in
                (part.partition("=") for part in token.split(":")[1:])}
    declared_p, observed_p = _params(declared), _params(observed)
    assert declared_p.keys() == observed_p.keys()
    assert abs(declared_p["lon_0"] - observed_p["lon_0"]) < 0.01
    assert abs(declared_p["lat_0"] - observed_p["lat_0"]) < 0.01
    assert declared_p["R"] == observed_p["R"]


def test_the_declared_projection_is_not_an_authority_code():
    token = native_grid_from_scenario(_scenario()).crs
    assert token.startswith("WRF-LCC:")
    assert not token.upper().startswith("EPSG")


# -- refusals -------------------------------------------------------------


def test_a_scenario_without_a_fire_mesh_is_refused():
    scenario = _scenario(fire_mesh_ratio=0)
    with pytest.raises(WrfGeoreferenceError, match="no configured domain"):
        native_grid_from_scenario(scenario, fire=True)


def test_an_unmodelled_projection_is_refused():
    scenario = _scenario()
    object.__setattr__(scenario, "map_proj", "mercator") \
        if hasattr(scenario, "__dataclass_fields__") else None
    scenario.map_proj = "mercator"
    with pytest.raises(WrfGeoreferenceError, match="not modelled"):
        native_grid_from_scenario(scenario)
