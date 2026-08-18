"""The recorded cascade, re-run as a preflight instead of as 48 hours.

`configs/wildfire_scenario.yaml` declares
`outputs.targets: [arrival_s, fire_area, ros_max, fire_intensity,
fuel_consumed]` at `pixel_m: 900`.  The retained log shows `arrival_s` failing
at write time on a 1001x1001 cube.  These tests reconstruct that plan and show
it being refused before any of it runs.
"""
from __future__ import annotations

import pytest

from contracts.placement import PlacementStatus
from cube.grid import SimulationGrid
from cube.preflight import (
    PlannedPublication,
    grid_descriptor,
    preflight_publications,
    preflight_targets,
)

TARGETS = ["arrival_s", "fire_area", "ros_max", "fire_intensity",
           "fuel_consumed"]


def _cube_grid() -> SimulationGrid:
    """The 1001x1001 900 m analysis grid from the recorded run."""
    return SimulationGrid(crs_epsg=32610, pixel_m=900.0, width=1001,
                          height=1001, x0=0.0, y1=900_900.0)


# -- the bridge ----------------------------------------------------------


def test_the_bridge_preserves_the_grid_exactly():
    grid = _cube_grid()
    descriptor = grid_descriptor(grid)
    assert descriptor.shape == grid.shape == (1001, 1001)
    assert descriptor.crs == "EPSG:32610"
    # SimulationGrid stores outer edges; the descriptor stores first centres.
    a, b, c, d, e, f = grid.transform[:6]
    assert tuple(float(v) for v in descriptor.affine) == (
        a, b, c + a / 2, d, e, f + e / 2)
    assert tuple(float(v) for v in descriptor.support_bounds) == grid.bounds


def test_a_grid_places_onto_itself():
    grid = _cube_grid()
    result = preflight_targets(grid, TARGETS, shape=(1001, 1001),
                               source=grid_descriptor(grid))
    assert result.ok
    assert "all 5 planned publications place" in result.report()


# -- the recorded failure, caught early ----------------------------------


def test_the_recorded_plan_is_refused_before_the_run():
    result = preflight_targets(_cube_grid(), TARGETS, shape=(253, 253))
    assert not result.ok
    # Every target shares the undeclared producer grid, so all five are named.
    assert len(result.blocking) == 5
    assert [name for name, _ in result.blocking] == TARGETS
    for _, assessment in result.blocking:
        assert assessment.status is PlacementStatus.UNDEFINED_NO_GEOREFERENCE


def test_the_report_names_arrival_s_and_the_actual_reason():
    result = preflight_targets(_cube_grid(), TARGETS, shape=(253, 253))
    report = result.report()
    assert "5 of 5 planned publications cannot be placed" in report
    assert "arrival_s" in report
    assert "declares no grid" in report
    # The old failure text was two shapes; this names what is missing.
    assert "(253, 253)" in report


def test_every_variable_is_assessed_not_just_the_first_failure():
    """One relaunch per defect is how a 48-hour failure becomes a week."""
    grid = _cube_grid()
    good = grid_descriptor(grid)
    result = preflight_publications(grid, [
        PlannedPublication("arrival_s", (253, 253), None),
        PlannedPublication("fire_area", (1001, 1001), good),
        PlannedPublication("ros_max", (253, 253), None),
    ])
    assert len(result.assessments) == 3
    assert [name for name, _ in result.blocking] == ["arrival_s", "ros_max"]


# -- the placement that would have worked --------------------------------


def test_a_declared_100m_fire_mesh_places_onto_the_900m_cube():
    """900/100 = 9, so an aligned whole-block fire mesh needs no resampling."""
    grid = _cube_grid()
    fire_mesh = grid_descriptor(SimulationGrid(
        crs_epsg=32610, pixel_m=100.0, width=252, height=252,
        x0=0.0, y1=25_200.0))
    result = preflight_publications(
        grid, [PlannedPublication("arrival_s", (252, 252), fire_mesh)])
    assert result.ok
    _, assessment = result.assessments[0]
    assert assessment.status is PlacementStatus.INTEGER_REFINEMENT
    assert assessment.refinement_x == 9


def test_a_declared_1000m_nest_is_reported_as_needing_a_declared_regrid():
    """The innermost nest is 1000 m; the cube is 900 m. Not an integer ratio."""
    grid = _cube_grid()
    nest = grid_descriptor(SimulationGrid(
        crs_epsg=32610, pixel_m=1000.0, width=253, height=253,
        x0=0.0, y1=253_000.0))
    result = preflight_publications(
        grid, [PlannedPublication("arrival_s", (253, 253), nest)])
    assert not result.ok
    _, assessment = result.assessments[0]
    assert assessment.status is PlacementStatus.REQUIRES_DECLARED_RESAMPLING
    assert assessment.resolvable_by_declared_transformation


# -- properties ----------------------------------------------------------


def test_preflight_touches_no_arrays():
    """The whole point: this is decidable from descriptors alone."""
    result = preflight_targets(_cube_grid(), TARGETS, shape=(253, 253))
    # A shape is all that is needed; no array was ever allocated.
    assert len(result.assessments) == len(TARGETS)


def test_preflight_rejects_untyped_plans():
    with pytest.raises(TypeError):
        preflight_publications(_cube_grid(), [("arrival_s", (253, 253))])


def test_an_empty_plan_is_vacuously_ok():
    result = preflight_publications(_cube_grid(), [])
    assert result.ok and result.assessments == ()
