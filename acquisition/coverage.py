"""Single-source coverage assessment with explicit, non-negotiable gaps.

Stage 5 claims exactly one coverage capability: assets *from one source* that
share a CRS and axis order and that jointly contain the requested region and
window.  A hole anywhere in that union is a ``GAP`` and stops the binding.  It
is never filled, never interpolated across, and never rounded away, because a
partially covered artifact that reports itself complete is the most damaging
failure this layer could have.

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


class CoverageStatus(str, Enum):
    COMPLETE = "COMPLETE"
    NO_ASSETS = "NO_ASSETS"
    SPATIAL_GAP = "SPATIAL_GAP"
    TEMPORAL_GAP = "TEMPORAL_GAP"
    CRS_MISMATCH = "CRS_MISMATCH"


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
) -> CoverageAssessment:
    """Choose the assets that cover a request, or explain the gap.

    Selection keeps every asset that intersects the halo-expanded target, then
    verifies containment of the *unexpanded* target.  Assets retained only for
    the halo are reported separately so a consumer can tell which inputs exist
    for edge support rather than for the answer itself.
    """
    if not isinstance(target_spatial, BBoxSupport):
        raise TypeError("target_spatial must be BBoxSupport")
    if not isinstance(target_temporal, TemporalSupport):
        raise TypeError("target_temporal must be TemporalSupport")
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

    if not bbox_union_covers(selected, target_spatial):
        return CoverageAssessment(
            CoverageStatus.SPATIAL_GAP, (), (),
            "the discovered assets leave a hole in the requested region; "
            "Stage 5 refuses to register a partially covered artifact")
    if not temporal_union_covers(selected, target_temporal):
        return CoverageAssessment(
            CoverageStatus.TEMPORAL_GAP, (), (),
            "the discovered assets leave a hole in the requested window")

    core = tuple(
        item.asset_id for item in selected
        if _intersects(item.extent.spatial, target_spatial))
    halo_only = tuple(sorted(
        {item.asset_id for item in selected} - set(core)))
    ordered = tuple(sorted(item.asset_id for item in selected))
    return CoverageAssessment(
        CoverageStatus.COMPLETE, ordered, halo_only,
        "")


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
