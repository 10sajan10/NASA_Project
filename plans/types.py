"""Strict immutable identities for candidate and bound derivation plans."""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable

from contracts import CompatibilityProof
from engine.runtime.identity import (
    freeze_json,
    require_object_fields,
    strict_copy,
    strict_hash,
)


_DIGEST_LENGTH = 64


def _text(value: str, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")


def _digest(value: str, label: str) -> None:
    _text(value, label)
    if (len(value) != _DIGEST_LENGTH
            or any(character not in "0123456789abcdef" for character in value)):
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")


def _unique(values: tuple[str, ...], label: str) -> None:
    if (not isinstance(values, tuple)
            or any(not isinstance(value, str) or not value for value in values)
            or len(values) != len(set(values))):
        raise ValueError(f"{label} must contain unique, non-empty strings")


def _exact(value: Any, cls: type, label: str) -> dict[str, Any]:
    return require_object_fields(
        value,
        {field.name for field in dataclasses.fields(cls)},
        label,
    )


class ProducerKind(str, Enum):
    INVOCATION = "INVOCATION"
    ARTIFACT_LEAF = "ARTIFACT_LEAF"


class SatisfactionKind(str, Enum):
    PRODUCERS = "PRODUCERS"
    DEFAULT = "DEFAULT"
    OMIT = "OMIT"


@dataclass(frozen=True)
class PlanSnapshotRef:
    """One frozen input to scientific selection."""

    name: str
    snapshot_id: str

    def __post_init__(self) -> None:
        _text(self.name, "snapshot name")
        _text(self.snapshot_id, "snapshot identity")

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "snapshot_id": self.snapshot_id}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "PlanSnapshotRef":
        return cls(**_exact(value, cls, "PlanSnapshotRef"))


@dataclass(frozen=True)
class CompatibilityProofRecord:
    """Canonical, selector-independent copy of one direct-match proof.

    Contract implementations can evolve independently.  A plan retains the
    exact strict-JSON proof that justified its selected satisfaction edge.
    """

    proof_id: str
    payload: dict[str, Any]

    def __post_init__(self) -> None:
        _digest(self.proof_id, "compatibility proof identity")
        object.__setattr__(self, "payload", freeze_json(self.payload))
        if not isinstance(self.payload, dict):
            raise ValueError("compatibility proof payload must be a JSON object")
        self._validate_typed_wrapper(self.payload)
        expected = strict_hash({
            "schema": "stage2-plan-compatibility-proof-v1",
            "payload": self.payload,
        })
        if self.proof_id != expected:
            raise ValueError("compatibility proof identity does not verify")

    @classmethod
    def bind(cls, payload: dict[str, Any]) -> "CompatibilityProofRecord":
        """Authenticate an already-standardized typed proof wrapper.

        This method deliberately does not turn arbitrary JSON into a selected
        compatibility proof.  Production callers should normally use
        :meth:`from_compatibility`.
        """
        detached = strict_copy(payload)
        if not isinstance(detached, dict):
            raise ValueError("compatibility proof payload must be a JSON object")
        cls._validate_typed_wrapper(detached)
        return cls(strict_hash({
            "schema": "stage2-plan-compatibility-proof-v1",
            "payload": detached,
        }), detached)

    @classmethod
    def from_compatibility(
            cls, proof: CompatibilityProof,
    ) -> "CompatibilityProofRecord":
        """Freeze a typed direct-match proof and its checked projections."""
        if not isinstance(proof, CompatibilityProof):
            raise TypeError("proof must be contracts.CompatibilityProof")
        return cls.bind({
            "schema": "stage2-selected-compatibility-proof-v1",
            "proof_id": proof.proof_id,
            "satisfied": proof.satisfied,
            "rejection_codes": [value.value for value in proof.rejection_codes],
            "caveat_codes": [value.value for value in proof.caveat_codes],
            "proof": proof.to_dict(),
        })

    @staticmethod
    def _validate_typed_wrapper(payload: dict[str, Any]) -> CompatibilityProof:
        payload = strict_copy(payload)
        required = {
            "schema", "proof_id", "satisfied", "rejection_codes",
            "caveat_codes", "proof",
        }
        if (set(payload) != required
                or payload.get("schema")
                != "stage2-selected-compatibility-proof-v1"):
            raise ValueError("selected edge has an unsupported proof wrapper")
        proof = CompatibilityProof.from_dict(payload["proof"])
        if (payload["proof_id"] != proof.proof_id
                or payload["satisfied"] is not proof.satisfied
                or payload["rejection_codes"]
                != [value.value for value in proof.rejection_codes]
                or payload["caveat_codes"]
                != [value.value for value in proof.caveat_codes]):
            raise ValueError(
                "selected compatibility proof wrapper does not verify")
        return proof

    def to_dict(self) -> dict[str, Any]:
        return {"proof_id": self.proof_id, "payload": strict_copy(self.payload)}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "CompatibilityProofRecord":
        return cls(**_exact(value, cls, "CompatibilityProofRecord"))


@dataclass(frozen=True)
class ProducerOutputRef:
    producer_id: str
    producer_kind: ProducerKind
    output_port_id: str
    proof_id: str

    def __post_init__(self) -> None:
        _text(self.producer_id, "producer identity")
        if not isinstance(self.producer_kind, ProducerKind):
            raise TypeError("producer_kind must be ProducerKind")
        _text(self.output_port_id, "output port identity")
        _digest(self.proof_id, "compatibility proof identity")

    @property
    def choice_id(self) -> str:
        return strict_hash({
            "schema": "stage2-producer-output-choice-v1",
            "producer_id": self.producer_id,
            "producer_kind": self.producer_kind.value,
            "output_port_id": self.output_port_id,
            "proof_id": self.proof_id,
        })

    def to_dict(self) -> dict[str, Any]:
        return {
            "producer_id": self.producer_id,
            "producer_kind": self.producer_kind.value,
            "output_port_id": self.output_port_id,
            "proof_id": self.proof_id,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ProducerOutputRef":
        raw = _exact(value, cls, "ProducerOutputRef")
        raw["producer_kind"] = ProducerKind(raw["producer_kind"])
        return cls(**raw)


@dataclass(frozen=True)
class SatisfactionBinding:
    """The complete, exclusive choice for one distinct requirement use."""

    use_id: str
    kind: SatisfactionKind
    outputs: tuple[ProducerOutputRef, ...] = ()
    default_id: str | None = None

    def __post_init__(self) -> None:
        _text(self.use_id, "requirement-use identity")
        if not isinstance(self.kind, SatisfactionKind):
            raise TypeError("satisfaction kind must be SatisfactionKind")
        if (not isinstance(self.outputs, tuple)
                or not all(isinstance(value, ProducerOutputRef)
                           for value in self.outputs)):
            raise TypeError("satisfaction outputs must be ProducerOutputRef values")
        if tuple(sorted(self.outputs, key=lambda value: value.choice_id)) != self.outputs:
            raise ValueError("satisfaction outputs must be in canonical order")
        if len({value.choice_id for value in self.outputs}) != len(self.outputs):
            raise ValueError("satisfaction outputs cannot contain duplicates")
        if self.kind is SatisfactionKind.PRODUCERS:
            if not self.outputs or self.default_id is not None:
                raise ValueError("producer satisfaction requires only output choices")
        elif self.kind is SatisfactionKind.DEFAULT:
            if self.outputs or not self.default_id:
                raise ValueError("default satisfaction requires exactly one default")
        elif self.outputs or self.default_id is not None:
            raise ValueError("omission cannot contain an output or default")

    @property
    def choice_ids(self) -> tuple[str, ...]:
        if self.kind is SatisfactionKind.PRODUCERS:
            return tuple(strict_hash({
                "schema": "stage2-satisfaction-edge-choice-v1",
                "use_id": self.use_id,
                "output": value.to_dict(),
            }) for value in self.outputs)
        return (strict_hash({
            "schema": "stage2-nonproducer-choice-v1",
            "use_id": self.use_id,
            "kind": self.kind.value,
            "default_id": self.default_id,
        }),)

    def to_dict(self) -> dict[str, Any]:
        return {
            "use_id": self.use_id,
            "kind": self.kind.value,
            "outputs": [value.to_dict() for value in self.outputs],
            "default_id": self.default_id,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "SatisfactionBinding":
        raw = _exact(value, cls, "SatisfactionBinding")
        if not isinstance(raw["outputs"], list):
            raise ValueError("SatisfactionBinding.outputs must be a JSON array")
        raw["kind"] = SatisfactionKind(raw["kind"])
        raw["outputs"] = tuple(ProducerOutputRef.from_dict(item)
                               for item in raw["outputs"])
        return cls(**raw)


@dataclass(frozen=True)
class CandidateDerivationPlan:
    """One fully selected scientific derivation over frozen candidates."""

    plan_id: str
    schema_version: str
    objective: str
    ordering_version: str
    oracle_problem_id: str
    discovery_complete: bool
    root_use_ids: tuple[str, ...]
    root_requirements: tuple[tuple[str, str], ...]
    selected_invocation_ids: tuple[str, ...]
    selected_artifact_leaf_ids: tuple[str, ...]
    satisfactions: tuple[SatisfactionBinding, ...]
    compatibility_proofs: tuple[CompatibilityProofRecord, ...]
    snapshot_refs: tuple[PlanSnapshotRef, ...]
    total_cost_units: int
    selection_signature: tuple[int, ...]

    def __post_init__(self) -> None:
        _digest(self.plan_id, "candidate plan identity")
        _text(self.schema_version, "candidate plan schema")
        _text(self.objective, "candidate plan objective")
        _text(self.ordering_version, "candidate plan ordering version")
        _digest(self.oracle_problem_id, "oracle problem identity")
        if type(self.discovery_complete) is not bool:
            raise TypeError("discovery completeness must be bool")
        _unique(self.root_use_ids, "root use IDs")
        if (not isinstance(self.root_requirements, tuple)
                or any(not isinstance(value, tuple) or len(value) != 2
                       or any(not isinstance(item, str) or not item
                              for item in value)
                       for value in self.root_requirements)
                or self.root_requirements
                != tuple(sorted(self.root_requirements))
                or len({value[0] for value in self.root_requirements})
                != len(self.root_requirements)
                or {value[0] for value in self.root_requirements}
                != set(self.root_use_ids)):
            raise ValueError(
                "root requirements must map every root use exactly once")
        _unique(self.selected_invocation_ids, "selected invocation IDs")
        _unique(self.selected_artifact_leaf_ids, "selected artifact-leaf IDs")
        if (self.selected_invocation_ids != tuple(sorted(self.selected_invocation_ids))
                or self.selected_artifact_leaf_ids
                != tuple(sorted(self.selected_artifact_leaf_ids))):
            raise ValueError("selected producer IDs must be canonically ordered")
        if set(self.selected_invocation_ids) & set(self.selected_artifact_leaf_ids):
            raise ValueError("an identity cannot be both invocation and artifact leaf")
        if (not isinstance(self.satisfactions, tuple)
                or not all(isinstance(value, SatisfactionBinding)
                           for value in self.satisfactions)
                or tuple(sorted(self.satisfactions, key=lambda value: value.use_id))
                != self.satisfactions
                or len({value.use_id for value in self.satisfactions})
                != len(self.satisfactions)):
            raise ValueError("satisfactions must be unique and canonically ordered")
        if not set(self.root_use_ids).issubset(
                {value.use_id for value in self.satisfactions}):
            raise ValueError("every root use must have a satisfaction")
        if (not isinstance(self.compatibility_proofs, tuple)
                or not all(isinstance(value, CompatibilityProofRecord)
                           for value in self.compatibility_proofs)
                or tuple(sorted(self.compatibility_proofs,
                                key=lambda value: value.proof_id))
                != self.compatibility_proofs
                or len({value.proof_id for value in self.compatibility_proofs})
                != len(self.compatibility_proofs)):
            raise ValueError("compatibility proofs must be unique and ordered")
        if (not isinstance(self.snapshot_refs, tuple)
                or not all(isinstance(value, PlanSnapshotRef)
                           for value in self.snapshot_refs)
                or tuple(sorted(self.snapshot_refs, key=lambda value: value.name))
                != self.snapshot_refs
                or len({value.name for value in self.snapshot_refs})
                != len(self.snapshot_refs)):
            raise ValueError("snapshot references must be unique and ordered")
        if (isinstance(self.total_cost_units, bool)
                or not isinstance(self.total_cost_units, int)
                or self.total_cost_units < 0):
            raise ValueError("plan cost must be a non-negative integer")
        if (not isinstance(self.selection_signature, tuple)
                or any(isinstance(value, bool) or value not in (0, 1)
                       for value in self.selection_signature)):
            raise ValueError("selection signature must contain integer bits")
        proof_ids = {value.proof_id for value in self.compatibility_proofs}
        referenced_proofs: set[str] = set()
        referenced_invocations: set[str] = set()
        referenced_leaves: set[str] = set()
        for satisfaction in self.satisfactions:
            for output in satisfaction.outputs:
                referenced_proofs.add(output.proof_id)
                if output.producer_kind is ProducerKind.INVOCATION:
                    referenced_invocations.add(output.producer_id)
                else:
                    referenced_leaves.add(output.producer_id)
        if referenced_proofs != proof_ids:
            raise ValueError("plan proofs must exactly match selected output edges")
        if referenced_invocations != set(self.selected_invocation_ids):
            raise ValueError("selected invocation set contains a missing or orphan producer")
        if referenced_leaves != set(self.selected_artifact_leaf_ids):
            raise ValueError("selected artifact set contains a missing or orphan leaf")

    @classmethod
    def bind(
            cls, *, root_use_ids: Iterable[str],
            root_requirements: Iterable[tuple[str, str]],
            oracle_problem_id: str,
            discovery_complete: bool,
            selected_invocation_ids: Iterable[str],
            selected_artifact_leaf_ids: Iterable[str],
            satisfactions: Iterable[SatisfactionBinding],
            compatibility_proofs: Iterable[CompatibilityProofRecord],
            snapshot_refs: Iterable[PlanSnapshotRef] = (),
            total_cost_units: int,
            selection_signature: Iterable[int],
            schema_version: str = "stage2-candidate-derivation-plan-v1",
            objective: str = "minimum_integer_cost",
            ordering_version: str = "producer-choice-bit-vector-v1",
    ) -> "CandidateDerivationPlan":
        values = {
            "schema_version": schema_version,
            "objective": objective,
            "ordering_version": ordering_version,
            "oracle_problem_id": oracle_problem_id,
            "discovery_complete": discovery_complete,
            "root_use_ids": tuple(root_use_ids),
            "root_requirements": tuple(sorted(root_requirements)),
            "selected_invocation_ids": tuple(sorted(selected_invocation_ids)),
            "selected_artifact_leaf_ids": tuple(sorted(selected_artifact_leaf_ids)),
            "satisfactions": tuple(sorted(satisfactions,
                                          key=lambda value: value.use_id)),
            "compatibility_proofs": tuple(sorted(
                compatibility_proofs, key=lambda value: value.proof_id)),
            "snapshot_refs": tuple(sorted(snapshot_refs,
                                          key=lambda value: value.name)),
            "total_cost_units": total_cost_units,
            "selection_signature": tuple(selection_signature),
        }
        plan_id = strict_hash(cls._identity_payload(values))
        return cls(plan_id=plan_id, **values)

    @staticmethod
    def _identity_payload(values: dict[str, Any]) -> dict[str, Any]:
        return {
            "schema": "stage2-candidate-derivation-plan-identity-v1",
            "schema_version": values["schema_version"],
            "objective": values["objective"],
            "ordering_version": values["ordering_version"],
            "oracle_problem_id": values["oracle_problem_id"],
            "discovery_complete": values["discovery_complete"],
            "root_use_ids": list(values["root_use_ids"]),
            "root_requirements": [list(value)
                                  for value in values["root_requirements"]],
            "selected_invocation_ids": list(values["selected_invocation_ids"]),
            "selected_artifact_leaf_ids": list(
                values["selected_artifact_leaf_ids"]),
            "satisfactions": [value.to_dict()
                              for value in values["satisfactions"]],
            "compatibility_proofs": [value.to_dict()
                                     for value in values["compatibility_proofs"]],
            "snapshot_refs": [value.to_dict()
                              for value in values["snapshot_refs"]],
            "total_cost_units": values["total_cost_units"],
            "selection_signature": list(values["selection_signature"]),
        }

    def validate_identity(self) -> None:
        values = self._values_without_id()
        if strict_hash(self._identity_payload(values)) != self.plan_id:
            raise ValueError("candidate derivation plan identity does not verify")

    def _values_without_id(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "objective": self.objective,
            "ordering_version": self.ordering_version,
            "oracle_problem_id": self.oracle_problem_id,
            "discovery_complete": self.discovery_complete,
            "root_use_ids": self.root_use_ids,
            "root_requirements": self.root_requirements,
            "selected_invocation_ids": self.selected_invocation_ids,
            "selected_artifact_leaf_ids": self.selected_artifact_leaf_ids,
            "satisfactions": self.satisfactions,
            "compatibility_proofs": self.compatibility_proofs,
            "snapshot_refs": self.snapshot_refs,
            "total_cost_units": self.total_cost_units,
            "selection_signature": self.selection_signature,
        }

    def to_dict(self) -> dict[str, Any]:
        values = self._values_without_id()
        payload = self._identity_payload(values)
        payload.pop("schema")
        return {"plan_id": self.plan_id, **payload}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "CandidateDerivationPlan":
        raw = _exact(value, cls, "CandidateDerivationPlan")
        for name in ("root_use_ids", "root_requirements",
                     "selected_invocation_ids",
                     "selected_artifact_leaf_ids", "satisfactions",
                     "compatibility_proofs", "snapshot_refs",
                     "selection_signature"):
            if not isinstance(raw[name], list):
                raise ValueError(f"CandidateDerivationPlan.{name} must be an array")
        raw["root_use_ids"] = tuple(raw["root_use_ids"])
        raw["root_requirements"] = tuple(
            tuple(value) for value in raw["root_requirements"])
        raw["selected_invocation_ids"] = tuple(raw["selected_invocation_ids"])
        raw["selected_artifact_leaf_ids"] = tuple(
            raw["selected_artifact_leaf_ids"])
        raw["satisfactions"] = tuple(
            SatisfactionBinding.from_dict(item) for item in raw["satisfactions"])
        raw["compatibility_proofs"] = tuple(
            CompatibilityProofRecord.from_dict(item)
            for item in raw["compatibility_proofs"])
        raw["snapshot_refs"] = tuple(
            PlanSnapshotRef.from_dict(item) for item in raw["snapshot_refs"])
        raw["selection_signature"] = tuple(raw["selection_signature"])
        plan = cls(**raw)
        plan.validate_identity()
        return plan


@dataclass(frozen=True)
class BoundInvocationBinding:
    """Exact result-affecting implementation of one scientific invocation.

    Placement, site choice, and attempt resources intentionally live in a
    separate :class:`DeploymentPlan`, so rescheduling cannot silently change
    the scientific derivation identity.
    """

    invocation_id: str
    component_id: str
    component_version: str
    implementation_digest: str
    operation_key: str
    runtime_parameters: dict[str, Any]

    def __post_init__(self) -> None:
        for value, label in (
                (self.invocation_id, "invocation identity"),
                (self.component_id, "component identity"),
                (self.component_version, "component version"),
                (self.operation_key, "operation key")):
            _text(value, label)
        _digest(self.implementation_digest, "implementation digest")
        object.__setattr__(self, "runtime_parameters",
                           freeze_json(self.runtime_parameters))
        if not isinstance(self.runtime_parameters, dict):
            raise ValueError("runtime parameters must be an object")

    def to_dict(self) -> dict[str, Any]:
        return {
            "invocation_id": self.invocation_id,
            "component_id": self.component_id,
            "component_version": self.component_version,
            "implementation_digest": self.implementation_digest,
            "operation_key": self.operation_key,
            "runtime_parameters": strict_copy(self.runtime_parameters),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "BoundInvocationBinding":
        return cls(**_exact(value, cls, "BoundInvocationBinding"))


@dataclass(frozen=True)
class InvocationDeploymentBinding:
    """Operational placement and resources for one bound invocation."""

    invocation_id: str
    execution_profile_id: str
    deployment_class_id: str
    resource_request: dict[str, Any]

    def __post_init__(self) -> None:
        for value, label in (
                (self.invocation_id, "invocation identity"),
                (self.execution_profile_id, "execution profile identity"),
                (self.deployment_class_id, "deployment class identity")):
            _text(value, label)
        object.__setattr__(self, "resource_request",
                           freeze_json(self.resource_request))
        if not isinstance(self.resource_request, dict):
            raise ValueError("resource request must be an object")

    def to_dict(self) -> dict[str, Any]:
        return {
            "invocation_id": self.invocation_id,
            "execution_profile_id": self.execution_profile_id,
            "deployment_class_id": self.deployment_class_id,
            "resource_request": strict_copy(self.resource_request),
        }

    @classmethod
    def from_dict(
            cls, value: dict[str, Any],
    ) -> "InvocationDeploymentBinding":
        return cls(**_exact(value, cls, "InvocationDeploymentBinding"))


@dataclass(frozen=True)
class ArtifactLeafBinding:
    leaf_id: str
    descriptor_id: str
    manifest_root: str
    content_digest: str

    def __post_init__(self) -> None:
        _text(self.leaf_id, "artifact leaf identity")
        _text(self.descriptor_id, "artifact descriptor identity")
        _digest(self.manifest_root, "artifact manifest root")
        _digest(self.content_digest, "artifact content digest")

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ArtifactLeafBinding":
        return cls(**_exact(value, cls, "ArtifactLeafBinding"))


@dataclass(frozen=True)
class BoundDerivationPlan:
    """Candidate plan plus exact result-affecting realizations."""

    bound_plan_id: str
    schema_version: str
    candidate_plan: CandidateDerivationPlan
    invocation_bindings: tuple[BoundInvocationBinding, ...]
    artifact_bindings: tuple[ArtifactLeafBinding, ...]
    scientific_snapshot_refs: tuple[PlanSnapshotRef, ...]

    def __post_init__(self) -> None:
        _digest(self.bound_plan_id, "bound plan identity")
        _text(self.schema_version, "bound plan schema")
        if not isinstance(self.candidate_plan, CandidateDerivationPlan):
            raise TypeError("bound plan requires a CandidateDerivationPlan")
        self.candidate_plan.validate_identity()
        for values, cls, key, label in (
                (self.invocation_bindings, BoundInvocationBinding,
                 lambda value: value.invocation_id, "invocation bindings"),
                (self.artifact_bindings, ArtifactLeafBinding,
                 lambda value: value.leaf_id, "artifact bindings"),
                (self.scientific_snapshot_refs, PlanSnapshotRef,
                 lambda value: value.name, "scientific snapshots")):
            if (not isinstance(values, tuple)
                    or not all(isinstance(value, cls) for value in values)
                    or tuple(sorted(values, key=key)) != values
                    or len({key(value) for value in values}) != len(values)):
                raise ValueError(f"{label} must be unique and canonically ordered")
        if ({value.invocation_id for value in self.invocation_bindings}
                != set(self.candidate_plan.selected_invocation_ids)):
            raise ValueError("bound invocation bindings do not match candidate plan")
        if ({value.leaf_id for value in self.artifact_bindings}
                != set(self.candidate_plan.selected_artifact_leaf_ids)):
            raise ValueError("bound artifact bindings do not match candidate plan")

    @classmethod
    def bind(
            cls, candidate_plan: CandidateDerivationPlan, *,
            invocation_bindings: Iterable[BoundInvocationBinding],
            artifact_bindings: Iterable[ArtifactLeafBinding],
            scientific_snapshot_refs: Iterable[PlanSnapshotRef] = (),
            schema_version: str = "stage2-bound-derivation-plan-v1",
    ) -> "BoundDerivationPlan":
        values = {
            "schema_version": schema_version,
            "candidate_plan": candidate_plan,
            "invocation_bindings": tuple(sorted(
                invocation_bindings, key=lambda value: value.invocation_id)),
            "artifact_bindings": tuple(sorted(
                artifact_bindings, key=lambda value: value.leaf_id)),
            "scientific_snapshot_refs": tuple(sorted(
                scientific_snapshot_refs, key=lambda value: value.name)),
        }
        return cls(
            bound_plan_id=strict_hash(cls._identity_payload(values)),
            **values,
        )

    @staticmethod
    def _identity_payload(values: dict[str, Any]) -> dict[str, Any]:
        return {
            "schema": "stage2-bound-derivation-plan-identity-v1",
            "schema_version": values["schema_version"],
            "candidate_plan": values["candidate_plan"].to_dict(),
            "invocation_bindings": [value.to_dict()
                                    for value in values["invocation_bindings"]],
            "artifact_bindings": [value.to_dict()
                                  for value in values["artifact_bindings"]],
            "scientific_snapshot_refs": [value.to_dict()
                for value in values["scientific_snapshot_refs"]],
        }

    def validate_identity(self) -> None:
        values = {
            "schema_version": self.schema_version,
            "candidate_plan": self.candidate_plan,
            "invocation_bindings": self.invocation_bindings,
            "artifact_bindings": self.artifact_bindings,
            "scientific_snapshot_refs": self.scientific_snapshot_refs,
        }
        if strict_hash(self._identity_payload(values)) != self.bound_plan_id:
            raise ValueError("bound derivation plan identity does not verify")

    def to_dict(self) -> dict[str, Any]:
        values = {
            "schema_version": self.schema_version,
            "candidate_plan": self.candidate_plan,
            "invocation_bindings": self.invocation_bindings,
            "artifact_bindings": self.artifact_bindings,
            "scientific_snapshot_refs": self.scientific_snapshot_refs,
        }
        payload = self._identity_payload(values)
        payload.pop("schema")
        return {"bound_plan_id": self.bound_plan_id, **payload}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "BoundDerivationPlan":
        raw = _exact(value, cls, "BoundDerivationPlan")
        for name in ("invocation_bindings", "artifact_bindings",
                     "scientific_snapshot_refs"):
            if not isinstance(raw[name], list):
                raise ValueError(f"BoundDerivationPlan.{name} must be an array")
        raw["candidate_plan"] = CandidateDerivationPlan.from_dict(
            raw["candidate_plan"])
        raw["invocation_bindings"] = tuple(
            BoundInvocationBinding.from_dict(item)
            for item in raw["invocation_bindings"])
        raw["artifact_bindings"] = tuple(
            ArtifactLeafBinding.from_dict(item)
            for item in raw["artifact_bindings"])
        raw["scientific_snapshot_refs"] = tuple(
            PlanSnapshotRef.from_dict(item)
            for item in raw["scientific_snapshot_refs"])
        plan = cls(**raw)
        plan.validate_identity()
        return plan


@dataclass(frozen=True)
class DeploymentPlan:
    """A replaceable operational realization of a bound scientific plan."""

    deployment_plan_id: str
    schema_version: str
    bound_plan_id: str
    deployment_snapshot_ref: PlanSnapshotRef
    invocation_bindings: tuple[InvocationDeploymentBinding, ...]

    def __post_init__(self) -> None:
        _digest(self.deployment_plan_id, "deployment plan identity")
        _text(self.schema_version, "deployment plan schema")
        _digest(self.bound_plan_id, "bound plan identity")
        if not isinstance(self.deployment_snapshot_ref, PlanSnapshotRef):
            raise TypeError("deployment snapshot reference must be PlanSnapshotRef")
        if (not isinstance(self.invocation_bindings, tuple)
                or not all(isinstance(value, InvocationDeploymentBinding)
                           for value in self.invocation_bindings)
                or self.invocation_bindings != tuple(sorted(
                    self.invocation_bindings,
                    key=lambda value: value.invocation_id))
                or len({value.invocation_id for value in self.invocation_bindings})
                != len(self.invocation_bindings)):
            raise ValueError(
                "deployment bindings must be unique and canonically ordered")

    @classmethod
    def bind(
            cls, bound_plan: BoundDerivationPlan, *,
            deployment_snapshot_ref: PlanSnapshotRef,
            invocation_bindings: Iterable[InvocationDeploymentBinding],
            schema_version: str = "stage2-deployment-plan-v1",
    ) -> "DeploymentPlan":
        if not isinstance(bound_plan, BoundDerivationPlan):
            raise TypeError("bound_plan must be BoundDerivationPlan")
        bound_plan.validate_identity()
        bindings = tuple(sorted(
            invocation_bindings, key=lambda value: value.invocation_id))
        if ({value.invocation_id for value in bindings}
                != set(bound_plan.candidate_plan.selected_invocation_ids)):
            raise ValueError(
                "deployment bindings do not match the bound derivation plan")
        values = {
            "schema_version": schema_version,
            "bound_plan_id": bound_plan.bound_plan_id,
            "deployment_snapshot_ref": deployment_snapshot_ref,
            "invocation_bindings": bindings,
        }
        return cls(
            deployment_plan_id=strict_hash(cls._identity_payload(values)),
            **values,
        )

    @staticmethod
    def _identity_payload(values: dict[str, Any]) -> dict[str, Any]:
        return {
            "schema": "stage2-deployment-plan-identity-v1",
            "schema_version": values["schema_version"],
            "bound_plan_id": values["bound_plan_id"],
            "deployment_snapshot_ref":
                values["deployment_snapshot_ref"].to_dict(),
            "invocation_bindings": [value.to_dict()
                                    for value in values["invocation_bindings"]],
        }

    def validate_identity(self) -> None:
        values = {
            "schema_version": self.schema_version,
            "bound_plan_id": self.bound_plan_id,
            "deployment_snapshot_ref": self.deployment_snapshot_ref,
            "invocation_bindings": self.invocation_bindings,
        }
        if strict_hash(self._identity_payload(values)) != self.deployment_plan_id:
            raise ValueError("deployment plan identity does not verify")

    def to_dict(self) -> dict[str, Any]:
        values = {
            "schema_version": self.schema_version,
            "bound_plan_id": self.bound_plan_id,
            "deployment_snapshot_ref": self.deployment_snapshot_ref,
            "invocation_bindings": self.invocation_bindings,
        }
        payload = self._identity_payload(values)
        payload.pop("schema")
        return {"deployment_plan_id": self.deployment_plan_id, **payload}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "DeploymentPlan":
        raw = _exact(value, cls, "DeploymentPlan")
        if not isinstance(raw["invocation_bindings"], list):
            raise ValueError("DeploymentPlan.invocation_bindings must be an array")
        raw["deployment_snapshot_ref"] = PlanSnapshotRef.from_dict(
            raw["deployment_snapshot_ref"])
        raw["invocation_bindings"] = tuple(
            InvocationDeploymentBinding.from_dict(item)
            for item in raw["invocation_bindings"])
        plan = cls(**raw)
        plan.validate_identity()
        return plan
