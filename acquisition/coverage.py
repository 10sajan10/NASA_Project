"""Single-source coverage assessment with explicit, non-negotiable gaps.

Stage 5 claims exactly one coverage capability: assets *from one source* that
share a CRS and axis order and that jointly contain the requested region and
window.  Coverage is assessed over the Cartesian space-time product, not as
independent spatial and temporal projections.  A hole anywhere in that product
is a ``GAP`` and stops the binding.  It is never filled, never interpolated
across, and never rounded away, because a partially covered artifact that
reports itself complete is the most damaging failure this layer could have.

Cross-provider mosaics and coverage atoms are deliberately out of scope.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Any, Iterable, Sequence

from contracts import BBoxSupport, TemporalKind, TemporalSupport
from contracts.identity import decimal_value
from engine.runtime.identity import require_object_fields

from .manifest import AssetRef, bbox_union_covers, temporal_union_covers
from .schema import AssemblyMode


class CoverageStatus(str, Enum):
    COMPLETE = "COMPLETE"
    NO_ASSETS = "NO_ASSETS"
    SPATIAL_GAP = "SPATIAL_GAP"
    TEMPORAL_GAP = "TEMPORAL_GAP"
    SPATIOTEMPORAL_GAP = "SPATIOTEMPORAL_GAP"
    CRS_MISMATCH = "CRS_MISMATCH"
    TEMPORAL_CONTRACT_MISMATCH = "TEMPORAL_CONTRACT_MISMATCH"
    ASSEMBLY_UNSUPPORTED = "ASSEMBLY_UNSUPPORTED"


@dataclass(frozen=True)
class CoverageAssessment:
    """The verdict on whether a candidate set can satisfy a request."""

    status: CoverageStatus
    selected_asset_ids: tuple[str, ...]
    halo_asset_ids: tuple[str, ...]
    detail: str

    def __post_init__(self) -> None:
        if not isinstance(self.status, CoverageStatus):
            raise TypeError("coverage status must be typed")
        for values, label in ((self.selected_asset_ids, "selected asset IDs"),
                              (self.halo_asset_ids, "halo asset IDs")):
            if (not isinstance(values, tuple)
                    or any(not isinstance(item, str) or not item
                           for item in values)):
                raise TypeError(f"{label} must be a text tuple")
            if values != tuple(sorted(set(values))):
                raise ValueError(f"{label} must be unique and sorted")
        if set(self.halo_asset_ids) - set(self.selected_asset_ids):
            raise ValueError("halo assets must also be selected assets")
        if self.status is CoverageStatus.COMPLETE and not self.selected_asset_ids:
            raise ValueError("complete coverage requires at least one asset")
        if self.status is not CoverageStatus.COMPLETE and not self.detail:
            raise ValueError("an incomplete coverage verdict needs a reason")

    @property
    def complete(self) -> bool:
        return self.status is CoverageStatus.COMPLETE

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "selected_asset_ids": list(self.selected_asset_ids),
            "halo_asset_ids": list(self.halo_asset_ids),
            "detail": self.detail,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "CoverageAssessment":
        raw = require_object_fields(
            value, {field.name for field in dataclasses.fields(cls)},
            "CoverageAssessment")
        raw["status"] = CoverageStatus(raw["status"])
        for name in ("selected_asset_ids", "halo_asset_ids"):
            if not isinstance(raw[name], list):
                raise ValueError(f"CoverageAssessment.{name} must be an array")
            raw[name] = tuple(raw[name])
        return cls(**raw)


def expand_bbox(box: BBoxSupport, halo: str | Decimal) -> BBoxSupport:
    """Grow a bbox by a halo in CRS units, for stencil-hungry consumers.

    Interpolation and regridding read outside the target cells.  Requesting the
    halo at *acquisition* time is what stops a later transform from silently
    producing edge values it had no inputs for.
    """
    margin = halo if isinstance(halo, Decimal) else decimal_value(halo)
    if margin < 0:
        raise ValueError("halo cannot be negative")
    bounds = tuple(decimal_value(item) for item in box.bounds)
    return BBoxSupport(
        box.crs, box.axis_order,
        (str(bounds[0] - margin), str(bounds[1] - margin),
         str(bounds[2] + margin), str(bounds[3] + margin)),
        box.coverage_complete,
    )


def _intersects(left: BBoxSupport, right: BBoxSupport) -> bool:
    if left.crs != right.crs or left.axis_order != right.axis_order:
        return False
    a = tuple(decimal_value(item) for item in left.bounds)
    b = tuple(decimal_value(item) for item in right.bounds)
    return not (a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1])


def _temporal_intersects(left: TemporalSupport, right: TemporalSupport) -> bool:
    if (left.kind is TemporalKind.TIME_INVARIANT
            or right.kind is TemporalKind.TIME_INVARIANT):
        return left.kind is right.kind
    from contracts.identity import timestamp_value
    return not (timestamp_value(left.end) <= timestamp_value(right.start)
                or timestamp_value(right.end) <= timestamp_value(left.start))


def assess_coverage(
    assets: Iterable[AssetRef],
    *,
    target_spatial: BBoxSupport,
    target_temporal: TemporalSupport,
    halo: str | Decimal = "0",
    assembly_mode: AssemblyMode | None = None,
) -> CoverageAssessment:
    """Choose the assets that cover a request, or explain the gap.

    Selection keeps every asset that intersects the halo-expanded target and
    verifies containment of that expanded target.  For a temporal series the
    check partitions the requested window at every asset boundary and requires
    full spatial coverage during every resulting interval.  This prevents two
    assets whose spatial and temporal *projections* look complete from hiding
    holes in their Cartesian product.

    Assets retained only for the halo are reported separately so a consumer
    can tell which inputs exist for edge support rather than for the answer
    itself.  A non-zero requested halo is mandatory input coverage, not merely
    a hint used during candidate selection.
    """
    if not isinstance(target_spatial, BBoxSupport):
        raise TypeError("target_spatial must be BBoxSupport")
    if not isinstance(target_temporal, TemporalSupport):
        raise TypeError("target_temporal must be TemporalSupport")
    if assembly_mode is not None and not isinstance(
            assembly_mode, AssemblyMode):
        raise TypeError("assembly_mode must be typed when supplied")
    values = tuple(assets)
    if not values:
        return CoverageAssessment(
            CoverageStatus.NO_ASSETS, (), (),
            "no candidate assets were discovered for this request")

    matching_crs = tuple(
        item for item in values
        if item.extent.spatial.crs == target_spatial.crs
        and item.extent.spatial.axis_order == target_spatial.axis_order)
    if not matching_crs:
        return CoverageAssessment(
            CoverageStatus.CRS_MISMATCH, (), (),
            f"no asset declares CRS {target_spatial.crs} with the requested "
            "axis order; Stage 5 does not reproject during acquisition")

    expanded = expand_bbox(target_spatial, halo)
    selected = tuple(
        item for item in matching_crs
        if _intersects(item.extent.spatial, expanded)
        and _temporal_intersects(item.extent.temporal, target_temporal))
    if not selected:
        return CoverageAssessment(
            CoverageStatus.NO_ASSETS, (), (),
            "no discovered asset intersects the requested region and window")

    if not bbox_union_covers(selected, expanded):
        return CoverageAssessment(
            CoverageStatus.SPATIAL_GAP, (), (),
            "the discovered assets leave a hole in the requested region or "
            "its required halo; "
            "Stage 5 refuses to register a partially covered artifact")
    if not temporal_union_covers(selected, target_temporal):
        return CoverageAssessment(
            CoverageStatus.TEMPORAL_GAP, (), (),
            "the discovered assets leave a hole in the requested window")

    if not _space_time_product_covers(
            selected, expanded, target_temporal):
        return CoverageAssessment(
            CoverageStatus.SPATIOTEMPORAL_GAP, (), (),
            "spatial and temporal projections are individually complete, "
            "but the assets leave a hole in the requested space-time product")

    temporal_failures = tuple(
        item.asset_id for item in selected
        if not _temporal_contract_covers(
            item.extent.temporal, target_temporal))
    if temporal_failures:
        return CoverageAssessment(
            CoverageStatus.TEMPORAL_CONTRACT_MISMATCH, (), (),
            "selected assets do not establish the requested cadence, "
            "alignment, maximum-gap, or sample-semantics contract: "
            + ", ".join(sorted(temporal_failures)))

    if assembly_mode is not None and not _assembly_is_executable(
            selected, expanded, target_temporal, assembly_mode):
        return CoverageAssessment(
            CoverageStatus.ASSEMBLY_UNSUPPORTED, (), (),
            f"asset layout is covered but cannot be assembled by the closed "
            f"runtime mode {assembly_mode.value}")

    core = tuple(
        item.asset_id for item in selected
        if _intersects(item.extent.spatial, target_spatial))
    halo_only = tuple(sorted(
        {item.asset_id for item in selected} - set(core)))
    ordered = tuple(sorted(item.asset_id for item in selected))
    return CoverageAssessment(
        CoverageStatus.COMPLETE, ordered, halo_only,
        "")


def _temporal_contract_covers(
    offered: TemporalSupport,
    requested: TemporalSupport,
) -> bool:
    """Whether one asset can preserve the requested timeline without work.

    Acquisition has no implicit resampler.  A finer cadence may be useful to a
    later explicit transform, but it cannot be labelled as the requested
    cadence by materialisation alone, so cadence is exact here.
    """
    if (offered.kind is not requested.kind
            or offered.sample_semantics is not requested.sample_semantics):
        return False
    if requested.kind is TemporalKind.TIME_INVARIANT:
        return True

    from contracts.identity import timestamp_value

    if (timestamp_value(offered.start) > timestamp_value(requested.start)
            or timestamp_value(offered.end) < timestamp_value(requested.end)):
        return False
    if requested.cadence_s is None or offered.cadence_s is None:
        # A series whose sampling is unspecified cannot establish an exact
        # executable timeline.
        return False
    cadence = decimal_value(requested.cadence_s)
    if decimal_value(offered.cadence_s) != cadence:
        return False
    requested_anchor = requested.anchor or requested.start
    offered_anchor = offered.anchor or offered.start
    delta = Decimal(str((timestamp_value(requested_anchor)
                         - timestamp_value(offered_anchor)).total_seconds()))
    if delta % cadence != 0:
        return False
    if requested.max_gap_s is not None:
        if (offered.max_gap_s is None
                or decimal_value(offered.max_gap_s)
                > decimal_value(requested.max_gap_s)):
            return False
    elif offered.max_gap_s is not None and decimal_value(offered.max_gap_s) > 0:
        # The derived descriptor would otherwise omit a known gap bound.
        return False
    if (requested.reference_time is not None
            and offered.reference_time != requested.reference_time):
        return False
    return True


def _assembly_is_executable(
    assets: Sequence[AssetRef],
    spatial: BBoxSupport,
    temporal: TemporalSupport,
    mode: AssemblyMode,
) -> bool:
    """Mirror the layouts implemented by the local materialiser.

    This is intentionally stricter than geometric coverage.  In particular,
    temporal mosaics are refused: the current runtime concatenates JSON field
    tiles only along x and requires identical y/time axes.
    """
    if mode is AssemblyMode.SINGLE_ASSET:
        return len(assets) == 1 and bbox_union_covers(assets, spatial) \
            and _temporal_contract_covers(
                assets[0].extent.temporal, temporal)

    if mode is not AssemblyMode.FIELD_JSON_X_TILES_V1:
        return False
    if not assets:
        return False
    reference_time = assets[0].extent.temporal
    if any(item.extent.temporal != reference_time for item in assets):
        return False
    bounds = [tuple(decimal_value(value)
                    for value in item.extent.spatial.bounds)
              for item in assets]
    # Runtime concatenation assumes identical y coordinates and strictly
    # increasing, non-overlapping x coordinates.
    if any((item[1], item[3]) != (bounds[0][1], bounds[0][3])
           for item in bounds):
        return False
    ordered = sorted(bounds, key=lambda item: (item[0], item[2]))
    if any(left[2] != right[0]
           for left, right in zip(ordered, ordered[1:])):
        return False
    target = tuple(decimal_value(value) for value in spatial.bounds)
    return (ordered[0][0] <= target[0]
            and ordered[-1][2] >= target[2]
            and bounds[0][1] <= target[1]
            and bounds[0][3] >= target[3])


def _space_time_product_covers(
    assets: Sequence[AssetRef],
    spatial: BBoxSupport,
    temporal: TemporalSupport,
) -> bool:
    """Return whether one asset covers every point of each time slab.

    Asset interval endpoints plus the target endpoints form a finite exact
    partition.  Within one open slab the active asset set is constant, so a
    two-dimensional union check for every slab is equivalent to coverage of
    the full three-dimensional Cartesian product.  Boundary points are covered
    by the adjacent closed extents under the manifest's interval convention.
    """
    if temporal.kind is TemporalKind.TIME_INVARIANT:
        invariant = tuple(
            item for item in assets
            if item.extent.temporal.kind is TemporalKind.TIME_INVARIANT)
        return bbox_union_covers(invariant, spatial)

    from contracts.identity import timestamp_value

    start = timestamp_value(temporal.start)
    end = timestamp_value(temporal.end)
    boundaries = {start, end}
    intervals: list[tuple[AssetRef, Any, Any]] = []
    for asset in assets:
        offered = asset.extent.temporal
        if offered.kind is TemporalKind.TIME_INVARIANT:
            # The Stage-5 contract does not silently reinterpret a static
            # field as a sampled series.
            continue
        offered_start = max(timestamp_value(offered.start), start)
        offered_end = min(timestamp_value(offered.end), end)
        if offered_start < offered_end:
            intervals.append((asset, offered_start, offered_end))
            boundaries.add(offered_start)
            boundaries.add(offered_end)

    ordered = sorted(boundaries)
    for slab_start, slab_end in zip(ordered, ordered[1:]):
        if slab_start >= slab_end:
            continue
        active = tuple(
            asset for asset, offered_start, offered_end in intervals
            if offered_start <= slab_start and offered_end >= slab_end)
        if not active or not bbox_union_covers(active, spatial):
            return False
    return bool(intervals)


def order_assets(assets: Sequence[AssetRef],
                 asset_ids: Iterable[str]) -> tuple[AssetRef, ...]:
    """Return the named assets in a deterministic, reproducible order."""
    wanted = set(asset_ids)
    by_id = {item.asset_id: item for item in assets}
    missing = wanted - set(by_id)
    if missing:
        raise KeyError(f"assets are not in the candidate set: {sorted(missing)}")
    return tuple(by_id[item] for item in sorted(wanted))


__all__ = [
    "CoverageAssessment",
    "CoverageStatus",
    "assess_coverage",
    "expand_bbox",
    "order_assets",
]
