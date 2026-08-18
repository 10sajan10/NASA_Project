"""Check every planned cube publication before the science runs.

`cube/store.py` compares an array's shape against the cube's shape at write
time.  That check is correct but it is the last thing to happen in a run: the
recorded `arrival_s: array shape (253, 253) != grid (1001, 1001)` failure
arrived after 174,849 s of compute and dead-lettered the whole cascade.

Placement does not depend on any array's contents, only on the two grids'
descriptors, so it can be settled before a single core-hour is spent.  This
module bridges the legacy `SimulationGrid` to the typed contract in
`contracts.placement` and reports on a whole publication plan at once.

The bridge is exact rather than approximate. ``SimulationGrid`` stores raster
outer edges while ``GridDescriptor`` stores first sample centres, so the bridge
performs the explicit half-cell translation and no resampling.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

from contracts.placement import (
    PlacementAssessment,
    assess_array_placement,
)
from contracts.types import GridDescriptor, SpatialScale

from .grid import SimulationGrid


def grid_descriptor(grid: SimulationGrid) -> GridDescriptor:
    """Describe a `SimulationGrid` in typed contract terms. Lossless."""
    pixel = repr(float(grid.pixel_m))
    first_x = repr(float(grid.x0 + grid.pixel_m / 2.0))
    first_y = repr(float(grid.y1 - grid.pixel_m / 2.0))
    return GridDescriptor(
        f"EPSG:{int(grid.crs_epsg)}",
        ("easting", "northing"),
        (int(grid.height), int(grid.width)),
        (pixel, "0", first_x,
         "0", repr(-float(grid.pixel_m)), first_y),
        SpatialScale(pixel, pixel, "m"),
    )


@dataclass(frozen=True)
class PlannedPublication:
    """A variable a run intends to write, and the grid it will arrive on.

    `source` is `None` when the producer does not declare a georeference for
    its output.  That is not a detail to be filled in later -- it is the defect
    that made the recorded failure undiagnosable, so it is represented
    explicitly rather than defaulted to the cube's own grid.
    """

    variable: str
    shape: tuple[int, int]
    source: GridDescriptor | None = None


@dataclass(frozen=True)
class PublicationPreflight:
    """What a run may publish, decided before it starts."""

    assessments: tuple[tuple[str, PlacementAssessment], ...]

    @property
    def blocking(self) -> tuple[tuple[str, PlacementAssessment], ...]:
        return tuple((name, assessment)
                     for name, assessment in self.assessments
                     if not assessment.placeable)

    @property
    def ok(self) -> bool:
        return not self.blocking

    def report(self) -> str:
        if self.ok:
            return (f"all {len(self.assessments)} planned publications place "
                    "onto the cube grid")
        lines = [f"{len(self.blocking)} of {len(self.assessments)} planned "
                 "publications cannot be placed onto the cube grid:"]
        for name, assessment in self.blocking:
            lines.append(f"  {name}: {assessment.status.value} — "
                         f"{assessment.detail}")
        return "\n".join(lines)


def preflight_publications(cube_grid: SimulationGrid,
                           planned: Iterable[PlannedPublication]
                           ) -> PublicationPreflight:
    """Assess a whole publication plan. Pure, and cheap enough to always run.

    Every variable is assessed, including those after the first failure: a run
    about to be abandoned should report every reason at once rather than
    surfacing them one relaunch at a time.
    """
    target = grid_descriptor(cube_grid)
    assessments: list[tuple[str, PlacementAssessment]] = []
    for plan in planned:
        if not isinstance(plan, PlannedPublication):
            raise TypeError("preflight requires PlannedPublication values")
        assessments.append((plan.variable, assess_array_placement(
            plan.variable, plan.shape, plan.source, target)))
    return PublicationPreflight(tuple(assessments))


def preflight_targets(cube_grid: SimulationGrid, targets: Sequence[str],
                      *, shape: tuple[int, int],
                      source: GridDescriptor | None = None
                      ) -> PublicationPreflight:
    """Convenience for `outputs.targets`, which share one producer grid."""
    return preflight_publications(
        cube_grid,
        [PlannedPublication(name, shape, source) for name in targets])


__all__ = [
    "PlannedPublication",
    "PublicationPreflight",
    "grid_descriptor",
    "preflight_publications",
    "preflight_targets",
]
