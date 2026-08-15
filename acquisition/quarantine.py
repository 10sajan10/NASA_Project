"""The quarantined bootstrap path for weakly identified sources.

Some providers genuinely cannot version their bytes: no ETag, no version ID, no
published checksum.  Refusing them outright would be dishonest in the other
direction — the data exists, and a scientist may legitimately want it.

What Stage 5 refuses is letting those bytes *silently* satisfy a scientific
requirement.  They enter through a separate plan that content-hashes what it
actually received and commits the result as an ``ArtifactLeaf``.  Only a later
planning session, resolving against that committed leaf, can use them — and by
then the thing being used is a content-addressed artifact with a manifest root,
not an unversioned remote URL.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable

from capabilities import ArtifactLeaf
from capabilities.implementation import _digest, _required_text
from contracts import ArtifactDescriptor
from engine.runtime.identity import require_object_fields, strict_hash

from .connector import AssetCandidate
from .manifest import (
    AssetConditionalIdentity,
    AssetManifest,
    AssetRef,
    ConditionalIdentityKind,
    ManifestShardStore,
)


class IngestionStatus(str, Enum):
    PLANNED = "PLANNED"
    COMMITTED = "COMMITTED"
    REJECTED = "REJECTED"


@dataclass(frozen=True)
class SnapshotIngestionPlan:
    """A quarantined transfer whose bytes cannot yet satisfy science.

    The plan names exactly which weakly identified candidates it intends to
    pull.  It carries no scientific requirement and produces no satisfier; its
    only product is a content-addressed artifact that a *future* session may
    consider.
    """

    plan_id: str
    source_id: str
    candidate_asset_ids: tuple[str, ...]
    reason: str

    def __post_init__(self) -> None:
        _digest(self.plan_id, "ingestion plan_id")
        _required_text(self.source_id, "ingestion source_id")
        if (not isinstance(self.candidate_asset_ids, tuple)
                or not self.candidate_asset_ids
                or self.candidate_asset_ids != tuple(sorted(set(
                    self.candidate_asset_ids)))):
            raise ValueError(
                "ingestion candidates must be a non-empty unique sorted tuple")
        _required_text(self.reason, "ingestion reason")
        if self.plan_id != self.expected_id():
            raise ValueError("ingestion plan identity does not verify")

    @classmethod
    def bind(cls, *, source_id: str, candidates: Iterable[AssetCandidate],
             reason: str) -> "SnapshotIngestionPlan":
        values = tuple(candidates)
        strong = [item.asset_id for item in values
                  if item.conditional_identity is not None]
        if strong:
            raise ValueError(
                "quarantine is for weakly identified assets only; "
                f"{sorted(strong)} already carry a conditional identity")
        asset_ids = tuple(sorted({item.asset_id for item in values}))
        return cls(
            strict_hash(cls._payload(source_id, asset_ids, reason)),
            source_id, asset_ids, reason)

    @staticmethod
    def _payload(source_id: str, candidate_asset_ids: tuple[str, ...],
                 reason: str) -> dict[str, Any]:
        return {
            "schema": "stage5-snapshot-ingestion-plan-v1",
            "source_id": source_id,
            "candidate_asset_ids": list(candidate_asset_ids),
            "reason": reason,
        }

    def expected_id(self) -> str:
        return strict_hash(self._payload(
            self.source_id, self.candidate_asset_ids, self.reason))

    def to_dict(self) -> dict[str, Any]:
        payload = self._payload(
            self.source_id, self.candidate_asset_ids, self.reason)
        payload["plan_id"] = self.plan_id
        return payload

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "SnapshotIngestionPlan":
        raw = require_object_fields(
            value,
            {"schema", "plan_id", "source_id", "candidate_asset_ids", "reason"},
            "SnapshotIngestionPlan")
        if raw.pop("schema") != "stage5-snapshot-ingestion-plan-v1":
            raise ValueError(
                "SnapshotIngestionPlan schema is not "
                "stage5-snapshot-ingestion-plan-v1")
        raw["candidate_asset_ids"] = tuple(raw["candidate_asset_ids"])
        return cls(**raw)


@dataclass(frozen=True)
class IngestionResult:
    """What quarantine produced: a committed leaf, or an explained refusal."""

    status: IngestionStatus
    plan_id: str
    leaf: ArtifactLeaf | None
    manifest: AssetManifest | None
    detail: str

    def __post_init__(self) -> None:
        if not isinstance(self.status, IngestionStatus):
            raise TypeError("ingestion status must be typed")
        _digest(self.plan_id, "ingestion result plan_id")
        if self.status is IngestionStatus.COMMITTED:
            if self.leaf is None or self.manifest is None:
                raise ValueError("a committed ingestion needs a leaf and manifest")
        elif self.leaf is not None:
            raise ValueError("only a committed ingestion may carry a leaf")
        if self.status is not IngestionStatus.COMMITTED and not self.detail:
            raise ValueError("a non-committed ingestion must explain itself")

    @property
    def committed(self) -> bool:
        return self.status is IngestionStatus.COMMITTED

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "plan_id": self.plan_id,
            "leaf": self.leaf.to_dict() if self.leaf is not None else None,
            "manifest": (
                self.manifest.to_dict() if self.manifest is not None else None),
            "detail": self.detail,
        }


def ingest_snapshot(
    plan: SnapshotIngestionPlan,
    candidates: Iterable[AssetCandidate],
    payloads: dict[str, bytes],
    descriptor: ArtifactDescriptor,
    *,
    shard_store: ManifestShardStore,
    artifact_id: str,
) -> IngestionResult:
    """Content-address quarantined bytes and commit them as an artifact leaf.

    Every byte this receives is hashed here and *becomes* its own conditional
    identity.  That is the whole point: the source could not version its data,
    so we version it ourselves at the moment we observed it, and we say so.
    """
    if not isinstance(plan, SnapshotIngestionPlan):
        raise TypeError("ingest_snapshot requires a SnapshotIngestionPlan")
    by_id = {item.asset_id: item for item in candidates}
    missing = [item for item in plan.candidate_asset_ids
               if item not in by_id or item not in payloads]
    if missing:
        return IngestionResult(
            IngestionStatus.REJECTED, plan.plan_id, None, None,
            f"quarantined payload is absent for {sorted(missing)}")

    refs: list[AssetRef] = []
    for asset_id in plan.candidate_asset_ids:
        candidate = by_id[asset_id]
        digest = hashlib.sha256(payloads[asset_id]).hexdigest()
        refs.append(AssetRef(
            asset_id=asset_id,
            source_id=plan.source_id,
            locator=candidate.locator,
            # Observed-at-ingest content identity: honest about *when* it was
            # pinned, and strong enough for a future session to detect drift.
            conditional_identity=AssetConditionalIdentity(
                ConditionalIdentityKind.CHECKSUM_SHA256, digest),
            extent=candidate.extent,
            byte_size=len(payloads[asset_id]),
        ))
    manifest = AssetManifest.freeze(
        source_id=plan.source_id, query_id=plan.plan_id, assets=tuple(refs),
        store=shard_store)
    leaf = ArtifactLeaf.bind(
        artifact_id=artifact_id,
        manifest_root_sha256=manifest.manifest_root,
        descriptor=descriptor,
    )
    return IngestionResult(
        IngestionStatus.COMMITTED, plan.plan_id, leaf, manifest, "")


__all__ = [
    "IngestionResult",
    "IngestionStatus",
    "SnapshotIngestionPlan",
    "ingest_snapshot",
]
