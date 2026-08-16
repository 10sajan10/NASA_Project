"""Declared aggregations, and the preconditions that make them valid.

The five `outputs.targets` variables do not share an aggregation, and the
difference is not cosmetic: `arrival_s` is a first-occurrence time, `ros_max`
is already an extremum, and `fire_area` / `fuel_consumed` are areal fractions
whose plain mean is only correct over equal-area blocks.
"""
from __future__ import annotations

import pytest

from contracts.placement import PlacementAssessment, PlacementStatus
from transformations.resampling import (
    AggregationKind,
    ResamplingNotAdmissible,
    UndeclaredResampling,
    declared_variables,
    equal_area_blocks_guaranteed,
    plan_publication,
    plan_resampling,
    resampling_rule,
)

TARGETS = ("arrival_s", "fire_area", "ros_max", "fire_intensity",
           "fuel_consumed")


def _placement(status: PlacementStatus, *, factor: int | None = 10
               ) -> PlacementAssessment:
    return PlacementAssessment(
        status, "constructed for a resampling admissibility test",
        refinement_x=factor, refinement_y=factor)


REFINEMENT = _placement(PlacementStatus.INTEGER_REFINEMENT)
EXACT = _placement(PlacementStatus.EXACT_MATCH, factor=1)


# -- the declarations ----------------------------------------------------


def test_every_published_target_declares_an_aggregation():
    for variable in TARGETS:
        assert resampling_rule(variable).aggregation in AggregationKind


def test_the_targets_do_not_share_one_aggregation():
    """A single block-mean would be wrong for most of them."""
    kinds = {resampling_rule(name).aggregation for name in TARGETS}
    assert len(kinds) >= 3
    assert AggregationKind.FIRST_OCCURRENCE_MIN in kinds
    assert AggregationKind.EXTREMUM_MAX in kinds


def test_arrival_time_takes_the_earliest_not_the_average():
    rule = resampling_rule("arrival_s")
    assert rule.source_variable == "TIGN_G"
    assert rule.units == "s"
    assert rule.aggregation is AggregationKind.FIRST_OCCURRENCE_MIN
    assert "earliest" in rule.rationale


def test_a_categorical_field_is_never_averaged():
    rule = resampling_rule("nfuel_cat")
    assert rule.aggregation is AggregationKind.CATEGORICAL_MAJORITY
    assert not rule.requires_equal_area_blocks


def test_an_undeclared_variable_is_refused_not_guessed():
    with pytest.raises(UndeclaredResampling, match="no declared aggregation"):
        resampling_rule("smoke_pm25")


def test_every_rule_justifies_itself():
    for variable in declared_variables():
        assert len(resampling_rule(variable).rationale) > 40


# -- the area-sensitivity distinction ------------------------------------


def test_only_the_means_are_area_sensitive():
    """An order statistic does not care how big the cells are; a mean does."""
    sensitive = {name for name in declared_variables()
                 if resampling_rule(name).requires_equal_area_blocks}
    assert sensitive == {"fire_area", "fuel_consumed", "fire_intensity"}


def test_equal_area_blocks_come_only_from_a_shared_aligned_lattice():
    assert equal_area_blocks_guaranteed(EXACT)
    assert equal_area_blocks_guaranteed(REFINEMENT)
    for status in (PlacementStatus.REQUIRES_DECLARED_RESAMPLING,
                   PlacementStatus.CRS_MISMATCH,
                   PlacementStatus.INTEGER_COARSENING):
        assert not equal_area_blocks_guaranteed(_placement(status))


def test_a_fraction_cannot_be_plain_meaned_across_a_reprojection():
    """The finding: WRF's Lambert is conformal, not equal-area."""
    reprojected = _placement(PlacementStatus.REQUIRES_DECLARED_RESAMPLING)
    with pytest.raises(ResamplingNotAdmissible, match="area-weighted regrid"):
        plan_resampling("fire_area", reprojected)


def test_a_pending_transformation_is_refused_with_what_it_must_guarantee():
    """Refusing without saying what would fix it just moves the guessing."""
    reprojected = _placement(PlacementStatus.REQUIRES_DECLARED_RESAMPLING)
    with pytest.raises(ResamplingNotAdmissible, match="earliest arrival"):
        plan_resampling("arrival_s", reprojected)
    with pytest.raises(ResamplingNotAdmissible, match="maximum"):
        plan_resampling("ros_max", reprojected)
    with pytest.raises(ResamplingNotAdmissible, match="category labels"):
        plan_resampling("nfuel_cat", reprojected)


# -- admissibility -------------------------------------------------------


def test_an_aligned_refinement_admits_every_target():
    for variable in TARGETS:
        plan = plan_resampling(variable, REFINEMENT)
        assert (plan.block_x, plan.block_y) == (10, 10)
        assert not plan.is_identity


def test_an_exact_match_is_the_identity_plan():
    plan = plan_resampling("arrival_s", EXACT)
    assert plan.is_identity


def test_an_unplaceable_source_admits_nothing():
    for status in (PlacementStatus.CRS_MISMATCH,
                   PlacementStatus.UNDEFINED_NO_GEOREFERENCE,
                   PlacementStatus.OUTSIDE_TARGET_EXTENT):
        with pytest.raises(ResamplingNotAdmissible,
                           match="no defined correspondence"):
            plan_resampling("arrival_s", _placement(status))


def test_coarsening_is_refused_as_manufactured_detail():
    """Replication would claim every 90 m subcell ignited at the same instant."""
    with pytest.raises(ResamplingNotAdmissible, match="manufacture"):
        plan_resampling("arrival_s",
                        _placement(PlacementStatus.INTEGER_COARSENING, factor=2))


# -- planning a whole target list ----------------------------------------


def test_a_whole_target_list_is_planned_in_one_pass():
    plans, refusals = plan_publication(TARGETS, REFINEMENT)
    assert set(plans) == set(TARGETS)
    assert refusals == {}


def test_a_reprojection_refuses_every_target_for_two_different_reasons():
    """No transformation is declared, so nothing is admissible — but the
    variables do not all need the *same* transformation, and the report says
    so rather than emitting one undifferentiated refusal."""
    plans, refusals = plan_publication(
        TARGETS, _placement(PlacementStatus.REQUIRES_DECLARED_RESAMPLING))
    assert plans == {}
    assert set(refusals) == set(TARGETS)
    area_weighted = {name for name, reason in refusals.items()
                     if "area-weighted" in reason}
    order_preserving = {name for name, reason in refusals.items()
                        if "earliest arrival" in reason or "maximum" in reason}
    assert area_weighted == {"fire_area", "fuel_consumed", "fire_intensity"}
    assert order_preserving == {"arrival_s", "ros_max"}


def test_an_undeclared_variable_is_reported_not_raised_in_bulk():
    plans, refusals = plan_publication(("arrival_s", "smoke_pm25"), REFINEMENT)
    assert set(plans) == {"arrival_s"}
    assert "no declared aggregation" in refusals["smoke_pm25"]
