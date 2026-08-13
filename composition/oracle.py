"""Exhaustive correctness oracle for explicit small derivation graphs.

This module does not discover capabilities and is not a production-scale
selector.  It intentionally enumerates a frozen, explicit candidate universe
using mechanics independent from the Stage-3 MILP implementation.  Its role is
to make the selection semantics executable and serve as a long-lived oracle.
"""
from __future__ import annotations

import dataclasses
import itertools
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Iterator

from capabilities import (
    ArtifactLeaf,
    BoundInvocation,
    DeploymentFeasibilityProof,
)
from contracts import (
    CompatibilityProof,
    DistinctnessPolicy,
    RequirementUse,
)
from engine.runtime.identity import freeze_json, strict_copy, strict_hash
from plans import (
    CandidateDerivationPlan,
    CompatibilityProofRecord,
    PlanSnapshotRef,
    ProducerKind,
    ProducerOutputRef,
    SatisfactionBinding,
    SatisfactionKind,
)


def _text(value: str, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")


def _integer(value: int, label: str, *, minimum: int = 0) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{label} must be an integer >= {minimum}")


def _unique_text(values: tuple[str, ...], label: str,
                 *, nonempty: bool = False) -> None:
    if (not isinstance(values, tuple)
            or (nonempty and not values)
            or any(not isinstance(value, str) or not value for value in values)
            or len(values) != len(set(values))):
        raise ValueError(f"{label} must contain unique, non-empty strings")


class DistinctBy(str, Enum):
    NONE = "NONE"
    OUTPUT = "OUTPUT"
    PRODUCER = "PRODUCER"


class BlockerLogic(str, Enum):
    LEAF = "LEAF"
    ALL = "ALL"
    ANY = "ANY"
    RANGE = "RANGE"


class OracleStatus(str, Enum):
    OPTIMAL = "OPTIMAL"
    UNSATISFIABLE = "UNSATISFIABLE"
    INCOMPLETE = "INCOMPLETE"


@dataclass(frozen=True)
class CardinalityRange:
    """Number of producer outputs when the producer alternative is chosen.

    A zero lower bound is normalized to an explicit optional omission during
    enumeration; a ``PRODUCERS`` binding itself always contains at least one
    output.  This keeps omission, default, and producer choices distinguishable.
    """

    minimum: int = 1
    maximum: int = 1

    def __post_init__(self) -> None:
        _integer(self.minimum, "cardinality minimum")
        _integer(self.maximum, "cardinality maximum", minimum=self.minimum)


@dataclass(frozen=True)
class RequirementUseNode:
    """A distinct consumer-port use of a potentially shared requirement."""

    use_id: str
    requirement_id: str
    port_id: str
    owner_invocation_id: str | None = None
    optional: bool = False
    default_id: str | None = None
    cardinality: CardinalityRange = field(default_factory=CardinalityRange)
    distinct_by: DistinctBy = DistinctBy.OUTPUT
    distinctness_group: str | None = None
    shareable: bool = True

    def __post_init__(self) -> None:
        for value, label in (
                (self.use_id, "requirement-use identity"),
                (self.requirement_id, "requirement identity"),
                (self.port_id, "consumer port identity")):
            _text(value, label)
        if self.owner_invocation_id is not None:
            _text(self.owner_invocation_id, "owner invocation identity")
        if type(self.optional) is not bool or type(self.shareable) is not bool:
            raise TypeError("optional and shareable must be bool")
        if self.default_id is not None:
            _text(self.default_id, "default identity")
            if not self.optional:
                raise ValueError("only an optional use may declare a default")
        if not isinstance(self.cardinality, CardinalityRange):
            raise TypeError("cardinality must be CardinalityRange")
        if not self.optional and self.cardinality.minimum == 0:
            raise ValueError("a required use must require a producer")
        if not isinstance(self.distinct_by, DistinctBy):
            raise TypeError("distinct_by must be DistinctBy")
        if self.distinctness_group is not None:
            _text(self.distinctness_group, "distinctness group")
            if self.distinct_by is DistinctBy.NONE:
                raise ValueError("a distinctness group requires a distinct key")

    @classmethod
    def from_requirement_use(
            cls, use: RequirementUse, *,
            owner_invocation_id: str | None = None,
            distinctness_group: str | None = None,
    ) -> "RequirementUseNode":
        """Project a typed scientific port use without collapsing its identity."""

        if not isinstance(use, RequirementUse):
            raise TypeError("use must be contracts.RequirementUse")
        distinct_by = {
            DistinctnessPolicy.ALLOW_SAME: DistinctBy.NONE,
            DistinctnessPolicy.DISTINCT_ARTIFACT: DistinctBy.OUTPUT,
            DistinctnessPolicy.DISTINCT_PRODUCER: DistinctBy.PRODUCER,
        }[use.distinctness]
        return cls(
            use_id=use.requirement_use_id,
            requirement_id=use.requirement.requirement_id,
            port_id=use.port_id,
            owner_invocation_id=owner_invocation_id,
            optional=use.optional,
            default_id=use.default_id,
            cardinality=CardinalityRange(
                use.cardinality.minimum, use.cardinality.maximum),
            distinct_by=distinct_by,
            distinctness_group=distinctness_group,
            shareable=use.shareable,
        )


@dataclass(frozen=True)
class InvocationNode:
    """One already-bound capability invocation in the explicit oracle graph."""

    invocation_id: str
    input_use_ids: tuple[str, ...]
    output_port_ids: tuple[str, ...]
    cost_units: int
    deployable: bool = True
    zero_input_source: bool = False
    atomic_iterative: bool = False

    def __post_init__(self) -> None:
        _text(self.invocation_id, "invocation identity")
        _unique_text(self.input_use_ids, "invocation input-use IDs")
        _unique_text(self.output_port_ids, "invocation output-port IDs",
                     nonempty=True)
        if (self.input_use_ids != tuple(sorted(self.input_use_ids))
                or self.output_port_ids != tuple(sorted(self.output_port_ids))):
            raise ValueError("invocation ports must be in canonical order")
        _integer(self.cost_units, "invocation cost")
        if (type(self.deployable) is not bool
                or type(self.zero_input_source) is not bool
                or type(self.atomic_iterative) is not bool):
            raise TypeError("invocation flags must be bool")

    @classmethod
    def from_bound_invocation(
            cls, invocation: BoundInvocation, *,
            deployment: DeploymentFeasibilityProof,
            atomic_iterative: bool = False,
    ) -> "InvocationNode":
        if not isinstance(invocation, BoundInvocation):
            raise TypeError("invocation must be capabilities.BoundInvocation")
        if not isinstance(deployment, DeploymentFeasibilityProof):
            raise TypeError("deployment must be DeploymentFeasibilityProof")
        if deployment.profile_id != invocation.execution_profile_id:
            raise ValueError(
                "deployment proof does not cover the invocation profile")
        if invocation.cost_model_id != "cost:declared-v1":
            raise ValueError(
                "typed oracle adapter requires cost:declared-v1")
        cost_units = invocation.metric_estimates.get("cost_units")
        _integer(cost_units, "declared invocation cost")
        deployable = deployment.feasible
        return cls(
            invocation_id=invocation.invocation_key,
            input_use_ids=tuple(sorted(
                use.requirement_use_id for use in invocation.input_uses)),
            output_port_ids=tuple(sorted(
                output.port_id for output in invocation.outputs)),
            cost_units=cost_units,
            deployable=deployable,
            zero_input_source=not invocation.input_uses,
            atomic_iterative=atomic_iterative,
        )


@dataclass(frozen=True)
class ArtifactLeafNode:
    """An artifact candidate plus a separately supplied commit-trust state."""

    leaf_id: str
    output_port_ids: tuple[str, ...]
    cost_units: int = 0
    committed: bool = False

    def __post_init__(self) -> None:
        _text(self.leaf_id, "artifact leaf identity")
        _unique_text(self.output_port_ids, "artifact output-port IDs",
                     nonempty=True)
        if self.output_port_ids != tuple(sorted(self.output_port_ids)):
            raise ValueError("artifact output ports must be in canonical order")
        _integer(self.cost_units, "artifact leaf cost")
        if type(self.committed) is not bool:
            raise TypeError("artifact committed flag must be bool")

    @classmethod
    def from_artifact_leaf(
            cls, leaf: ArtifactLeaf, *, output_port_id: str = "artifact",
            cost_units: int = 0,
    ) -> "ArtifactLeafNode":
        if not isinstance(leaf, ArtifactLeaf):
            raise TypeError("leaf must be capabilities.ArtifactLeaf")
        return cls(
            leaf_id=leaf.leaf_id,
            output_port_ids=(output_port_id,),
            cost_units=cost_units,
            # A typed declaration does not attest presence in a trusted store.
            committed=False,
        )


@dataclass(frozen=True)
class SatisfactionArc:
    """One directly compatible producer-output/use edge."""

    use_id: str
    producer_id: str
    producer_kind: ProducerKind
    output_port_id: str
    proof: CompatibilityProofRecord

    def __post_init__(self) -> None:
        _text(self.use_id, "arc requirement-use identity")
        _text(self.producer_id, "arc producer identity")
        if not isinstance(self.producer_kind, ProducerKind):
            raise TypeError("arc producer_kind must be ProducerKind")
        _text(self.output_port_id, "arc output-port identity")
        if not isinstance(self.proof, CompatibilityProofRecord):
            raise TypeError("arc proof must be CompatibilityProofRecord")
        if self.proof.payload.get("satisfied") is not True:
            raise ValueError("a satisfaction arc requires a satisfied direct-match proof")

    @classmethod
    def from_compatibility(
            cls, use: RequirementUse,
            producer: BoundInvocation | ArtifactLeaf,
            output_port_id: str,
            proof: CompatibilityProof,
    ) -> "SatisfactionArc":
        """Create an edge only from a satisfied typed direct-match proof."""

        if not isinstance(use, RequirementUse):
            raise TypeError("use must be contracts.RequirementUse")
        if not isinstance(proof, CompatibilityProof):
            raise TypeError("proof must be contracts.CompatibilityProof")
        if proof.requirement_id != use.requirement.requirement_id:
            raise ValueError("compatibility proof covers a different requirement")
        if not proof.satisfied:
            raise ValueError("an unsatisfied proof cannot form a satisfaction arc")
        kind, producer_id, descriptor_id = _scientific_producer_identity(
            producer, output_port_id)
        if proof.descriptor_id != descriptor_id:
            raise ValueError("compatibility proof covers a different descriptor")
        return cls(
            use_id=use.requirement_use_id,
            producer_id=producer_id,
            producer_kind=kind,
            output_port_id=output_port_id,
            proof=CompatibilityProofRecord.from_compatibility(proof),
        )

    @property
    def output_ref(self) -> ProducerOutputRef:
        return ProducerOutputRef(
            producer_id=self.producer_id,
            producer_kind=self.producer_kind,
            output_port_id=self.output_port_id,
            proof_id=self.proof.proof_id,
        )

    @property
    def arc_id(self) -> str:
        return SatisfactionBinding(
            use_id=self.use_id,
            kind=SatisfactionKind.PRODUCERS,
            outputs=(self.output_ref,),
        ).choice_ids[0]


@dataclass(frozen=True)
class CandidateRejection:
    """A considered producer output that failed direct compatibility."""

    use_id: str
    producer_id: str
    producer_kind: ProducerKind
    output_port_id: str
    rejection_codes: tuple[str, ...]
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for value, label in (
                (self.use_id, "rejected requirement-use identity"),
                (self.producer_id, "rejected producer identity"),
                (self.output_port_id, "rejected output-port identity")):
            _text(value, label)
        if not isinstance(self.producer_kind, ProducerKind):
            raise TypeError("rejected producer_kind must be ProducerKind")
        _unique_text(self.rejection_codes, "direct-match rejection codes",
                     nonempty=True)
        object.__setattr__(self, "details", freeze_json(self.details))
        if not isinstance(self.details, dict):
            raise ValueError("candidate rejection details must be a JSON object")

    @classmethod
    def from_compatibility(
            cls, use: RequirementUse,
            producer: BoundInvocation | ArtifactLeaf,
            output_port_id: str,
            proof: CompatibilityProof,
    ) -> "CandidateRejection":
        if not isinstance(use, RequirementUse):
            raise TypeError("use must be contracts.RequirementUse")
        if not isinstance(proof, CompatibilityProof):
            raise TypeError("proof must be contracts.CompatibilityProof")
        if proof.requirement_id != use.requirement.requirement_id:
            raise ValueError("compatibility proof covers a different requirement")
        if proof.satisfied:
            raise ValueError("a satisfied proof cannot form a rejection")
        kind, producer_id, descriptor_id = _scientific_producer_identity(
            producer, output_port_id)
        if proof.descriptor_id != descriptor_id:
            raise ValueError("compatibility proof covers a different descriptor")
        return cls(
            use_id=use.requirement_use_id,
            producer_id=producer_id,
            producer_kind=kind,
            output_port_id=output_port_id,
            rejection_codes=tuple(sorted(
                code.value for code in proof.rejection_codes)),
            details={"compatibility_proof": _compatibility_payload(proof)},
        )

    @property
    def candidate_key(self) -> tuple[str, str, str, str]:
        return (self.use_id, self.producer_kind.value,
                self.producer_id, self.output_port_id)


def _compatibility_payload(proof: CompatibilityProof) -> dict[str, Any]:
    """Serialize exact checks plus computed acceptance/caveat projections."""

    return {
        "schema": "stage2-selected-compatibility-proof-v1",
        "proof_id": proof.proof_id,
        "satisfied": proof.satisfied,
        "rejection_codes": [code.value for code in proof.rejection_codes],
        "caveat_codes": [code.value for code in proof.caveat_codes],
        "proof": proof.to_dict(),
    }


def validate_compatibility_record(
        record: CompatibilityProofRecord,
) -> CompatibilityProof:
    """Authenticate the typed direct-match proof inside a selected edge."""
    payload = strict_copy(record.payload)
    required = {
        "schema", "proof_id", "satisfied", "rejection_codes",
        "caveat_codes", "proof",
    }
    if set(payload) != required or payload["schema"] != (
            "stage2-selected-compatibility-proof-v1"):
        raise ValueError("selected edge has an unsupported proof wrapper")
    proof = CompatibilityProof.from_dict(payload["proof"])
    if (proof.proof_id != payload["proof_id"]
            or proof.satisfied is not payload["satisfied"]
            or [value.value for value in proof.rejection_codes]
            != payload["rejection_codes"]
            or [value.value for value in proof.caveat_codes]
            != payload["caveat_codes"]):
        raise ValueError("selected compatibility proof wrapper does not verify")
    return proof


def _scientific_producer_identity(
        producer: BoundInvocation | ArtifactLeaf,
        output_port_id: str,
) -> tuple[ProducerKind, str, str]:
    if isinstance(producer, BoundInvocation):
        output = producer.output(output_port_id)
        return (ProducerKind.INVOCATION, producer.invocation_key,
                output.descriptor.descriptor_id)
    if isinstance(producer, ArtifactLeaf):
        _text(output_port_id, "artifact output port")
        return (ProducerKind.ARTIFACT_LEAF, producer.leaf_id,
                producer.descriptor.descriptor_id)
    raise TypeError("producer must be BoundInvocation or ArtifactLeaf")


@dataclass(frozen=True)
class OracleProblem:
    """Frozen explicit candidate universe consumed by the exhaustive oracle."""

    problem_id: str
    name: str
    uses: tuple[RequirementUseNode, ...]
    root_use_ids: tuple[str, ...]
    invocations: tuple[InvocationNode, ...]
    artifact_leaves: tuple[ArtifactLeafNode, ...]
    satisfaction_arcs: tuple[SatisfactionArc, ...]
    candidate_rejections: tuple[CandidateRejection, ...]
    snapshot_refs: tuple[PlanSnapshotRef, ...] = ()

    def __post_init__(self) -> None:
        _text(self.problem_id, "oracle problem identity")
        _text(self.name, "oracle problem name")
        for values, cls, key, label in (
                (self.uses, RequirementUseNode, lambda value: value.use_id,
                 "requirement uses"),
                (self.invocations, InvocationNode,
                 lambda value: value.invocation_id, "invocations"),
                (self.artifact_leaves, ArtifactLeafNode,
                 lambda value: value.leaf_id, "artifact leaves"),
                (self.satisfaction_arcs, SatisfactionArc,
                 lambda value: value.arc_id, "satisfaction arcs"),
                (self.candidate_rejections, CandidateRejection,
                 lambda value: value.candidate_key, "candidate rejections"),
                (self.snapshot_refs, PlanSnapshotRef,
                 lambda value: value.name, "snapshot references")):
            if (not isinstance(values, tuple)
                    or not all(isinstance(value, cls) for value in values)
                    or tuple(sorted(values, key=key)) != values
                    or len({key(value) for value in values}) != len(values)):
                raise ValueError(f"{label} must be unique and canonically ordered")
        _unique_text(self.root_use_ids, "root requirement-use IDs", nonempty=True)
        use_by_id = {value.use_id: value for value in self.uses}
        invocation_by_id = {value.invocation_id: value
                            for value in self.invocations}
        leaf_by_id = {value.leaf_id: value for value in self.artifact_leaves}
        if set(invocation_by_id) & set(leaf_by_id):
            raise ValueError("producer identities must be globally unambiguous")
        if not set(self.root_use_ids).issubset(use_by_id):
            raise ValueError("root references an unknown requirement use")
        for root_id in self.root_use_ids:
            if use_by_id[root_id].owner_invocation_id is not None:
                raise ValueError("a root requirement use cannot have an owner")

        owned: dict[str, str] = {}
        for invocation in self.invocations:
            for use_id in invocation.input_use_ids:
                if use_id not in use_by_id:
                    raise ValueError(
                        f"invocation {invocation.invocation_id!r} references "
                        f"unknown input use {use_id!r}")
                use = use_by_id[use_id]
                if use.owner_invocation_id != invocation.invocation_id:
                    raise ValueError(
                        f"input use {use_id!r} has the wrong owner")
                if use_id in owned:
                    raise ValueError(f"input use {use_id!r} has several owners")
                owned[use_id] = invocation.invocation_id
        nonroots = set(use_by_id) - set(self.root_use_ids)
        if nonroots != set(owned):
            raise ValueError("every non-root use must belong to one invocation")

        group_kind: dict[str, DistinctBy] = {}
        for use in self.uses:
            if use.owner_invocation_id is not None and use.owner_invocation_id not in invocation_by_id:
                raise ValueError(f"use {use.use_id!r} has an unknown owner")
            if use.distinctness_group is not None:
                previous = group_kind.setdefault(
                    use.distinctness_group, use.distinct_by)
                if previous is not use.distinct_by:
                    raise ValueError(
                        "all uses in a distinctness group need the same key")

        arc_keys: set[tuple[str, str, str, str]] = set()
        for arc in self.satisfaction_arcs:
            if arc.use_id not in use_by_id:
                raise ValueError("satisfaction arc references an unknown use")
            ports = self._producer_ports(
                arc.producer_kind, arc.producer_id,
                invocation_by_id, leaf_by_id)
            if arc.output_port_id not in ports:
                raise ValueError("satisfaction arc references an unknown output port")
            key = (arc.use_id, arc.producer_kind.value,
                   arc.producer_id, arc.output_port_id)
            if key in arc_keys:
                raise ValueError("producer output can have only one proof per use")
            arc_keys.add(key)
        for rejection in self.candidate_rejections:
            if rejection.use_id not in use_by_id:
                raise ValueError("candidate rejection references an unknown use")
            ports = self._producer_ports(
                rejection.producer_kind, rejection.producer_id,
                invocation_by_id, leaf_by_id)
            if rejection.output_port_id not in ports:
                raise ValueError("candidate rejection references an unknown output port")
            if rejection.candidate_key in arc_keys:
                raise ValueError("one candidate cannot be both matched and rejected")
        if strict_hash(self._identity_payload()) != self.problem_id:
            raise ValueError("oracle problem identity does not verify")

    @staticmethod
    def _producer_ports(
            kind: ProducerKind, producer_id: str,
            invocations: dict[str, InvocationNode],
            leaves: dict[str, ArtifactLeafNode]) -> tuple[str, ...]:
        try:
            if kind is ProducerKind.INVOCATION:
                return invocations[producer_id].output_port_ids
            return leaves[producer_id].output_port_ids
        except KeyError as exc:
            raise ValueError("candidate references an unknown producer") from exc

    @classmethod
    def bind(
            cls, name: str, *, uses: Iterable[RequirementUseNode],
            root_use_ids: Iterable[str],
            invocations: Iterable[InvocationNode] = (),
            artifact_leaves: Iterable[ArtifactLeafNode] = (),
            satisfaction_arcs: Iterable[SatisfactionArc] = (),
            candidate_rejections: Iterable[CandidateRejection] = (),
            snapshot_refs: Iterable[PlanSnapshotRef] = (),
    ) -> "OracleProblem":
        values = {
            "name": name,
            "uses": tuple(sorted(uses, key=lambda value: value.use_id)),
            "root_use_ids": tuple(root_use_ids),
            "invocations": tuple(sorted(
                invocations, key=lambda value: value.invocation_id)),
            "artifact_leaves": tuple(sorted(
                artifact_leaves, key=lambda value: value.leaf_id)),
            "satisfaction_arcs": tuple(sorted(
                satisfaction_arcs, key=lambda value: value.arc_id)),
            "candidate_rejections": tuple(sorted(
                candidate_rejections, key=lambda value: value.candidate_key)),
            "snapshot_refs": tuple(sorted(
                snapshot_refs, key=lambda value: value.name)),
        }
        provisional = object.__new__(cls)
        for name_, value in values.items():
            object.__setattr__(provisional, name_, value)
        problem_id = strict_hash(provisional._identity_payload())
        return cls(problem_id=problem_id, **values)

    def _identity_payload(self) -> dict[str, Any]:
        def dataclass_payload(value: Any) -> dict[str, Any]:
            result = dataclasses.asdict(value)
            for key, item in tuple(result.items()):
                if isinstance(item, Enum):
                    result[key] = item.value
            return strict_copy(result)

        return {
            "schema": "stage2-explicit-oracle-problem-v1",
            "name": self.name,
            "uses": [dataclass_payload(value) for value in self.uses],
            "root_use_ids": list(self.root_use_ids),
            "invocations": [dataclass_payload(value)
                            for value in self.invocations],
            "artifact_leaves": [dataclass_payload(value)
                                for value in self.artifact_leaves],
            "satisfaction_arcs": [{
                "use_id": value.use_id,
                "producer_id": value.producer_id,
                "producer_kind": value.producer_kind.value,
                "output_port_id": value.output_port_id,
                "proof": value.proof.to_dict(),
            } for value in self.satisfaction_arcs],
            "candidate_rejections": [{
                "use_id": value.use_id,
                "producer_id": value.producer_id,
                "producer_kind": value.producer_kind.value,
                "output_port_id": value.output_port_id,
                "rejection_codes": list(value.rejection_codes),
                "details": strict_copy(value.details),
            } for value in self.candidate_rejections],
            "snapshot_refs": [value.to_dict() for value in self.snapshot_refs],
        }


@dataclass(frozen=True)
class BlockerNode:
    code: str
    subject_id: str
    message: str
    logic: BlockerLogic = BlockerLogic.LEAF
    children: tuple["BlockerNode", ...] = ()
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for value, label in ((self.code, "blocker code"),
                             (self.subject_id, "blocker subject"),
                             (self.message, "blocker message")):
            _text(value, label)
        if not isinstance(self.logic, BlockerLogic):
            raise TypeError("blocker logic must be BlockerLogic")
        if (not isinstance(self.children, tuple)
                or not all(isinstance(value, BlockerNode)
                           for value in self.children)):
            raise TypeError("blocker children must be BlockerNode values")
        if tuple(sorted(self.children, key=lambda value: value.blocker_id)) != self.children:
            raise ValueError("blocker children must be canonically ordered")
        if self.logic is BlockerLogic.LEAF and self.children:
            raise ValueError("a leaf blocker cannot have children")
        object.__setattr__(self, "details", freeze_json(self.details))
        if not isinstance(self.details, dict):
            raise ValueError("blocker details must be a JSON object")

    @classmethod
    def bind(cls, code: str, subject_id: str, message: str, *,
             logic: BlockerLogic = BlockerLogic.LEAF,
             children: Iterable["BlockerNode"] = (),
             details: dict[str, Any] | None = None) -> "BlockerNode":
        return cls(
            code=code,
            subject_id=subject_id,
            message=message,
            logic=logic,
            children=tuple(sorted(children, key=lambda value: value.blocker_id)),
            details={} if details is None else details,
        )

    @property
    def blocker_id(self) -> str:
        return strict_hash(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "subject_id": self.subject_id,
            "message": self.message,
            "logic": self.logic.value,
            "children": [value.to_dict() for value in self.children],
            "details": strict_copy(self.details),
        }


@dataclass(frozen=True)
class BlockerTree:
    complete: bool
    root: BlockerNode

    def __post_init__(self) -> None:
        if type(self.complete) is not bool:
            raise TypeError("blocker-tree completeness must be bool")
        if not isinstance(self.root, BlockerNode):
            raise TypeError("blocker tree requires a BlockerNode root")

    def to_dict(self) -> dict[str, Any]:
        return {"complete": self.complete, "root": self.root.to_dict()}


@dataclass(frozen=True)
class ExhaustiveOracleResult:
    problem_id: str
    status: OracleStatus
    complete: bool
    explored_states: int
    state_limit: int
    plans: tuple[CandidateDerivationPlan, ...]
    blocker_tree: BlockerTree | None

    def __post_init__(self) -> None:
        _text(self.problem_id, "oracle result problem identity")
        if not isinstance(self.status, OracleStatus):
            raise TypeError("oracle status must be OracleStatus")
        if type(self.complete) is not bool:
            raise TypeError("oracle completeness must be bool")
        _integer(self.explored_states, "explored state count")
        _integer(self.state_limit, "oracle state limit", minimum=1)
        if (not isinstance(self.plans, tuple)
                or not all(isinstance(value, CandidateDerivationPlan)
                           for value in self.plans)):
            raise TypeError("oracle plans must be CandidateDerivationPlan values")
        expected = tuple(sorted(
            self.plans,
            key=lambda value: (
                value.total_cost_units,
                value.selection_signature,
                value.plan_id,
            ),
        ))
        if expected != self.plans or len({value.plan_id for value in self.plans}) != len(self.plans):
            raise ValueError("oracle plans must be unique and deterministically ordered")
        if self.complete != (self.status is not OracleStatus.INCOMPLETE):
            raise ValueError("oracle status and completeness disagree")
        if (self.status is OracleStatus.INCOMPLETE
                and any(value.discovery_complete for value in self.plans)):
            raise ValueError(
                "incomplete-search incumbents cannot claim complete discovery")
        if (self.status is OracleStatus.OPTIMAL
                and any(not value.discovery_complete for value in self.plans)):
            raise ValueError("optimal plans require complete discovery")
        if self.status is OracleStatus.OPTIMAL and not self.plans:
            raise ValueError("an optimal result requires a plan")
        if self.status is OracleStatus.UNSATISFIABLE and self.plans:
            raise ValueError("an unsatisfiable result cannot contain a plan")
        if ((self.status is OracleStatus.UNSATISFIABLE
             or self.status is OracleStatus.INCOMPLETE)
                and self.blocker_tree is None):
            raise ValueError("unsatisfied/incomplete results require blockers")
        if self.status is OracleStatus.OPTIMAL and self.blocker_tree is not None:
            raise ValueError("an optimal result cannot carry a blocker tree")

    @property
    def optimal_plan(self) -> CandidateDerivationPlan:
        if self.status is not OracleStatus.OPTIMAL:
            raise RuntimeError("no proven-optimal plan is available")
        return self.plans[0]

    @property
    def incumbent_plan(self) -> CandidateDerivationPlan | None:
        return self.plans[0] if self.plans else None


@dataclass(frozen=True)
class _Failure:
    code: str
    subjects: tuple[str, ...]
    details: dict[str, Any]

    @property
    def key(self) -> str:
        return strict_hash({
            "code": self.code,
            "subjects": list(self.subjects),
            "details": self.details,
        })


class _StateBudget:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.explored = 0
        self.exhausted = False

    def claim(self) -> bool:
        if self.explored >= self.limit:
            self.exhausted = True
            return False
        self.explored += 1
        return True


def exhaustive_enumerate(
        problem: OracleProblem, *, state_limit: int = 100_000,
) -> ExhaustiveOracleResult:
    """Enumerate every plan in a bounded explicit candidate universe.

    The result is optimal only if enumeration completed.  On a state limit,
    valid plans found so far are merely incumbents and the blocker tree is
    explicitly incomplete.
    """

    if not isinstance(problem, OracleProblem):
        raise TypeError("problem must be OracleProblem")
    _integer(state_limit, "oracle state limit", minimum=1)
    use_by_id = {value.use_id: value for value in problem.uses}
    invocation_by_id = {value.invocation_id: value
                        for value in problem.invocations}
    leaf_by_id = {value.leaf_id: value for value in problem.artifact_leaves}
    arcs_by_use: dict[str, tuple[SatisfactionArc, ...]] = {}
    for use in problem.uses:
        arcs_by_use[use.use_id] = tuple(sorted(
            (arc for arc in problem.satisfaction_arcs if arc.use_id == use.use_id),
            key=lambda value: value.arc_id,
        ))

    producer_order = tuple(sorted(
        [(ProducerKind.INVOCATION, value.invocation_id)
         for value in problem.invocations]
        + [(ProducerKind.ARTIFACT_LEAF, value.leaf_id)
           for value in problem.artifact_leaves],
        key=lambda value: (value[0].value, value[1]),
    ))
    all_choice_ids = _all_choice_ids(problem)
    budget = _StateBudget(state_limit)
    plans: dict[str, CandidateDerivationPlan] = {}
    failures: dict[str, _Failure] = {}

    producer_count = len(producer_order)
    for mask in range(1 << producer_count):
        if budget.exhausted:
            break
        selected = {
            producer_order[index]
            for index in range(producer_count)
            if mask & (1 << index)
        }
        active_use_ids = set(problem.root_use_ids)
        for kind, producer_id in selected:
            if kind is ProducerKind.INVOCATION:
                active_use_ids.update(invocation_by_id[producer_id].input_use_ids)
        ordered_uses = tuple(sorted(active_use_ids))
        options_by_use = {
            use_id: _options_for_use(
                use_by_id[use_id], arcs_by_use[use_id], selected)
            for use_id in ordered_uses
        }
        missing = next((use_id for use_id in ordered_uses
                        if not options_by_use[use_id]), None)
        if missing is not None:
            if not budget.claim():
                break
            continue

        for assignment in _assignment_product(ordered_uses, options_by_use):
            if not budget.claim():
                break
            assignment_by_use = {value.use_id: value for value in assignment}
            assignment_failures = _validate_assignment(
                problem, selected, assignment_by_use,
                invocation_by_id, leaf_by_id, use_by_id)
            if assignment_failures:
                for failure in assignment_failures:
                    failures.setdefault(failure.key, failure)
                continue
            plan = _make_plan(
                problem, producer_order, all_choice_ids,
                selected, assignment)
            plans.setdefault(plan.plan_id, plan)
        if budget.exhausted:
            break

    ordered_plans = tuple(sorted(
        plans.values(),
        key=lambda value: (
            value.total_cost_units,
            value.selection_signature,
            value.plan_id,
        ),
    ))
    if budget.exhausted:
        ordered_plans = tuple(_with_discovery_complete(value, False)
                              for value in ordered_plans)
        ordered_plans = tuple(sorted(
            ordered_plans,
            key=lambda value: (
                value.total_cost_units,
                value.selection_signature,
                value.plan_id,
            ),
        ))
        blocker = _build_blocker_tree(
            problem, tuple(failures.values()), complete=False,
            limit=state_limit, explored=budget.explored)
        return ExhaustiveOracleResult(
            problem_id=problem.problem_id,
            status=OracleStatus.INCOMPLETE,
            complete=False,
            explored_states=budget.explored,
            state_limit=state_limit,
            plans=ordered_plans,
            blocker_tree=blocker,
        )
    if ordered_plans:
        return ExhaustiveOracleResult(
            problem_id=problem.problem_id,
            status=OracleStatus.OPTIMAL,
            complete=True,
            explored_states=budget.explored,
            state_limit=state_limit,
            plans=ordered_plans,
            blocker_tree=None,
        )
    return ExhaustiveOracleResult(
        problem_id=problem.problem_id,
        status=OracleStatus.UNSATISFIABLE,
        complete=True,
        explored_states=budget.explored,
        state_limit=state_limit,
        plans=(),
        blocker_tree=_build_blocker_tree(
            problem, tuple(failures.values()), complete=True),
    )


def _all_choice_ids(problem: OracleProblem) -> tuple[str, ...]:
    values = {arc.arc_id for arc in problem.satisfaction_arcs}
    for use in problem.uses:
        if use.default_id is not None:
            values.update(SatisfactionBinding(
                use_id=use.use_id,
                kind=SatisfactionKind.DEFAULT,
                default_id=use.default_id,
            ).choice_ids)
        if use.optional:
            values.update(SatisfactionBinding(
                use_id=use.use_id,
                kind=SatisfactionKind.OMIT,
            ).choice_ids)
    return tuple(sorted(values))


def _options_for_use(
        use: RequirementUseNode, arcs: tuple[SatisfactionArc, ...],
        selected: set[tuple[ProducerKind, str]],
) -> tuple[SatisfactionBinding, ...]:
    available = tuple(arc for arc in arcs
                      if (arc.producer_kind, arc.producer_id) in selected)
    result: list[SatisfactionBinding] = []
    upper = min(use.cardinality.maximum, len(available))
    for count in range(max(1, use.cardinality.minimum), upper + 1):
        for combination in itertools.combinations(available, count):
            references = tuple(sorted(
                (arc.output_ref for arc in combination),
                key=lambda value: value.choice_id,
            ))
            if not _within_use_distinct(use, references):
                continue
            result.append(SatisfactionBinding(
                use_id=use.use_id,
                kind=SatisfactionKind.PRODUCERS,
                outputs=references,
            ))
    if use.default_id is not None:
        result.append(SatisfactionBinding(
            use_id=use.use_id,
            kind=SatisfactionKind.DEFAULT,
            default_id=use.default_id,
        ))
    if use.optional:
        result.append(SatisfactionBinding(
            use_id=use.use_id,
            kind=SatisfactionKind.OMIT,
        ))
    return tuple(sorted(result, key=lambda value: value.choice_ids))


def _within_use_distinct(
        use: RequirementUseNode,
        outputs: tuple[ProducerOutputRef, ...],
) -> bool:
    if use.distinct_by is DistinctBy.NONE:
        return True
    keys = [_distinct_key(use.distinct_by, value) for value in outputs]
    return len(keys) == len(set(keys))


def _assignment_product(
        ordered_use_ids: tuple[str, ...],
        options_by_use: dict[str, tuple[SatisfactionBinding, ...]],
) -> Iterator[tuple[SatisfactionBinding, ...]]:
    if not ordered_use_ids:
        yield ()
        return
    yield from itertools.product(
        *(options_by_use[use_id] for use_id in ordered_use_ids))


def _validate_assignment(
        problem: OracleProblem,
        selected: set[tuple[ProducerKind, str]],
        assignments: dict[str, SatisfactionBinding],
        invocation_by_id: dict[str, InvocationNode],
        leaf_by_id: dict[str, ArtifactLeafNode],
        use_by_id: dict[str, RequirementUseNode],
) -> tuple[_Failure, ...]:
    failures: list[_Failure] = []
    referenced: set[tuple[ProducerKind, str]] = set()
    for binding in assignments.values():
        for output in binding.outputs:
            referenced.add((output.producer_kind, output.producer_id))
    for kind, producer_id in sorted(selected,
                                    key=lambda value: (value[0].value, value[1])):
        if (kind, producer_id) not in referenced:
            failures.append(_failure(
                "ORPHAN_PRODUCER", (producer_id,),
                {"producer_kind": kind.value}))
        if kind is ProducerKind.INVOCATION:
            if not invocation_by_id[producer_id].deployable:
                failures.append(_failure(
                    "DEPLOYMENT_INFEASIBLE", (producer_id,), {}))
        elif not leaf_by_id[producer_id].committed:
            failures.append(_failure(
                "ARTIFACT_NOT_COMMITTED", (producer_id,), {}))

    output_uses: dict[tuple[str, str, str], set[str]] = {}
    groups: dict[str, list[tuple[RequirementUseNode, ProducerOutputRef]]] = {}
    for use_id, binding in assignments.items():
        use = use_by_id[use_id]
        for output in binding.outputs:
            output_key = _output_key(output)
            output_uses.setdefault(output_key, set()).add(use_id)
            if use.distinctness_group is not None:
                groups.setdefault(use.distinctness_group, []).append((use, output))
    for output_key, uses in sorted(output_uses.items()):
        if len(uses) > 1 and any(not use_by_id[use_id].shareable
                                 for use_id in uses):
            failures.append(_failure(
                "NON_SHAREABLE_OUTPUT_REUSED", tuple(sorted(uses)),
                {"output": list(output_key)}))
    for group, values in sorted(groups.items()):
        keys = [_distinct_key(use.distinct_by, output)
                for use, output in values]
        if len(keys) != len(set(keys)):
            failures.append(_failure(
                "DISTINCTNESS_CONFLICT",
                tuple(sorted({use.use_id for use, _output in values})),
                {"group": group}))

    edges = _dependency_edges(assignments, use_by_id)
    cycle = _find_cycle(tuple(invocation_by_id), edges)
    if cycle is not None:
        failures.append(_failure(
            "SELECTED_CYCLE", tuple(cycle), {"cycle": list(cycle)}))
    elif not _is_grounded(
            selected, assignments, invocation_by_id, leaf_by_id, use_by_id):
        failures.append(_failure(
            "UNGROUNDED_DERIVATION",
            tuple(sorted(producer_id for kind, producer_id in selected
                         if kind is ProducerKind.INVOCATION)), {}))
    return tuple(failures)


def _failure(code: str, subjects: tuple[str, ...],
             details: dict[str, Any]) -> _Failure:
    return _Failure(code, subjects, strict_copy(details))


def _output_key(output: ProducerOutputRef) -> tuple[str, str, str]:
    return (output.producer_kind.value,
            output.producer_id, output.output_port_id)


def _distinct_key(kind: DistinctBy, output: ProducerOutputRef) -> tuple[str, ...]:
    if kind is DistinctBy.PRODUCER:
        return (output.producer_kind.value, output.producer_id)
    return _output_key(output)


def _dependency_edges(
        assignments: dict[str, SatisfactionBinding],
        use_by_id: dict[str, RequirementUseNode],
) -> dict[str, set[str]]:
    edges: dict[str, set[str]] = {}
    for use_id, binding in assignments.items():
        owner = use_by_id[use_id].owner_invocation_id
        if owner is None:
            continue
        edges.setdefault(owner, set())
        for output in binding.outputs:
            if output.producer_kind is ProducerKind.INVOCATION:
                edges.setdefault(output.producer_id, set()).add(owner)
    return edges


def _find_cycle(
        invocation_ids: tuple[str, ...],
        edges: dict[str, set[str]],
) -> tuple[str, ...] | None:
    state: dict[str, int] = {}
    stack: list[str] = []

    def visit(node: str) -> tuple[str, ...] | None:
        state[node] = 1
        stack.append(node)
        for child in sorted(edges.get(node, ())):
            if state.get(child, 0) == 0:
                found = visit(child)
                if found is not None:
                    return found
            elif state[child] == 1:
                start = stack.index(child)
                return tuple(stack[start:] + [child])
        stack.pop()
        state[node] = 2
        return None

    for invocation_id in sorted(invocation_ids):
        if state.get(invocation_id, 0) == 0:
            found = visit(invocation_id)
            if found is not None:
                return found
    return None


def _is_grounded(
        selected: set[tuple[ProducerKind, str]],
        assignments: dict[str, SatisfactionBinding],
        invocation_by_id: dict[str, InvocationNode],
        leaf_by_id: dict[str, ArtifactLeafNode],
        use_by_id: dict[str, RequirementUseNode],
) -> bool:
    memo: dict[str, bool] = {}
    visiting: set[str] = set()

    def invocation_grounded(invocation_id: str) -> bool:
        if invocation_id in memo:
            return memo[invocation_id]
        if invocation_id in visiting:
            return False
        visiting.add(invocation_id)
        invocation = invocation_by_id[invocation_id]
        if not invocation.input_use_ids:
            result = invocation.zero_input_source
        else:
            result = True
            for use_id in invocation.input_use_ids:
                binding = assignments[use_id]
                if binding.kind is not SatisfactionKind.PRODUCERS:
                    continue
                for output in binding.outputs:
                    if output.producer_kind is ProducerKind.ARTIFACT_LEAF:
                        if not leaf_by_id[output.producer_id].committed:
                            result = False
                    elif not invocation_grounded(output.producer_id):
                        result = False
        visiting.remove(invocation_id)
        memo[invocation_id] = result
        return result

    return all(invocation_grounded(producer_id)
               for kind, producer_id in selected
               if kind is ProducerKind.INVOCATION)


def _make_plan(
        problem: OracleProblem,
        producer_order: tuple[tuple[ProducerKind, str], ...],
        all_choice_ids: tuple[str, ...],
        selected: set[tuple[ProducerKind, str]],
        assignments: tuple[SatisfactionBinding, ...],
) -> CandidateDerivationPlan:
    invocation_by_id = {value.invocation_id: value
                        for value in problem.invocations}
    leaf_by_id = {value.leaf_id: value for value in problem.artifact_leaves}
    selected_invocations = tuple(sorted(
        producer_id for kind, producer_id in selected
        if kind is ProducerKind.INVOCATION))
    selected_leaves = tuple(sorted(
        producer_id for kind, producer_id in selected
        if kind is ProducerKind.ARTIFACT_LEAF))
    cost = sum(
        invocation_by_id[producer_id].cost_units
        if kind is ProducerKind.INVOCATION
        else leaf_by_id[producer_id].cost_units
        for kind, producer_id in selected)
    selected_choice_ids = {
        choice_id
        for binding in assignments
        for choice_id in binding.choice_ids
    }
    signature = tuple(
        int(value in selected) for value in producer_order
    ) + tuple(
        int(value in selected_choice_ids) for value in all_choice_ids
    )
    proof_by_id = {arc.proof.proof_id: arc.proof
                   for arc in problem.satisfaction_arcs}
    proofs = tuple(sorted(
        (proof_by_id[output.proof_id]
         for binding in assignments for output in binding.outputs),
        key=lambda value: value.proof_id,
    ))
    # One proof may justify a shareable output projected to several uses.
    proofs = tuple({value.proof_id: value for value in proofs}[key]
                   for key in sorted({value.proof_id for value in proofs}))
    return CandidateDerivationPlan.bind(
        root_use_ids=problem.root_use_ids,
        root_requirements=tuple(
            (use_id, next(value.requirement_id for value in problem.uses
                          if value.use_id == use_id))
            for use_id in problem.root_use_ids),
        oracle_problem_id=problem.problem_id,
        discovery_complete=True,
        selected_invocation_ids=selected_invocations,
        selected_artifact_leaf_ids=selected_leaves,
        satisfactions=assignments,
        compatibility_proofs=proofs,
        snapshot_refs=problem.snapshot_refs,
        total_cost_units=cost,
        selection_signature=signature,
    )


def _with_discovery_complete(
        plan: CandidateDerivationPlan, complete: bool,
) -> CandidateDerivationPlan:
    """Rebind an incumbent when the enclosing search is not exhaustive."""
    return CandidateDerivationPlan.bind(
        root_use_ids=plan.root_use_ids,
        root_requirements=plan.root_requirements,
        oracle_problem_id=plan.oracle_problem_id,
        discovery_complete=complete,
        selected_invocation_ids=plan.selected_invocation_ids,
        selected_artifact_leaf_ids=plan.selected_artifact_leaf_ids,
        satisfactions=plan.satisfactions,
        compatibility_proofs=plan.compatibility_proofs,
        snapshot_refs=plan.snapshot_refs,
        total_cost_units=plan.total_cost_units,
        selection_signature=plan.selection_signature,
        schema_version=plan.schema_version,
        objective=plan.objective,
        ordering_version=plan.ordering_version,
    )


def _build_blocker_tree(
        problem: OracleProblem,
        failures: tuple[_Failure, ...], *, complete: bool,
        limit: int | None = None, explored: int | None = None,
) -> BlockerTree:
    use_by_id = {value.use_id: value for value in problem.uses}
    invocation_by_id = {value.invocation_id: value
                        for value in problem.invocations}
    leaf_by_id = {value.leaf_id: value for value in problem.artifact_leaves}
    arcs_by_use = {
        use.use_id: tuple(arc for arc in problem.satisfaction_arcs
                          if arc.use_id == use.use_id)
        for use in problem.uses
    }
    rejections_by_use = {
        use.use_id: tuple(value for value in problem.candidate_rejections
                          if value.use_id == use.use_id)
        for use in problem.uses
    }

    def explain_use(use_id: str, stack: tuple[str, ...]) -> BlockerNode | None:
        use = use_by_id[use_id]
        if use.optional or use.default_id is not None:
            return None
        viable: list[SatisfactionArc] = []
        blocked: list[BlockerNode] = []
        for arc in sorted(arcs_by_use[use_id], key=lambda value: value.arc_id):
            if arc.producer_kind is ProducerKind.ARTIFACT_LEAF:
                leaf = leaf_by_id[arc.producer_id]
                if leaf.committed:
                    viable.append(arc)
                else:
                    blocked.append(BlockerNode.bind(
                        "ARTIFACT_NOT_COMMITTED", leaf.leaf_id,
                        "artifact leaf is not committed"))
                continue
            invocation = invocation_by_id[arc.producer_id]
            if not invocation.deployable:
                blocked.append(BlockerNode.bind(
                    "DEPLOYMENT_INFEASIBLE", invocation.invocation_id,
                    "invocation has no feasible frozen deployment class"))
                continue
            if invocation.invocation_id in stack:
                cycle = stack[stack.index(invocation.invocation_id):] + (
                    invocation.invocation_id,)
                blocked.append(BlockerNode.bind(
                    "SELECTED_CYCLE", invocation.invocation_id,
                    "candidate branch closes a selected dependency cycle",
                    details={"cycle": list(cycle)}))
                continue
            if not invocation.input_use_ids:
                if invocation.zero_input_source:
                    viable.append(arc)
                else:
                    blocked.append(BlockerNode.bind(
                        "UNGROUNDED_INVOCATION", invocation.invocation_id,
                        "zero-input invocation is not declared as a source"))
                continue
            child_blockers = [
                child for input_use_id in invocation.input_use_ids
                if (child := explain_use(
                    input_use_id, stack + (invocation.invocation_id,)))
                is not None
            ]
            if child_blockers:
                blocked.append(BlockerNode.bind(
                    "INVOCATION_INPUTS_BLOCKED", invocation.invocation_id,
                    "one or more required invocation inputs are blocked",
                    logic=BlockerLogic.ALL,
                    children=child_blockers))
            else:
                viable.append(arc)

        legal_viable = False
        upper = min(use.cardinality.maximum, len(viable))
        for count in range(max(1, use.cardinality.minimum), upper + 1):
            if any(_within_use_distinct(
                    use, tuple(arc.output_ref for arc in combination))
                   for combination in itertools.combinations(viable, count)):
                legal_viable = True
                break
        if legal_viable:
            return None
        rejected = [BlockerNode.bind(
            "DIRECT_MATCH_REJECTED",
            f"{value.producer_kind.value}:{value.producer_id}:{value.output_port_id}",
            "producer output failed direct scientific compatibility",
            details={
                "rejection_codes": list(value.rejection_codes),
                "match_details": strict_copy(value.details),
            },
        ) for value in rejections_by_use[use_id]]
        children = blocked + rejected
        if not children:
            children.append(BlockerNode.bind(
                "NO_CANDIDATE", use_id,
                "no producer output or declared default can satisfy this use"))
        return BlockerNode.bind(
            "CARDINALITY_UNSATISFIED", use_id,
            "compatible grounded producer outputs cannot meet required cardinality",
            logic=BlockerLogic.RANGE,
            children=children,
            details={
                "minimum": use.cardinality.minimum,
                "maximum": use.cardinality.maximum,
                "viable_count": len(viable),
            })

    root_children = [
        blocker for use_id in problem.root_use_ids
        if (blocker := explain_use(use_id, ())) is not None
    ]
    for failure in sorted(failures, key=lambda value: value.key):
        if failure.code == "ORPHAN_PRODUCER":
            # Orphan subsets are an enumeration artifact, not an explanation
            # for why no derivation exists.
            continue
        root_children.append(BlockerNode.bind(
            failure.code,
            ",".join(failure.subjects) or problem.problem_id,
            _failure_message(failure.code),
            details=strict_copy(failure.details)))
    root_children = list({value.blocker_id: value for value in root_children}.values())
    if not complete:
        root_children.append(BlockerNode.bind(
            "STATE_SPACE_LIMIT", problem.problem_id,
            "the exhaustive oracle reached its declared state limit",
            details={"state_limit": limit, "explored_states": explored}))
        code = "SEARCH_INCOMPLETE"
        message = "candidate enumeration is incomplete; no global optimality claim is valid"
    else:
        code = "NO_VALID_PLAN"
        message = "no complete, grounded, deployable acyclic derivation exists"
    if not root_children:
        root_children.append(BlockerNode.bind(
            "NO_ASSIGNMENT", problem.problem_id,
            "no complete satisfaction assignment exists"))
    return BlockerTree(
        complete=complete,
        root=BlockerNode.bind(
            code, problem.problem_id, message,
            logic=BlockerLogic.ANY,
            children=root_children),
    )


def _failure_message(code: str) -> str:
    return {
        "DEPLOYMENT_INFEASIBLE":
            "selected invocation has no feasible frozen deployment class",
        "ARTIFACT_NOT_COMMITTED":
            "selected artifact leaf is not committed",
        "NON_SHAREABLE_OUTPUT_REUSED":
            "one output was assigned to several non-shareable uses",
        "DISTINCTNESS_CONFLICT":
            "selected outputs violate a cross-use distinctness group",
        "SELECTED_CYCLE":
            "selected invocation dependencies contain a cycle",
        "UNGROUNDED_DERIVATION":
            "selected invocation dependencies do not terminate in declared sources",
    }.get(code, "candidate assignment violates a hard selection constraint")
