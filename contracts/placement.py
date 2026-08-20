"""Whether one grid's array may be placed onto another, decided in advance.

This exists because of a recorded, recurrent failure. A 48.6-hour WRF-SFIRE
run ended with:

```text
ValueError: arrival_s: array shape (253, 253) != grid (1001, 1001)
```

Three retained logs show the same defect, and `stage0/wrf_interface_audit.md`
calls it "a blocker for WRF output publication". Two things were wrong, and
only one of them is about shapes.

The first is *when*: a bare shape comparison at publication time cannot fail
until the science has already been computed. Placement is a property of two
grid **descriptors**, so it is knowable before a single core-hour is spent.
:func:`assess_placement` is pure and cheap, and is meant to run at preflight.

The second is *what*: "shape mismatch" is the symptom. The real question is
whether a declared relationship exists between the two grids at all. A nested
fire mesh and an analysis cube can differ in shape and still be perfectly
placeable when one is an aligned integer refinement of the other; conversely
two grids of identical shape in different CRSs are not placeable without a
reprojection nobody declared. So the verdict names the relationship, and where
none exists it says exactly what is missing rather than reducing or slicing to
make the arrays fit.

Nothing here resamples. When placement needs interpolation the answer is
`REQUIRES_DECLARED_RESAMPLING`, which is an instruction to route through an
explicit Stage-4 transformation with its own cost and assumptions -- not
permission for an adapter to do it quietly.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Any

from .identity import decimal_value
from .types import GridDescriptor

# Grid origins rarely land on exact binary fractions, so alignment is judged
# against a tolerance expressed in *cells* rather than in CRS units.
_ALIGNMENT_TOLERANCE_CELLS = Decimal("0.01")


class PlacementStatus(str, Enum):
    EXACT_MATCH = "EXACT_MATCH"
    INTEGER_REFINEMENT = "INTEGER_REFINEMENT"
    INTEGER_COARSENING = "INTEGER_COARSENING"
    REQUIRES_DECLARED_RESAMPLING = "REQUIRES_DECLARED_RESAMPLING"
    CRS_MISMATCH = "CRS_MISMATCH"
    AXIS_ORDER_MISMATCH = "AXIS_ORDER_MISMATCH"
    AXIS_DIRECTION_MISMATCH = "AXIS_DIRECTION_MISMATCH"
    UNDEFINED_NO_GEOREFERENCE = "UNDEFINED_NO_GEOREFERENCE"
    ROTATED_OR_SKEWED = "ROTATED_OR_SKEWED"
    DEGENERATE_GRID = "DEGENERATE_GRID"
    OUTSIDE_TARGET_EXTENT = "OUTSIDE_TARGET_EXTENT"


@dataclass(frozen=True)
class PlacementAssessment:
    """The relationship between a source grid and a target grid."""

    status: PlacementStatus
    detail: str
    refinement_x: int | None = None
    refinement_y: int | None = None
    offset_cells_x: str | None = None
    offset_cells_y: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, PlacementStatus):
            raise TypeError("placement status must be typed")
        if not self.detail:
            raise ValueError("a placement verdict must explain itself")

    @property
    def placeable(self) -> bool:
        """True only when the array can be placed with no invented values.

        ``REQUIRES_DECLARED_RESAMPLING`` is deliberately *not* placeable: the
        data may well be usable, but only through a declared transformation,
        and saying yes here is how a silent regrid gets back in.
        ``INTEGER_COARSENING`` likewise describes a real lattice relationship
        without authorizing publication: filling its finer target would invent
        values and therefore needs an explicit upsampling transformation.
        """
        return self.status in (
            PlacementStatus.EXACT_MATCH,
            PlacementStatus.INTEGER_REFINEMENT,
        )

    @property
    def resolvable_by_declared_transformation(self) -> bool:
        return self.status in (
            PlacementStatus.REQUIRES_DECLARED_RESAMPLING,
            PlacementStatus.INTEGER_COARSENING,
            PlacementStatus.AXIS_DIRECTION_MISMATCH,
        )

    def to_dict(self) -> dict[str, Any]:
        payload = dataclasses.asdict(self)
        payload["status"] = self.status.value
        payload["placeable"] = self.placeable
        return payload


class PlacementUndefined(ValueError):
    """Placement was required but no declared relationship supports it."""

    def __init__(self, field_name: str,
                 assessment: PlacementAssessment) -> None:
        super().__init__(
            f"{field_name}: {assessment.status.value} — {assessment.detail}")
        self.field_name = field_name
        self.assessment = assessment


# ``GridDescriptor.affine`` uses ``(a,b,c,d,e,f)`` ordering but, unlike a
# rasterio transform, c/f are the first *sample centre*.  Everything below
# reports (x, y) pairs in that order, while ``shape`` stays row-major (y, x).
_X_SCALE, _Y_SCALE = 0, 4
_ROTATION_TERMS = (1, 3)
_X_ORIGIN, _Y_ORIGIN = 2, 5


def _is_axis_aligned(grid: GridDescriptor) -> bool:
    """No rotation or skew: the affine's off-diagonal terms are zero."""
    return all(decimal_value(grid.affine[index]) == 0
               for index in _ROTATION_TERMS)


def _origin(grid: GridDescriptor) -> tuple[Decimal, Decimal]:
    """First sample centre in array order."""
    return (decimal_value(grid.affine[_X_ORIGIN]),
            decimal_value(grid.affine[_Y_ORIGIN]))


def _cell_size(grid: GridDescriptor) -> tuple[Decimal, Decimal]:
    """Signed (x, y) cell size; y is negative on a north-up grid."""
    return (decimal_value(grid.affine[_X_SCALE]),
            decimal_value(grid.affine[_Y_SCALE]))


def _footprint(grid: GridDescriptor) -> tuple[Decimal, Decimal, Decimal,
                                              Decimal]:
    """(min_x, min_y, max_x, max_y) outer cell-edge footprint."""
    return tuple(decimal_value(value) for value in grid.support_bounds)


def _first_edge(grid: GridDescriptor) -> tuple[Decimal, Decimal]:
    """Leading cell edge along each signed array axis."""
    origin_x, origin_y = _origin(grid)
    cell_x, cell_y = _cell_size(grid)
    return origin_x - cell_x / 2, origin_y - cell_y / 2


def _contained(inner: tuple[Decimal, Decimal, Decimal, Decimal],
               outer: tuple[Decimal, Decimal, Decimal, Decimal],
               tolerance: Decimal) -> bool:
    return (inner[0] >= outer[0] - tolerance
            and inner[1] >= outer[1] - tolerance
            and inner[2] <= outer[2] + tolerance
            and inner[3] <= outer[3] + tolerance)


def _integer_ratio(finer: Decimal, coarser: Decimal) -> int | None:
    """The integer n where coarser == n * finer, or None."""
    if finer == 0:
        return None
    ratio = coarser / finer
    nearest = int(ratio.to_integral_value())
    if nearest < 1:
        return None
    return nearest if abs(ratio - nearest) <= Decimal("1e-9") else None


def assess_placement(source: GridDescriptor,
                     target: GridDescriptor) -> PlacementAssessment:
    """Decide how ``source`` relates to ``target``. Pure; runs at preflight.

    Deliberately conservative: every path that would require inventing a value
    ends in a non-placeable verdict naming what is missing.
    """
    if not isinstance(source, GridDescriptor) or not isinstance(
            target, GridDescriptor):
        raise TypeError("placement requires two GridDescriptor values")

    if source.crs != target.crs:
        return PlacementAssessment(
            PlacementStatus.CRS_MISMATCH,
            f"source is {source.crs} and target is {target.crs}; placement "
            "needs a declared reprojection, which this layer never performs")
    if source.axis_order != target.axis_order:
        return PlacementAssessment(
            PlacementStatus.AXIS_ORDER_MISMATCH,
            f"source axes {source.axis_order} differ from target "
            f"{target.axis_order}; the arrays are not comparable as laid out")
    for grid, label in ((source, "source"), (target, "target")):
        if not _is_axis_aligned(grid):
            return PlacementAssessment(
                PlacementStatus.ROTATED_OR_SKEWED,
                f"the {label} grid is rotated or skewed, so placement is not "
                "a cell-index operation and needs a declared reprojection")
        if any(term == 0 for term in _cell_size(grid)):
            return PlacementAssessment(
                PlacementStatus.DEGENERATE_GRID,
                f"the {label} grid has a zero cell size, so it covers no area "
                "and no array can be placed against it")

    source_cell = _cell_size(source)
    target_cell = _cell_size(target)
    source_origin = _origin(source)
    target_origin = _origin(target)

    reversed_axes = tuple(
        axis for axis, source_step, target_step in zip(
            ("x", "y"), source_cell, target_cell)
        if (source_step < 0) != (target_step < 0))
    if reversed_axes:
        return PlacementAssessment(
            PlacementStatus.AXIS_DIRECTION_MISMATCH,
            f"source and target traverse the {'/'.join(reversed_axes)} array "
            "axis in opposite directions; their footprints may coincide, "
            "but cell indexes name mirrored locations until an explicit "
            "reorientation transformation reverses the affected axis")

    if source.shape == target.shape and source_cell == target_cell and \
            source_origin == target_origin:
        return PlacementAssessment(
            PlacementStatus.EXACT_MATCH,
            "identical grid: the array maps cell-for-cell")

    ratios: list[int | None] = []
    coarsening: list[int | None] = []
    for index in range(2):
        ratios.append(_integer_ratio(abs(source_cell[index]),
                                     abs(target_cell[index])))
        coarsening.append(_integer_ratio(abs(target_cell[index]),
                                         abs(source_cell[index])))

    source_edge = _first_edge(source)
    target_edge = _first_edge(target)
    offset_x = ((source_edge[0] - target_edge[0]) / target_cell[0]
                if target_cell[0] != 0 else None)
    offset_y = ((source_edge[1] - target_edge[1]) / target_cell[1]
                if target_cell[1] != 0 else None)
    aligned = (offset_x is not None and offset_y is not None
               and abs(offset_x - offset_x.to_integral_value())
               <= _ALIGNMENT_TOLERANCE_CELLS
               and abs(offset_y - offset_y.to_integral_value())
               <= _ALIGNMENT_TOLERANCE_CELLS)

    integral = None
    if all(item is not None and item > 1 for item in ratios) and aligned:
        integral = (PlacementStatus.INTEGER_REFINEMENT, ratios,
                    f"source is an aligned {ratios[0]}x{ratios[1]} refinement "
                    "of the target; each target cell covers a whole block of "
                    "source cells")
    elif all(item is not None and item > 1 for item in coarsening) and aligned:
        integral = (PlacementStatus.INTEGER_COARSENING, coarsening,
                    f"source is an aligned {coarsening[0]}x{coarsening[1]} "
                    "coarsening of the target; publishing it on the finer "
                    "target would manufacture spatial detail, so it requires "
                    "an explicitly declared upsampling transformation")

    if integral is not None and integral[0] is \
            PlacementStatus.INTEGER_REFINEMENT:
        # Aggregating a finer source into coarser target cells is exact only
        # when the source spans whole blocks.  A trailing partial block means
        # the edge target cell cannot be computed from the data in hand, and
        # deciding what to do about that is a resampling policy, not placement.
        factors = integral[1]
        partial = [axis for axis, factor, extent in
                   (("y", factors[1], source.shape[0]),
                    ("x", factors[0], source.shape[1]))
                   if extent % factor]
        if partial:
            return PlacementAssessment(
                PlacementStatus.REQUIRES_DECLARED_RESAMPLING,
                f"source is an aligned {factors[0]}x{factors[1]} refinement "
                f"of the target, but its {'/'.join(partial)} extent "
                f"{source.shape} does not span whole blocks; the edge target "
                "cell would be only partly covered, so aggregation requires a "
                "declared policy for partial blocks",
                refinement_x=factors[0], refinement_y=factors[1],
                offset_cells_x=str(offset_x), offset_cells_y=str(offset_y))

    if integral is not None:
        status, factors, detail = integral
        # Alignment says the lattices agree; it says nothing about whether the
        # source actually lies inside the target.  A nested domain placed off
        # the parent is a configuration error, not a placement.
        tolerance = min(abs(target_cell[0]), abs(target_cell[1])) \
            * _ALIGNMENT_TOLERANCE_CELLS
        if not _contained(_footprint(source), _footprint(target), tolerance):
            return PlacementAssessment(
                PlacementStatus.OUTSIDE_TARGET_EXTENT,
                f"source footprint {_footprint(source)} is not contained by "
                f"target footprint {_footprint(target)}; the grids share a "
                "lattice but the source covers ground the target does not")
        return PlacementAssessment(
            status, detail, refinement_x=factors[0], refinement_y=factors[1],
            offset_cells_x=str(offset_x), offset_cells_y=str(offset_y))

    return PlacementAssessment(
        PlacementStatus.REQUIRES_DECLARED_RESAMPLING,
        f"source {source.shape} at cell {source_cell} and target "
        f"{target.shape} at cell {target_cell} are georeferenced but neither "
        "identical nor an aligned integer refinement; placement requires an "
        "explicitly declared resampling transformation with its own cost and "
        "assumptions",
        offset_cells_x=None if offset_x is None else str(offset_x),
        offset_cells_y=None if offset_y is None else str(offset_y))


def assess_array_placement(field_name: str, array_shape: tuple[int, int],
                           source: GridDescriptor | None,
                           target: GridDescriptor) -> PlacementAssessment:
    """Assess a concrete array, catching the ungeoreferenced case explicitly.

    The legacy failure lived here: an array arrived with a shape and *no
    declared grid*, so the only check available was ``shape != grid`` and the
    only possible answer was a late, uninformative error.
    """
    if source is None:
        return PlacementAssessment(
            PlacementStatus.UNDEFINED_NO_GEOREFERENCE,
            f"{field_name} has shape {array_shape} but declares no grid, so "
            f"its relationship to the {target.shape} target is undefined; a "
            "shape comparison alone cannot establish placement")
    if tuple(source.shape) != tuple(array_shape):
        return PlacementAssessment(
            PlacementStatus.UNDEFINED_NO_GEOREFERENCE,
            f"{field_name} has shape {array_shape} but its declared grid is "
            f"{tuple(source.shape)}; the array does not match its own "
            "georeference, so nothing downstream can be trusted")
    return assess_placement(source, target)


def require_placement(field_name: str, assessment: PlacementAssessment
                      ) -> PlacementAssessment:
    """Raise unless the array can be placed without inventing values."""
    if not assessment.placeable:
        raise PlacementUndefined(field_name, assessment)
    return assessment


__all__ = [
    "PlacementAssessment",
    "PlacementStatus",
    "PlacementUndefined",
    "assess_array_placement",
    "assess_placement",
    "require_placement",
]
