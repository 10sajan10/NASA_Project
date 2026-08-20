"""The 253/1001 blocker, and the contract that answers it before the run.

Every grid in this file is built from the real project configuration rather
than from convenient numbers:

- the analysis cube is `outputs.pixel_m: 900` at 1001x1001 (`configs/`),
- `domain.resolutions_m` ends at a 1000 m innermost nest,
- `domain.fire_mesh_ratio: 10` puts the SFIRE fire mesh at 100 m.

The recorded failure is in `logs/20260708_005323_cascade_20190904T120000_targets.json`:
`arrival_s: array shape (253, 253) != grid (1001, 1001)`, `dead_letter: true`,
after `elapsed_s: 174849.76`.
"""
from __future__ import annotations

import pytest

from contracts.placement import (
    PlacementStatus,
    PlacementUndefined,
    assess_array_placement,
    assess_placement,
    require_placement,
)
from contracts.types import GridDescriptor, SpatialScale


def _grid(shape: tuple[int, int], cell_m: int, *, origin=(0, 0),
          crs: str = "EPSG:32610", rotation: bool = False) -> GridDescriptor:
    """A north-up grid from an outer upper-left edge."""
    return GridDescriptor(
        crs, ("easting", "northing"), shape,
        (str(cell_m), "1" if rotation else "0",
         str(origin[0] + cell_m / 2),
         "0", str(-cell_m), str(origin[1] - cell_m / 2)),
        SpatialScale(str(cell_m), str(cell_m), "m"))


# The analysis cube every published product lands on.
def _cube() -> GridDescriptor:
    return _grid((1001, 1001), 900, origin=(0, 900_900))


# -- the recorded failure ------------------------------------------------


def test_the_recorded_arrival_s_case_is_refused_with_a_reason():
    """The exact 253/1001 case, as it actually arrived: no declared grid."""
    assessment = assess_array_placement("arrival_s", (253, 253), None, _cube())
    assert assessment.status is PlacementStatus.UNDEFINED_NO_GEOREFERENCE
    assert not assessment.placeable
    # The old message named two shapes and nothing else.  This one names the
    # missing thing, which is the georeference.
    assert "declares no grid" in assessment.detail
    assert not assessment.resolvable_by_declared_transformation


def test_the_recorded_failure_needed_no_science_to_detect():
    """Placement is a property of descriptors, so it is knowable at preflight.

    This is the whole point: the recorded run burned 174,849 s before raising.
    Nothing in this assessment touches an array.
    """
    fire_mesh = _grid((253, 253), 1000, origin=(0, 253_000))
    assessment = assess_placement(fire_mesh, _cube())
    assert not assessment.placeable
    # 1000 m against 900 m is not an integer ratio in either direction.
    assert assessment.status is PlacementStatus.REQUIRES_DECLARED_RESAMPLING
    assert assessment.resolvable_by_declared_transformation


def test_requiring_placement_raises_a_typed_error_carrying_the_verdict():
    assessment = assess_array_placement("arrival_s", (253, 253), None, _cube())
    with pytest.raises(PlacementUndefined) as caught:
        require_placement("arrival_s", assessment)
    assert caught.value.field_name == "arrival_s"
    assert caught.value.assessment is assessment
    assert "UNDEFINED_NO_GEOREFERENCE" in str(caught.value)


def test_an_array_disagreeing_with_its_own_grid_is_refused():
    """A declared grid that does not match the array is worse than none."""
    assessment = assess_array_placement(
        "arrival_s", (253, 253), _grid((1001, 1001), 900), _cube())
    assert assessment.status is PlacementStatus.UNDEFINED_NO_GEOREFERENCE
    assert "does not match its own" in assessment.detail


# -- the placements that genuinely work ----------------------------------


def test_the_100m_fire_mesh_aggregates_exactly_onto_the_900m_cube():
    """fire_mesh_ratio 10 on a 1000 m nest gives 100 m cells; 900/100 = 9."""
    # 252 = 28 whole 9-cell blocks.
    fire_mesh = _grid((252, 252), 100, origin=(0, 25_200))
    assessment = assess_placement(fire_mesh, _cube())
    assert assessment.status is PlacementStatus.INTEGER_REFINEMENT
    assert assessment.placeable
    assert (assessment.refinement_x, assessment.refinement_y) == (9, 9)


def test_an_identical_grid_places_cell_for_cell():
    assessment = assess_placement(_cube(), _cube())
    assert assessment.status is PlacementStatus.EXACT_MATCH
    assert assessment.placeable


def test_a_coarser_aligned_source_is_reported_as_coarsening():
    coarse = _grid((100, 100), 1800, origin=(0, 180_000))
    assessment = assess_placement(coarse, _cube())
    assert assessment.status is PlacementStatus.INTEGER_COARSENING
    assert not assessment.placeable
    assert assessment.resolvable_by_declared_transformation
    assert assessment.refinement_x == 2


def test_opposite_y_directions_require_declared_reorientation():
    target = _grid((2, 2), 10, origin=(0, 20))
    south_up = GridDescriptor(
        target.crs, target.axis_order, target.shape,
        ("10", "0", "5", "0", "10", "5"), target.spacing)
    assessment = assess_placement(south_up, target)
    assert assessment.status is PlacementStatus.AXIS_DIRECTION_MISMATCH
    assert not assessment.placeable
    assert assessment.resolvable_by_declared_transformation
    assert "reorientation" in assessment.detail


def test_opposite_x_directions_require_declared_reorientation():
    target = _grid((2, 2), 10, origin=(0, 20))
    westward = GridDescriptor(
        target.crs, target.axis_order, target.shape,
        ("-10", "0", "15", "0", "-10", "15"), target.spacing)
    assessment = assess_placement(westward, target)
    assert assessment.status is PlacementStatus.AXIS_DIRECTION_MISMATCH
    assert not assessment.placeable
    assert "x" in assessment.detail


def test_coarsening_is_blocked_by_the_publication_gate():
    coarse = _grid((100, 100), 1800, origin=(0, 180_000))
    assessment = assess_placement(coarse, _cube())
    with pytest.raises(PlacementUndefined) as caught:
        require_placement("arrival_s", assessment)
    assert caught.value.assessment.status is PlacementStatus.INTEGER_COARSENING


# -- the ways placement is refused ---------------------------------------


def test_a_partial_trailing_block_is_not_treated_as_exact():
    """253 at 100 m is 28 blocks plus one cell; the edge cell is incomplete.

    The origin is on the cube lattice, so alignment is not what refuses this.
    """
    fire_mesh = _grid((253, 253), 100, origin=(0, 25_200))
    assessment = assess_placement(fire_mesh, _cube())
    assert assessment.status is PlacementStatus.REQUIRES_DECLARED_RESAMPLING
    assert not assessment.placeable
    assert "whole blocks" in assessment.detail
    # The factor is still reported, because it is real and useful.
    assert assessment.refinement_x == 9


def test_a_misaligned_origin_defeats_an_exact_ratio():
    """The right resolution on the wrong lattice is still not placeable."""
    offset = _grid((252, 252), 100, origin=(450, 25_200))  # half a cube cell
    assessment = assess_placement(offset, _cube())
    assert assessment.status is PlacementStatus.REQUIRES_DECLARED_RESAMPLING
    assert not assessment.placeable


def test_a_source_outside_the_target_extent_is_refused():
    """Aligned lattices say nothing about covering the same ground."""
    far = _grid((252, 252), 100, origin=(9_000_000, 9_025_200))
    assessment = assess_placement(far, _cube())
    assert assessment.status is PlacementStatus.OUTSIDE_TARGET_EXTENT
    assert not assessment.placeable


def test_matching_shapes_in_different_crs_are_refused():
    """Identical shapes are not evidence of placement."""
    other = _grid((1001, 1001), 900, origin=(0, 900_900), crs="EPSG:32611")
    assessment = assess_placement(other, _cube())
    assert assessment.status is PlacementStatus.CRS_MISMATCH
    assert not assessment.placeable


def test_a_rotated_grid_is_refused_rather_than_approximated():
    rotated = _grid((252, 252), 100, origin=(0, 25_200), rotation=True)
    assessment = assess_placement(rotated, _cube())
    assert assessment.status is PlacementStatus.ROTATED_OR_SKEWED
    assert not assessment.placeable


def test_swapped_axis_order_is_refused():
    swapped = GridDescriptor(
        "EPSG:32610", ("northing", "easting"), (1001, 1001),
        ("900", "0", "0", "0", "-900", "900900"),
        SpatialScale("900", "900", "m"))
    assessment = assess_placement(swapped, _cube())
    assert assessment.status is PlacementStatus.AXIS_ORDER_MISMATCH


def test_a_zero_cell_size_is_refused_not_divided_by():
    degenerate = GridDescriptor(
        "EPSG:32610", ("easting", "northing"), (10, 10),
        ("0", "0", "0", "0", "-900", "9000"),
        SpatialScale("900", "900", "m"))
    assessment = assess_placement(degenerate, _cube())
    assert assessment.status is PlacementStatus.DEGENERATE_GRID
    assert not assessment.placeable


# -- properties of the verdict itself ------------------------------------


def test_resampling_is_never_silently_placeable():
    """The failure mode this module exists to prevent."""
    nest = _grid((253, 253), 1000, origin=(0, 253_000))
    assessment = assess_placement(nest, _cube())
    assert assessment.resolvable_by_declared_transformation
    assert not assessment.placeable
    with pytest.raises(PlacementUndefined):
        require_placement("arrival_s", assessment)


def test_every_verdict_explains_itself():
    cube = _cube()
    cases = [
        assess_placement(cube, cube),
        assess_placement(_grid((252, 252), 100, origin=(0, 25_200)), cube),
        assess_placement(_grid((253, 253), 1000, origin=(0, 253_000)), cube),
        assess_placement(_grid((1001, 1001), 900, origin=(0, 900_900),
                               crs="EPSG:32611"), cube),
        assess_array_placement("arrival_s", (253, 253), None, cube),
    ]
    for assessment in cases:
        assert assessment.detail and len(assessment.detail) > 40
        payload = assessment.to_dict()
        assert payload["status"] == assessment.status.value
        assert payload["placeable"] is assessment.placeable


def test_assessment_rejects_an_untyped_status():
    from contracts.placement import PlacementAssessment
    with pytest.raises(TypeError):
        PlacementAssessment("EXACT_MATCH", "stringly typed")  # type: ignore[arg-type]


def test_placement_requires_grid_descriptors():
    with pytest.raises(TypeError):
        assess_placement(_cube(), {"shape": (1001, 1001)})  # type: ignore[arg-type]
