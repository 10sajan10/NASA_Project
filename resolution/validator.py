"""Backend-independent validation of an untrusted selected derivation plan.

The validator is deliberately separate from both the exhaustive Stage-2 oracle
and future Stage-3 solvers.  It treats a solver result as an untrusted claim,
replays the scientific checks from typed inputs, and reports every independent
failure it can find in one deterministic pass.

This module performs no discovery, I/O, scheduling, or execution.  In
particular, an :class:`ArtifactLeaf` is only a declaration; reuse is accepted
only when the caller supplies a separate commit attestation from its trusted
artifact-index boundary.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable

from capabilities import (
    ArtifactLeaf,
    BoundInvocation,
    DeploymentCapabilitySnapshot,
    DeploymentFeasibilityProof,
    ExecutionProfile,
    artifact_evidence_subject,
    invocation_evidence_subject,
)
from composition.oracle import (
    DistinctBy,
    OracleProblem,
    RequirementUseNode,
    validate_compatibility_record,
)
from contracts import (
    DistinctnessPolicy,
    EvidenceProfile,
    EvidenceSnapshot,
    EvidenceSubject,
    RequirementUse,
    direct_match,
)
from engine.runtime.identity import freeze_json, strict_copy, strict_hash
from plans import (
    CandidateDerivationPlan,
    ProducerKind,
    SatisfactionBinding,
    SatisfactionKind,
)
from resolution.hypergraph import (
    ArtifactAvailabilitySnapshot,
    ArtifactCommitStatus,
)
from resolution.milp import SelectionConstraints as SolverSelectionConstraints


class ValidationCode(str, Enum):
    """Stable machine-readable selected-plan rejection codes."""

    PLAN_IDENTITY_INVALID = "PLAN_IDENTITY_INVALID"
    PROBLEM_IDENTITY_INVALID = "PROBLEM_IDENTITY_INVALID"
    ORACLE_PROBLEM_MISMATCH = "ORACLE_PROBLEM_MISMATCH"
    DISCOVERY_COMPLETENESS_MISMATCH = "DISCOVERY_COMPLETENESS_MISMATCH"
    CANDIDATE_UNIVERSE_INCOMPLETE = "CANDIDATE_UNIVERSE_INCOMPLETE"
    SNAPSHOT_SET_MISMATCH = "SNAPSHOT_SET_MISMATCH"
    ROOT_SET_MISMATCH = "ROOT_SET_MISMATCH"
    ROOT_REQUIREMENT_MISMATCH = "ROOT_REQUIREMENT_MISMATCH"
    DUPLICATE_TYPED_INPUT = "DUPLICATE_TYPED_INPUT"
    REQUIREMENT_USE_MISSING = "REQUIREMENT_USE_MISSING"
    REQUIREMENT_USE_PROJECTION_MISMATCH = (
        "REQUIREMENT_USE_PROJECTION_MISMATCH")
    INVOCATION_RECORD_MISSING = "INVOCATION_RECORD_MISSING"
    INVOCATION_PROJECTION_MISMATCH = "INVOCATION_PROJECTION_MISMATCH"
    ARTIFACT_RECORD_MISSING = "ARTIFACT_RECORD_MISSING"
    SELECTED_PRODUCER_UNKNOWN = "SELECTED_PRODUCER_UNKNOWN"
    SATISFACTION_MISSING = "SATISFACTION_MISSING"
    SATISFACTION_INACTIVE = "SATISFACTION_INACTIVE"
    REQUIRED_USE_NOT_PRODUCED = "REQUIRED_USE_NOT_PRODUCED"
    DEFAULT_NOT_ALLOWED = "DEFAULT_NOT_ALLOWED"
    DEFAULT_ID_MISMATCH = "DEFAULT_ID_MISMATCH"
    OMIT_NOT_ALLOWED = "OMIT_NOT_ALLOWED"
    CARDINALITY_VIOLATION = "CARDINALITY_VIOLATION"
    OUTPUT_UNKNOWN = "OUTPUT_UNKNOWN"
    EDGE_NOT_IN_CANDIDATE_UNIVERSE = "EDGE_NOT_IN_CANDIDATE_UNIVERSE"
    PROOF_RECORD_MISSING = "PROOF_RECORD_MISSING"
    PROOF_RECORD_INVALID = "PROOF_RECORD_INVALID"
    PROOF_REQUIREMENT_MISMATCH = "PROOF_REQUIREMENT_MISMATCH"
    PROOF_DESCRIPTOR_MISMATCH = "PROOF_DESCRIPTOR_MISMATCH"
    PROOF_NOT_SATISFIED = "PROOF_NOT_SATISFIED"
    EVIDENCE_REPLAY_CONTEXT_MISSING = "EVIDENCE_REPLAY_CONTEXT_MISSING"
    EVIDENCE_PROFILE_MISMATCH = "EVIDENCE_PROFILE_MISMATCH"
    EVIDENCE_SNAPSHOT_REQUIRED = "EVIDENCE_SNAPSHOT_REQUIRED"
    EVIDENCE_SNAPSHOT_MISMATCH = "EVIDENCE_SNAPSHOT_MISMATCH"
    EVIDENCE_SUBJECT_MISMATCH = "EVIDENCE_SUBJECT_MISMATCH"
    PROOF_REPLAY_FAILED = "PROOF_REPLAY_FAILED"
    PROOF_REPLAY_MISMATCH = "PROOF_REPLAY_MISMATCH"
    DIRECT_MATCH_REJECTED = "DIRECT_MATCH_REJECTED"
    SELECTED_SET_MISMATCH = "SELECTED_SET_MISMATCH"
    ORPHAN_PRODUCER = "ORPHAN_PRODUCER"
    NON_SHAREABLE_OUTPUT_REUSED = "NON_SHAREABLE_OUTPUT_REUSED"
    DISTINCTNESS_VIOLATION = "DISTINCTNESS_VIOLATION"
    DEPLOYMENT_SNAPSHOT_MISSING = "DEPLOYMENT_SNAPSHOT_MISSING"
    DEPLOYMENT_SNAPSHOT_UNBOUND = "DEPLOYMENT_SNAPSHOT_UNBOUND"
    EXECUTION_PROFILE_MISSING = "EXECUTION_PROFILE_MISSING"
    EXECUTION_PROFILE_MISMATCH = "EXECUTION_PROFILE_MISMATCH"
    DEPLOYMENT_PROOF_MISSING = "DEPLOYMENT_PROOF_MISSING"
    DEPLOYMENT_PROOF_MISMATCH = "DEPLOYMENT_PROOF_MISMATCH"
    DEPLOYMENT_INFEASIBLE = "DEPLOYMENT_INFEASIBLE"
    DEPLOYMENT_CHOICE_MISSING = "DEPLOYMENT_CHOICE_MISSING"
    DEPLOYMENT_CHOICE_UNKNOWN = "DEPLOYMENT_CHOICE_UNKNOWN"
    DEPLOYMENT_CHOICE_INFEASIBLE = "DEPLOYMENT_CHOICE_INFEASIBLE"
    DEPLOYMENT_CHOICE_SET_MISMATCH = "DEPLOYMENT_CHOICE_SET_MISMATCH"
    DEPLOYABILITY_PROJECTION_MISMATCH = (
        "DEPLOYABILITY_PROJECTION_MISMATCH")
    ARTIFACT_ATTESTATION_MISSING = "ARTIFACT_ATTESTATION_MISSING"
    ARTIFACT_ATTESTATION_MISMATCH = "ARTIFACT_ATTESTATION_MISMATCH"
    ARTIFACT_AVAILABILITY_SNAPSHOT_UNBOUND = (
        "ARTIFACT_AVAILABILITY_SNAPSHOT_UNBOUND")
    ARTIFACT_AVAILABILITY_RECORD_MISSING = (
        "ARTIFACT_AVAILABILITY_RECORD_MISSING")
    ARTIFACT_NOT_COMMITTED = "ARTIFACT_NOT_COMMITTED"
    COST_MODEL_UNSUPPORTED = "COST_MODEL_UNSUPPORTED"
    COST_ESTIMATE_INVALID = "COST_ESTIMATE_INVALID"
    COST_PROJECTION_MISMATCH = "COST_PROJECTION_MISMATCH"
    TOTAL_COST_MISMATCH = "TOTAL_COST_MISMATCH"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    INCLUDE_CONSTRAINT_UNSATISFIED = "INCLUDE_CONSTRAINT_UNSATISFIED"
    EXCLUDED_PRODUCER_SELECTED = "EXCLUDED_PRODUCER_SELECTED"
    CONSTRAINT_PRODUCER_UNKNOWN = "CONSTRAINT_PRODUCER_UNKNOWN"
    SELECTION_SIGNATURE_MISMATCH = "SELECTION_SIGNATURE_MISMATCH"
    CYCLE_DETECTED = "CYCLE_DETECTED"
    UNGROUNDED_DERIVATION = "UNGROUNDED_DERIVATION"


def _required_text(value: str, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")


def _digest(value: str, label: str) -> None:
    _required_text(value, label)
    if (len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)):
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")


def _canonical_ids(values: Iterable[str], label: str) -> tuple[str, ...]:
    if isinstance(values, str):
        raise TypeError(f"{label} must be an iterable of identities")
    result = tuple(values)
    for value in result:
        _required_text(value, label)
    if len(result) != len(set(result)):
        raise ValueError(f"{label} cannot contain duplicates")
    return tuple(sorted(result))


@dataclass(frozen=True)
class ProofReplayContext:
    """Exact empirical inputs needed to reproduce one selected proof.

    A proof that names an evidence profile is rejected unless this context also
    supplies the exact frozen snapshot and expected evidence subject.  This
    prevents deserializing a historically satisfied proof from becoming a
    substitute for replayable evidence.
    """

    proof_id: str
    evidence_profile: EvidenceProfile | None = None
    evidence_snapshot: EvidenceSnapshot | None = None
    evidence_subject: EvidenceSubject | None = None

    def __post_init__(self) -> None:
        _digest(self.proof_id, "proof replay identity")
        if (self.evidence_profile is not None
                and not isinstance(self.evidence_profile, EvidenceProfile)):
            raise TypeError("evidence_profile must be EvidenceProfile or None")
        if (self.evidence_snapshot is not None
                and not isinstance(self.evidence_snapshot, EvidenceSnapshot)):
            raise TypeError("evidence_snapshot must be EvidenceSnapshot or None")
        if (self.evidence_subject is not None
                and not isinstance(self.evidence_subject, EvidenceSubject)):
            raise TypeError("evidence_subject must be EvidenceSubject or None")


@dataclass(frozen=True)
class ArtifactCommitAttestation:
    """A caller-supplied projection of a trusted artifact commit snapshot.

    The validator authenticates this record's content identity and its exact
    agreement with an :class:`ArtifactLeaf`.  Establishing that ``snapshot_id``
    came from a trusted store remains the caller's trust-boundary obligation;
    this pure module intentionally performs no storage lookup.
    """

    attestation_id: str
    snapshot_id: str
    leaf_id: str
    artifact_id: str
    manifest_root_sha256: str
    descriptor_id: str
    committed: bool

    def __post_init__(self) -> None:
        for value, label in (
                (self.attestation_id, "artifact attestation identity"),
                (self.snapshot_id, "artifact commit snapshot identity"),
                (self.leaf_id, "artifact leaf identity"),
                (self.artifact_id, "artifact identity"),
                (self.manifest_root_sha256, "artifact manifest root"),
                (self.descriptor_id, "artifact descriptor identity")):
            _digest(value, label)
        if type(self.committed) is not bool:
            raise TypeError("artifact committed state must be bool")
        if self.attestation_id != self.expected_id():
            raise ValueError("artifact commit attestation identity does not verify")

    @classmethod
    def bind(
            cls, snapshot_id: str, leaf: ArtifactLeaf, *, committed: bool,
    ) -> "ArtifactCommitAttestation":
        if not isinstance(leaf, ArtifactLeaf):
            raise TypeError("leaf must be ArtifactLeaf")
        values = {
            "snapshot_id": snapshot_id,
            "leaf_id": leaf.leaf_id,
            "artifact_id": leaf.artifact_id,
            "manifest_root_sha256": leaf.manifest_root_sha256,
            "descriptor_id": leaf.descriptor.descriptor_id,
            "committed": committed,
        }
        return cls(strict_hash(cls._identity_payload(values)), **values)

    @staticmethod
    def _identity_payload(values: dict[str, Any]) -> dict[str, Any]:
        return {
            "schema": "stage3-artifact-commit-attestation-v1",
            **values,
        }

    def expected_id(self) -> str:
        return strict_hash(self._identity_payload({
            "snapshot_id": self.snapshot_id,
            "leaf_id": self.leaf_id,
            "artifact_id": self.artifact_id,
            "manifest_root_sha256": self.manifest_root_sha256,
            "descriptor_id": self.descriptor_id,
            "committed": self.committed,
        }))

    def to_dict(self) -> dict[str, Any]:
        return {
            "attestation_id": self.attestation_id,
            "snapshot_id": self.snapshot_id,
            "leaf_id": self.leaf_id,
            "artifact_id": self.artifact_id,
            "manifest_root_sha256": self.manifest_root_sha256,
            "descriptor_id": self.descriptor_id,
            "committed": self.committed,
        }


@dataclass(frozen=True)
class SelectionConstraints:
    """Hard policy constraints independently checked after selection."""

    max_cost_units: int | None = None
    include_invocation_ids: tuple[str, ...] = ()
    exclude_invocation_ids: tuple[str, ...] = ()
    include_artifact_leaf_ids: tuple[str, ...] = ()
    exclude_artifact_leaf_ids: tuple[str, ...] = ()
    require_complete_universe: bool = True

    def __post_init__(self) -> None:
        if (self.max_cost_units is not None
                and (isinstance(self.max_cost_units, bool)
                     or not isinstance(self.max_cost_units, int)
                     or self.max_cost_units < 0)):
            raise ValueError("max_cost_units must be a non-negative integer")
        for name in (
                "include_invocation_ids", "exclude_invocation_ids",
                "include_artifact_leaf_ids", "exclude_artifact_leaf_ids"):
            object.__setattr__(
                self, name, _canonical_ids(getattr(self, name), name))
        if (set(self.include_invocation_ids) & set(self.exclude_invocation_ids)
                or set(self.include_artifact_leaf_ids)
                & set(self.exclude_artifact_leaf_ids)):
            raise ValueError("the same producer cannot be included and excluded")
        if type(self.require_complete_universe) is not bool:
            raise TypeError("require_complete_universe must be bool")


def _normalize_constraints(
    constraints: SelectionConstraints | SolverSelectionConstraints | None,
) -> SelectionConstraints:
    """Translate the selector's public policy record into validator policy.

    Keeping this conversion explicit prevents the validator from reaching into
    solver state while still allowing it to replay the exact hard constraints
    carried by :class:`resolution.milp.MilpSelectionProblem`.
    """
    if constraints is None:
        return SelectionConstraints()
    if isinstance(constraints, SelectionConstraints):
        return constraints
    if not isinstance(constraints, SolverSelectionConstraints):
        raise TypeError(
            "constraints must be resolution.validator.SelectionConstraints "
            "or resolution.milp.SelectionConstraints")

    included_invocations: list[str] = []
    excluded_invocations: list[str] = []
    included_leaves: list[str] = []
    excluded_leaves: list[str] = []
    for reference in constraints.include:
        target = (included_invocations
                  if reference.producer_kind is ProducerKind.INVOCATION
                  else included_leaves)
        target.append(reference.producer_id)
    for reference in constraints.exclude:
        target = (excluded_invocations
                  if reference.producer_kind is ProducerKind.INVOCATION
                  else excluded_leaves)
        target.append(reference.producer_id)
    return SelectionConstraints(
        max_cost_units=constraints.maximum_cost_units,
        include_invocation_ids=tuple(included_invocations),
        exclude_invocation_ids=tuple(excluded_invocations),
        include_artifact_leaf_ids=tuple(included_leaves),
        exclude_artifact_leaf_ids=tuple(excluded_leaves),
    )


@dataclass(frozen=True)
class ValidationBlocker:
    code: ValidationCode
    subject_id: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.code, ValidationCode):
            raise TypeError("validation blocker code must be ValidationCode")
        _required_text(self.subject_id, "validation blocker subject")
        _required_text(self.message, "validation blocker message")
        object.__setattr__(self, "details", freeze_json(self.details))
        if not isinstance(self.details, dict):
            raise TypeError("validation blocker details must be a JSON object")

    @property
    def blocker_id(self) -> str:
        return strict_hash({
            "schema": "stage3-selected-plan-blocker-v1",
            **self.to_dict(),
        })

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code.value,
            "subject_id": self.subject_id,
            "message": self.message,
            "details": strict_copy(self.details),
        }


@dataclass(frozen=True)
class ValidationBlockerTree:
    """All independent validation failures; every child must be resolved."""

    logic: str
    complete: bool
    children: tuple[ValidationBlocker, ...]

    def __post_init__(self) -> None:
        if self.logic != "ALL":
            raise ValueError("selected-plan blocker-tree logic must be ALL")
        if type(self.complete) is not bool:
            raise TypeError("blocker-tree completeness must be bool")
        if (not isinstance(self.children, tuple) or not self.children
                or not all(isinstance(value, ValidationBlocker)
                           for value in self.children)):
            raise ValueError("blocker tree requires typed children")
        expected = tuple(sorted(
            self.children,
            key=lambda value: (
                value.code.value, value.subject_id, value.blocker_id),
        ))
        if self.children != expected:
            raise ValueError("blocker children must be canonically ordered")
        if len({value.blocker_id for value in self.children}) != len(self.children):
            raise ValueError("blocker tree cannot repeat a blocker")

    @property
    def tree_id(self) -> str:
        return strict_hash({
            "schema": "stage3-selected-plan-blocker-tree-v1",
            **self.to_dict(),
        })

    def to_dict(self) -> dict[str, Any]:
        return {
            "logic": self.logic,
            "complete": self.complete,
            "children": [value.to_dict() for value in self.children],
        }


@dataclass(frozen=True)
class SelectedPlanValidationReport:
    schema: str
    plan_id: str
    oracle_problem_id: str
    valid: bool
    validation_complete: bool
    candidate_universe_complete: bool
    recomputed_cost_units: int
    blocker_tree: ValidationBlockerTree | None

    def __post_init__(self) -> None:
        if self.schema != "stage3-selected-plan-validation-report-v1":
            raise ValueError("unsupported selected-plan validation schema")
        _required_text(self.plan_id, "candidate plan identity")
        _required_text(self.oracle_problem_id, "oracle problem identity")
        for name in (
                "valid", "validation_complete", "candidate_universe_complete"):
            if type(getattr(self, name)) is not bool:
                raise TypeError(f"{name} must be bool")
        if (isinstance(self.recomputed_cost_units, bool)
                or not isinstance(self.recomputed_cost_units, int)
                or self.recomputed_cost_units < 0):
            raise ValueError("recomputed cost must be a non-negative integer")
        if self.valid != (self.blocker_tree is None):
            raise ValueError("validity and blocker tree disagree")
        if (self.blocker_tree is not None
                and not isinstance(self.blocker_tree, ValidationBlockerTree)):
            raise TypeError("blocker_tree must be ValidationBlockerTree or None")

    @property
    def report_id(self) -> str:
        return strict_hash({
            "schema": "stage3-selected-plan-validation-report-identity-v1",
            **self.to_dict(),
        })

    @property
    def blockers(self) -> tuple[ValidationBlocker, ...]:
        return () if self.blocker_tree is None else self.blocker_tree.children

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "plan_id": self.plan_id,
            "oracle_problem_id": self.oracle_problem_id,
            "valid": self.valid,
            "validation_complete": self.validation_complete,
            "candidate_universe_complete": self.candidate_universe_complete,
            "recomputed_cost_units": self.recomputed_cost_units,
            "blocker_tree": (
                self.blocker_tree.to_dict() if self.blocker_tree else None),
        }


class _Blockers:
    def __init__(self) -> None:
        self._values: dict[str, ValidationBlocker] = {}

    def add(
            self, code: ValidationCode, subject_id: str, message: str,
            details: dict[str, Any] | None = None,
    ) -> None:
        blocker = ValidationBlocker(
            code, subject_id, message, {} if details is None else details)
        self._values.setdefault(blocker.blocker_id, blocker)

    def finish(self) -> tuple[ValidationBlocker, ...]:
        return tuple(sorted(
            self._values.values(),
            key=lambda value: (
                value.code.value, value.subject_id, value.blocker_id),
        ))


def validate_selected_plan(
    plan: CandidateDerivationPlan,
    problem: OracleProblem,
    *,
    requirement_uses: Iterable[RequirementUse],
    invocations: Iterable[BoundInvocation],
    artifact_leaves: Iterable[ArtifactLeaf] = (),
    execution_profiles: Iterable[ExecutionProfile] = (),
    deployment_snapshot: DeploymentCapabilitySnapshot | None = None,
    deployment_proofs: Iterable[DeploymentFeasibilityProof] = (),
    deployment_choices: Iterable[tuple[str, str]] | None = None,
    evidence_replays: Iterable[ProofReplayContext] = (),
    artifact_attestations: Iterable[ArtifactCommitAttestation] = (),
    artifact_availability_snapshot: ArtifactAvailabilitySnapshot | None = None,
    constraints: SelectionConstraints | SolverSelectionConstraints | None = None,
    candidate_universe_complete: bool = True,
) -> SelectedPlanValidationReport:
    """Validate one selected plan without trusting the selecting backend.

    ``problem`` is the immutable explicit candidate universe.  ``invocations``
    and ``artifact_leaves`` need only contain selected producers; root
    ``requirement_uses`` are mandatory, while selected invocation inputs are
    also recovered from their immutable :class:`BoundInvocation` records.

    The returned report is deterministic and exhaustive over the supplied
    inputs.  Invalid selections are data, not exceptions.  Exceptions are
    reserved for misuse of this API (wrong top-level types or malformed policy
    objects that cannot be interpreted safely).
    """
    if not isinstance(plan, CandidateDerivationPlan):
        raise TypeError("plan must be CandidateDerivationPlan")
    if not isinstance(problem, OracleProblem):
        raise TypeError("problem must be OracleProblem")
    constraints = _normalize_constraints(constraints)
    if (artifact_availability_snapshot is not None
            and not isinstance(
                artifact_availability_snapshot, ArtifactAvailabilitySnapshot)):
        raise TypeError(
            "artifact_availability_snapshot must be "
            "ArtifactAvailabilitySnapshot or None")
    if type(candidate_universe_complete) is not bool:
        raise TypeError("candidate_universe_complete must be bool")
    deployment_choice_values: tuple[tuple[str, str], ...] | None = None
    if deployment_choices is not None:
        deployment_choice_values = tuple(deployment_choices)
        if any(
                not isinstance(value, tuple) or len(value) != 2
                or any(not isinstance(item, str) or not item
                       for item in value)
                for value in deployment_choice_values):
            raise TypeError(
                "deployment_choices must contain (invocation_id, site_class_id) tuples")

    blockers = _Blockers()
    _validate_identities_and_universe(
        plan, problem, candidate_universe_complete, constraints, blockers)

    typed_use_input = _unique_map(
        requirement_uses, lambda value: value.requirement_use_id,
        RequirementUse, "requirement use", plan.plan_id, blockers)
    invocation_by_id = _unique_map(
        invocations, lambda value: value.invocation_key,
        BoundInvocation, "invocation", plan.plan_id, blockers)
    artifact_by_id = _unique_map(
        artifact_leaves, lambda value: value.leaf_id,
        ArtifactLeaf, "artifact leaf", plan.plan_id, blockers)
    profile_by_id = _unique_map(
        execution_profiles, lambda value: value.profile_id,
        ExecutionProfile, "execution profile", plan.plan_id, blockers)
    proof_context_by_id = _unique_map(
        evidence_replays, lambda value: value.proof_id,
        ProofReplayContext, "proof replay context", plan.plan_id, blockers)
    attestation_by_leaf = _unique_map(
        artifact_attestations, lambda value: value.leaf_id,
        ArtifactCommitAttestation, "artifact attestation", plan.plan_id,
        blockers)
    deployment_proof_by_profile = _unique_map(
        deployment_proofs, lambda value: value.profile_id,
        DeploymentFeasibilityProof, "deployment proof", plan.plan_id,
        blockers)

    problem_use = {value.use_id: value for value in problem.uses}
    problem_invocation = {
        value.invocation_id: value for value in problem.invocations}
    problem_leaf = {value.leaf_id: value for value in problem.artifact_leaves}
    satisfaction_by_use = {
        value.use_id: value for value in plan.satisfactions}

    selected_invocations = set(plan.selected_invocation_ids)
    selected_leaves = set(plan.selected_artifact_leaf_ids)
    _validate_selected_producer_records(
        plan, problem_invocation, problem_leaf, invocation_by_id,
        artifact_by_id, blockers)

    typed_use = dict(typed_use_input)
    # Active uses come from the frozen candidate universe, not from whichever
    # typed invocation records the untrusted caller happened to supply.
    owner_by_use: dict[str, str] = {}
    for invocation_id in sorted(selected_invocations):
        node = problem_invocation.get(invocation_id)
        if node is None:
            continue
        for use_id in node.input_use_ids:
            previous_owner = owner_by_use.setdefault(use_id, invocation_id)
            if previous_owner != invocation_id:
                blockers.add(
                    ValidationCode.REQUIREMENT_USE_PROJECTION_MISMATCH,
                    use_id,
                    "one active requirement use belongs to several invocations",
                    {"owners": sorted((previous_owner, invocation_id))},
                )
    for invocation_id in sorted(selected_invocations):
        invocation = invocation_by_id.get(invocation_id)
        if invocation is None:
            continue
        for use in invocation.input_uses:
            use_id = use.requirement_use_id
            previous = typed_use.get(use_id)
            if previous is not None and previous != use:
                blockers.add(
                    ValidationCode.REQUIREMENT_USE_PROJECTION_MISMATCH,
                    use_id,
                    "supplied and invocation-bound requirement uses disagree",
                    {"invocation_id": invocation_id},
                )
            typed_use.setdefault(use_id, use)
            expected_owner = owner_by_use.get(use_id)
            if expected_owner != invocation_id:
                blockers.add(
                    ValidationCode.REQUIREMENT_USE_PROJECTION_MISMATCH,
                    use_id,
                    "typed requirement use has the wrong active owner",
                    {
                        "expected_owner": expected_owner,
                        "observed_owner": invocation_id,
                    },
                )

    active_uses = set(plan.root_use_ids) | set(owner_by_use)
    _validate_roots_and_uses(
        plan, problem, problem_use, typed_use, owner_by_use, blockers)
    _validate_active_coverage(
        active_uses, satisfaction_by_use, plan.plan_id, blockers)

    proof_by_id = {
        value.proof_id: value for value in plan.compatibility_proofs}
    arc_by_key = {
        (value.use_id, value.producer_kind, value.producer_id,
         value.output_port_id): value
        for value in problem.satisfaction_arcs
    }
    output_consumers: dict[tuple[str, str, str], set[str]] = {}
    valid_artifact_attestations: set[str] = set()
    referenced_invocations: set[str] = set()
    referenced_leaves: set[str] = set()

    for use_id in sorted(active_uses):
        binding = satisfaction_by_use.get(use_id)
        use = typed_use.get(use_id)
        node = problem_use.get(use_id)
        if binding is None or use is None or node is None:
            continue
        _validate_satisfaction_kind(use, binding, blockers)
        if binding.kind is not SatisfactionKind.PRODUCERS:
            continue
        count = len(binding.outputs)
        if not (use.cardinality.minimum <= count <= use.cardinality.maximum):
            blockers.add(
                ValidationCode.CARDINALITY_VIOLATION,
                use_id,
                "selected producer count violates the requirement cardinality",
                {
                    "minimum": use.cardinality.minimum,
                    "maximum": use.cardinality.maximum,
                    "observed": count,
                },
            )
        _validate_within_use_distinctness(use, binding, blockers)
        for output in binding.outputs:
            output_key = (
                output.producer_kind.value, output.producer_id,
                output.output_port_id)
            output_consumers.setdefault(output_key, set()).add(use_id)
            if output.producer_kind is ProducerKind.INVOCATION:
                referenced_invocations.add(output.producer_id)
            else:
                referenced_leaves.add(output.producer_id)
            descriptor = _resolve_descriptor(
                output.producer_kind, output.producer_id,
                output.output_port_id, invocation_by_id, artifact_by_id,
                problem_invocation, problem_leaf, use_id, blockers)
            arc = arc_by_key.get((
                use_id, output.producer_kind, output.producer_id,
                output.output_port_id))
            if arc is None:
                blockers.add(
                    ValidationCode.EDGE_NOT_IN_CANDIDATE_UNIVERSE,
                    use_id,
                    "selected satisfaction edge was not in the frozen universe",
                    {"output": output.to_dict()},
                )
            elif arc.proof.proof_id != output.proof_id:
                blockers.add(
                    ValidationCode.EDGE_NOT_IN_CANDIDATE_UNIVERSE,
                    use_id,
                    "selected edge uses a proof not frozen in the universe",
                    {
                        "expected_proof_id": arc.proof.proof_id,
                        "observed_proof_id": output.proof_id,
                    },
                )
            if descriptor is not None:
                derived_subject = None
                expected_profile_id = None
                if output.producer_kind is ProducerKind.INVOCATION:
                    producer = invocation_by_id.get(output.producer_id)
                    if producer is not None:
                        expected_profile_id = producer.evidence_profile_id
                        derived_subject = invocation_evidence_subject(
                            producer, output.output_port_id)
                else:
                    producer = artifact_by_id.get(output.producer_id)
                    if producer is not None:
                        expected_profile_id = producer.evidence_profile_id
                        derived_subject = artifact_evidence_subject(
                            producer, output.output_port_id)
                _replay_selected_proof(
                    use, descriptor, output.proof_id, proof_by_id,
                    proof_context_by_id, blockers,
                    expected_evidence_profile_id=expected_profile_id,
                    derived_evidence_subject=derived_subject,
                )

    _validate_selected_sets(
        plan, referenced_invocations, referenced_leaves, blockers)
    _validate_sharing(
        output_consumers, typed_use, plan.plan_id, blockers)
    _validate_distinctness_groups(
        problem, satisfaction_by_use, active_uses, blockers)

    _validate_deployment(
        selected_invocations, invocation_by_id, problem_invocation,
        profile_by_id, deployment_snapshot, deployment_proof_by_profile,
        deployment_choice_values, problem, blockers)
    valid_artifact_attestations.update(_validate_artifact_attestations(
        selected_leaves, artifact_by_id, attestation_by_leaf,
        artifact_availability_snapshot, problem, blockers))

    recomputed_cost = _validate_cost_and_constraints(
        plan, problem, problem_invocation, problem_leaf, constraints, blockers)
    _validate_selection_signature(plan, problem, blockers)
    _validate_cycles_and_grounding(
        plan, problem_invocation, invocation_by_id, satisfaction_by_use,
        valid_artifact_attestations, blockers)

    values = blockers.finish()
    tree = (None if not values else ValidationBlockerTree(
        logic="ALL", complete=True, children=values))
    return SelectedPlanValidationReport(
        schema="stage3-selected-plan-validation-report-v1",
        plan_id=plan.plan_id,
        oracle_problem_id=problem.problem_id,
        valid=not values,
        validation_complete=True,
        candidate_universe_complete=candidate_universe_complete,
        recomputed_cost_units=recomputed_cost,
        blocker_tree=tree,
    )


def _unique_map(
    values: Iterable[Any],
    key,
    expected_type: type,
    label: str,
    subject_id: str,
    blockers: _Blockers,
) -> dict[str, Any]:
    if isinstance(values, (str, bytes, dict)):
        raise TypeError(f"{label} values must be a typed iterable")
    result: dict[str, Any] = {}
    for value in values:
        if not isinstance(value, expected_type):
            raise TypeError(
                f"{label} values must be {expected_type.__name__}")
        identity = key(value)
        if identity in result:
            blockers.add(
                ValidationCode.DUPLICATE_TYPED_INPUT,
                identity,
                f"duplicate {label} input was supplied",
                {"input_kind": label, "validation_subject": subject_id},
            )
        else:
            result[identity] = value
    return result


def _validate_identities_and_universe(
    plan: CandidateDerivationPlan,
    problem: OracleProblem,
    candidate_universe_complete: bool,
    constraints: SelectionConstraints,
    blockers: _Blockers,
) -> None:
    try:
        plan.validate_identity()
    except (TypeError, ValueError) as exc:
        blockers.add(
            ValidationCode.PLAN_IDENTITY_INVALID,
            plan.plan_id,
            "candidate-plan content identity does not verify",
            {"error": str(exc)},
        )
    try:
        expected_problem_id = strict_hash(problem._identity_payload())
    except (AttributeError, TypeError, ValueError) as exc:
        blockers.add(
            ValidationCode.PROBLEM_IDENTITY_INVALID,
            problem.problem_id,
            "candidate-universe identity could not be recomputed",
            {"error": str(exc)},
        )
    else:
        if expected_problem_id != problem.problem_id:
            blockers.add(
                ValidationCode.PROBLEM_IDENTITY_INVALID,
                problem.problem_id,
                "candidate-universe content identity does not verify",
                {"expected": expected_problem_id},
            )
    if plan.oracle_problem_id != problem.problem_id:
        blockers.add(
            ValidationCode.ORACLE_PROBLEM_MISMATCH,
            plan.plan_id,
            "selected plan names a different candidate universe",
            {
                "expected": problem.problem_id,
                "observed": plan.oracle_problem_id,
            },
        )
    if plan.discovery_complete is not candidate_universe_complete:
        blockers.add(
            ValidationCode.DISCOVERY_COMPLETENESS_MISMATCH,
            plan.plan_id,
            "selected plan's discovery claim disagrees with the supplied universe",
            {
                "plan_claim": plan.discovery_complete,
                "supplied": candidate_universe_complete,
            },
        )
    if constraints.require_complete_universe and not candidate_universe_complete:
        blockers.add(
            ValidationCode.CANDIDATE_UNIVERSE_INCOMPLETE,
            problem.problem_id,
            "policy requires a complete candidate universe",
        )
    if plan.snapshot_refs != problem.snapshot_refs:
        blockers.add(
            ValidationCode.SNAPSHOT_SET_MISMATCH,
            plan.plan_id,
            "selected plan does not retain the universe snapshot set exactly",
            {
                "expected": [value.to_dict() for value in problem.snapshot_refs],
                "observed": [value.to_dict() for value in plan.snapshot_refs],
            },
        )


def _validate_selected_producer_records(
    plan: CandidateDerivationPlan,
    problem_invocation: dict[str, Any],
    problem_leaf: dict[str, Any],
    invocation_by_id: dict[str, BoundInvocation],
    artifact_by_id: dict[str, ArtifactLeaf],
    blockers: _Blockers,
) -> None:
    for invocation_id in plan.selected_invocation_ids:
        node = problem_invocation.get(invocation_id)
        if node is None:
            blockers.add(
                ValidationCode.SELECTED_PRODUCER_UNKNOWN,
                invocation_id,
                "selected invocation is absent from the candidate universe",
            )
            continue
        invocation = invocation_by_id.get(invocation_id)
        if invocation is None:
            blockers.add(
                ValidationCode.INVOCATION_RECORD_MISSING,
                invocation_id,
                "selected invocation has no typed bound record",
            )
            continue
        expected_inputs = tuple(sorted(
            value.requirement_use_id for value in invocation.input_uses))
        expected_outputs = tuple(sorted(
            value.port_id for value in invocation.outputs))
        if (node.input_use_ids != expected_inputs
                or node.output_port_ids != expected_outputs
                or node.zero_input_source != (not invocation.input_uses)):
            blockers.add(
                ValidationCode.INVOCATION_PROJECTION_MISMATCH,
                invocation_id,
                "candidate-universe invocation projection disagrees with its typed record",
                {
                    "expected_input_use_ids": list(expected_inputs),
                    "observed_input_use_ids": list(node.input_use_ids),
                    "expected_output_port_ids": list(expected_outputs),
                    "observed_output_port_ids": list(node.output_port_ids),
                    "expected_zero_input_source": not invocation.input_uses,
                    "observed_zero_input_source": node.zero_input_source,
                },
            )
        if invocation.cost_model_id != "cost:declared-v1":
            blockers.add(
                ValidationCode.COST_MODEL_UNSUPPORTED,
                invocation_id,
                "selected-plan validator cannot authenticate this cost model",
                {"cost_model_id": invocation.cost_model_id},
            )
        declared_cost = invocation.metric_estimates.get("cost_units")
        if (isinstance(declared_cost, bool)
                or not isinstance(declared_cost, int)
                or declared_cost < 0):
            blockers.add(
                ValidationCode.COST_ESTIMATE_INVALID,
                invocation_id,
                "bound invocation lacks a non-negative integer cost estimate",
                {"observed": declared_cost},
            )
        elif node.cost_units != declared_cost:
            blockers.add(
                ValidationCode.COST_PROJECTION_MISMATCH,
                invocation_id,
                "candidate-universe cost differs from the bound invocation estimate",
                {"expected": declared_cost, "observed": node.cost_units},
            )
    for leaf_id in plan.selected_artifact_leaf_ids:
        if leaf_id not in problem_leaf:
            blockers.add(
                ValidationCode.SELECTED_PRODUCER_UNKNOWN,
                leaf_id,
                "selected artifact leaf is absent from the candidate universe",
            )
        if leaf_id not in artifact_by_id:
            blockers.add(
                ValidationCode.ARTIFACT_RECORD_MISSING,
                leaf_id,
                "selected artifact leaf has no typed declaration",
            )


def _validate_roots_and_uses(
    plan: CandidateDerivationPlan,
    problem: OracleProblem,
    problem_use: dict[str, RequirementUseNode],
    typed_use: dict[str, RequirementUse],
    owner_by_use: dict[str, str],
    blockers: _Blockers,
) -> None:
    if set(plan.root_use_ids) != set(problem.root_use_ids):
        blockers.add(
            ValidationCode.ROOT_SET_MISMATCH,
            plan.plan_id,
            "selected plan roots differ from the frozen candidate universe",
            {
                "expected": sorted(problem.root_use_ids),
                "observed": sorted(plan.root_use_ids),
            },
        )
    root_requirements = dict(plan.root_requirements)
    for root_id in sorted(set(plan.root_use_ids) | set(problem.root_use_ids)):
        use = typed_use.get(root_id)
        if use is None:
            blockers.add(
                ValidationCode.REQUIREMENT_USE_MISSING,
                root_id,
                "root requirement lacks its typed RequirementUse",
            )
            continue
        expected = use.requirement.requirement_id
        if root_requirements.get(root_id) != expected:
            blockers.add(
                ValidationCode.ROOT_REQUIREMENT_MISMATCH,
                root_id,
                "root requirement identity does not match its typed use",
                {
                    "expected": expected,
                    "observed": root_requirements.get(root_id),
                },
            )

    active_ids = set(plan.root_use_ids) | set(owner_by_use)
    for use_id in sorted(active_ids):
        use = typed_use.get(use_id)
        node = problem_use.get(use_id)
        if use is None:
            blockers.add(
                ValidationCode.REQUIREMENT_USE_MISSING,
                use_id,
                "active requirement lacks its typed RequirementUse",
            )
            continue
        if node is None:
            blockers.add(
                ValidationCode.REQUIREMENT_USE_PROJECTION_MISMATCH,
                use_id,
                "active requirement use is absent from the candidate universe",
            )
            continue
        expected_distinct = {
            DistinctnessPolicy.ALLOW_SAME: DistinctBy.NONE,
            DistinctnessPolicy.DISTINCT_ARTIFACT: DistinctBy.OUTPUT,
            DistinctnessPolicy.DISTINCT_PRODUCER: DistinctBy.PRODUCER,
        }[use.distinctness]
        expected_owner = owner_by_use.get(use_id)
        differences: dict[str, Any] = {}
        comparisons = (
            ("requirement_id", use.requirement.requirement_id,
             node.requirement_id),
            ("port_id", use.port_id, node.port_id),
            ("owner_invocation_id", expected_owner,
             node.owner_invocation_id),
            ("optional", use.optional, node.optional),
            ("default_id", use.default_id, node.default_id),
            ("cardinality_minimum", use.cardinality.minimum,
             node.cardinality.minimum),
            ("cardinality_maximum", use.cardinality.maximum,
             node.cardinality.maximum),
            ("distinct_by", expected_distinct.value,
             node.distinct_by.value),
            ("shareable", use.shareable, node.shareable),
        )
        for name, expected, observed in comparisons:
            if expected != observed:
                differences[name] = {
                    "expected": expected, "observed": observed}
        if differences:
            blockers.add(
                ValidationCode.REQUIREMENT_USE_PROJECTION_MISMATCH,
                use_id,
                "candidate-universe requirement projection disagrees with its typed use",
                differences,
            )


def _validate_active_coverage(
    active_uses: set[str],
    satisfaction_by_use: dict[str, SatisfactionBinding],
    plan_id: str,
    blockers: _Blockers,
) -> None:
    for use_id in sorted(active_uses - set(satisfaction_by_use)):
        blockers.add(
            ValidationCode.SATISFACTION_MISSING,
            use_id,
            "active requirement use has no selected satisfaction",
        )
    for use_id in sorted(set(satisfaction_by_use) - active_uses):
        blockers.add(
            ValidationCode.SATISFACTION_INACTIVE,
            use_id,
            "selected satisfaction belongs to an inactive requirement use",
            {"plan_id": plan_id},
        )


def _validate_satisfaction_kind(
    use: RequirementUse,
    binding: SatisfactionBinding,
    blockers: _Blockers,
) -> None:
    if binding.kind is SatisfactionKind.PRODUCERS:
        if not binding.outputs:
            blockers.add(
                ValidationCode.REQUIRED_USE_NOT_PRODUCED,
                binding.use_id,
                "producer satisfaction contains no outputs",
            )
        return
    if binding.kind is SatisfactionKind.DEFAULT:
        if not use.optional or use.default_id is None:
            blockers.add(
                ValidationCode.DEFAULT_NOT_ALLOWED,
                binding.use_id,
                "requirement use does not permit a default",
            )
        elif binding.default_id != use.default_id:
            blockers.add(
                ValidationCode.DEFAULT_ID_MISMATCH,
                binding.use_id,
                "selected default differs from the bound requirement default",
                {"expected": use.default_id, "observed": binding.default_id},
            )
        return
    if not use.optional:
        blockers.add(
            ValidationCode.OMIT_NOT_ALLOWED,
            binding.use_id,
            "required use cannot be omitted",
        )


def _validate_within_use_distinctness(
    use: RequirementUse,
    binding: SatisfactionBinding,
    blockers: _Blockers,
) -> None:
    if use.distinctness is DistinctnessPolicy.ALLOW_SAME:
        return
    if use.distinctness is DistinctnessPolicy.DISTINCT_PRODUCER:
        keys = [(value.producer_kind.value, value.producer_id)
                for value in binding.outputs]
    else:
        keys = [(
            value.producer_kind.value, value.producer_id,
            value.output_port_id) for value in binding.outputs]
    if len(keys) != len(set(keys)):
        blockers.add(
            ValidationCode.DISTINCTNESS_VIOLATION,
            binding.use_id,
            "selected outputs violate within-use distinctness",
            {"policy": use.distinctness.value},
        )


def _resolve_descriptor(
    kind: ProducerKind,
    producer_id: str,
    output_port_id: str,
    invocations: dict[str, BoundInvocation],
    artifacts: dict[str, ArtifactLeaf],
    problem_invocations: dict[str, Any],
    problem_leaves: dict[str, Any],
    use_id: str,
    blockers: _Blockers,
):
    if kind is ProducerKind.INVOCATION:
        node = problem_invocations.get(producer_id)
        invocation = invocations.get(producer_id)
        if node is None:
            blockers.add(
                ValidationCode.SELECTED_PRODUCER_UNKNOWN,
                producer_id,
                "satisfaction references an invocation outside the universe",
                {"use_id": use_id},
            )
            return None
        if invocation is None:
            blockers.add(
                ValidationCode.INVOCATION_RECORD_MISSING,
                producer_id,
                "satisfaction references an invocation without a typed record",
                {"use_id": use_id},
            )
            return None
        if (output_port_id not in node.output_port_ids
                or output_port_id not in {
                    value.port_id for value in invocation.outputs}):
            blockers.add(
                ValidationCode.OUTPUT_UNKNOWN,
                producer_id,
                "satisfaction references an undeclared invocation output",
                {"use_id": use_id, "output_port_id": output_port_id},
            )
            return None
        return invocation.output(output_port_id).descriptor

    node = problem_leaves.get(producer_id)
    artifact = artifacts.get(producer_id)
    if node is None:
        blockers.add(
            ValidationCode.SELECTED_PRODUCER_UNKNOWN,
            producer_id,
            "satisfaction references an artifact outside the universe",
            {"use_id": use_id},
        )
        return None
    if artifact is None:
        blockers.add(
            ValidationCode.ARTIFACT_RECORD_MISSING,
            producer_id,
            "satisfaction references an artifact without a typed declaration",
            {"use_id": use_id},
        )
        return None
    if output_port_id not in node.output_port_ids:
        blockers.add(
            ValidationCode.OUTPUT_UNKNOWN,
            producer_id,
            "satisfaction references an undeclared artifact output",
            {"use_id": use_id, "output_port_id": output_port_id},
        )
        return None
    return artifact.descriptor


def _replay_selected_proof(
    use: RequirementUse,
    descriptor,
    proof_id: str,
    proof_by_id: dict[str, Any],
    context_by_id: dict[str, ProofReplayContext],
    blockers: _Blockers,
    *,
    expected_evidence_profile_id: str | None,
    derived_evidence_subject: EvidenceSubject | None,
) -> None:
    record = proof_by_id.get(proof_id)
    if record is None:
        blockers.add(
            ValidationCode.PROOF_RECORD_MISSING,
            proof_id,
            "selected edge has no frozen compatibility-proof record",
            {"use_id": use.requirement_use_id},
        )
        return
    try:
        proof = validate_compatibility_record(record)
    except (TypeError, ValueError, KeyError) as exc:
        blockers.add(
            ValidationCode.PROOF_RECORD_INVALID,
            proof_id,
            "selected compatibility-proof record does not authenticate",
            {"error": str(exc), "use_id": use.requirement_use_id},
        )
        return
    if proof.requirement_id != use.requirement.requirement_id:
        blockers.add(
            ValidationCode.PROOF_REQUIREMENT_MISMATCH,
            proof_id,
            "compatibility proof covers another requirement",
            {
                "expected": use.requirement.requirement_id,
                "observed": proof.requirement_id,
            },
        )
    if proof.descriptor_id != descriptor.descriptor_id:
        blockers.add(
            ValidationCode.PROOF_DESCRIPTOR_MISMATCH,
            proof_id,
            "compatibility proof covers another descriptor",
            {
                "expected": descriptor.descriptor_id,
                "observed": proof.descriptor_id,
            },
        )
    if not proof.satisfied:
        blockers.add(
            ValidationCode.PROOF_NOT_SATISFIED,
            proof_id,
            "selected edge carries a rejecting compatibility proof",
            {"rejection_codes": [
                value.value for value in proof.rejection_codes]},
        )

    context = context_by_id.get(proof_id)
    profile = None
    snapshot = None
    subject = None
    context_valid = True
    evidence_named = any((
        proof.evidence_profile_id,
        proof.evidence_snapshot_id,
        proof.evidence_subject_id,
    ))
    if (not evidence_named
            and expected_evidence_profile_id not in (None, "evidence:unknown")):
        blockers.add(
            ValidationCode.EVIDENCE_PROFILE_MISMATCH,
            proof_id,
            "selected producer names an evidence profile but the proof omits it",
            {"expected": expected_evidence_profile_id, "observed": None},
        )
        context_valid = False
    if evidence_named:
        if (proof.evidence_profile_id is not None
                and expected_evidence_profile_id is not None
                and proof.evidence_profile_id != expected_evidence_profile_id):
            blockers.add(
                ValidationCode.EVIDENCE_PROFILE_MISMATCH,
                proof_id,
                "proof evidence profile is not bound to the selected producer",
                {
                    "expected": expected_evidence_profile_id,
                    "observed": proof.evidence_profile_id,
                },
            )
            context_valid = False
        if (derived_evidence_subject is not None
                and proof.evidence_subject_id
                != derived_evidence_subject.identity):
            blockers.add(
                ValidationCode.EVIDENCE_SUBJECT_MISMATCH,
                proof_id,
                "proof evidence subject is not the selected invocation/output",
                {
                    "expected": derived_evidence_subject.identity,
                    "observed": proof.evidence_subject_id,
                },
            )
            context_valid = False
        if context is None:
            blockers.add(
                ValidationCode.EVIDENCE_REPLAY_CONTEXT_MISSING,
                proof_id,
                "evidence-bearing proof lacks exact replay inputs",
            )
            context_valid = False
        else:
            profile = context.evidence_profile
            snapshot = context.evidence_snapshot
            subject = context.evidence_subject
            if (profile is None
                    or profile.profile_id != proof.evidence_profile_id):
                blockers.add(
                    ValidationCode.EVIDENCE_PROFILE_MISMATCH,
                    proof_id,
                    "replay profile does not match the frozen proof",
                    {
                        "expected": proof.evidence_profile_id,
                        "observed": (
                            profile.profile_id if profile else None),
                    },
                )
                context_valid = False
            if proof.evidence_snapshot_id is None or snapshot is None:
                blockers.add(
                    ValidationCode.EVIDENCE_SNAPSHOT_REQUIRED,
                    proof_id,
                    "evidence-bearing proof requires its exact frozen snapshot",
                    {"expected": proof.evidence_snapshot_id},
                )
                context_valid = False
            elif snapshot.snapshot_id != proof.evidence_snapshot_id:
                blockers.add(
                    ValidationCode.EVIDENCE_SNAPSHOT_MISMATCH,
                    proof_id,
                    "replay snapshot does not match the frozen proof",
                    {
                        "expected": proof.evidence_snapshot_id,
                        "observed": snapshot.snapshot_id,
                    },
                )
                context_valid = False
            if (subject is None
                    or subject.identity != proof.evidence_subject_id):
                blockers.add(
                    ValidationCode.EVIDENCE_SUBJECT_MISMATCH,
                    proof_id,
                    "replay subject does not match the frozen proof",
                    {
                        "expected": proof.evidence_subject_id,
                        "observed": subject.identity if subject else None,
                    },
                )
                context_valid = False
            elif (derived_evidence_subject is not None
                  and subject != derived_evidence_subject):
                blockers.add(
                    ValidationCode.EVIDENCE_SUBJECT_MISMATCH,
                    proof_id,
                    "replay subject is not derived from the selected invocation",
                    {
                        "expected": derived_evidence_subject.identity,
                        "observed": subject.identity,
                    },
                )
                context_valid = False
            if (profile is not None and snapshot is not None
                    and not snapshot.contains(profile)):
                blockers.add(
                    ValidationCode.EVIDENCE_SNAPSHOT_MISMATCH,
                    proof_id,
                    "replay snapshot does not contain the exact evidence profile",
                )
                context_valid = False
    elif context is not None and any((
            context.evidence_profile, context.evidence_snapshot,
            context.evidence_subject)):
        # Extra evidence changes direct_match's proof identity, so it cannot be
        # silently introduced during replay.
        context_valid = False
        blockers.add(
            ValidationCode.PROOF_REPLAY_MISMATCH,
            proof_id,
            "replay supplied evidence that the frozen proof did not use",
        )

    if not context_valid:
        return
    try:
        replayed = direct_match(
            descriptor,
            use.requirement,
            profile,
            evidence_snapshot=snapshot,
            evidence_subject=subject,
        )
    except (TypeError, ValueError, KeyError) as exc:
        blockers.add(
            ValidationCode.PROOF_REPLAY_FAILED,
            proof_id,
            "direct compatibility replay raised an error",
            {"error": str(exc)},
        )
        return
    if replayed.to_dict() != proof.to_dict():
        blockers.add(
            ValidationCode.PROOF_REPLAY_MISMATCH,
            proof_id,
            "recomputed direct-match proof differs from the selected proof",
            {
                "expected_proof_id": replayed.proof_id,
                "observed_proof_id": proof.proof_id,
            },
        )
    if not replayed.satisfied:
        blockers.add(
            ValidationCode.DIRECT_MATCH_REJECTED,
            proof_id,
            "recomputed direct compatibility rejects the selected edge",
            {"rejection_codes": [
                value.value for value in replayed.rejection_codes]},
        )


def _validate_selected_sets(
    plan: CandidateDerivationPlan,
    referenced_invocations: set[str],
    referenced_leaves: set[str],
    blockers: _Blockers,
) -> None:
    for label, selected, referenced in (
            ("invocation", set(plan.selected_invocation_ids),
             referenced_invocations),
            ("artifact leaf", set(plan.selected_artifact_leaf_ids),
             referenced_leaves)):
        if selected != referenced:
            blockers.add(
                ValidationCode.SELECTED_SET_MISMATCH,
                plan.plan_id,
                f"selected {label} set differs from satisfaction references",
                {
                    "selected": sorted(selected),
                    "referenced": sorted(referenced),
                    "producer_kind": label,
                },
            )
        for producer_id in sorted(selected - referenced):
            blockers.add(
                ValidationCode.ORPHAN_PRODUCER,
                producer_id,
                f"selected {label} is not used by an active satisfaction",
            )


def _validate_sharing(
    output_consumers: dict[tuple[str, str, str], set[str]],
    typed_use: dict[str, RequirementUse],
    plan_id: str,
    blockers: _Blockers,
) -> None:
    for output, consumers in sorted(output_consumers.items()):
        if (len(consumers) > 1
                and any(use_id in typed_use and not typed_use[use_id].shareable
                        for use_id in consumers)):
            blockers.add(
                ValidationCode.NON_SHAREABLE_OUTPUT_REUSED,
                strict_hash({"plan_id": plan_id, "output": list(output)}),
                "one producer output is reused by a non-shareable requirement",
                {"output": list(output), "uses": sorted(consumers)},
            )


def _validate_distinctness_groups(
    problem: OracleProblem,
    satisfaction_by_use: dict[str, SatisfactionBinding],
    active_uses: set[str],
    blockers: _Blockers,
) -> None:
    groups: dict[str, list[tuple[Any, Any]]] = {}
    for node in problem.uses:
        if (node.use_id not in active_uses
                or node.distinctness_group is None):
            continue
        binding = satisfaction_by_use.get(node.use_id)
        if binding is None or binding.kind is not SatisfactionKind.PRODUCERS:
            continue
        for output in binding.outputs:
            groups.setdefault(node.distinctness_group, []).append((node, output))
    for group, values in sorted(groups.items()):
        keys: list[tuple[str, ...]] = []
        for node, output in values:
            if node.distinct_by is DistinctBy.PRODUCER:
                keys.append((output.producer_kind.value, output.producer_id))
            else:
                keys.append((output.producer_kind.value, output.producer_id,
                             output.output_port_id))
        if len(keys) != len(set(keys)):
            blockers.add(
                ValidationCode.DISTINCTNESS_VIOLATION,
                group,
                "selected outputs violate cross-use distinctness",
                {"uses": sorted({node.use_id for node, _ in values})},
            )


def _validate_deployment(
    selected_invocations: set[str],
    invocation_by_id: dict[str, BoundInvocation],
    problem_invocation: dict[str, Any],
    profile_by_id: dict[str, ExecutionProfile],
    snapshot: DeploymentCapabilitySnapshot | None,
    proof_by_profile: dict[str, DeploymentFeasibilityProof],
    deployment_choices: tuple[tuple[str, str], ...] | None,
    problem: OracleProblem,
    blockers: _Blockers,
) -> None:
    if selected_invocations and snapshot is None:
        blockers.add(
            ValidationCode.DEPLOYMENT_SNAPSHOT_MISSING,
            problem.problem_id,
            "selected invocations require a frozen deployment snapshot",
        )
    if snapshot is not None and snapshot.snapshot_id not in {
            value.snapshot_id for value in problem.snapshot_refs
            if value.name in {
                "deployment_feasibility",
                "selection_deployment_feasibility",
            }}:
        blockers.add(
            ValidationCode.DEPLOYMENT_SNAPSHOT_UNBOUND,
            snapshot.snapshot_id,
            "deployment snapshot is not bound into the candidate universe",
        )
    choice_by_invocation: dict[str, str] | None = None
    if deployment_choices is not None:
        choice_by_invocation = {}
        duplicate_ids: set[str] = set()
        for invocation_id, site_class_id in deployment_choices:
            if invocation_id in choice_by_invocation:
                duplicate_ids.add(invocation_id)
            choice_by_invocation[invocation_id] = site_class_id
        observed_ids = set(choice_by_invocation)
        if duplicate_ids or observed_ids != selected_invocations:
            blockers.add(
                ValidationCode.DEPLOYMENT_CHOICE_SET_MISMATCH,
                problem.problem_id,
                "deployment choices must cover each selected invocation exactly once",
                {
                    "expected_invocation_ids": sorted(selected_invocations),
                    "observed_invocation_ids": sorted(observed_ids),
                    "duplicate_invocation_ids": sorted(duplicate_ids),
                },
            )
    for invocation_id in sorted(selected_invocations):
        invocation = invocation_by_id.get(invocation_id)
        node = problem_invocation.get(invocation_id)
        if invocation is None or node is None:
            continue
        profile = profile_by_id.get(invocation.execution_profile_id)
        if profile is None:
            blockers.add(
                ValidationCode.EXECUTION_PROFILE_MISSING,
                invocation_id,
                "selected invocation lacks its exact execution profile",
                {"profile_id": invocation.execution_profile_id},
            )
            continue
        if profile.implementation != invocation.implementation:
            blockers.add(
                ValidationCode.EXECUTION_PROFILE_MISMATCH,
                invocation_id,
                "execution profile describes another result implementation",
                {"profile_id": profile.profile_id},
            )
        if snapshot is None:
            continue
        expected = snapshot.check(profile)
        supplied = proof_by_profile.get(profile.profile_id)
        if supplied is None:
            blockers.add(
                ValidationCode.DEPLOYMENT_PROOF_MISSING,
                invocation_id,
                "selected invocation lacks a supplied static feasibility proof",
                {"profile_id": profile.profile_id,
                 "snapshot_id": snapshot.snapshot_id},
            )
        elif supplied.to_dict() != expected.to_dict():
            blockers.add(
                ValidationCode.DEPLOYMENT_PROOF_MISMATCH,
                invocation_id,
                "supplied deployment proof differs from a fresh static check",
                {"profile_id": profile.profile_id,
                 "snapshot_id": snapshot.snapshot_id},
            )
        if not expected.feasible:
            blockers.add(
                ValidationCode.DEPLOYMENT_INFEASIBLE,
                invocation_id,
                "no frozen site class satisfies the execution profile",
                {"deployment_proof": expected.to_dict()},
            )
        if choice_by_invocation is not None:
            selected_site_id = choice_by_invocation.get(invocation_id)
            if selected_site_id is None:
                blockers.add(
                    ValidationCode.DEPLOYMENT_CHOICE_MISSING,
                    invocation_id,
                    "selected invocation has no exact deployment choice",
                )
            else:
                site = next((value for value in expected.sites
                             if value.site_class_id == selected_site_id), None)
                if site is None:
                    blockers.add(
                        ValidationCode.DEPLOYMENT_CHOICE_UNKNOWN,
                        invocation_id,
                        "selected deployment site is absent from the frozen proof",
                        {"site_class_id": selected_site_id},
                    )
                elif not site.feasible:
                    blockers.add(
                        ValidationCode.DEPLOYMENT_CHOICE_INFEASIBLE,
                        invocation_id,
                        "selected deployment site fails static feasibility",
                        {
                            "site_class_id": selected_site_id,
                            "rejections": [value.to_dict()
                                           for value in site.rejections],
                        },
                    )
        if node.deployable is not expected.feasible:
            blockers.add(
                ValidationCode.DEPLOYABILITY_PROJECTION_MISMATCH,
                invocation_id,
                "candidate-universe deployability flag disagrees with static replay",
                {
                    "expected": expected.feasible,
                    "observed": node.deployable,
                },
            )


def _validate_artifact_attestations(
    selected_leaves: set[str],
    artifact_by_id: dict[str, ArtifactLeaf],
    attestation_by_leaf: dict[str, ArtifactCommitAttestation],
    availability_snapshot: ArtifactAvailabilitySnapshot | None,
    problem: OracleProblem,
    blockers: _Blockers,
) -> tuple[str, ...]:
    bound_snapshot_ids = tuple(sorted({
        value.snapshot_id for value in problem.snapshot_refs
        if value.name == "artifact_availability"
    }))
    bound_snapshot_id = (
        bound_snapshot_ids[0] if len(bound_snapshot_ids) == 1 else None)
    if selected_leaves and bound_snapshot_id is None:
        blockers.add(
            ValidationCode.ARTIFACT_AVAILABILITY_SNAPSHOT_UNBOUND,
            problem.problem_id,
            "selected artifacts require exactly one availability snapshot "
            "bound into the candidate universe",
            {"bound_snapshot_ids": list(bound_snapshot_ids)},
        )
    availability_trusted = (
        availability_snapshot is not None
        and bound_snapshot_id is not None
        and availability_snapshot.snapshot_id == bound_snapshot_id)
    if availability_snapshot is not None and not availability_trusted:
        blockers.add(
            ValidationCode.ARTIFACT_AVAILABILITY_SNAPSHOT_UNBOUND,
            availability_snapshot.snapshot_id,
            "artifact availability snapshot does not match the exact snapshot "
            "bound into the candidate universe",
            {
                "expected_snapshot_id": bound_snapshot_id,
                "observed_snapshot_id": availability_snapshot.snapshot_id,
                "oracle_problem_id": problem.problem_id,
            },
        )
    valid: list[str] = []
    for leaf_id in sorted(selected_leaves):
        artifact = artifact_by_id.get(leaf_id)
        if artifact is None:
            continue
        attestation = attestation_by_leaf.get(leaf_id)
        snapshot_status: ArtifactCommitStatus | None = None
        if availability_trusted:
            try:
                snapshot_status = availability_snapshot.status_for(leaf_id)
            except KeyError:
                blockers.add(
                    ValidationCode.ARTIFACT_AVAILABILITY_RECORD_MISSING,
                    leaf_id,
                    "artifact availability snapshot has no record for the "
                    "selected leaf",
                    {"snapshot_id": availability_snapshot.snapshot_id},
                )
        if attestation is None and snapshot_status is None:
            blockers.add(
                ValidationCode.ARTIFACT_ATTESTATION_MISSING,
                leaf_id,
                "selected artifact lacks a separate trusted commit attestation",
            )
            continue
        attestation_valid = False
        if attestation is not None:
            expected = {
                "leaf_id": artifact.leaf_id,
                "artifact_id": artifact.artifact_id,
                "manifest_root_sha256": artifact.manifest_root_sha256,
                "descriptor_id": artifact.descriptor.descriptor_id,
            }
            observed = {
                "leaf_id": attestation.leaf_id,
                "artifact_id": attestation.artifact_id,
                "manifest_root_sha256": attestation.manifest_root_sha256,
                "descriptor_id": attestation.descriptor_id,
            }
            if (bound_snapshot_id is None
                    or attestation.snapshot_id != bound_snapshot_id):
                blockers.add(
                    ValidationCode.ARTIFACT_AVAILABILITY_SNAPSHOT_UNBOUND,
                    attestation.snapshot_id,
                    "artifact attestation does not belong to the exact "
                    "availability snapshot bound into the candidate universe",
                    {
                        "expected_snapshot_id": bound_snapshot_id,
                        "observed_snapshot_id": attestation.snapshot_id,
                        "leaf_id": leaf_id,
                    },
                )
            elif expected != observed:
                blockers.add(
                    ValidationCode.ARTIFACT_ATTESTATION_MISMATCH,
                    leaf_id,
                    "artifact commit attestation describes another realization",
                    {"expected": expected, "observed": observed},
                )
            elif not attestation.committed:
                blockers.add(
                    ValidationCode.ARTIFACT_NOT_COMMITTED,
                    leaf_id,
                    "artifact attestation does not assert a committed realization",
                    {"attestation_id": attestation.attestation_id},
                )
            else:
                attestation_valid = True

        snapshot_valid = snapshot_status is ArtifactCommitStatus.COMMITTED
        if snapshot_status is not None and not snapshot_valid:
            blockers.add(
                ValidationCode.ARTIFACT_NOT_COMMITTED,
                leaf_id,
                "trusted availability snapshot does not mark the selected "
                "artifact committed",
                {
                    "snapshot_id": availability_snapshot.snapshot_id,
                    "status": snapshot_status.value,
                },
            )
        if attestation_valid or snapshot_valid:
            valid.append(leaf_id)
    return tuple(valid)


def _validate_cost_and_constraints(
    plan: CandidateDerivationPlan,
    problem: OracleProblem,
    invocation_nodes: dict[str, Any],
    leaf_nodes: dict[str, Any],
    constraints: SelectionConstraints,
    blockers: _Blockers,
) -> int:
    selected_invocations = set(plan.selected_invocation_ids)
    selected_leaves = set(plan.selected_artifact_leaf_ids)
    cost = sum(
        invocation_nodes[value].cost_units
        for value in selected_invocations if value in invocation_nodes)
    cost += sum(
        leaf_nodes[value].cost_units
        for value in selected_leaves if value in leaf_nodes)
    if plan.total_cost_units != cost:
        blockers.add(
            ValidationCode.TOTAL_COST_MISMATCH,
            plan.plan_id,
            "selected plan's declared total cost does not recompute",
            {"expected": cost, "observed": plan.total_cost_units},
        )
    if constraints.max_cost_units is not None and cost > constraints.max_cost_units:
        blockers.add(
            ValidationCode.BUDGET_EXCEEDED,
            plan.plan_id,
            "selected plan exceeds the hard cost budget",
            {"budget": constraints.max_cost_units, "observed": cost},
        )

    universe_invocations = set(invocation_nodes)
    universe_leaves = set(leaf_nodes)
    for values, universe, kind in (
            (constraints.include_invocation_ids, universe_invocations,
             "invocation"),
            (constraints.exclude_invocation_ids, universe_invocations,
             "invocation"),
            (constraints.include_artifact_leaf_ids, universe_leaves,
             "artifact leaf"),
            (constraints.exclude_artifact_leaf_ids, universe_leaves,
             "artifact leaf")):
        for producer_id in sorted(set(values) - universe):
            blockers.add(
                ValidationCode.CONSTRAINT_PRODUCER_UNKNOWN,
                producer_id,
                f"hard constraint names an unknown {kind}",
                {"oracle_problem_id": problem.problem_id},
            )
    for producer_id in sorted(
            set(constraints.include_invocation_ids) - selected_invocations):
        blockers.add(
            ValidationCode.INCLUDE_CONSTRAINT_UNSATISFIED,
            producer_id,
            "required invocation was not selected",
        )
    for producer_id in sorted(
            set(constraints.include_artifact_leaf_ids) - selected_leaves):
        blockers.add(
            ValidationCode.INCLUDE_CONSTRAINT_UNSATISFIED,
            producer_id,
            "required artifact leaf was not selected",
        )
    for producer_id in sorted(
            set(constraints.exclude_invocation_ids) & selected_invocations):
        blockers.add(
            ValidationCode.EXCLUDED_PRODUCER_SELECTED,
            producer_id,
            "excluded invocation was selected",
        )
    for producer_id in sorted(
            set(constraints.exclude_artifact_leaf_ids) & selected_leaves):
        blockers.add(
            ValidationCode.EXCLUDED_PRODUCER_SELECTED,
            producer_id,
            "excluded artifact leaf was selected",
        )
    return cost


def _validate_selection_signature(
    plan: CandidateDerivationPlan,
    problem: OracleProblem,
    blockers: _Blockers,
) -> None:
    producer_order = tuple(sorted(
        [(ProducerKind.INVOCATION, value.invocation_id)
         for value in problem.invocations]
        + [(ProducerKind.ARTIFACT_LEAF, value.leaf_id)
           for value in problem.artifact_leaves],
        key=lambda value: (value[0].value, value[1]),
    ))
    choice_ids = {value.arc_id for value in problem.satisfaction_arcs}
    for use in problem.uses:
        if use.default_id is not None:
            choice_ids.update(SatisfactionBinding(
                use_id=use.use_id,
                kind=SatisfactionKind.DEFAULT,
                default_id=use.default_id,
            ).choice_ids)
        if use.optional:
            choice_ids.update(SatisfactionBinding(
                use_id=use.use_id,
                kind=SatisfactionKind.OMIT,
            ).choice_ids)
    ordered_choices = tuple(sorted(choice_ids))
    selected_producers = {
        *((ProducerKind.INVOCATION, value)
          for value in plan.selected_invocation_ids),
        *((ProducerKind.ARTIFACT_LEAF, value)
          for value in plan.selected_artifact_leaf_ids),
    }
    selected_choices = {
        value for binding in plan.satisfactions for value in binding.choice_ids}
    expected = tuple(
        int(value in selected_producers) for value in producer_order
    ) + tuple(
        int(value in selected_choices) for value in ordered_choices)
    if plan.selection_signature != expected:
        blockers.add(
            ValidationCode.SELECTION_SIGNATURE_MISMATCH,
            plan.plan_id,
            "selection signature does not match the frozen candidate ordering",
            {
                "expected": list(expected),
                "observed": list(plan.selection_signature),
            },
        )


def _validate_cycles_and_grounding(
    plan: CandidateDerivationPlan,
    problem_invocation: dict[str, Any],
    invocation_by_id: dict[str, BoundInvocation],
    satisfaction_by_use: dict[str, SatisfactionBinding],
    valid_artifact_attestations: set[str],
    blockers: _Blockers,
) -> None:
    selected = set(plan.selected_invocation_ids)
    dependencies: dict[str, set[str]] = {
        value: set() for value in selected}
    for consumer_id in sorted(selected):
        invocation = invocation_by_id.get(consumer_id)
        if invocation is None:
            continue
        for use in invocation.input_uses:
            binding = satisfaction_by_use.get(use.requirement_use_id)
            if binding is None or binding.kind is not SatisfactionKind.PRODUCERS:
                continue
            for output in binding.outputs:
                if output.producer_kind is not ProducerKind.INVOCATION:
                    continue
                node = problem_invocation.get(consumer_id)
                if (output.producer_id == consumer_id
                        and node is not None and node.atomic_iterative):
                    # Internal iteration is legal only when represented by one
                    # explicitly atomic node; it is not an inter-task edge.
                    continue
                dependencies[consumer_id].add(output.producer_id)

    cycles = _strongly_connected_cycles(dependencies)
    for cycle in cycles:
        blockers.add(
            ValidationCode.CYCLE_DETECTED,
            cycle[0],
            "selected derivation contains a non-atomic dependency cycle",
            {"invocation_ids": list(cycle)},
        )

    memo: dict[str, bool] = {}
    visiting: set[str] = set()

    def grounded(invocation_id: str) -> bool:
        if invocation_id in memo:
            return memo[invocation_id]
        if invocation_id in visiting:
            return False
        invocation = invocation_by_id.get(invocation_id)
        if invocation is None:
            return False
        visiting.add(invocation_id)
        result = True
        for use in invocation.input_uses:
            binding = satisfaction_by_use.get(use.requirement_use_id)
            if binding is None:
                result = False
                continue
            if binding.kind is SatisfactionKind.DEFAULT:
                if not use.optional or binding.default_id != use.default_id:
                    result = False
                continue
            if binding.kind is SatisfactionKind.OMIT:
                if not use.optional:
                    result = False
                continue
            for output in binding.outputs:
                if output.producer_kind is ProducerKind.ARTIFACT_LEAF:
                    if output.producer_id not in valid_artifact_attestations:
                        result = False
                elif (output.producer_id == invocation_id
                      and problem_invocation.get(invocation_id) is not None
                      and problem_invocation[invocation_id].atomic_iterative):
                    continue
                elif output.producer_id not in selected or not grounded(
                        output.producer_id):
                    result = False
        visiting.remove(invocation_id)
        memo[invocation_id] = result
        return result

    for invocation_id in sorted(selected):
        if not grounded(invocation_id):
            blockers.add(
                ValidationCode.UNGROUNDED_DERIVATION,
                invocation_id,
                "selected invocation is not grounded in executable sources or committed artifacts",
            )


def _strongly_connected_cycles(
    dependencies: dict[str, set[str]],
) -> tuple[tuple[str, ...], ...]:
    """Return deterministic cyclic strongly connected components."""
    index = 0
    indexes: dict[str, int] = {}
    lowlinks: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    components: list[tuple[str, ...]] = []

    def visit(node: str) -> None:
        nonlocal index
        indexes[node] = index
        lowlinks[node] = index
        index += 1
        stack.append(node)
        on_stack.add(node)
        for dependency in sorted(dependencies.get(node, ())):
            if dependency not in dependencies:
                continue
            if dependency not in indexes:
                visit(dependency)
                lowlinks[node] = min(lowlinks[node], lowlinks[dependency])
            elif dependency in on_stack:
                lowlinks[node] = min(lowlinks[node], indexes[dependency])
        if lowlinks[node] != indexes[node]:
            return
        component: list[str] = []
        while True:
            value = stack.pop()
            on_stack.remove(value)
            component.append(value)
            if value == node:
                break
        ordered = tuple(sorted(component))
        if (len(ordered) > 1
                or (len(ordered) == 1
                    and ordered[0] in dependencies.get(ordered[0], set()))):
            components.append(ordered)

    for node in sorted(dependencies):
        if node not in indexes:
            visit(node)
    return tuple(sorted(components))
