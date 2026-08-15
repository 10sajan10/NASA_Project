"""Domain-neutral Stage-3 discovery, selection, and validation service."""
from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from time import perf_counter_ns
from typing import Any, Iterable

from capabilities import (
    ArtifactLeaf,
    CapabilityCatalog,
    DeploymentCapabilitySnapshot,
)
from composition import OracleProblem, validate_compatibility_record
from contracts import EvidenceSnapshot, RequirementUse
from engine.runtime.identity import strict_hash

from .hypergraph import (
    ArtifactAvailabilitySnapshot,
    DiscoveryLimits,
    FeasibleDerivationHypergraph,
    build_feasible_hypergraph,
)
from .milp import (
    DeploymentOption,
    MilpSelectionProblem,
    MilpSelectionResult,
    MilpSolveOptions,
    MilpStatus,
    ProducerSelectionRef,
    SelectionConstraints,
    solve_milp,
)
from .projection import project_oracle_problem
from .validator import (
    ProofReplayContext,
    SelectedPlanValidationReport,
    SelectionConstraints as ValidatorSelectionConstraints,
    validate_selected_plan,
)


class ResolutionStatus(str, Enum):
    READY = "READY"
    FEASIBLE_NOT_PROVEN_OPTIMAL = "FEASIBLE_NOT_PROVEN_OPTIMAL"
    INCOMPLETE = "INCOMPLETE"
    UNSATISFIABLE = "UNSATISFIABLE"
    INVALID_SELECTION = "INVALID_SELECTION"
    ERROR = "ERROR"


@dataclass(frozen=True)
class PlanningMetrics:
    graph_build_ns: int
    selector_projection_ns: int
    solve_ns: int
    validation_ns: int
    serialization_ns: int
    total_ns: int

    def __post_init__(self) -> None:
        for name, value in self.to_dict().items():
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")

    def to_dict(self) -> dict[str, int]:
        return {
            "graph_build_ns": self.graph_build_ns,
            "selector_projection_ns": self.selector_projection_ns,
            "solve_ns": self.solve_ns,
            "validation_ns": self.validation_ns,
            "serialization_ns": self.serialization_ns,
            "total_ns": self.total_ns,
        }


@dataclass(frozen=True)
class ResolutionOutcome:
    """One immutable planning result plus measured, non-identity timings."""

    resolution_id: str
    status: ResolutionStatus
    hypergraph: FeasibleDerivationHypergraph
    selector_problem: OracleProblem
    selection: MilpSelectionResult
    validation: SelectedPlanValidationReport | None
    require_proven_optimal: bool
    eligible_for_binding: bool
    metrics: PlanningMetrics
    # Truncation reported by discovery that ran *before* this resolver and
    # produced its catalog (Stage-4 transformation closure, later Stage-5
    # remote search).  Empty means no upstream limit fired.
    upstream_limit_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.status, ResolutionStatus):
            raise TypeError("resolution status must be ResolutionStatus")
        if (not isinstance(self.upstream_limit_codes, tuple)
                or any(not isinstance(value, str) or not value.strip()
                       for value in self.upstream_limit_codes)
                or self.upstream_limit_codes
                != tuple(sorted(set(self.upstream_limit_codes)))):
            raise ValueError(
                "upstream limit codes must be unique, sorted, non-empty text")
        if type(self.require_proven_optimal) is not bool:
            raise TypeError("require_proven_optimal must be bool")
        if type(self.eligible_for_binding) is not bool:
            raise TypeError("eligible_for_binding must be bool")
        if self.selection.graph_problem_id != self.selector_problem.problem_id:
            raise ValueError("selection covers another selector problem")
        if self.validation is not None and (
                self.selection.plan is None
                or self.validation.plan_id != self.selection.plan.plan_id):
            raise ValueError("validation covers another selected plan")
        if self.resolution_id != strict_hash(self._identity_payload()):
            raise ValueError("resolution identity does not verify")

    def _identity_payload(self) -> dict[str, Any]:
        return {
            "schema": "stage3-resolution-outcome-v1",
            "status": self.status.value,
            "hypergraph_id": self.hypergraph.graph_id,
            "selector_problem_id": self.selector_problem.problem_id,
            "selection_problem_id": self.selection.selection_problem_id,
            "selection_status": self.selection.status.value,
            "primary_cost_proven": self.selection.primary_cost_proven,
            "tie_break_complete": self.selection.tie_break_complete,
            "selected_plan_id": (
                self.selection.plan.plan_id if self.selection.plan else None),
            "deployment_choices": [
                list(value) for value in self.selection.deployment_choices],
            "objective_cost_units": self.selection.objective_cost_units,
            "solver_status": self.selection.solver_status,
            "mip_gap": self.selection.mip_gap,
            "validation_report_id": (
                self.validation.report_id if self.validation else None),
            "require_proven_optimal": self.require_proven_optimal,
            "eligible_for_binding": self.eligible_for_binding,
            "upstream_limit_codes": list(self.upstream_limit_codes),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "stage3-resolution-outcome-v1",
            "resolution_id": self.resolution_id,
            "status": self.status.value,
            "hypergraph_id": self.hypergraph.graph_id,
            "selector_problem_id": self.selector_problem.problem_id,
            "discovery": {
                # ``complete`` is the effective value the optimality claim
                # rests on: this resolver's own expansion *and* any upstream
                # discovery that produced its catalog.
                # ``complete`` is what the optimality claim rests on;
                # ``graph_expansion_complete`` isolates this resolver's own
                # expansion so an upstream truncation stays attributable.
                # Upstream completeness is not re-derived from these two: a
                # conjunction cannot be inverted, and the limit codes below
                # already name every truncation that actually fired.
                "complete": self.selection.discovery_complete,
                "graph_expansion_complete": self.hypergraph.discovery_complete,
                "upstream_limit_codes": list(self.upstream_limit_codes),
                "requirements": len(self.hypergraph.requirement_nodes),
                "uses": len(self.hypergraph.use_nodes),
                "invocations": len(self.hypergraph.invocation_nodes),
                "artifacts": len(self.hypergraph.artifact_nodes),
                "satisfaction_arcs": len(self.hypergraph.satisfaction_arcs),
                "candidate_count": self.hypergraph.candidate_count,
                "limit_reasons": [
                    value.to_dict() for value in self.hypergraph.limit_reasons],
                "rejections": [
                    value.to_dict() for value in self.hypergraph.rejections],
            },
            "selection": {
                "selection_problem_id": self.selection.selection_problem_id,
                "graph_problem_id": self.selection.graph_problem_id,
                "status": self.selection.status.value,
                "discovery_complete": self.selection.discovery_complete,
                "primary_cost_proven": self.selection.primary_cost_proven,
                "tie_break_complete": self.selection.tie_break_complete,
                "globally_optimal": (
                    self.selection.globally_optimal_over_discovery_space),
                "plan_id": (
                    self.selection.plan.plan_id
                    if self.selection.plan is not None else None),
                "deployment_choices": [
                    list(value)
                    for value in self.selection.deployment_choices
                ],
                "objective_cost_units": self.selection.objective_cost_units,
                "solver_status": self.selection.solver_status,
                "mip_gap": self.selection.mip_gap,
                "mip_node_count": self.selection.mip_node_count,
                "solver_calls": self.selection.solver_calls,
                "solve_seconds": self.selection.solve_seconds,
                "solver_message": self.selection.solver_message,
                "blockers": [value.to_dict()
                             for value in self.selection.blockers],
            },
            "validation": (
                self.validation.to_dict() if self.validation else None),
            "require_proven_optimal": self.require_proven_optimal,
            "eligible_for_binding": self.eligible_for_binding,
            "metrics": self.metrics.to_dict(),
        }


class WorkflowResolver:
    """Resolve typed requirements against frozen catalogs and snapshots.

    This class performs no payload fetch and no component execution.  Its output
    becomes bindable only after independent validation and the requested
    optimality policy pass.
    """

    def __init__(
        self,
        catalog: CapabilityCatalog,
        deployment_snapshot: DeploymentCapabilitySnapshot,
        *,
        artifact_leaves: Iterable[ArtifactLeaf] = (),
        availability_snapshot: ArtifactAvailabilitySnapshot | None = None,
        evidence_snapshot: EvidenceSnapshot | None = None,
        discovery_limits: DiscoveryLimits = DiscoveryLimits(),
        upstream_discovery_complete: bool = True,
        upstream_limit_codes: Iterable[str] = (),
    ) -> None:
        if not isinstance(catalog, CapabilityCatalog):
            raise TypeError("catalog must be CapabilityCatalog")
        if not isinstance(deployment_snapshot, DeploymentCapabilitySnapshot):
            raise TypeError(
                "deployment_snapshot must be DeploymentCapabilitySnapshot")
        if type(upstream_discovery_complete) is not bool:
            raise TypeError("upstream_discovery_complete must be bool")
        codes = tuple(upstream_limit_codes)
        if any(not isinstance(value, str) or not value.strip()
               for value in codes):
            raise TypeError("upstream limit codes must be non-empty text")
        if upstream_discovery_complete and codes:
            raise ValueError(
                "complete upstream discovery cannot report limit codes")
        if not upstream_discovery_complete and not codes:
            raise ValueError(
                "incomplete upstream discovery must name its limit codes")
        self.catalog = catalog
        self.deployment_snapshot = deployment_snapshot
        self.artifact_leaves = tuple(artifact_leaves)
        self.availability_snapshot = availability_snapshot
        self.evidence_snapshot = evidence_snapshot
        self.discovery_limits = discovery_limits
        # Discovery performed before this resolver — Stage-4 transformation
        # closure today, Stage-5 remote metadata search later — may itself be
        # truncated.  A selection that is optimal over a catalog which is
        # missing candidates is not globally optimal, so upstream truncation
        # has to reach the same completeness flag the graph builder feeds.
        self.upstream_discovery_complete = upstream_discovery_complete
        self.upstream_limit_codes = tuple(sorted(set(codes)))

    def resolve(
        self,
        roots: Iterable[RequirementUse],
        *,
        constraints: SelectionConstraints | None = None,
        solve_options: MilpSolveOptions | None = None,
        require_proven_optimal: bool = True,
    ) -> ResolutionOutcome:
        if type(require_proven_optimal) is not bool:
            raise TypeError("require_proven_optimal must be bool")
        root_values = tuple(roots)
        request_constraints = constraints or SelectionConstraints()
        options = solve_options or MilpSolveOptions()
        started = perf_counter_ns()

        graph_started = perf_counter_ns()
        graph = build_feasible_hypergraph(
            self.catalog,
            self.deployment_snapshot,
            root_values,
            artifact_leaves=self.artifact_leaves,
            availability_snapshot=self.availability_snapshot,
            evidence_snapshot=self.evidence_snapshot,
            limits=self.discovery_limits,
        )
        graph_ns = perf_counter_ns() - graph_started

        projection_started = perf_counter_ns()
        selector_problem = project_oracle_problem(graph)
        projection_ns = perf_counter_ns() - projection_started

        discovery_complete = (
            graph.discovery_complete and self.upstream_discovery_complete)
        selection_request = MilpSelectionProblem.bind(
            selector_problem,
            discovery_complete=discovery_complete,
            discovery_limit_codes=(
                tuple(value.code.value for value in graph.limit_reasons)
                + self.upstream_limit_codes),
            deployment_options=_deployment_options(graph),
            constraints=request_constraints,
        )
        solve_started = perf_counter_ns()
        selection = solve_milp(selection_request, options=options)
        solve_ns = perf_counter_ns() - solve_started

        validation_started = perf_counter_ns()
        validation = self._validate(
            root_values, graph, selector_problem, selection,
            request_constraints, discovery_complete)
        validation_ns = perf_counter_ns() - validation_started

        structurally_valid = validation is not None and validation.valid
        feasible = selection.status in (
            MilpStatus.OPTIMAL,
            MilpStatus.FEASIBLE_NOT_PROVEN_OPTIMAL,
        )
        optimality_ok = (
            selection.globally_optimal_over_discovery_space
            or not require_proven_optimal)
        eligible = structurally_valid and feasible and optimality_ok
        if validation is not None and not validation.valid:
            status = ResolutionStatus.INVALID_SELECTION
        elif selection.status is MilpStatus.UNSATISFIABLE:
            # Infeasible over a truncated candidate universe is epistemically
            # unknown, never proof that no derivation exists.  Truncation
            # upstream of this resolver counts the same as truncation inside
            # its own graph expansion.
            status = (
                ResolutionStatus.UNSATISFIABLE
                if discovery_complete else ResolutionStatus.INCOMPLETE)
        elif selection.status is MilpStatus.LIMIT_NO_INCUMBENT:
            status = ResolutionStatus.INCOMPLETE
        elif selection.status is MilpStatus.ERROR:
            status = ResolutionStatus.ERROR
        elif selection.globally_optimal_over_discovery_space:
            status = ResolutionStatus.READY
        else:
            status = ResolutionStatus.FEASIBLE_NOT_PROVEN_OPTIMAL

        serialization_started = perf_counter_ns()
        json.dumps({
            "hypergraph": graph.to_dict(),
            "selected_plan": (
                selection.plan.to_dict() if selection.plan else None),
            "validation": validation.to_dict() if validation else None,
        }, allow_nan=False, sort_keys=True, separators=(",", ":"))
        serialization_ns = perf_counter_ns() - serialization_started
        metrics = PlanningMetrics(
            graph_build_ns=graph_ns,
            selector_projection_ns=projection_ns,
            solve_ns=solve_ns,
            validation_ns=validation_ns,
            serialization_ns=serialization_ns,
            total_ns=perf_counter_ns() - started,
        )
        provisional = object.__new__(ResolutionOutcome)
        identity_values = {
            "status": status,
            "hypergraph": graph,
            "selector_problem": selector_problem,
            "selection": selection,
            "validation": validation,
            "require_proven_optimal": require_proven_optimal,
            "eligible_for_binding": eligible,
            "metrics": metrics,
            "upstream_limit_codes": self.upstream_limit_codes,
        }
        for name, value in identity_values.items():
            object.__setattr__(provisional, name, value)
        resolution_id = strict_hash(provisional._identity_payload())
        return ResolutionOutcome(resolution_id=resolution_id, **identity_values)

    def _validate(
        self,
        roots: tuple[RequirementUse, ...],
        graph: FeasibleDerivationHypergraph,
        selector_problem: OracleProblem,
        selection: MilpSelectionResult,
        constraints: SelectionConstraints,
        candidate_universe_complete: bool,
    ) -> SelectedPlanValidationReport | None:
        plan = selection.plan
        if plan is None:
            return None
        selected_invocation_ids = set(plan.selected_invocation_ids)
        selected_leaf_ids = set(plan.selected_artifact_leaf_ids)
        invocations = tuple(
            value.invocation for value in graph.invocation_nodes
            if value.invocation_id in selected_invocation_ids)
        leaves = tuple(
            value.leaf for value in graph.artifact_nodes
            if value.leaf_id in selected_leaf_ids)
        return validate_selected_plan(
            plan,
            selector_problem,
            requirement_uses=roots,
            invocations=invocations,
            artifact_leaves=leaves,
            execution_profiles=self.catalog.execution_profiles,
            deployment_snapshot=self.deployment_snapshot,
            deployment_proofs=graph.deployment_proofs,
            deployment_choices=selection.deployment_choices,
            evidence_replays=_proof_replays(
                plan, self.evidence_snapshot),
            artifact_availability_snapshot=self.availability_snapshot,
            constraints=_validator_constraints(constraints),
            candidate_universe_complete=candidate_universe_complete,
        )


def _deployment_options(
    graph: FeasibleDerivationHypergraph,
) -> tuple[DeploymentOption, ...]:
    proof_by_profile = {
        value.profile_id: value for value in graph.deployment_proofs
    }
    result: list[DeploymentOption] = []
    for node in graph.invocation_nodes:
        proof = proof_by_profile[node.invocation.execution_profile_id]
        for site in proof.sites:
            result.append(DeploymentOption(
                invocation_id=node.invocation_id,
                site_class_id=site.site_class_id,
                feasible=site.feasible,
                blocker_codes=tuple(sorted({
                    value.code.value for value in site.rejections
                })),
            ))
    return tuple(sorted(result))


def _proof_replays(
    plan,
    evidence_snapshot: EvidenceSnapshot | None,
) -> tuple[ProofReplayContext, ...]:
    if evidence_snapshot is None:
        return ()
    profile_by_id = {
        value.profile_id: value for value in evidence_snapshot.profiles
    }
    result: list[ProofReplayContext] = []
    for record in plan.compatibility_proofs:
        proof = validate_compatibility_record(record)
        if proof.evidence_profile_id is None:
            continue
        profile = profile_by_id.get(proof.evidence_profile_id)
        if profile is None:
            continue
        result.append(ProofReplayContext(
            # Satisfaction edges select the authenticated wrapper identity,
            # while the wrapper carries the inner direct-match proof.
            proof_id=record.proof_id,
            evidence_profile=profile,
            evidence_snapshot=evidence_snapshot,
            evidence_subject=profile.subject,
        ))
    return tuple(result)


def _validator_constraints(
    value: SelectionConstraints,
) -> ValidatorSelectionConstraints:
    included_invocations = tuple(
        item.producer_id for item in value.include
        if item.producer_kind.value == "INVOCATION")
    included_leaves = tuple(
        item.producer_id for item in value.include
        if item.producer_kind.value == "ARTIFACT_LEAF")
    excluded_invocations = tuple(
        item.producer_id for item in value.exclude
        if item.producer_kind.value == "INVOCATION")
    excluded_leaves = tuple(
        item.producer_id for item in value.exclude
        if item.producer_kind.value == "ARTIFACT_LEAF")
    return ValidatorSelectionConstraints(
        max_cost_units=value.maximum_cost_units,
        include_invocation_ids=included_invocations,
        exclude_invocation_ids=excluded_invocations,
        include_artifact_leaf_ids=included_leaves,
        exclude_artifact_leaf_ids=excluded_leaves,
        # Completeness is an execution-policy gate on ResolutionOutcome.  The
        # validator must still establish structural safety for a timeout or
        # truncated-discovery incumbent.
        require_complete_universe=False,
    )


__all__ = [
    "PlanningMetrics",
    "ResolutionOutcome",
    "ResolutionStatus",
    "WorkflowResolver",
]
