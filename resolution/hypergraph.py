"""Recursive construction of a finite scientific derivation hypergraph.

The builder in this module performs *discovery*, not selection or execution.
It starts from typed :class:`contracts.RequirementUse` roots, discovers every
directly compatible producer in a frozen Stage-2 catalog, and recursively
expands the input requirements of bound invocations.

Two identities are deliberately kept separate:

* equal ``Requirement`` values share one memoized discovery node; and
* every consumer port remains a distinct ``RequirementUseNode`` with its own
  cardinality, optional/default, distinctness, and sharing semantics.

Candidate cycles are retained as graph back-references.  A later selector must
choose an acyclic, grounded plan.  Search limits are fail-visible: hitting any
limit makes ``discovery_complete`` false and records the omitted frontier.

There is no domain-specific logic, I/O, scheduling, or model execution here.
"""
from __future__ import annotations

import dataclasses
import re
from dataclasses import dataclass, field
from enum import Enum
from functools import cached_property
from time import perf_counter_ns
from types import MappingProxyType
from typing import Any, Iterable

from capabilities import (
    ArtifactLeaf,
    BindingRejection,
    BoundInvocation,
    CapabilityCatalog,
    DeploymentCapabilitySnapshot,
    DeploymentFeasibilityProof,
    DeploymentRejection,
    DeploymentRejectionCode,
    SiteFeasibility,
    artifact_evidence_subject,
)
from contracts import (
    CompatibilityProof,
    EvidenceProfile,
    EvidenceSnapshot,
    Requirement,
    RequirementUse,
    direct_match,
)
from engine.runtime.identity import (
    freeze_json,
    require_object_fields,
    strict_copy,
    strict_hash,
)


_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_MAX_MILP_COST_UNITS = (1 << 31) - 1


def _digest(value: str, label: str) -> None:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")


def _text(value: str, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")


def _integer(value: int, label: str, *, minimum: int = 0) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{label} must be an integer >= {minimum}")


def _typed_tuple(values: tuple[Any, ...], expected: type, label: str) -> None:
    if (not isinstance(values, tuple)
            or not all(isinstance(value, expected) for value in values)):
        raise TypeError(f"{label} must be an immutable typed tuple")


class ArtifactCommitStatus(str, Enum):
    """Trusted availability state supplied by an external artifact index."""

    COMMITTED = "COMMITTED"
    UNAVAILABLE = "UNAVAILABLE"
    EVICTED = "EVICTED"


@dataclass(frozen=True)
class ArtifactCommitRecord:
    """Commit state for one leaf in a frozen trusted availability snapshot."""

    leaf_id: str
    status: ArtifactCommitStatus

    def __post_init__(self) -> None:
        _digest(self.leaf_id, "artifact commit leaf_id")
        if not isinstance(self.status, ArtifactCommitStatus):
            raise TypeError("artifact commit status must be typed")

    def to_dict(self) -> dict[str, Any]:
        return {"leaf_id": self.leaf_id, "status": self.status.value}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ArtifactCommitRecord":
        raw = require_object_fields(
            value, {field.name for field in dataclasses.fields(cls)},
            "ArtifactCommitRecord")
        raw["status"] = ArtifactCommitStatus(raw["status"])
        return cls(**raw)


@dataclass(frozen=True)
class ArtifactAvailabilitySnapshot:
    """Frozen trust boundary for artifact commit state.

    ``ArtifactLeaf`` is only a scientific declaration.  It is never promoted
    to a satisfier merely because a caller supplied hashes.  A committed record
    in this independently identified snapshot is required.
    """

    snapshot_id: str
    records: tuple[ArtifactCommitRecord, ...]

    def __post_init__(self) -> None:
        _digest(self.snapshot_id, "artifact availability snapshot_id")
        _typed_tuple(self.records, ArtifactCommitRecord,
                     "artifact availability records")
        if tuple(sorted(self.records, key=lambda item: item.leaf_id)) != self.records:
            raise ValueError("artifact availability records must be sorted")
        if len({item.leaf_id for item in self.records}) != len(self.records):
            raise ValueError("artifact availability records cannot repeat leaves")
        if not self.records:
            raise ValueError("artifact availability snapshot cannot be empty")
        if self.snapshot_id != self.expected_id():
            raise ValueError("artifact availability snapshot identity does not verify")

    @classmethod
    def freeze(cls, records: Iterable[ArtifactCommitRecord]
               ) -> "ArtifactAvailabilitySnapshot":
        frozen = tuple(sorted(records, key=lambda item: item.leaf_id))
        payload = {
            "schema": "stage3-artifact-availability-snapshot-v1",
            "records": [item.to_dict() for item in frozen],
        }
        return cls(strict_hash(payload), frozen)

    def expected_id(self) -> str:
        return strict_hash({
            "schema": "stage3-artifact-availability-snapshot-v1",
            "records": [item.to_dict() for item in self.records],
        })

    def status_for(self, leaf_id: str) -> ArtifactCommitStatus:
        try:
            return self._status_by_leaf[leaf_id]
        except KeyError as exc:
            raise KeyError(leaf_id) from exc

    @cached_property
    def _status_by_leaf(self):
        return MappingProxyType({
            item.leaf_id: item.status for item in self.records})

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "records": [item.to_dict() for item in self.records],
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]
                  ) -> "ArtifactAvailabilitySnapshot":
        raw = require_object_fields(
            value, {field.name for field in dataclasses.fields(cls)},
            "ArtifactAvailabilitySnapshot")
        if not isinstance(raw["records"], list):
            raise ValueError("ArtifactAvailabilitySnapshot.records must be an array")
        raw["records"] = tuple(
            ArtifactCommitRecord.from_dict(item) for item in raw["records"])
        return cls(**raw)


@dataclass(frozen=True)
class DiscoveryLimits:
    """Finite, explicit graph-construction budgets."""

    max_requirements: int = 10_000
    max_invocations: int = 50_000
    max_arcs: int = 250_000
    max_depth: int = 64
    max_candidates: int = 100_000

    def __post_init__(self) -> None:
        for name in (
            "max_requirements", "max_invocations", "max_arcs",
            "max_candidates",
        ):
            _integer(getattr(self, name), name, minimum=1)
        _integer(self.max_depth, "max_depth")

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "DiscoveryLimits":
        raw = require_object_fields(
            value, {field.name for field in dataclasses.fields(cls)},
            "DiscoveryLimits")
        return cls(**raw)


class DiscoveryLimitCode(str, Enum):
    MAX_REQUIREMENTS = "MAX_REQUIREMENTS"
    MAX_INVOCATIONS = "MAX_INVOCATIONS"
    MAX_ARCS = "MAX_ARCS"
    MAX_DEPTH = "MAX_DEPTH"
    MAX_CANDIDATES = "MAX_CANDIDATES"
    BINDING_ENUMERATION_INCOMPLETE = "BINDING_ENUMERATION_INCOMPLETE"


@dataclass(frozen=True)
class DiscoveryLimitReason:
    """A precise omitted frontier caused by a configured discovery bound."""

    code: DiscoveryLimitCode
    requirement_id: str
    use_id: str | None
    depth: int
    observed: int
    maximum: int
    detail: str

    def __post_init__(self) -> None:
        if not isinstance(self.code, DiscoveryLimitCode):
            raise TypeError("discovery limit code must be typed")
        _digest(self.requirement_id, "limited requirement_id")
        if self.use_id is not None:
            _digest(self.use_id, "limited use_id")
        _integer(self.depth, "limited depth")
        _integer(self.observed, "limit observed value")
        _integer(self.maximum, "limit maximum")
        _text(self.detail, "limit detail")

    @property
    def reason_id(self) -> str:
        return strict_hash(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code.value,
            "requirement_id": self.requirement_id,
            "use_id": self.use_id,
            "depth": self.depth,
            "observed": self.observed,
            "maximum": self.maximum,
            "detail": self.detail,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "DiscoveryLimitReason":
        raw = require_object_fields(
            value, {field.name for field in dataclasses.fields(cls)},
            "DiscoveryLimitReason")
        raw["code"] = DiscoveryLimitCode(raw["code"])
        return cls(**raw)


class BackReferenceKind(str, Enum):
    CYCLE = "CYCLE"
    MEMOIZED = "MEMOIZED"


@dataclass(frozen=True)
class BackReference:
    """An invocation input that points to an active or expanded requirement."""

    kind: BackReferenceKind
    invocation_id: str
    use_id: str
    requirement_id: str
    depth: int

    def __post_init__(self) -> None:
        if not isinstance(self.kind, BackReferenceKind):
            raise TypeError("back-reference kind must be typed")
        _digest(self.invocation_id, "back-reference invocation_id")
        _digest(self.use_id, "back-reference use_id")
        _digest(self.requirement_id, "back-reference requirement_id")
        _integer(self.depth, "back-reference depth")

    @property
    def reference_id(self) -> str:
        return strict_hash(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "invocation_id": self.invocation_id,
            "use_id": self.use_id,
            "requirement_id": self.requirement_id,
            "depth": self.depth,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "BackReference":
        raw = require_object_fields(
            value, {field.name for field in dataclasses.fields(cls)},
            "BackReference")
        raw["kind"] = BackReferenceKind(raw["kind"])
        return cls(**raw)


@dataclass(frozen=True)
class RequirementUseNode:
    """One distinct consumer port referencing a shared requirement node."""

    use_id: str
    use: RequirementUse
    owner_invocation_id: str | None = None

    def __post_init__(self) -> None:
        _digest(self.use_id, "requirement-use node ID")
        if not isinstance(self.use, RequirementUse):
            raise TypeError("requirement-use node requires typed RequirementUse")
        if self.use_id != self.use.requirement_use_id:
            raise ValueError("requirement-use node identity does not verify")
        if self.owner_invocation_id is not None:
            _digest(self.owner_invocation_id, "requirement-use owner")

    @property
    def requirement_id(self) -> str:
        return self.use.requirement.requirement_id

    @property
    def port_id(self) -> str:
        return self.use.port_id

    def to_dict(self) -> dict[str, Any]:
        return {
            "use_id": self.use_id,
            "use": self.use.to_dict(),
            "owner_invocation_id": self.owner_invocation_id,
        }

    @classmethod
    def bind(cls, use: RequirementUse, *, owner_invocation_id: str | None = None
             ) -> "RequirementUseNode":
        return cls(use.requirement_use_id, use, owner_invocation_id)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "RequirementUseNode":
        raw = require_object_fields(
            value, {field.name for field in dataclasses.fields(cls)},
            "RequirementUseNode")
        raw["use"] = RequirementUse.from_dict(raw["use"])
        return cls(**raw)


@dataclass(frozen=True)
class RequirementNode:
    """One normalized requirement value shared by all equivalent uses."""

    requirement_id: str
    requirement: Requirement
    use_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _digest(self.requirement_id, "requirement node ID")
        if not isinstance(self.requirement, Requirement):
            raise TypeError("requirement node requires a typed Requirement")
        if self.requirement_id != self.requirement.requirement_id:
            raise ValueError("requirement node identity does not verify")
        if (not isinstance(self.use_ids, tuple) or not self.use_ids
                or tuple(sorted(self.use_ids)) != self.use_ids
                or len(set(self.use_ids)) != len(self.use_ids)):
            raise ValueError("requirement node use IDs must be unique and sorted")
        for use_id in self.use_ids:
            _digest(use_id, "requirement node use_id")

    def to_dict(self) -> dict[str, Any]:
        return {
            "requirement_id": self.requirement_id,
            "requirement": self.requirement.to_dict(),
            "use_ids": list(self.use_ids),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "RequirementNode":
        raw = require_object_fields(
            value, {field.name for field in dataclasses.fields(cls)},
            "RequirementNode")
        raw["requirement"] = Requirement.from_dict(raw["requirement"])
        if not isinstance(raw["use_ids"], list):
            raise ValueError("RequirementNode.use_ids must be an array")
        raw["use_ids"] = tuple(raw["use_ids"])
        return cls(**raw)


@dataclass(frozen=True)
class InvocationNode:
    """An interned bound invocation admitted by static deployment checks."""

    invocation_id: str
    invocation: BoundInvocation

    def __post_init__(self) -> None:
        _digest(self.invocation_id, "invocation node ID")
        if not isinstance(self.invocation, BoundInvocation):
            raise TypeError("invocation node requires typed BoundInvocation")
        if self.invocation_id != self.invocation.invocation_key:
            raise ValueError("invocation node identity does not verify")

    @property
    def input_use_ids(self) -> tuple[str, ...]:
        return tuple(sorted(
            use.requirement_use_id for use in self.invocation.input_uses))

    @property
    def output_port_ids(self) -> tuple[str, ...]:
        return tuple(sorted(output.port_id for output in self.invocation.outputs))

    @property
    def cost_units(self) -> int:
        value = self.invocation.metric_estimates.get("cost_units")
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("invocation has no non-negative integer cost_units")
        return value

    def to_dict(self) -> dict[str, Any]:
        return {
            "invocation_id": self.invocation_id,
            "invocation": self.invocation.to_dict(),
        }

    @classmethod
    def bind(cls, invocation: BoundInvocation) -> "InvocationNode":
        return cls(invocation.invocation_key, invocation)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "InvocationNode":
        raw = require_object_fields(
            value, {field.name for field in dataclasses.fields(cls)},
            "InvocationNode")
        raw["invocation"] = BoundInvocation.from_dict(raw["invocation"])
        return cls(**raw)


@dataclass(frozen=True)
class ArtifactNode:
    """An examined artifact declaration plus trusted availability state."""

    leaf_id: str
    leaf: ArtifactLeaf
    availability_snapshot_id: str
    status: ArtifactCommitStatus

    def __post_init__(self) -> None:
        _digest(self.leaf_id, "artifact node ID")
        if not isinstance(self.leaf, ArtifactLeaf):
            raise TypeError("artifact node requires typed ArtifactLeaf")
        if self.leaf_id != self.leaf.leaf_id:
            raise ValueError("artifact node identity does not verify")
        _digest(self.availability_snapshot_id,
                "artifact node availability snapshot")
        if not isinstance(self.status, ArtifactCommitStatus):
            raise TypeError("artifact node status must be typed")

    @property
    def committed(self) -> bool:
        return self.status is ArtifactCommitStatus.COMMITTED

    @property
    def output_port_ids(self) -> tuple[str, ...]:
        return ("artifact",)

    @property
    def cost_units(self) -> int:
        return 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "leaf_id": self.leaf_id,
            "leaf": self.leaf.to_dict(),
            "availability_snapshot_id": self.availability_snapshot_id,
            "status": self.status.value,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ArtifactNode":
        raw = require_object_fields(
            value, {field.name for field in dataclasses.fields(cls)},
            "ArtifactNode")
        raw["leaf"] = ArtifactLeaf.from_dict(raw["leaf"])
        raw["status"] = ArtifactCommitStatus(raw["status"])
        return cls(**raw)


class ProducerKind(str, Enum):
    INVOCATION = "INVOCATION"
    ARTIFACT = "ARTIFACT"


@dataclass(frozen=True)
class SatisfactionArc:
    """A compatible producer output projected onto one distinct use."""

    arc_id: str
    use_id: str
    requirement_id: str
    producer_kind: ProducerKind
    producer_id: str
    output_port_id: str
    compatibility: CompatibilityProof

    def __post_init__(self) -> None:
        _digest(self.arc_id, "satisfaction arc ID")
        _digest(self.use_id, "satisfaction arc use_id")
        _digest(self.requirement_id, "satisfaction arc requirement_id")
        if not isinstance(self.producer_kind, ProducerKind):
            raise TypeError("satisfaction arc producer kind must be typed")
        _digest(self.producer_id, "satisfaction arc producer_id")
        _text(self.output_port_id, "satisfaction arc output port")
        if not isinstance(self.compatibility, CompatibilityProof):
            raise TypeError("satisfaction arc requires a typed proof")
        if not self.compatibility.satisfied:
            raise ValueError("satisfaction arc requires a satisfied proof")
        if self.compatibility.requirement_id != self.requirement_id:
            raise ValueError("satisfaction arc proof covers another requirement")
        if self.arc_id != self.expected_id():
            raise ValueError("satisfaction arc identity does not verify")

    @classmethod
    def bind(cls, *, use_id: str, requirement_id: str,
             producer_kind: ProducerKind, producer_id: str,
             output_port_id: str, compatibility: CompatibilityProof,
             ) -> "SatisfactionArc":
        payload = {
            "schema": "stage3-satisfaction-arc-v1",
            "use_id": use_id,
            "requirement_id": requirement_id,
            "producer_kind": producer_kind.value,
            "producer_id": producer_id,
            "output_port_id": output_port_id,
            "compatibility_proof_id": compatibility.proof_id,
        }
        return cls(
            strict_hash(payload), use_id, requirement_id, producer_kind,
            producer_id, output_port_id, compatibility)

    def expected_id(self) -> str:
        return strict_hash({
            "schema": "stage3-satisfaction-arc-v1",
            "use_id": self.use_id,
            "requirement_id": self.requirement_id,
            "producer_kind": self.producer_kind.value,
            "producer_id": self.producer_id,
            "output_port_id": self.output_port_id,
            "compatibility_proof_id": self.compatibility.proof_id,
        })

    def to_dict(self) -> dict[str, Any]:
        return {
            "arc_id": self.arc_id,
            "use_id": self.use_id,
            "requirement_id": self.requirement_id,
            "producer_kind": self.producer_kind.value,
            "producer_id": self.producer_id,
            "output_port_id": self.output_port_id,
            "compatibility": self.compatibility.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "SatisfactionArc":
        raw = require_object_fields(
            value, {field.name for field in dataclasses.fields(cls)},
            "SatisfactionArc")
        raw["producer_kind"] = ProducerKind(raw["producer_kind"])
        raw["compatibility"] = CompatibilityProof.from_dict(
            raw["compatibility"])
        return cls(**raw)


class RejectionCandidateKind(str, Enum):
    CAPABILITY = "CAPABILITY"
    ARTIFACT = "ARTIFACT"


@dataclass(frozen=True)
class DiscoveryRejection:
    """A considered producer alternative that was not admitted."""

    requirement_id: str
    candidate_kind: RejectionCandidateKind
    candidate_id: str
    output_port_id: str
    codes: tuple[str, ...]
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _digest(self.requirement_id, "rejection requirement_id")
        if not isinstance(self.candidate_kind, RejectionCandidateKind):
            raise TypeError("rejection candidate kind must be typed")
        _digest(self.candidate_id, "rejection candidate_id")
        _text(self.output_port_id, "rejection output port")
        if (not isinstance(self.codes, tuple) or not self.codes
                or tuple(sorted(set(self.codes))) != self.codes):
            raise ValueError("rejection codes must be unique and sorted")
        for code in self.codes:
            _text(code, "rejection code")
        _text(self.message, "rejection message")
        object.__setattr__(self, "details", freeze_json(self.details))
        if not isinstance(self.details, dict):
            raise ValueError("rejection details must be a JSON object")

    @property
    def rejection_id(self) -> str:
        return strict_hash(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "requirement_id": self.requirement_id,
            "candidate_kind": self.candidate_kind.value,
            "candidate_id": self.candidate_id,
            "output_port_id": self.output_port_id,
            "codes": list(self.codes),
            "message": self.message,
            "details": strict_copy(self.details),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "DiscoveryRejection":
        raw = require_object_fields(
            value, {field.name for field in dataclasses.fields(cls)},
            "DiscoveryRejection")
        raw["candidate_kind"] = RejectionCandidateKind(raw["candidate_kind"])
        if not isinstance(raw["codes"], list):
            raise ValueError("DiscoveryRejection.codes must be an array")
        raw["codes"] = tuple(raw["codes"])
        return cls(**raw)


@dataclass(frozen=True)
class DiscoveryTimings:
    """Measured wall-clock durations; deliberately excluded from graph ID."""

    total_ns: int
    artifact_match_ns: int
    catalog_lookup_ns: int
    binding_ns: int
    deployment_check_ns: int
    arc_projection_ns: int

    def __post_init__(self) -> None:
        for field_ in dataclasses.fields(self):
            _integer(getattr(self, field_.name), field_.name)

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "DiscoveryTimings":
        raw = require_object_fields(
            value, {field.name for field in dataclasses.fields(cls)},
            "DiscoveryTimings")
        return cls(**raw)


def _deployment_proof_from_dict(
        value: dict[str, Any]) -> DeploymentFeasibilityProof:
    raw = require_object_fields(
        value, {"profile_id", "snapshot_id", "sites"},
        "DeploymentFeasibilityProof")
    if not isinstance(raw["sites"], list):
        raise ValueError("DeploymentFeasibilityProof.sites must be an array")
    sites: list[SiteFeasibility] = []
    for site_value in raw["sites"]:
        site = require_object_fields(
            site_value, {"site_class_id", "feasible", "rejections"},
            "SiteFeasibility")
        if not isinstance(site["rejections"], list):
            raise ValueError("SiteFeasibility.rejections must be an array")
        rejections: list[DeploymentRejection] = []
        for rejection_value in site["rejections"]:
            rejection = require_object_fields(
                rejection_value,
                {"code", "field", "expected", "observed"},
                "DeploymentRejection")
            if (not isinstance(rejection["expected"], list)
                    or not isinstance(rejection["observed"], list)):
                raise ValueError(
                    "DeploymentRejection expected/observed must be arrays")
            rejections.append(DeploymentRejection(
                DeploymentRejectionCode(rejection["code"]),
                rejection["field"], tuple(rejection["expected"]),
                tuple(rejection["observed"])))
        sites.append(SiteFeasibility(
            site["site_class_id"], site["feasible"], tuple(rejections)))
    return DeploymentFeasibilityProof(
        raw["profile_id"], raw["snapshot_id"], tuple(sites))


@dataclass(frozen=True)
class FeasibleDerivationHypergraph:
    """Finite, frozen producer universe for a later global selector."""

    graph_id: str
    catalog_id: str
    deployment_snapshot_id: str
    availability_snapshot_id: str | None
    evidence_snapshot_id: str | None
    root_use_ids: tuple[str, ...]
    requirement_nodes: tuple[RequirementNode, ...]
    use_nodes: tuple[RequirementUseNode, ...]
    invocation_nodes: tuple[InvocationNode, ...]
    artifact_nodes: tuple[ArtifactNode, ...]
    satisfaction_arcs: tuple[SatisfactionArc, ...]
    rejections: tuple[DiscoveryRejection, ...]
    deployment_proofs: tuple[DeploymentFeasibilityProof, ...]
    back_references: tuple[BackReference, ...]
    discovery_complete: bool
    limit_reasons: tuple[DiscoveryLimitReason, ...]
    limits: DiscoveryLimits
    candidate_count: int
    timings: DiscoveryTimings

    def __post_init__(self) -> None:
        for value, label in (
            (self.graph_id, "hypergraph ID"),
            (self.catalog_id, "hypergraph catalog ID"),
            (self.deployment_snapshot_id,
             "hypergraph deployment snapshot ID"),
        ):
            _digest(value, label)
        if self.availability_snapshot_id is not None:
            _digest(self.availability_snapshot_id,
                    "hypergraph availability snapshot ID")
        if self.evidence_snapshot_id is not None:
            _digest(self.evidence_snapshot_id,
                    "hypergraph evidence snapshot ID")
        if type(self.discovery_complete) is not bool:
            raise TypeError("discovery_complete must be bool")
        if not isinstance(self.limits, DiscoveryLimits):
            raise TypeError("hypergraph limits must be typed")
        if not isinstance(self.timings, DiscoveryTimings):
            raise TypeError("hypergraph timings must be typed")
        _integer(self.candidate_count, "candidate_count")
        if (not isinstance(self.root_use_ids, tuple) or not self.root_use_ids
                or tuple(sorted(self.root_use_ids)) != self.root_use_ids
                or len(set(self.root_use_ids)) != len(self.root_use_ids)):
            raise ValueError("root use IDs must be non-empty, unique, and sorted")

        specifications = (
            (self.requirement_nodes, RequirementNode,
             lambda item: item.requirement_id, "requirement nodes"),
            (self.use_nodes, RequirementUseNode,
             lambda item: item.use_id, "requirement-use nodes"),
            (self.invocation_nodes, InvocationNode,
             lambda item: item.invocation_id, "invocation nodes"),
            (self.artifact_nodes, ArtifactNode,
             lambda item: item.leaf_id, "artifact nodes"),
            (self.satisfaction_arcs, SatisfactionArc,
             lambda item: item.arc_id, "satisfaction arcs"),
            (self.rejections, DiscoveryRejection,
             lambda item: item.rejection_id, "discovery rejections"),
            (self.deployment_proofs, DeploymentFeasibilityProof,
             lambda item: item.profile_id, "deployment proofs"),
            (self.back_references, BackReference,
             lambda item: item.reference_id, "back references"),
            (self.limit_reasons, DiscoveryLimitReason,
             lambda item: item.reason_id, "discovery limit reasons"),
        )
        for values, expected, key, label in specifications:
            _typed_tuple(values, expected, label)
            keys = tuple(key(item) for item in values)
            if keys != tuple(sorted(keys)) or len(keys) != len(set(keys)):
                raise ValueError(f"{label} must be unique and canonically sorted")

        if self.discovery_complete != (not self.limit_reasons):
            raise ValueError(
                "discovery completeness must agree with recorded limits")
        requirements = {item.requirement_id: item
                        for item in self.requirement_nodes}
        uses = {item.use_id: item for item in self.use_nodes}
        invocations = {item.invocation_id: item
                       for item in self.invocation_nodes}
        artifacts = {item.leaf_id: item for item in self.artifact_nodes}
        if set(invocations).intersection(artifacts):
            raise ValueError("producer IDs must be globally unambiguous")
        if not set(self.root_use_ids).issubset(uses):
            raise ValueError("root references an unknown requirement use")
        for use_id in self.root_use_ids:
            if uses[use_id].owner_invocation_id is not None:
                raise ValueError("root requirement use cannot have an owner")

        uses_by_requirement: dict[str, list[str]] = {}
        for use in self.use_nodes:
            if use.requirement_id not in requirements:
                raise ValueError("use references an unknown requirement node")
            uses_by_requirement.setdefault(use.requirement_id, []).append(use.use_id)
            if (use.owner_invocation_id is not None
                    and use.owner_invocation_id not in invocations):
                raise ValueError("use references an unknown owner invocation")
        for requirement in self.requirement_nodes:
            expected = tuple(sorted(
                uses_by_requirement.get(requirement.requirement_id, ())))
            if requirement.use_ids != expected:
                raise ValueError("requirement node has an incorrect use projection")

        owned_uses: set[str] = set()
        for invocation_node in self.invocation_nodes:
            invocation = invocation_node.invocation
            proof = next((item for item in self.deployment_proofs
                          if item.profile_id == invocation.execution_profile_id), None)
            if proof is None or not proof.feasible:
                raise ValueError(
                    "admitted invocation lacks a feasible deployment proof")
            if proof.snapshot_id != self.deployment_snapshot_id:
                raise ValueError("invocation deployment proof snapshot differs")
            for use_id in invocation_node.input_use_ids:
                if use_id not in uses:
                    raise ValueError("invocation references an unknown input use")
                if uses[use_id].owner_invocation_id != invocation_node.invocation_id:
                    raise ValueError("invocation input use has the wrong owner")
                if use_id in owned_uses:
                    raise ValueError("requirement use has several invocation owners")
                owned_uses.add(use_id)
        if set(uses) - set(self.root_use_ids) != owned_uses:
            raise ValueError("every non-root use must belong to one invocation")

        for proof in self.deployment_proofs:
            if proof.snapshot_id != self.deployment_snapshot_id:
                raise ValueError("deployment proof covers another snapshot")
        for artifact in self.artifact_nodes:
            if self.availability_snapshot_id is None:
                raise ValueError("artifact nodes require an availability snapshot")
            if artifact.availability_snapshot_id != self.availability_snapshot_id:
                raise ValueError("artifact node covers another availability snapshot")

        for arc in self.satisfaction_arcs:
            use = uses.get(arc.use_id)
            if use is None or use.requirement_id != arc.requirement_id:
                raise ValueError("satisfaction arc references an invalid use")
            if arc.producer_kind is ProducerKind.INVOCATION:
                producer = invocations.get(arc.producer_id)
                if producer is None:
                    raise ValueError("arc references an unknown invocation")
                try:
                    descriptor = producer.invocation.output(
                        arc.output_port_id).descriptor
                except KeyError as exc:
                    raise ValueError("arc references an unknown output port") from exc
            else:
                producer = artifacts.get(arc.producer_id)
                if producer is None or not producer.committed:
                    raise ValueError("arc references an unavailable artifact")
                if arc.output_port_id != "artifact":
                    raise ValueError("artifact arcs use the canonical artifact port")
                descriptor = producer.leaf.descriptor
            if arc.compatibility.descriptor_id != descriptor.descriptor_id:
                raise ValueError("arc proof covers another descriptor")

        for reference in self.back_references:
            use = uses.get(reference.use_id)
            if (reference.invocation_id not in invocations or use is None
                    or use.owner_invocation_id != reference.invocation_id
                    or use.requirement_id != reference.requirement_id):
                raise ValueError("back reference does not describe a graph edge")
        if self.graph_id != self.expected_id():
            raise ValueError("hypergraph identity does not verify")

    @classmethod
    def bind(
            cls, *, catalog_id: str, deployment_snapshot_id: str,
            availability_snapshot_id: str | None,
            evidence_snapshot_id: str | None,
            root_use_ids: Iterable[str],
            requirement_nodes: Iterable[RequirementNode],
            use_nodes: Iterable[RequirementUseNode],
            invocation_nodes: Iterable[InvocationNode],
            artifact_nodes: Iterable[ArtifactNode],
            satisfaction_arcs: Iterable[SatisfactionArc],
            rejections: Iterable[DiscoveryRejection],
            deployment_proofs: Iterable[DeploymentFeasibilityProof],
            back_references: Iterable[BackReference],
            discovery_complete: bool,
            limit_reasons: Iterable[DiscoveryLimitReason],
            limits: DiscoveryLimits, candidate_count: int,
            timings: DiscoveryTimings,
    ) -> "FeasibleDerivationHypergraph":
        values: dict[str, Any] = {
            "catalog_id": catalog_id,
            "deployment_snapshot_id": deployment_snapshot_id,
            "availability_snapshot_id": availability_snapshot_id,
            "evidence_snapshot_id": evidence_snapshot_id,
            "root_use_ids": tuple(sorted(root_use_ids)),
            "requirement_nodes": tuple(sorted(
                requirement_nodes, key=lambda item: item.requirement_id)),
            "use_nodes": tuple(sorted(use_nodes, key=lambda item: item.use_id)),
            "invocation_nodes": tuple(sorted(
                invocation_nodes, key=lambda item: item.invocation_id)),
            "artifact_nodes": tuple(sorted(
                artifact_nodes, key=lambda item: item.leaf_id)),
            "satisfaction_arcs": tuple(sorted(
                satisfaction_arcs, key=lambda item: item.arc_id)),
            "rejections": tuple(sorted(
                rejections, key=lambda item: item.rejection_id)),
            "deployment_proofs": tuple(sorted(
                deployment_proofs, key=lambda item: item.profile_id)),
            "back_references": tuple(sorted(
                back_references, key=lambda item: item.reference_id)),
            "discovery_complete": discovery_complete,
            "limit_reasons": tuple(sorted(
                limit_reasons, key=lambda item: item.reason_id)),
            "limits": limits,
            "candidate_count": candidate_count,
            "timings": timings,
        }
        provisional = object.__new__(cls)
        for name, value in values.items():
            object.__setattr__(provisional, name, value)
        graph_id = strict_hash(provisional._identity_payload())
        return cls(graph_id=graph_id, **values)

    def _identity_payload(self) -> dict[str, Any]:
        return {
            "schema": "stage3-feasible-derivation-hypergraph-v1",
            "catalog_id": self.catalog_id,
            "deployment_snapshot_id": self.deployment_snapshot_id,
            "availability_snapshot_id": self.availability_snapshot_id,
            "evidence_snapshot_id": self.evidence_snapshot_id,
            "root_use_ids": list(self.root_use_ids),
            "requirement_nodes": [item.to_dict()
                                  for item in self.requirement_nodes],
            "use_nodes": [item.to_dict() for item in self.use_nodes],
            "invocation_nodes": [item.to_dict()
                                 for item in self.invocation_nodes],
            "artifact_nodes": [item.to_dict() for item in self.artifact_nodes],
            "satisfaction_arcs": [item.to_dict()
                                  for item in self.satisfaction_arcs],
            "rejections": [item.to_dict() for item in self.rejections],
            "deployment_proofs": [item.to_dict()
                                  for item in self.deployment_proofs],
            "back_references": [item.to_dict()
                                for item in self.back_references],
            "discovery_complete": self.discovery_complete,
            "limit_reasons": [item.to_dict() for item in self.limit_reasons],
            "limits": self.limits.to_dict(),
            "candidate_count": self.candidate_count,
        }

    def expected_id(self) -> str:
        return strict_hash(self._identity_payload())

    def to_dict(self) -> dict[str, Any]:
        return {
            "graph_id": self.graph_id,
            **self._identity_payload(),
            "timings": self.timings.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]
                  ) -> "FeasibleDerivationHypergraph":
        expected = {field.name for field in dataclasses.fields(cls)} | {"schema"}
        raw = require_object_fields(value, expected,
                                    "FeasibleDerivationHypergraph")
        if raw.pop("schema") != "stage3-feasible-derivation-hypergraph-v1":
            raise ValueError("unsupported feasible hypergraph schema")
        tuple_fields = (
            ("root_use_ids", None),
            ("requirement_nodes", RequirementNode.from_dict),
            ("use_nodes", RequirementUseNode.from_dict),
            ("invocation_nodes", InvocationNode.from_dict),
            ("artifact_nodes", ArtifactNode.from_dict),
            ("satisfaction_arcs", SatisfactionArc.from_dict),
            ("rejections", DiscoveryRejection.from_dict),
            ("deployment_proofs", _deployment_proof_from_dict),
            ("back_references", BackReference.from_dict),
            ("limit_reasons", DiscoveryLimitReason.from_dict),
        )
        for name, parser in tuple_fields:
            if not isinstance(raw[name], list):
                raise ValueError(f"FeasibleDerivationHypergraph.{name} must be an array")
            raw[name] = tuple(
                parser(item) if parser is not None else item
                for item in raw[name])
        raw["limits"] = DiscoveryLimits.from_dict(raw["limits"])
        raw["timings"] = DiscoveryTimings.from_dict(raw["timings"])
        return cls(**raw)


@dataclass(frozen=True)
class _CandidateOutput:
    requirement_id: str
    producer_kind: ProducerKind
    producer_id: str
    output_port_id: str
    compatibility: CompatibilityProof

    @property
    def key(self) -> tuple[str, str, str, str]:
        return (
            self.requirement_id, self.producer_kind.value,
            self.producer_id, self.output_port_id,
        )


class _ExpansionState(str, Enum):
    EXPANDING = "EXPANDING"
    EXPANDED = "EXPANDED"


class HypergraphBuilder:
    """Build one deterministic finite graph from frozen scientific context."""

    def __init__(
            self, catalog: CapabilityCatalog,
            deployment_snapshot: DeploymentCapabilitySnapshot, *,
            artifact_leaves: Iterable[ArtifactLeaf] = (),
            availability_snapshot: ArtifactAvailabilitySnapshot | None = None,
            evidence_snapshot: EvidenceSnapshot | None = None,
            limits: DiscoveryLimits = DiscoveryLimits(),
    ) -> None:
        if not isinstance(catalog, CapabilityCatalog):
            raise TypeError("catalog must be a frozen CapabilityCatalog")
        if not isinstance(deployment_snapshot, DeploymentCapabilitySnapshot):
            raise TypeError(
                "deployment_snapshot must be DeploymentCapabilitySnapshot")
        if not isinstance(limits, DiscoveryLimits):
            raise TypeError("limits must be DiscoveryLimits")
        supplied_leaves = tuple(artifact_leaves)
        if not all(isinstance(item, ArtifactLeaf) for item in supplied_leaves):
            raise TypeError("artifact leaves must be typed ArtifactLeaf values")
        leaves = tuple(sorted(supplied_leaves, key=lambda item: item.leaf_id))
        if len({item.leaf_id for item in leaves}) != len(leaves):
            raise ValueError("artifact leaves cannot repeat")
        if leaves and availability_snapshot is None:
            raise ValueError(
                "artifact leaves require a trusted availability snapshot")
        if availability_snapshot is not None and not isinstance(
                availability_snapshot, ArtifactAvailabilitySnapshot):
            raise TypeError(
                "availability_snapshot must be ArtifactAvailabilitySnapshot")
        if evidence_snapshot is not None and not isinstance(
                evidence_snapshot, EvidenceSnapshot):
            raise TypeError("evidence_snapshot must be EvidenceSnapshot")
        if availability_snapshot is not None:
            covered = {item.leaf_id for item in availability_snapshot.records}
            missing = {item.leaf_id for item in leaves} - covered
            if missing:
                raise ValueError(
                    "availability snapshot omits supplied artifact leaves: "
                    + ", ".join(sorted(missing)))
        self.catalog = catalog
        self.deployment_snapshot = deployment_snapshot
        self.artifact_leaves = leaves
        self.availability_snapshot = availability_snapshot
        self.evidence_snapshot = evidence_snapshot
        self.limits = limits

    def build(self, roots: Iterable[RequirementUse]
              ) -> FeasibleDerivationHypergraph:
        """Recursively discover a packed graph from typed root uses."""
        supplied_roots = tuple(roots)
        if not supplied_roots:
            raise ValueError("hypergraph discovery requires at least one root use")
        if not all(isinstance(item, RequirementUse) for item in supplied_roots):
            raise TypeError("roots must be typed RequirementUse values")
        root_values = tuple(sorted(
            supplied_roots, key=lambda item: item.requirement_use_id))
        root_ids = tuple(item.requirement_use_id for item in root_values)
        if len(set(root_ids)) != len(root_ids):
            raise ValueError("root requirement uses cannot repeat")
        unique_root_requirements = {
            item.requirement.requirement_id for item in root_values}
        if len(unique_root_requirements) > self.limits.max_requirements:
            raise ValueError(
                "max_requirements cannot represent all requested roots")
        state = _BuildState(self)
        started = perf_counter_ns()
        for root in root_values:
            state.add_use(root, owner_invocation_id=None, depth=0)
        for root in root_values:
            state.expand_requirement(
                root.requirement.requirement_id, depth=0,
                via_use_id=root.requirement_use_id)
        projection_started = perf_counter_ns()
        arcs = state.project_arcs()
        state.arc_projection_ns += perf_counter_ns() - projection_started
        total_ns = perf_counter_ns() - started
        requirement_nodes = tuple(RequirementNode(
            requirement_id,
            requirement,
            tuple(sorted(state.use_ids_by_requirement[requirement_id])),
        ) for requirement_id, requirement in sorted(state.requirements.items()))
        timings = DiscoveryTimings(
            total_ns=total_ns,
            artifact_match_ns=state.artifact_match_ns,
            catalog_lookup_ns=state.catalog_lookup_ns,
            binding_ns=state.binding_ns,
            deployment_check_ns=state.deployment_check_ns,
            arc_projection_ns=state.arc_projection_ns,
        )
        return FeasibleDerivationHypergraph.bind(
            catalog_id=self.catalog.catalog_id,
            deployment_snapshot_id=self.deployment_snapshot.snapshot_id,
            availability_snapshot_id=(
                self.availability_snapshot.snapshot_id
                if self.availability_snapshot is not None else None),
            evidence_snapshot_id=(
                self.evidence_snapshot.snapshot_id
                if self.evidence_snapshot is not None else None),
            root_use_ids=root_ids,
            requirement_nodes=requirement_nodes,
            use_nodes=state.uses.values(),
            invocation_nodes=state.invocations.values(),
            artifact_nodes=state.artifacts.values(),
            satisfaction_arcs=arcs,
            rejections=state.rejections.values(),
            deployment_proofs=state.deployment_proofs.values(),
            back_references=state.back_references.values(),
            discovery_complete=not state.limit_reasons,
            limit_reasons=state.limit_reasons.values(),
            limits=self.limits,
            candidate_count=len(state.candidates),
            timings=timings,
        )


class _BuildState:
    def __init__(self, builder: HypergraphBuilder) -> None:
        self.builder = builder
        self.requirements: dict[str, Requirement] = {}
        self.uses: dict[str, RequirementUseNode] = {}
        self.use_ids_by_requirement: dict[str, set[str]] = {}
        self.invocations: dict[str, InvocationNode] = {}
        # Memoization is depth-sensitive: a result discovered from a deeper
        # path is not complete evidence for the same node reached shallower.
        # The shallower path has a larger remaining discovery budget.
        self.requirement_expansion_depths: dict[str, int] = {}
        self.invocation_expansion_depths: dict[str, int] = {}
        self.artifacts: dict[str, ArtifactNode] = {}
        self.candidates: dict[tuple[str, str, str, str], _CandidateOutput] = {}
        self.rejections: dict[str, DiscoveryRejection] = {}
        self.deployment_proofs: dict[str, DeploymentFeasibilityProof] = {}
        self.back_references: dict[str, BackReference] = {}
        self.limit_reasons: dict[str, DiscoveryLimitReason] = {}
        self.expansion_state: dict[str, _ExpansionState] = {}
        self.use_depths: dict[str, int] = {}
        self.artifact_match_ns = 0
        self.catalog_lookup_ns = 0
        self.binding_ns = 0
        self.deployment_check_ns = 0
        self.arc_projection_ns = 0

        self.leaves_by_concept: dict[str, tuple[ArtifactLeaf, ...]] = {}
        for leaf in builder.artifact_leaves:
            current = self.leaves_by_concept.get(
                leaf.descriptor.concept_id, ())
            self.leaves_by_concept[leaf.descriptor.concept_id] = (
                *current, leaf)
        self.evidence_by_id: dict[str, EvidenceProfile] = (
            {item.profile_id: item for item in builder.evidence_snapshot.profiles}
            if builder.evidence_snapshot is not None else {})

    def add_use(self, use: RequirementUse,
                owner_invocation_id: str | None, *, depth: int
                ) -> RequirementUseNode:
        _integer(depth, "requirement-use discovery depth")
        use_id = use.requirement_use_id
        node = RequirementUseNode.bind(
            use, owner_invocation_id=owner_invocation_id)
        previous = self.uses.get(use_id)
        if previous is not None:
            if previous != node:
                raise ValueError(
                    "one requirement-use identity has conflicting owners")
            self.use_depths[use_id] = min(self.use_depths[use_id], depth)
            return previous
        requirement_id = use.requirement.requirement_id
        previous_requirement = self.requirements.get(requirement_id)
        if (previous_requirement is not None
                and previous_requirement != use.requirement):
            raise ValueError("one requirement identity has conflicting values")
        if previous_requirement is None:
            if len(self.requirements) >= self.builder.limits.max_requirements:
                raise RuntimeError(
                    "internal error: requirement limit was not preflighted")
            self.requirements[requirement_id] = use.requirement
        self.uses[use_id] = node
        self.use_ids_by_requirement.setdefault(requirement_id, set()).add(use_id)
        self.use_depths[use_id] = depth
        return node

    def expand_requirement(self, requirement_id: str, *, depth: int,
                           via_use_id: str) -> None:
        current = self.expansion_state.get(requirement_id)
        if current is _ExpansionState.EXPANDING:
            return
        if current is _ExpansionState.EXPANDED:
            expanded_depth = self.requirement_expansion_depths[requirement_id]
            if expanded_depth <= depth:
                return
            # Re-open a node first expanded deeper.  Replaying its already
            # interned candidates is idempotent, while its invocation inputs
            # must receive the additional remaining depth budget.
        requirement = self.requirements[requirement_id]
        self.expansion_state[requirement_id] = _ExpansionState.EXPANDING
        self._discover_artifacts(
            requirement, via_use_id=via_use_id, depth=depth)

        lookup_started = perf_counter_ns()
        specs = self.builder.catalog.lookup(requirement.concept_id)
        self.catalog_lookup_ns += perf_counter_ns() - lookup_started
        if depth >= self.builder.limits.max_depth and specs:
            self._record_limit(DiscoveryLimitReason(
                DiscoveryLimitCode.MAX_DEPTH,
                requirement_id,
                via_use_id,
                depth,
                depth + 1,
                self.builder.limits.max_depth,
                "capability expansion omitted at the configured depth bound",
            ))
            # A shallower path discovered later may still expand this node.
            if requirement_id in self.requirement_expansion_depths:
                self.expansion_state[requirement_id] = _ExpansionState.EXPANDED
            else:
                self.expansion_state.pop(requirement_id, None)
            return
        self._clear_resolved_depth_limits(requirement_id)
        for spec in specs:
            deployment_started = perf_counter_ns()
            deployment = self.builder.deployment_snapshot.check(
                self.builder.catalog.profile(spec.execution_profile_id))
            self.deployment_check_ns += perf_counter_ns() - deployment_started
            previous = self.deployment_proofs.setdefault(
                deployment.profile_id, deployment)
            if previous != deployment:
                raise ValueError("one execution profile has conflicting deployment proofs")

            output_ports = tuple(sorted(
                output.port_id for output in spec.output_ports
                if output.descriptor.concept_id == requirement.concept_id))
            for output_port_id in output_ports:
                evidence_profile = self.evidence_by_id.get(
                    spec.evidence_profile_id)
                if (evidence_profile is None
                        and spec.evidence_profile_id != "evidence:unknown"):
                    self._record_rejection(DiscoveryRejection(
                        requirement_id,
                        RejectionCandidateKind.CAPABILITY,
                        spec.spec_id,
                        output_port_id,
                        ("EVIDENCE_PROFILE_UNBOUND",),
                        "capability names an evidence profile absent from the "
                        "frozen evidence snapshot",
                        {"evidence_profile_id": spec.evidence_profile_id},
                    ))
                    continue
                kwargs: dict[str, Any] = {
                    "deployment_snapshot": self.builder.deployment_snapshot,
                }
                if evidence_profile is not None:
                    kwargs.update({
                        "evidence_profile": evidence_profile,
                        "evidence_snapshot": self.builder.evidence_snapshot,
                    })
                binding_started = perf_counter_ns()
                enumeration = self.builder.catalog.bind_candidates(
                    spec.spec_id, output_port_id, requirement, **kwargs)
                self.binding_ns += perf_counter_ns() - binding_started
                if not enumeration.complete:
                    enumerated = (len(enumeration.accepted)
                                  + len(enumeration.rejected))
                    self._record_limit(DiscoveryLimitReason(
                        DiscoveryLimitCode.BINDING_ENUMERATION_INCOMPLETE,
                        requirement_id,
                        via_use_id,
                        depth,
                        enumerated,
                        enumerated,
                        "capability binder reported a bounded/incomplete "
                        "parameterization frontier",
                    ))
                for rejection in enumeration.rejected:
                    self._record_binding_rejection(
                        requirement_id, rejection)
                for candidate in enumeration.accepted:
                    candidate_key = (
                        requirement_id,
                        ProducerKind.INVOCATION.value,
                        candidate.invocation.invocation_key,
                        candidate.offered_output_port,
                    )
                    if (candidate_key not in self.candidates
                            and len(self.candidates)
                            >= self.builder.limits.max_candidates):
                        self._record_candidate_limit(
                            requirement_id, via_use_id, depth)
                        continue
                    cost = candidate.invocation.metric_estimates.get(
                        "cost_units")
                    if candidate.invocation.cost_model_id != "cost:declared-v1":
                        self._record_rejection(DiscoveryRejection(
                            requirement_id,
                            RejectionCandidateKind.CAPABILITY,
                            candidate.invocation.invocation_key,
                            candidate.offered_output_port,
                            ("COST_MODEL_UNSUPPORTED",),
                            "Stage 3 MVP supports only cost:declared-v1",
                            {"cost_model_id":
                             candidate.invocation.cost_model_id},
                        ))
                        continue
                    if (isinstance(cost, bool) or not isinstance(cost, int)
                            or not 0 <= cost <= _MAX_MILP_COST_UNITS):
                        self._record_rejection(DiscoveryRejection(
                            requirement_id,
                            RejectionCandidateKind.CAPABILITY,
                            candidate.invocation.invocation_key,
                            candidate.offered_output_port,
                            ("COST_ESTIMATE_INVALID",),
                            "declared cost_units is outside the supported "
                            "Stage 3 MILP range",
                            {
                                "cost_units": cost,
                                "maximum_supported": _MAX_MILP_COST_UNITS,
                            },
                        ))
                        continue
                    if not self._admit_invocation(
                            candidate.invocation, requirement_id,
                            via_use_id, depth):
                        continue
                    discovered = _CandidateOutput(
                        requirement_id,
                        ProducerKind.INVOCATION,
                        candidate.invocation.invocation_key,
                        candidate.offered_output_port,
                        candidate.compatibility,
                    )
                    self._record_candidate(discovered)
                    self._expand_invocation_inputs(
                        candidate.invocation, depth=depth + 1)
        previous_depth = self.requirement_expansion_depths.get(requirement_id)
        self.requirement_expansion_depths[requirement_id] = (
            depth if previous_depth is None else min(previous_depth, depth))
        self.expansion_state[requirement_id] = _ExpansionState.EXPANDED

    def _discover_artifacts(
            self, requirement: Requirement, *,
            via_use_id: str, depth: int) -> None:
        snapshot = self.builder.availability_snapshot
        if snapshot is None:
            return
        for leaf in self.leaves_by_concept.get(requirement.concept_id, ()):
            status = snapshot.status_for(leaf.leaf_id)
            node = ArtifactNode(
                leaf.leaf_id, leaf, snapshot.snapshot_id, status)
            previous = self.artifacts.setdefault(leaf.leaf_id, node)
            if previous != node:
                raise ValueError("artifact identity has conflicting state")
            if not node.committed:
                self._record_rejection(DiscoveryRejection(
                    requirement.requirement_id,
                    RejectionCandidateKind.ARTIFACT,
                    leaf.leaf_id,
                    "artifact",
                    ("ARTIFACT_NOT_COMMITTED",),
                    "trusted availability state is not COMMITTED",
                    {"status": status.value,
                     "availability_snapshot_id": snapshot.snapshot_id},
                ))
                continue
            evidence_profile = self.evidence_by_id.get(leaf.evidence_profile_id)
            if (evidence_profile is None
                    and leaf.evidence_profile_id != "evidence:unknown"):
                self._record_rejection(DiscoveryRejection(
                    requirement.requirement_id,
                    RejectionCandidateKind.ARTIFACT,
                    leaf.leaf_id,
                    "artifact",
                    ("EVIDENCE_PROFILE_UNBOUND",),
                    "artifact names an evidence profile absent from the "
                    "frozen evidence snapshot",
                    {"evidence_profile_id": leaf.evidence_profile_id},
                ))
                continue
            match_started = perf_counter_ns()
            if evidence_profile is None:
                proof = direct_match(leaf.descriptor, requirement)
            else:
                proof = direct_match(
                    leaf.descriptor, requirement, evidence_profile,
                    evidence_snapshot=self.builder.evidence_snapshot,
                    evidence_subject=artifact_evidence_subject(leaf),
                )
            self.artifact_match_ns += perf_counter_ns() - match_started
            if not proof.satisfied:
                self._record_rejection(DiscoveryRejection(
                    requirement.requirement_id,
                    RejectionCandidateKind.ARTIFACT,
                    leaf.leaf_id,
                    "artifact",
                    tuple(sorted(code.value for code in proof.rejection_codes)),
                    "artifact descriptor does not directly satisfy requirement",
                    {"compatibility": proof.to_dict()},
                ))
                continue
            candidate = _CandidateOutput(
                requirement.requirement_id,
                ProducerKind.ARTIFACT,
                leaf.leaf_id,
                "artifact",
                proof,
            )
            if (candidate.key not in self.candidates
                    and len(self.candidates)
                    >= self.builder.limits.max_candidates):
                self._record_candidate_limit(
                    requirement.requirement_id, via_use_id, depth)
                continue
            self._record_candidate(candidate)

    def _admit_invocation(
            self, invocation: BoundInvocation, requirement_id: str,
            via_use_id: str, depth: int) -> bool:
        invocation_id = invocation.invocation_key
        if invocation_id in self.invocations:
            if self.invocations[invocation_id].invocation != invocation:
                raise ValueError(
                    "one invocation identity has conflicting planning records")
            return True
        if len(self.invocations) >= self.builder.limits.max_invocations:
            self._record_limit(DiscoveryLimitReason(
                DiscoveryLimitCode.MAX_INVOCATIONS,
                requirement_id,
                via_use_id,
                depth,
                len(self.invocations) + 1,
                self.builder.limits.max_invocations,
                "new bound invocation omitted",
            ))
            return False
        new_requirement_ids = {
            use.requirement.requirement_id for use in invocation.input_uses
            if use.requirement.requirement_id not in self.requirements}
        available = (self.builder.limits.max_requirements
                     - len(self.requirements))
        if len(new_requirement_ids) > available:
            for child_id in sorted(new_requirement_ids):
                self._record_limit(DiscoveryLimitReason(
                    DiscoveryLimitCode.MAX_REQUIREMENTS,
                    child_id,
                    None,
                    depth + 1,
                    len(self.requirements) + len(new_requirement_ids),
                    self.builder.limits.max_requirements,
                    "invocation omitted because its input requirement could not be represented",
                ))
            return False
        for use in invocation.input_uses:
            existing = self.uses.get(use.requirement_use_id)
            expected = RequirementUseNode.bind(
                use, owner_invocation_id=invocation_id)
            if existing is not None and existing != expected:
                raise ValueError(
                    "bound invocation input use conflicts with an existing node")
        self.invocations[invocation_id] = InvocationNode.bind(invocation)
        for use in invocation.input_uses:
            self.add_use(
                use, owner_invocation_id=invocation_id, depth=depth + 1)
        return True

    def _expand_invocation_inputs(
            self, invocation: BoundInvocation, *, depth: int) -> None:
        invocation_id = invocation.invocation_key
        previous_depth = self.invocation_expansion_depths.get(invocation_id)
        if previous_depth is not None and previous_depth <= depth:
            return
        self.invocation_expansion_depths[invocation_id] = (
            depth if previous_depth is None else min(previous_depth, depth))
        for use in sorted(
                invocation.input_uses, key=lambda item: item.requirement_use_id):
            requirement_id = use.requirement.requirement_id
            current = self.expansion_state.get(requirement_id)
            kind: BackReferenceKind | None = None
            if current is _ExpansionState.EXPANDING:
                kind = BackReferenceKind.CYCLE
            elif (current is _ExpansionState.EXPANDED
                  and self.requirement_expansion_depths[requirement_id]
                  <= depth):
                # Only label an edge memoized when the cached expansion had at
                # least as much remaining depth budget as this traversal.
                kind = BackReferenceKind.MEMOIZED
            if kind is not None:
                reference = BackReference(
                    kind, invocation_id, use.requirement_use_id,
                    requirement_id, depth)
                self.back_references.setdefault(
                    reference.reference_id, reference)
            self.expand_requirement(
                requirement_id, depth=depth,
                via_use_id=use.requirement_use_id)

    def _record_binding_rejection(
            self, requirement_id: str,
            rejection: BindingRejection) -> None:
        value = DiscoveryRejection(
            requirement_id,
            RejectionCandidateKind.CAPABILITY,
            rejection.capability_spec_id,
            rejection.offered_output_port,
            (rejection.code.value,),
            rejection.message,
            rejection.details,
        )
        self._record_rejection(value)

    def _record_rejection(self, rejection: DiscoveryRejection) -> None:
        self.rejections.setdefault(rejection.rejection_id, rejection)

    def _record_limit(self, reason: DiscoveryLimitReason) -> None:
        self.limit_reasons.setdefault(reason.reason_id, reason)

    def _clear_resolved_depth_limits(self, requirement_id: str) -> None:
        """Remove a deep-path truncation once the shared node expands shallower."""
        for reason_id, reason in tuple(self.limit_reasons.items()):
            if (reason.code is DiscoveryLimitCode.MAX_DEPTH
                    and reason.requirement_id == requirement_id):
                del self.limit_reasons[reason_id]

    def _record_candidate(self, candidate: _CandidateOutput) -> None:
        previous = self.candidates.setdefault(candidate.key, candidate)
        if previous != candidate:
            raise ValueError(
                "one producer-output/requirement edge has conflicting proofs")

    def _record_candidate_limit(
            self, requirement_id: str, use_id: str, depth: int) -> None:
        self._record_limit(DiscoveryLimitReason(
            DiscoveryLimitCode.MAX_CANDIDATES,
            requirement_id,
            use_id,
            depth,
            len(self.candidates) + 1,
            self.builder.limits.max_candidates,
            "additional producer alternatives were omitted",
        ))

    def project_arcs(self) -> tuple[SatisfactionArc, ...]:
        arcs: list[SatisfactionArc] = []
        candidates_by_requirement: dict[str, list[_CandidateOutput]] = {}
        for candidate in self.candidates.values():
            candidates_by_requirement.setdefault(
                candidate.requirement_id, []).append(candidate)
        for use_id, use_node in sorted(self.uses.items()):
            candidates = sorted(
                candidates_by_requirement.get(use_node.requirement_id, ()),
                key=lambda item: item.key)
            for candidate in candidates:
                if len(arcs) >= self.builder.limits.max_arcs:
                    self._record_limit(DiscoveryLimitReason(
                        DiscoveryLimitCode.MAX_ARCS,
                        use_node.requirement_id,
                        use_id,
                        self.use_depths[use_id],
                        len(arcs) + 1,
                        self.builder.limits.max_arcs,
                        "per-use satisfaction arc projection was truncated",
                    ))
                    return tuple(arcs)
                arcs.append(SatisfactionArc.bind(
                    use_id=use_id,
                    requirement_id=use_node.requirement_id,
                    producer_kind=candidate.producer_kind,
                    producer_id=candidate.producer_id,
                    output_port_id=candidate.output_port_id,
                    compatibility=candidate.compatibility,
                ))
        return tuple(arcs)


def build_feasible_hypergraph(
        catalog: CapabilityCatalog,
        deployment_snapshot: DeploymentCapabilitySnapshot,
        roots: Iterable[RequirementUse], *,
        artifact_leaves: Iterable[ArtifactLeaf] = (),
        availability_snapshot: ArtifactAvailabilitySnapshot | None = None,
        evidence_snapshot: EvidenceSnapshot | None = None,
        limits: DiscoveryLimits = DiscoveryLimits(),
) -> FeasibleDerivationHypergraph:
    """Functional convenience wrapper around :class:`HypergraphBuilder`."""
    return HypergraphBuilder(
        catalog,
        deployment_snapshot,
        artifact_leaves=artifact_leaves,
        availability_snapshot=availability_snapshot,
        evidence_snapshot=evidence_snapshot,
        limits=limits,
    ).build(roots)


# Concise alias for callers that do not need the architectural qualifier.
FeasibleHypergraph = FeasibleDerivationHypergraph
