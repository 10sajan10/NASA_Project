"""Exact manifest binding, staleness detection, and honest exclusion.

Binding is the moment a plan stops talking about "some coarse data from that
provider" and commits to *these* bytes at *these* versions.  Everything before
it is metadata; everything after it is allowed to move payload.

Two refusals live here and neither has an escape hatch:

* a candidate with no conditional identity is ``UNBINDABLE`` — it can only
  enter science through the quarantined ingestion path; and
* a bound asset that has since vanished or changed ends the plan with
  ``BINDING_STALE`` and a child plan that *records* the exclusion.  Nothing
  substitutes a replacement at runtime, because a run that quietly swaps its
  inputs is no longer the run that was reviewed.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Any, Iterable, Mapping

from capabilities.implementation import _digest, _required_text
from contracts import BBoxSupport, TemporalSupport
from engine.runtime.identity import (
    freeze_json,
    require_object_fields,
    strict_copy,
    strict_hash,
)

from .connector import (
    AssetCandidate,
    MetadataQuery,
    _mint_authorization,
)
from .coverage import CoverageAssessment, assess_coverage
from .manifest import (
    AssetManifest,
    AssetRef,
    ManifestShardStore,
    DEFAULT_SHARD_SIZE,
)


class BindingRejectionCode(str, Enum):
    UNBINDABLE_NO_CONDITIONAL_IDENTITY = "UNBINDABLE_NO_CONDITIONAL_IDENTITY"
    COVERAGE_GAP = "COVERAGE_GAP"
    NO_CANDIDATES = "NO_CANDIDATES"


@dataclass(frozen=True)
class BindingRejection:
    code: BindingRejectionCode
    subject_ids: tuple[str, ...]
    detail: str

    def __post_init__(self) -> None:
        if not isinstance(self.code, BindingRejectionCode):
            raise TypeError("binding rejection code must be typed")
        if (not isinstance(self.subject_ids, tuple)
                or any(not isinstance(item, str) or not item
                       for item in self.subject_ids)):
            raise TypeError("binding rejection subjects must be a text tuple")
        if self.subject_ids != tuple(sorted(set(self.subject_ids))):
            raise ValueError("binding rejection subjects must be unique/sorted")
        _required_text(self.detail, "binding rejection detail")

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code.value,
            "subject_ids": list(self.subject_ids),
            "detail": self.detail,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "BindingRejection":
        raw = require_object_fields(
            value, {field.name for field in dataclasses.fields(cls)},
            "BindingRejection")
        raw["code"] = BindingRejectionCode(raw["code"])
        if not isinstance(raw["subject_ids"], list):
            raise ValueError("BindingRejection.subject_ids must be an array")
        raw["subject_ids"] = tuple(raw["subject_ids"])
        return cls(**raw)


@dataclass(frozen=True)
class BoundAssetManifest:
    """A manifest plus the coverage verdict that justified binding it."""

    manifest: AssetManifest
    coverage: CoverageAssessment
    query_payload: dict[str, Any]

    def __post_init__(self) -> None:
        if not isinstance(self.manifest, AssetManifest):
            raise TypeError("bound manifest requires an AssetManifest")
        if not isinstance(self.coverage, CoverageAssessment):
            raise TypeError("bound manifest requires a CoverageAssessment")
        if not self.coverage.complete:
            raise ValueError(
                "a manifest cannot be bound over incomplete coverage")
        if not isinstance(self.query_payload, dict):
            raise TypeError("bound manifest query payload must be an object")
        # Detach and freeze before doing any identity checks.  ``frozen=True``
        # on a dataclass does not make a nested dict immutable; without this a
        # caller could mutate the query after binding and relabel the same
        # manifest root as a different scientific concept.
        object.__setattr__(self, "query_payload", freeze_json(
            self.query_payload))
        query = MetadataQuery.from_dict(strict_copy(self.query_payload))
        if query.query_id != self.manifest.query_id:
            raise ValueError(
                "bound query identity does not match the manifest query_id")
        if query.source_id != self.manifest.source_id:
            raise ValueError(
                "bound query source does not match the manifest source")
        if self.coverage.selected_asset_ids != tuple(sorted(set(
                self.coverage.selected_asset_ids))):
            raise ValueError("bound asset IDs must be unique and sorted")
        if len(self.coverage.selected_asset_ids) != self.manifest.asset_count:
            raise ValueError(
                "coverage asset count does not match the exact manifest")

    @property
    def manifest_root(self) -> str:
        return self.manifest.manifest_root

    @property
    def source_id(self) -> str:
        return self.manifest.source_id

    @property
    def asset_ids(self) -> tuple[str, ...]:
        return self.coverage.selected_asset_ids

    def authorization(self):
        """Mint the token that lets a connector release payload bytes.

        This is the only constructor of a :class:`FetchAuthorization`, which is
        why "bind before transfer" holds structurally rather than by review.
        """
        return _mint_authorization(
            self.manifest_root, self.source_id, frozenset(self.asset_ids))

    def to_dict(self) -> dict[str, Any]:
        return {
            "manifest": self.manifest.to_dict(),
            "coverage": self.coverage.to_dict(),
            "query_payload": strict_copy(self.query_payload),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "BoundAssetManifest":
        raw = require_object_fields(
            value, {"manifest", "coverage", "query_payload"},
            "BoundAssetManifest")
        raw["manifest"] = AssetManifest.from_dict(raw["manifest"])
        raw["coverage"] = CoverageAssessment.from_dict(raw["coverage"])
        return cls(**raw)


@dataclass(frozen=True)
class BindingResult:
    """Either a bound manifest or the typed reasons there is none."""

    bound: BoundAssetManifest | None
    rejections: tuple[BindingRejection, ...]
    unbindable_asset_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.bound is not None and not isinstance(
                self.bound, BoundAssetManifest):
            raise TypeError("binding result bound value is invalid")
        if (not isinstance(self.rejections, tuple)
                or not all(isinstance(item, BindingRejection)
                           for item in self.rejections)):
            raise TypeError("binding rejections are invalid")
        if (not isinstance(self.unbindable_asset_ids, tuple)
                or self.unbindable_asset_ids != tuple(sorted(set(
                    self.unbindable_asset_ids)))):
            raise ValueError("unbindable asset IDs must be unique and sorted")
        if self.bound is None and not self.rejections:
            raise ValueError("a failed binding must explain itself")

    @property
    def ok(self) -> bool:
        return self.bound is not None


def bind_manifest(
    candidates: Iterable[AssetCandidate],
    *,
    query: MetadataQuery,
    store: ManifestShardStore,
    target_spatial: BBoxSupport,
    target_temporal: TemporalSupport,
    halo: str | Decimal = "0",
    shard_size: int = DEFAULT_SHARD_SIZE,
) -> BindingResult:
    """Freeze an exact manifest for one query, or refuse with typed reasons.

    No payload byte moves in this function.  It reads descriptions, decides
    which assets are pinnable and whether they cover the request, and seals the
    answer.
    """
    if not isinstance(query, MetadataQuery):
        raise TypeError("bind_manifest requires a MetadataQuery")
    values = tuple(candidates)
    if not values:
        return BindingResult(None, (BindingRejection(
            BindingRejectionCode.NO_CANDIDATES, (),
            "metadata search returned no candidates for this query"),), ())

    bindable: list[AssetRef] = []
    unbindable: list[str] = []
    for candidate in values:
        if candidate.conditional_identity is None:
            unbindable.append(candidate.asset_id)
            continue
        bindable.append(AssetRef(
            asset_id=candidate.asset_id,
            source_id=query.source_id,
            locator=candidate.locator,
            conditional_identity=candidate.conditional_identity,
            extent=candidate.extent,
            byte_size=candidate.byte_size,
        ))

    rejections: list[BindingRejection] = []
    if unbindable:
        rejections.append(BindingRejection(
            BindingRejectionCode.UNBINDABLE_NO_CONDITIONAL_IDENTITY,
            tuple(sorted(unbindable)),
            "the provider offers no ETag, version, or checksum for these "
            "assets, so a scientific run cannot pin them; use a quarantined "
            "SnapshotIngestionPlan to content-address them first"))
    if not bindable:
        return BindingResult(None, tuple(rejections), tuple(sorted(unbindable)))

    coverage = assess_coverage(
        bindable, target_spatial=target_spatial,
        target_temporal=target_temporal, halo=halo)
    if not coverage.complete:
        rejections.append(BindingRejection(
            BindingRejectionCode.COVERAGE_GAP, (), coverage.detail))
        return BindingResult(None, tuple(rejections), tuple(sorted(unbindable)))

    selected = set(coverage.selected_asset_ids)
    ordered = tuple(sorted((item for item in bindable
                            if item.asset_id in selected),
                           key=lambda item: item.asset_id))
    manifest = AssetManifest.freeze(
        source_id=query.source_id, query_id=query.query_id, assets=ordered,
        store=store, shard_size=shard_size)
    return BindingResult(
        BoundAssetManifest(manifest, coverage, query.to_dict()),
        tuple(rejections), tuple(sorted(unbindable)))


class BindingStatus(str, Enum):
    FRESH = "FRESH"
    BINDING_STALE = "BINDING_STALE"


class StaleReasonCode(str, Enum):
    ASSET_MISSING = "ASSET_MISSING"
    ASSET_MUTATED = "ASSET_MUTATED"


@dataclass(frozen=True)
class StaleReason:
    code: StaleReasonCode
    asset_id: str
    expected: str
    observed: str

    def __post_init__(self) -> None:
        if not isinstance(self.code, StaleReasonCode):
            raise TypeError("stale reason code must be typed")
        _required_text(self.asset_id, "stale reason asset_id")
        _required_text(self.expected, "stale reason expected value")
        if self.code is StaleReasonCode.ASSET_MISSING:
            if self.observed:
                raise ValueError("a missing asset has no observed version")
        else:
            _required_text(self.observed, "stale reason observed value")

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code.value,
            "asset_id": self.asset_id,
            "expected": self.expected,
            "observed": self.observed,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "StaleReason":
        raw = require_object_fields(
            value, {field.name for field in dataclasses.fields(cls)},
            "StaleReason")
        raw["code"] = StaleReasonCode(raw["code"])
        return cls(**raw)


@dataclass(frozen=True)
class BindingVerification:
    status: BindingStatus
    reasons: tuple[StaleReason, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.status, BindingStatus):
            raise TypeError("binding status must be typed")
        if (not isinstance(self.reasons, tuple)
                or not all(isinstance(item, StaleReason)
                           for item in self.reasons)):
            raise TypeError("binding verification reasons are invalid")
        if (self.status is BindingStatus.BINDING_STALE) != bool(self.reasons):
            raise ValueError("binding status must agree with its reasons")

    @property
    def fresh(self) -> bool:
        return self.status is BindingStatus.FRESH

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "reasons": [item.to_dict() for item in self.reasons],
        }


def verify_binding(bound: BoundAssetManifest, store: ManifestShardStore,
                   observed: Mapping[str, str | None]) -> BindingVerification:
    """Compare a bound manifest against currently observed asset versions.

    ``observed`` maps asset ID to its current conditional-identity value, or to
    ``None`` when the provider no longer offers it.  Streaming the manifest
    keeps this bounded for large bindings.
    """
    reasons: list[StaleReason] = []
    for asset in bound.manifest.iter_assets(store):
        if asset.asset_id not in bound.coverage.selected_asset_ids:
            continue
        expected = asset.conditional_identity.value
        current = observed.get(asset.asset_id, None)
        if current is None:
            reasons.append(StaleReason(
                StaleReasonCode.ASSET_MISSING, asset.asset_id, expected, ""))
        elif current != expected:
            reasons.append(StaleReason(
                StaleReasonCode.ASSET_MUTATED, asset.asset_id, expected,
                current))
    if reasons:
        return BindingVerification(
            BindingStatus.BINDING_STALE,
            tuple(sorted(reasons, key=lambda item: item.asset_id)))
    return BindingVerification(BindingStatus.FRESH, ())


@dataclass(frozen=True)
class ExclusionChildPlan:
    """A record that some assets may not be used again, and why.

    This is planning input for a *new* session, never a runtime substitution.
    The parent manifest root is retained so the exclusion is attributable to
    the exact binding that failed rather than to a source in general.
    """

    child_plan_id: str
    parent_manifest_root: str
    excluded_asset_ids: tuple[str, ...]
    reasons: tuple[StaleReason, ...]

    def __post_init__(self) -> None:
        _digest(self.child_plan_id, "exclusion child_plan_id")
        _digest(self.parent_manifest_root, "parent manifest_root")
        if (not isinstance(self.excluded_asset_ids, tuple)
                or not self.excluded_asset_ids
                or self.excluded_asset_ids != tuple(sorted(set(
                    self.excluded_asset_ids)))):
            raise ValueError(
                "excluded asset IDs must be a non-empty unique sorted tuple")
        if (not isinstance(self.reasons, tuple) or not self.reasons
                or not all(isinstance(item, StaleReason)
                           for item in self.reasons)):
            raise TypeError("an exclusion plan needs typed stale reasons")
        if self.child_plan_id != self.expected_id():
            raise ValueError("exclusion child plan identity does not verify")

    def expected_id(self) -> str:
        return strict_hash(self._payload(
            self.parent_manifest_root, self.excluded_asset_ids, self.reasons))

    @staticmethod
    def _payload(parent_manifest_root: str,
                 excluded_asset_ids: tuple[str, ...],
                 reasons: tuple[StaleReason, ...]) -> dict[str, Any]:
        return {
            "schema": "stage5-exclusion-child-plan-v1",
            "parent_manifest_root": parent_manifest_root,
            "excluded_asset_ids": list(excluded_asset_ids),
            "reasons": [item.to_dict() for item in reasons],
        }

    def to_dict(self) -> dict[str, Any]:
        payload = self._payload(
            self.parent_manifest_root, self.excluded_asset_ids, self.reasons)
        payload["child_plan_id"] = self.child_plan_id
        return payload


def derive_exclusion_plan(bound: BoundAssetManifest,
                          verification: BindingVerification
                          ) -> ExclusionChildPlan:
    """Turn a stale verification into a durable exclusion record."""
    if verification.fresh:
        raise ValueError("a fresh binding has nothing to exclude")
    excluded = tuple(sorted({item.asset_id for item in verification.reasons}))
    reasons = tuple(sorted(verification.reasons, key=lambda item: item.asset_id))
    return ExclusionChildPlan(
        strict_hash(ExclusionChildPlan._payload(
            bound.manifest_root, excluded, reasons)),
        bound.manifest_root, excluded, reasons)


__all__ = [
    "BindingRejection",
    "BindingRejectionCode",
    "BindingResult",
    "BindingStatus",
    "BindingVerification",
    "BoundAssetManifest",
    "ExclusionChildPlan",
    "StaleReason",
    "StaleReasonCode",
    "bind_manifest",
    "derive_exclusion_plan",
    "verify_binding",
]
