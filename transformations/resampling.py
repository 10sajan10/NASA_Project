"""Which aggregation a variable may be resampled with, and when it is valid.

`contracts/placement.py` decides *whether* an array can be placed onto the cube
and, when it cannot, says that an explicit transformation is required. This
module is the other side of that sentence: for each published variable it
declares the one admissible aggregation, and the conditions under which that
aggregation is actually correct.

Nothing here resamples. It decides admissibility and refuses; the arithmetic is
a runtime operation and stays there.

The declarations are read off WRF-SFIRE's own Registry rather than inferred
from variable names (`wrf-sfire-stack/WRF-SFIRE/Registry/registry.fire`):

- `TIGN_G` — "ignition time on ground", units `s`. A first-occurrence time, so
  a coarse cell's arrival is the **minimum** over the fine cells it covers.
  Averaging arrival times across a fire perimeter produces a time at which
  nothing happened.
- `FIRE_AREA` — "fraction of cell area on fire", units `1`.
- `FUEL_FRAC` — "fuel remaining", units `1`.

Those last two carry the subtle constraint that motivates this module. The
fraction of a coarse cell that is on fire is the **area-weighted** mean of its
fine cells' fractions. A plain mean equals that only when the fine cells within
a block are equal-area. That holds for an aligned integer refinement inside one
CRS -- and it stops holding the moment a reprojection is involved, because
WRF's Lambert Conformal is conformal, not equal-area, so cell areas vary across
the domain. An order statistic like a minimum or a maximum is indifferent to
cell area; a mean of fractions is not. So the constraint is declared per rule
rather than assumed globally.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType

from contracts.placement import PlacementAssessment, PlacementStatus


class AggregationKind(str, Enum):
    """How fine cells combine into one coarse cell."""

    FIRST_OCCURRENCE_MIN = "FIRST_OCCURRENCE_MIN"
    EXTREMUM_MAX = "EXTREMUM_MAX"
    AREAL_FRACTION_MEAN = "AREAL_FRACTION_MEAN"
    INTENSIVE_AREA_WEIGHTED_MEAN = "INTENSIVE_AREA_WEIGHTED_MEAN"
    CATEGORICAL_MAJORITY = "CATEGORICAL_MAJORITY"


#: Aggregations whose result depends on the relative areas of the fine cells.
#: An order statistic or a mode does not; any kind of mean does.
_AREA_SENSITIVE = frozenset({
    AggregationKind.AREAL_FRACTION_MEAN,
    AggregationKind.INTENSIVE_AREA_WEIGHTED_MEAN,
})


class UndeclaredResampling(KeyError):
    """The variable has no declared aggregation, so none is guessed."""


class ResamplingNotAdmissible(ValueError):
    """The declared aggregation is not valid under this placement."""

    def __init__(self, variable: str, reason: str) -> None:
        super().__init__(f"{variable}: {reason}")
        self.variable = variable
        self.reason = reason


@dataclass(frozen=True)
class ResamplingRule:
    """One variable's declared aggregation, with its stated justification."""

    variable: str
    source_variable: str
    units: str
    aggregation: AggregationKind
    rationale: str

    def __post_init__(self) -> None:
        if not isinstance(self.aggregation, AggregationKind):
            raise TypeError("resampling aggregation must be typed")
        if not self.rationale:
            raise ValueError("a declared aggregation must justify itself")

    @property
    def requires_equal_area_blocks(self) -> bool:
        return self.aggregation in _AREA_SENSITIVE


@dataclass(frozen=True)
class ResamplingPlan:
    """An admissible aggregation for one variable under one placement."""

    rule: ResamplingRule
    placement: PlacementAssessment
    block_x: int
    block_y: int

    @property
    def is_identity(self) -> bool:
        return self.block_x == 1 and self.block_y == 1


# Closed registry. Adding a variable is a declaration, not a configuration
# value, so it is made here and reviewed rather than passed in at runtime.
_RULES: MappingProxyType = MappingProxyType({
    "arrival_s": ResamplingRule(
        "arrival_s", "TIGN_G", "s", AggregationKind.FIRST_OCCURRENCE_MIN,
        "TIGN_G is 'ignition time on ground'; a coarse cell's arrival time is "
        "the earliest arrival among the fine cells it covers, and a mean "
        "across a fire perimeter names a time at which nothing happened"),
    "fire_area": ResamplingRule(
        "fire_area", "FIRE_AREA", "1", AggregationKind.AREAL_FRACTION_MEAN,
        "FIRE_AREA is 'fraction of cell area on fire'; the coarse fraction is "
        "the area-weighted mean of the fine fractions, which is a plain mean "
        "only when the fine cells are equal-area"),
    "fuel_consumed": ResamplingRule(
        "fuel_consumed", "FUEL_FRAC", "1", AggregationKind.AREAL_FRACTION_MEAN,
        "derived as 1 - FUEL_FRAC ('fuel remaining'), so it is an areal "
        "fraction and aggregates exactly as fire_area does"),
    "fire_intensity": ResamplingRule(
        "fire_intensity", "FGRNHFX", "W/m^2",
        AggregationKind.INTENSIVE_AREA_WEIGHTED_MEAN,
        "a ground heat flux per unit area; totals do not add across cells, so "
        "it combines as an area-weighted mean rather than a sum"),
    "ros_max": ResamplingRule(
        "ros_max", "ROS", "m/s", AggregationKind.EXTREMUM_MAX,
        "the reported quantity is already a maximum rate of spread, so the "
        "coarse value is the largest fine value, not their average"),
    "nfuel_cat": ResamplingRule(
        "nfuel_cat", "NFUEL_CAT", "1", AggregationKind.CATEGORICAL_MAJORITY,
        "fuel category is a label, not a magnitude; averaging category "
        "numbers produces a category that does not exist"),
})


def declared_variables() -> tuple[str, ...]:
    return tuple(sorted(_RULES))


def resampling_rule(variable: str) -> ResamplingRule:
    """The declared rule for a variable, or a refusal to invent one."""
    try:
        return _RULES[variable]
    except KeyError:
        raise UndeclaredResampling(
            f"{variable} has no declared aggregation; publishing it would mean "
            f"choosing one silently. Declared: {', '.join(declared_variables())}"
        ) from None


def equal_area_blocks_guaranteed(assessment: PlacementAssessment) -> bool:
    """Whether the placement guarantees the fine cells in a block are equal-area.

    True only when source and target share a CRS and an aligned lattice, which
    is exactly what `EXACT_MATCH` and `INTEGER_REFINEMENT` assert. Any placement
    reached through a reprojection cannot promise it: WRF's Lambert Conformal
    preserves angles, not areas.
    """
    return assessment.status in (PlacementStatus.EXACT_MATCH,
                                 PlacementStatus.INTEGER_REFINEMENT)


def transformation_requirement(rule: ResamplingRule) -> str:
    """What a declared regrid must guarantee to carry this variable.

    A refusal that only says "no" leaves the next person guessing. When
    placement reports that a transformation is required, the variable's
    semantics already determine what that transformation has to preserve, so
    the refusal says it.
    """
    if rule.requires_equal_area_blocks:
        return ("requires a declared area-weighted regrid; a plain mean of "
                "per-cell fractions is exact only over equal-area blocks, "
                "which a reprojection cannot promise because WRF's Lambert "
                "Conformal preserves angles rather than areas")
    if rule.aggregation is AggregationKind.FIRST_OCCURRENCE_MIN:
        return ("requires a declared regrid that preserves the earliest "
                "arrival within each target cell; interpolating arrival times "
                "across a fire perimeter names a time at which nothing "
                "happened")
    if rule.aggregation is AggregationKind.EXTREMUM_MAX:
        return ("requires a declared regrid that preserves the maximum within "
                "each target cell; a smoothed maximum is no longer a maximum")
    return ("requires a declared regrid that does not interpolate between "
            "category labels; nearest or majority selection only")


def plan_resampling(variable: str,
                    placement: PlacementAssessment) -> ResamplingPlan:
    """The admissible plan for this variable under this placement, or refuse."""
    rule = resampling_rule(variable)
    if placement.status is PlacementStatus.INTEGER_COARSENING:
        raise ResamplingNotAdmissible(
            variable,
            "the source is coarser than the target, so this is replication "
            "rather than aggregation; it would manufacture spatial detail the "
            "source does not contain and requires an explicit upsampling "
            "transformation")
    if placement.status is PlacementStatus.AXIS_DIRECTION_MISMATCH:
        raise ResamplingNotAdmissible(
            variable,
            "source and target array axes run in opposite directions; an "
            "explicit reorientation transformation must reverse the affected "
            "axis before any cell correspondence can be used")
    if placement.resolvable_by_declared_transformation:
        # Placement is decidable but not yet decided: say what would settle it.
        raise ResamplingNotAdmissible(
            variable, transformation_requirement(rule))
    if not placement.placeable:
        raise ResamplingNotAdmissible(
            variable,
            f"placement is {placement.status.value}, so there is no defined "
            "correspondence between fine and coarse cells to aggregate over")
    if rule.requires_equal_area_blocks and not equal_area_blocks_guaranteed(
            placement):
        raise ResamplingNotAdmissible(
            variable,
            f"{rule.aggregation.value} is area-sensitive and this placement "
            "does not guarantee equal-area blocks; it requires an explicit "
            "area-weighted regrid rather than a plain mean")
    block_x = placement.refinement_x or 1
    block_y = placement.refinement_y or 1
    return ResamplingPlan(rule, placement, block_x, block_y)


def plan_publication(variables: tuple[str, ...],
                     placement: PlacementAssessment
                     ) -> tuple[dict[str, ResamplingPlan], dict[str, str]]:
    """Plan a whole target list, returning admissible plans and refusals.

    Both halves are returned rather than raising on the first problem, so a
    publication plan can be reported in one pass.
    """
    plans: dict[str, ResamplingPlan] = {}
    refusals: dict[str, str] = {}
    for variable in variables:
        try:
            plans[variable] = plan_resampling(variable, placement)
        except (ResamplingNotAdmissible, UndeclaredResampling) as exc:
            refusals[variable] = getattr(exc, "reason", str(exc))
    return plans, refusals


__all__ = [
    "AggregationKind",
    "ResamplingNotAdmissible",
    "ResamplingPlan",
    "ResamplingRule",
    "UndeclaredResampling",
    "declared_variables",
    "transformation_requirement",
    "equal_area_blocks_guaranteed",
    "plan_publication",
    "plan_resampling",
    "resampling_rule",
]
