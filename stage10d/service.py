"""Durable target -> resolve -> bind -> compile -> execute orchestration.

This module is an application boundary, not another planner.  It only launches
an exact, independently validated Stage-3 result and retains every compiler
input needed to reconstruct the private Stage-10C publication authority after
a process restart.  A portable :class:`artifacts.WorkflowManifest` is useful
for inspection, but it is never accepted as executable authority.
"""
from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from artifacts import (
    ArtifactAvailability,
    ArtifactRecord,
    ArtifactRegistrySnapshot,
    ArtifactTargetCoordinator,
    ArtifactWorkflowOutcome,
    RuntimeArtifactEventBridge,
    TargetRequest,
    TargetStatus,
    WorkflowManifest,
)
from capabilities import (
    BoundInvocation,
    CapabilityCatalog,
    DeploymentCapabilitySnapshot,
)
from composition import CompilationStatus, compile_bound_plan
from contracts import EvidenceSnapshot
from engine.runtime import BoundExecutionGraph, RunState, WorkflowController
from engine.runtime.identity import (
    require_object_fields,
    strict_canonical_json,
    strict_hash,
    strict_json_loads,
)
from plans import (
    ArtifactLeafBinding,
    BoundDerivationPlan,
    BoundInvocationBinding,
    DeploymentPlan,
    InvocationDeploymentBinding,
    PlanSnapshotRef,
)
from resolution import ResolutionStatus


_SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS target_executions (
    target_id       TEXT PRIMARY KEY,
    execution_id    TEXT NOT NULL UNIQUE,
    context_json    TEXT NOT NULL,
    status          TEXT NOT NULL,
    run_id          TEXT NOT NULL UNIQUE,
    run_state       TEXT,
    receipt_json    TEXT,
    error           TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS target_executions_status
    ON target_executions(status, execution_id);
CREATE TABLE IF NOT EXISTS execution_service_meta (
    key     TEXT PRIMARY KEY,
    value   TEXT NOT NULL
);
"""


class StaleTargetContextError(RuntimeError):
    """The durable target receipt no longer equals a fresh exact resolution."""


class TargetExecutionStatus(str, Enum):
    PREPARED = "PREPARED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


@dataclass(frozen=True)
class TargetExecutionContext:
    """All immutable inputs required to replay one authorized compilation."""

    execution_id: str
    runtime_root: str
    coordinator_path: str
    artifact_registry_path: str
    target_request: TargetRequest
    target_manifest: WorkflowManifest
    resolution_id: str
    selector_problem_id: str
    hypergraph_id: str
    validation_report_id: str
    artifact_snapshot: ArtifactRegistrySnapshot
    capability_catalog: CapabilityCatalog
    deployment_snapshot: DeploymentCapabilitySnapshot
    evidence_snapshot: EvidenceSnapshot | None
    selected_invocations: tuple[BoundInvocation, ...]
    bound_plan: BoundDerivationPlan
    deployment_plan: DeploymentPlan
    compilation_record_id: str
    compilation_authority_id: str
    graph: BoundExecutionGraph

    def __post_init__(self) -> None:
        for value, label in (
                (self.execution_id, "execution identity"),
                (self.resolution_id, "resolution identity"),
                (self.selector_problem_id, "selector problem identity"),
                (self.hypergraph_id, "hypergraph identity"),
                (self.validation_report_id, "validation report identity"),
                (self.compilation_record_id, "compilation record identity"),
                (self.compilation_authority_id,
                 "compilation authority identity")):
            _require_digest(value, label)
        if not isinstance(self.target_request, TargetRequest):
            raise TypeError("execution context requires a TargetRequest")
        for value, label in (
                (self.runtime_root, "runtime_root"),
                (self.coordinator_path, "coordinator_path"),
                (self.artifact_registry_path, "artifact_registry_path")):
            if (not isinstance(value, str)
                    or not Path(value).is_absolute()
                    or str(Path(value).resolve()) != value):
                raise ValueError(
                    f"execution context {label} must be canonical")
        if not isinstance(self.target_manifest, WorkflowManifest):
            raise TypeError("execution context requires a WorkflowManifest")
        if not isinstance(self.artifact_snapshot, ArtifactRegistrySnapshot):
            raise TypeError("execution context requires an artifact snapshot")
        if not isinstance(self.capability_catalog, CapabilityCatalog):
            raise TypeError("execution context requires a capability catalog")
        if not isinstance(
                self.deployment_snapshot, DeploymentCapabilitySnapshot):
            raise TypeError("execution context requires a deployment snapshot")
        if (self.evidence_snapshot is not None
                and not isinstance(self.evidence_snapshot, EvidenceSnapshot)):
            raise TypeError("execution evidence snapshot has the wrong type")
        if (not isinstance(self.selected_invocations, tuple)
                or not all(isinstance(value, BoundInvocation)
                           for value in self.selected_invocations)
                or self.selected_invocations != tuple(sorted(
                    self.selected_invocations,
                    key=lambda value: value.invocation_key))
                or len({value.invocation_key
                        for value in self.selected_invocations})
                != len(self.selected_invocations)):
            raise ValueError(
                "selected invocations must be unique and canonically ordered")
        if not isinstance(self.bound_plan, BoundDerivationPlan):
            raise TypeError("execution context requires a bound plan")
        if not isinstance(self.deployment_plan, DeploymentPlan):
            raise TypeError("execution context requires a deployment plan")
        if not isinstance(self.graph, BoundExecutionGraph):
            raise TypeError("execution context requires a runtime graph")

        self.bound_plan.validate_identity()
        self.deployment_plan.validate_identity()
        self.graph.validate_identity()
        if (self.target_manifest.target_id != self.target_request.target_id
                or self.target_manifest.root_uses
                != self.target_request.roots
                or self.target_manifest.resolution_id != self.resolution_id
                or self.target_manifest.artifact_snapshot_id
                != self.artifact_snapshot.snapshot_id
                or self.target_manifest.plan_id
                != self.bound_plan.candidate_plan.plan_id):
            raise ValueError(
                "execution target, manifest, and frozen plan disagree")
        selected_ids = tuple(
            value.invocation_key for value in self.selected_invocations)
        if selected_ids != self.bound_plan.candidate_plan.selected_invocation_ids:
            raise ValueError(
                "execution invocations do not equal the selected plan")
        if self.deployment_plan.bound_plan_id != self.bound_plan.bound_plan_id:
            raise ValueError("execution deployment covers another bound plan")
        if (self.deployment_plan.deployment_snapshot_ref.snapshot_id
                != self.deployment_snapshot.snapshot_id):
            raise ValueError(
                "execution deployment names another deployment snapshot")
        selected_leaf_ids = set(
            self.bound_plan.candidate_plan.selected_artifact_leaf_ids)
        if selected_leaf_ids != {
                value.leaf_id for value in self.bound_plan.artifact_bindings}:
            raise ValueError("execution artifact bindings are incomplete")
        entries_by_leaf: dict[str, list[object]] = {}
        for value in self.artifact_snapshot.entries:
            entries_by_leaf.setdefault(
                value.record.leaf.leaf_id, []).append(value)
        for binding in self.bound_plan.artifact_bindings:
            matches = entries_by_leaf.get(binding.leaf_id, [])
            if len(matches) != 1:
                raise ValueError(
                    "selected artifact must occur exactly once in the frozen "
                    "snapshot")
            entry = matches[0]
            record = entry.record
            if (entry.availability is not ArtifactAvailability.COMMITTED
                    or binding.descriptor_id
                    != record.descriptor.descriptor_id
                    or binding.manifest_root
                    != record.manifest_root_sha256
                    or binding.content_digest != record.content_sha256):
                raise ValueError(
                    "selected artifact binding does not equal its committed "
                    "snapshot record")
        if self.execution_id != self.expected_id():
            raise ValueError("execution context identity does not verify")

    @property
    def run_id(self) -> str:
        """A deterministic run identity; retries cannot mint another run."""
        return self.execution_id

    @property
    def selected_records(self) -> tuple[ArtifactRecord, ...]:
        return tuple(sorted(
            (self.artifact_snapshot.record_for_leaf(value)
             for value in
             self.bound_plan.candidate_plan.selected_artifact_leaf_ids),
            key=lambda value: value.record_id,
        ))

    def _payload(self) -> dict[str, Any]:
        return {
            "schema": "stage10d-target-execution-context-v1",
            "runtime_root": self.runtime_root,
            "coordinator_path": self.coordinator_path,
            "artifact_registry_path": self.artifact_registry_path,
            "target_request": self.target_request.to_dict(),
            "target_manifest": self.target_manifest.to_dict(),
            "resolution_id": self.resolution_id,
            "selector_problem_id": self.selector_problem_id,
            "hypergraph_id": self.hypergraph_id,
            "validation_report_id": self.validation_report_id,
            "artifact_snapshot": self.artifact_snapshot.to_dict(),
            "capability_catalog": self.capability_catalog.to_dict(),
            "deployment_snapshot": self.deployment_snapshot.to_dict(),
            "evidence_snapshot": (
                None if self.evidence_snapshot is None
                else self.evidence_snapshot.to_dict()),
            "selected_invocations": [
                value.to_dict() for value in self.selected_invocations],
            "bound_plan": self.bound_plan.to_dict(),
            "deployment_plan": self.deployment_plan.to_dict(),
            "compilation_record_id": self.compilation_record_id,
            "compilation_authority_id": self.compilation_authority_id,
            "graph": self.graph.to_dict(),
        }

    def expected_id(self) -> str:
        return strict_hash(self._payload())

    def to_dict(self) -> dict[str, Any]:
        return {"execution_id": self.execution_id, **self._payload()}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "TargetExecutionContext":
        raw = require_object_fields(value, {
            "schema", "execution_id", "runtime_root", "coordinator_path",
            "artifact_registry_path", "target_request", "target_manifest",
            "resolution_id", "selector_problem_id", "hypergraph_id",
            "validation_report_id", "artifact_snapshot",
            "capability_catalog", "deployment_snapshot", "evidence_snapshot",
            "selected_invocations", "bound_plan", "deployment_plan",
            "compilation_record_id", "compilation_authority_id", "graph",
        }, "TargetExecutionContext")
        if raw.pop("schema") != "stage10d-target-execution-context-v1":
            raise ValueError("unsupported target execution context schema")
        if not isinstance(raw["selected_invocations"], list):
            raise TypeError(
                "TargetExecutionContext.selected_invocations must be an array")
        raw["target_request"] = TargetRequest.from_dict(
            raw["target_request"])
        raw["target_manifest"] = WorkflowManifest.from_dict(
            raw["target_manifest"])
        raw["artifact_snapshot"] = ArtifactRegistrySnapshot.from_dict(
            raw["artifact_snapshot"])
        raw["capability_catalog"] = CapabilityCatalog.from_dict(
            raw["capability_catalog"])
        raw["deployment_snapshot"] = DeploymentCapabilitySnapshot.from_dict(
            raw["deployment_snapshot"])
        if raw["evidence_snapshot"] is not None:
            raw["evidence_snapshot"] = EvidenceSnapshot.from_dict(
                raw["evidence_snapshot"])
        raw["selected_invocations"] = tuple(
            BoundInvocation.from_dict(item)
            for item in raw["selected_invocations"])
        raw["bound_plan"] = BoundDerivationPlan.from_dict(raw["bound_plan"])
        raw["deployment_plan"] = DeploymentPlan.from_dict(
            raw["deployment_plan"])
        raw["graph"] = BoundExecutionGraph.from_dict(raw["graph"])
        return cls(**raw)


@dataclass(frozen=True)
class TargetExecutionReceipt:
    receipt_id: str
    execution_id: str
    target_id: str
    target_manifest_id: str
    resolution_id: str
    artifact_snapshot_id: str
    bound_plan_id: str
    deployment_plan_id: str
    compilation_record_id: str
    compilation_authority_id: str
    stage1_graph_id: str
    run_id: str
    run_state: RunState

    def __post_init__(self) -> None:
        for value, label in (
                (self.receipt_id, "execution receipt identity"),
                (self.execution_id, "execution context identity"),
                (self.target_id, "execution target identity"),
                (self.target_manifest_id, "target manifest identity"),
                (self.resolution_id, "resolution identity"),
                (self.artifact_snapshot_id, "artifact snapshot identity"),
                (self.bound_plan_id, "bound plan identity"),
                (self.deployment_plan_id, "deployment plan identity"),
                (self.compilation_record_id, "compilation record identity"),
                (self.compilation_authority_id,
                 "compilation authority identity"),
                (self.stage1_graph_id, "runtime graph identity"),
                (self.run_id, "runtime run identity")):
            _require_digest(value, label)
        if self.run_state not in (
                RunState.SUCCEEDED, RunState.FAILED, RunState.CANCELLED):
            raise ValueError("execution receipt requires a terminal run state")
        if self.receipt_id != self.expected_id():
            raise ValueError("target execution receipt identity does not verify")

    @classmethod
    def bind(
            cls, context: TargetExecutionContext,
            run_state: RunState) -> "TargetExecutionReceipt":
        values = {
            "execution_id": context.execution_id,
            "target_id": context.target_request.target_id,
            "target_manifest_id": context.target_manifest.manifest_id,
            "resolution_id": context.resolution_id,
            "artifact_snapshot_id": context.artifact_snapshot.snapshot_id,
            "bound_plan_id": context.bound_plan.bound_plan_id,
            "deployment_plan_id": context.deployment_plan.deployment_plan_id,
            "compilation_record_id": context.compilation_record_id,
            "compilation_authority_id": context.compilation_authority_id,
            "stage1_graph_id": context.graph.plan_id,
            "run_id": context.run_id,
            "run_state": run_state,
        }
        provisional = object.__new__(cls)
        for name, value in values.items():
            object.__setattr__(provisional, name, value)
        object.__setattr__(provisional, "receipt_id", "")
        return cls(receipt_id=strict_hash(provisional._payload()), **values)

    def _payload(self) -> dict[str, Any]:
        return {
            "schema": "stage10d-target-execution-receipt-v1",
            "execution_id": self.execution_id,
            "target_id": self.target_id,
            "target_manifest_id": self.target_manifest_id,
            "resolution_id": self.resolution_id,
            "artifact_snapshot_id": self.artifact_snapshot_id,
            "bound_plan_id": self.bound_plan_id,
            "deployment_plan_id": self.deployment_plan_id,
            "compilation_record_id": self.compilation_record_id,
            "compilation_authority_id": self.compilation_authority_id,
            "stage1_graph_id": self.stage1_graph_id,
            "run_id": self.run_id,
            "run_state": self.run_state.value,
        }

    def expected_id(self) -> str:
        return strict_hash(self._payload())

    def to_dict(self) -> dict[str, Any]:
        return {"receipt_id": self.receipt_id, **self._payload()}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "TargetExecutionReceipt":
        raw = require_object_fields(value, {
            "schema", "receipt_id", "execution_id", "target_id",
            "target_manifest_id", "resolution_id", "artifact_snapshot_id",
            "bound_plan_id", "deployment_plan_id", "compilation_record_id",
            "compilation_authority_id", "stage1_graph_id", "run_id",
            "run_state",
        }, "TargetExecutionReceipt")
        if raw.pop("schema") != "stage10d-target-execution-receipt-v1":
            raise ValueError("unsupported target execution receipt schema")
        raw["run_state"] = RunState(raw["run_state"])
        return cls(**raw)


@dataclass(frozen=True)
class TargetExecutionState:
    context: TargetExecutionContext
    status: TargetExecutionStatus
    run_state: RunState | None
    receipt: TargetExecutionReceipt | None
    error: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.context, TargetExecutionContext):
            raise TypeError("target execution state requires a context")
        if not isinstance(self.status, TargetExecutionStatus):
            raise TypeError("target execution status must be typed")
        if self.run_state is not None and not isinstance(
                self.run_state, RunState):
            raise TypeError("runtime state must be RunState or None")
        if self.status in (
                TargetExecutionStatus.SUCCEEDED,
                TargetExecutionStatus.FAILED,
                TargetExecutionStatus.CANCELLED):
            if self.receipt is None:
                raise ValueError("terminal execution state requires a receipt")
            expected = {
                TargetExecutionStatus.SUCCEEDED: RunState.SUCCEEDED,
                TargetExecutionStatus.FAILED: RunState.FAILED,
                TargetExecutionStatus.CANCELLED: RunState.CANCELLED,
            }[self.status]
            if self.run_state is not expected:
                raise ValueError("application and runtime terminal states disagree")
        elif self.receipt is not None:
            raise ValueError("nonterminal execution state cannot have a receipt")
        if self.receipt is not None and (
                self.receipt.execution_id != self.context.execution_id):
            raise ValueError("execution receipt covers another context")


class TargetExecutionService:
    """Execute exact durable targets once, with crash-safe restart replay."""

    def __init__(
            self, path: str | Path, runtime_root: str | Path,
            coordinator: ArtifactTargetCoordinator, *,
            walltime_s: int = 30, timeout_s: float = 30.0,
            poll_interval_s: float = 0.01) -> None:
        if not isinstance(coordinator, ArtifactTargetCoordinator):
            raise TypeError("execution service requires a target coordinator")
        if (isinstance(walltime_s, bool) or not isinstance(walltime_s, int)
                or walltime_s < 1):
            raise ValueError("execution walltime must be a positive integer")
        for value, label in (
                (timeout_s, "execution timeout"),
                (poll_interval_s, "controller poll interval")):
            if (isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(float(value)) or value <= 0):
                raise ValueError(f"{label} must be positive and finite")
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.runtime_root = Path(runtime_root).resolve()
        self.coordinator = coordinator
        self.walltime_s = walltime_s
        self.timeout_s = float(timeout_s)
        self.poll_interval_s = float(poll_interval_s)
        with self._connect() as connection:
            connection.executescript(_SCHEMA)
            bindings = {
                "runtime_root": str(self.runtime_root),
                "coordinator_path": str(self.coordinator.path),
                "artifact_registry_path": str(
                    self.coordinator.resolver.artifact_registry.path),
            }
            for key, expected in bindings.items():
                row = connection.execute(
                    "SELECT value FROM execution_service_meta WHERE key=?",
                    (key,),
                ).fetchone()
                if row is None:
                    connection.execute(
                        "INSERT INTO execution_service_meta(key,value) "
                        "VALUES(?,?)", (key, expected))
                elif row["value"] != expected:
                    raise ValueError(
                        "execution service database is bound to another "
                        + key)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path), timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA synchronous = FULL")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def execute(
            self, target: str | TargetRequest) -> TargetExecutionState:
        """Resolve and run one durable target, or resume its exact prior run.

        Passing a :class:`WorkflowManifest` is intentionally unsupported.  It
        is an inspection/export record, not planning, compilation, or launch
        authority.
        """
        request = self._durable_request(target)
        existing = self._state_or_none(request.target_id)
        if existing is not None:
            if existing.context.target_request != request:
                raise ValueError(
                    "target execution names conflicting durable request bytes")
            if existing.status in (
                    TargetExecutionStatus.SUCCEEDED,
                    TargetExecutionStatus.FAILED,
                    TargetExecutionStatus.CANCELLED):
                # A completed receipt remains a valid historical record after
                # an input is archived. Service identity and resolver context
                # are still checked; byte liveness is no longer a prerequisite
                # for reading a terminal outcome.
                self._assert_resume_authority(
                    existing.context, require_live_inputs=False)
                return existing
            context = existing.context
        else:
            target_state, outcome = self._current_ready_outcome(request)
            context = self._build_context(
                request, target_state.workflow_manifest, outcome)
            context = self._insert_or_load_context(context)

        self._assert_resume_authority(context)

        compilation = self._recompile(context)
        assert compilation.graph is not None
        assert compilation.authority is not None
        bridge = RuntimeArtifactEventBridge(
            self.coordinator,
            compilation_authorities=(compilation.authority,),
        )
        try:
            with WorkflowController(
                    self.runtime_root,
                    poll_interval_s=self.poll_interval_s,
                    artifact_commit_observer=bridge) as controller:
                try:
                    run_state = controller.store.run_state(context.run_id)
                except KeyError:
                    # No attempt or run exists yet.  Re-resolve at the last
                    # possible application boundary and refuse a stale target
                    # instead of executing the stored manifest.
                    self._assert_context_is_current(context)
                    controller.create_run(
                        compilation.graph, run_id=context.run_id)
                    run_state = RunState.RUNNING
                self._record_running(context.execution_id, run_state)
                if run_state is RunState.RUNNING:
                    run_state = controller.run_until_terminal(
                        context.run_id, timeout_s=self.timeout_s)
                else:
                    # A terminal run may have committed just before the old
                    # process crashed. One tick replays the authorized output
                    # observer and is idempotent.
                    run_state = controller.tick(context.run_id)
        except BaseException as exc:
            self._record_error(context.execution_id, exc)
            raise
        return self._record_terminal(context, run_state)

    def state(self, target_id: str) -> TargetExecutionState:
        state = self._state_or_none(target_id)
        if state is None:
            raise KeyError(target_id)
        return state

    def states(self) -> tuple[TargetExecutionState, ...]:
        with self._connect() as connection:
            target_ids = tuple(row["target_id"] for row in connection.execute(
                "SELECT target_id FROM target_executions ORDER BY target_id"))
        return tuple(self.state(value) for value in target_ids)

    def _durable_request(self, target: str | TargetRequest) -> TargetRequest:
        if isinstance(target, WorkflowManifest):
            raise TypeError(
                "WorkflowManifest is not executable authority; pass its "
                "durable target_id")
        if isinstance(target, TargetRequest):
            state = self.coordinator.target(target.target_id)
            if state.request != target:
                raise ValueError(
                    "provided TargetRequest does not equal durable coordinator "
                    "bytes")
            return target
        if not isinstance(target, str) or not target:
            raise TypeError("target must be a target_id or TargetRequest")
        return self.coordinator.target(target).request

    def _current_ready_outcome(self, request: TargetRequest):
        target_state = self.coordinator.target(request.target_id)
        if (target_state.request != request
                or target_state.workflow_manifest is None):
            raise StaleTargetContextError(
                "durable target has no exact planning manifest")
        outcome = self.coordinator.resolver.resolve(
            request.roots,
            constraints=request.constraints,
            require_proven_optimal=request.require_proven_optimal,
        )
        current_manifest = WorkflowManifest.bind(
            request.target_id, request.roots, outcome)
        if target_state.workflow_manifest != current_manifest:
            raise StaleTargetContextError(
                "durable target manifest/snapshot is stale; resolve the target "
                "again before execution")
        self._require_ready(outcome)
        if target_state.status is not TargetStatus.PLANNED_WORKFLOW:
            raise RuntimeError(
                "coordinator target status disagrees with its READY workflow")
        return target_state, outcome

    @staticmethod
    def _require_ready(outcome: ArtifactWorkflowOutcome) -> None:
        resolution = outcome.resolution
        validation = resolution.validation
        if (resolution.status is not ResolutionStatus.READY
                or not resolution.eligible_for_binding
                or resolution.selection.plan is None
                or not resolution.selection.globally_optimal_over_discovery_space
                or validation is None
                or not validation.valid
                or not validation.validation_complete
                or not validation.candidate_universe_complete):
            raise ValueError(
                "target execution requires a READY, independently validated, "
                "globally optimal outcome")
        if not resolution.selection.plan.selected_invocation_ids:
            raise ValueError(
                "target is artifact-satisfied and has no workflow to execute")

    def _build_context(
            self, request: TargetRequest, manifest: WorkflowManifest | None,
            outcome: ArtifactWorkflowOutcome) -> TargetExecutionContext:
        assert manifest is not None
        plan = outcome.resolution.selection.plan
        validation = outcome.resolution.validation
        assert plan is not None and validation is not None
        selected_ids = set(plan.selected_invocation_ids)
        selected = tuple(sorted(
            (node.invocation
             for node in outcome.resolution.hypergraph.invocation_nodes
             if node.invocation_id in selected_ids),
            key=lambda value: value.invocation_key,
        ))
        if {value.invocation_key for value in selected} != selected_ids:
            raise ValueError(
                "selected plan invocations do not map exactly to the hypergraph")
        records = {
            value.leaf.leaf_id: value
            for value in outcome.selected_artifacts
        }
        if len(records) != len(outcome.selected_artifacts):
            raise ValueError(
                "selected artifact records are ambiguous for one leaf")
        if set(records) != set(plan.selected_artifact_leaf_ids):
            raise ValueError(
                "selected artifact records do not equal selected plan leaves")
        bound = BoundDerivationPlan.bind(
            plan,
            invocation_bindings=tuple(
                _bound_invocation_binding(value) for value in selected),
            artifact_bindings=tuple(
                ArtifactLeafBinding(
                    leaf_id=leaf_id,
                    descriptor_id=records[leaf_id].descriptor.descriptor_id,
                    manifest_root=records[leaf_id].manifest_root_sha256,
                    content_digest=records[leaf_id].content_sha256,
                )
                for leaf_id in sorted(records)),
            scientific_snapshot_refs=_scientific_snapshot_refs(
                self.coordinator, outcome),
        )
        sites = dict(outcome.resolution.selection.deployment_choices)
        profiles = {
            value.profile_id: value
            for value in self.coordinator.resolver.catalog.execution_profiles
        }
        if set(sites) != selected_ids:
            raise ValueError(
                "selected deployment choices do not equal selected invocations")
        deployment = DeploymentPlan.bind(
            bound,
            deployment_snapshot_ref=PlanSnapshotRef(
                "deployment",
                self.coordinator.resolver.deployment_snapshot.snapshot_id),
            invocation_bindings=tuple(
                _deployment_binding(
                    value, sites[value.invocation_key],
                    profiles[value.execution_profile_id], self.walltime_s)
                for value in selected),
        )
        compilation = compile_bound_plan(
            bound,
            selected,
            deployment_plan=deployment,
            name=f"stage10d-target-{request.target_id}",
            deployment_snapshot=self.coordinator.resolver.deployment_snapshot,
            execution_profiles=
                self.coordinator.resolver.catalog.execution_profiles,
            root_uses=request.roots,
            artifact_leaves=tuple(
                records[value].leaf
                for value in plan.selected_artifact_leaf_ids),
            evidence_snapshot=self.coordinator.resolver.evidence_snapshot,
            artifact_snapshot=(
                outcome.artifact_snapshot
                if plan.selected_artifact_leaf_ids else None),
            artifact_records=(
                outcome.selected_artifacts
                if plan.selected_artifact_leaf_ids else ()),
        )
        if (compilation.record.status is not CompilationStatus.COMPILED
                or compilation.graph is None
                or compilation.authority is None):
            raise ValueError(
                "validated target did not compile with publication authority: "
                + compilation.record.message)
        values = {
            "runtime_root": str(self.runtime_root),
            "coordinator_path": str(self.coordinator.path),
            "artifact_registry_path": str(
                self.coordinator.resolver.artifact_registry.path),
            "target_request": request,
            "target_manifest": manifest,
            "resolution_id": outcome.resolution.resolution_id,
            "selector_problem_id":
                outcome.resolution.selector_problem.problem_id,
            "hypergraph_id": outcome.resolution.hypergraph.graph_id,
            "validation_report_id": validation.report_id,
            "artifact_snapshot": outcome.artifact_snapshot,
            "capability_catalog": self.coordinator.resolver.catalog,
            "deployment_snapshot":
                self.coordinator.resolver.deployment_snapshot,
            "evidence_snapshot": self.coordinator.resolver.evidence_snapshot,
            "selected_invocations": selected,
            "bound_plan": bound,
            "deployment_plan": deployment,
            "compilation_record_id": compilation.record.record_id,
            "compilation_authority_id": compilation.authority.authority_id,
            "graph": compilation.graph,
        }
        provisional = object.__new__(TargetExecutionContext)
        for name, value in values.items():
            object.__setattr__(provisional, name, value)
        object.__setattr__(provisional, "execution_id", "")
        return TargetExecutionContext(
            execution_id=strict_hash(provisional._payload()), **values)

    def _recompile(self, context: TargetExecutionContext):
        compilation = compile_bound_plan(
            context.bound_plan,
            context.selected_invocations,
            deployment_plan=context.deployment_plan,
            name=f"stage10d-target-{context.target_request.target_id}",
            deployment_snapshot=context.deployment_snapshot,
            execution_profiles=context.capability_catalog.execution_profiles,
            root_uses=context.target_request.roots,
            artifact_leaves=tuple(
                value.leaf for value in context.selected_records),
            evidence_snapshot=context.evidence_snapshot,
            artifact_snapshot=(
                context.artifact_snapshot
                if context.bound_plan.candidate_plan.selected_artifact_leaf_ids
                else None),
            artifact_records=(
                context.selected_records
                if context.bound_plan.candidate_plan.selected_artifact_leaf_ids
                else ()),
        )
        if (compilation.record.status is not CompilationStatus.COMPILED
                or compilation.graph != context.graph
                or compilation.record.record_id
                != context.compilation_record_id
                or compilation.authority is None
                or compilation.authority.authority_id
                != context.compilation_authority_id):
            raise PermissionError(
                "frozen execution context no longer recompiles to the exact "
                "authorized graph")
        if context.runtime_root != str(self.runtime_root):
            raise PermissionError(
                "frozen execution context names another runtime_root")
        return compilation

    def _assert_resume_authority(
            self, context: TargetExecutionContext, *,
            require_live_inputs: bool = True) -> None:
        """Refuse publication through a different service authority set."""
        resolver = self.coordinator.resolver
        if (context.runtime_root != str(self.runtime_root)
                or context.coordinator_path != str(self.coordinator.path)
                or context.artifact_registry_path
                != str(resolver.artifact_registry.path)):
            raise PermissionError(
                "execution context is bound to another runtime/coordinator/"
                "registry authority")
        if (resolver.catalog != context.capability_catalog
                or resolver.deployment_snapshot != context.deployment_snapshot
                or resolver.evidence_snapshot != context.evidence_snapshot
                or strict_canonical_json(
                    resolver.discovery_certificate.to_dict())
                != strict_canonical_json(
                    context.target_manifest.discovery_certificate)
                or strict_canonical_json(resolver.discovery_universe.to_dict())
                != strict_canonical_json(
                    context.target_manifest.discovery_universe)):
            raise PermissionError(
                "execution resolver context changed since launch authority "
                "was frozen")
        if not require_live_inputs:
            return
        current = resolver.artifact_registry.snapshot()
        by_record: dict[str, list[object]] = {}
        for entry in current.entries:
            by_record.setdefault(entry.record.record_id, []).append(entry)
        for record in context.selected_records:
            entries = by_record.get(record.record_id, [])
            if (len(entries) != 1 or entries[0].record != record
                    or entries[0].availability
                    is not ArtifactAvailability.COMMITTED):
                raise PermissionError(
                    "frozen selected artifact is not exactly COMMITTED in "
                    "the current bound registry")

    def _assert_context_is_current(
            self, context: TargetExecutionContext) -> None:
        _state, outcome = self._current_ready_outcome(
            context.target_request)
        current = self._build_context(
            context.target_request,
            WorkflowManifest.bind(
                context.target_request.target_id,
                context.target_request.roots,
                outcome),
            outcome,
        )
        if current != context:
            raise StaleTargetContextError(
                "target planning context changed before runtime launch")

    def _insert_or_load_context(
            self, context: TargetExecutionContext) -> TargetExecutionContext:
        encoded = strict_canonical_json(context.to_dict())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT execution_id,context_json FROM target_executions "
                "WHERE target_id=?", (context.target_request.target_id,),
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO target_executions "
                    "(target_id,execution_id,context_json,status,run_id) "
                    "VALUES (?,?,?,?,?)",
                    (context.target_request.target_id, context.execution_id,
                     encoded, TargetExecutionStatus.PREPARED.value,
                     context.run_id),
                )
                connection.commit()
                return context
            connection.commit()
        existing = TargetExecutionContext.from_dict(
            _strict_object(row["context_json"], "execution context"))
        if (row["execution_id"] != existing.execution_id
                or existing != context):
            raise StaleTargetContextError(
                "target already has a different prepared execution context")
        return existing

    def _state_or_none(self, target_id: str) -> TargetExecutionState | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM target_executions WHERE target_id=?",
                (target_id,),
            ).fetchone()
        if row is None:
            return None
        context = TargetExecutionContext.from_dict(
            _strict_object(row["context_json"], "execution context"))
        if (row["execution_id"] != context.execution_id
                or row["run_id"] != context.run_id
                or row["target_id"] != context.target_request.target_id):
            raise ValueError("durable execution row conflicts with its context")
        run_state = (
            None if row["run_state"] is None else RunState(row["run_state"]))
        receipt = (None if row["receipt_json"] is None else
                   TargetExecutionReceipt.from_dict(_strict_object(
                       row["receipt_json"], "execution receipt")))
        if receipt is not None:
            if run_state is None or receipt != TargetExecutionReceipt.bind(
                    context, run_state):
                raise ValueError(
                    "durable execution receipt does not equal its exact "
                    "context and runtime state")
        return TargetExecutionState(
            context=context,
            status=TargetExecutionStatus(row["status"]),
            run_state=run_state,
            receipt=receipt,
            error=row["error"],
        )

    def _record_running(
            self, execution_id: str, run_state: RunState) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status FROM target_executions WHERE execution_id=?",
                (execution_id,),
            ).fetchone()
            if row is None:
                raise KeyError(execution_id)
            status = TargetExecutionStatus(row["status"])
            if status in (
                    TargetExecutionStatus.SUCCEEDED,
                    TargetExecutionStatus.FAILED,
                    TargetExecutionStatus.CANCELLED):
                raise RuntimeError("terminal target execution cannot restart")
            connection.execute(
                "UPDATE target_executions SET status=?,run_state=?,error='',"
                "updated_at=CURRENT_TIMESTAMP WHERE execution_id=?",
                (TargetExecutionStatus.RUNNING.value, run_state.value,
                 execution_id),
            )
            connection.commit()

    def _record_terminal(
            self, context: TargetExecutionContext,
            run_state: RunState) -> TargetExecutionState:
        if run_state not in (
                RunState.SUCCEEDED, RunState.FAILED, RunState.CANCELLED):
            raise RuntimeError(
                f"runtime returned a nonterminal state: {run_state.value}")
        receipt = TargetExecutionReceipt.bind(context, run_state)
        status = {
            RunState.SUCCEEDED: TargetExecutionStatus.SUCCEEDED,
            RunState.FAILED: TargetExecutionStatus.FAILED,
            RunState.CANCELLED: TargetExecutionStatus.CANCELLED,
        }[run_state]
        encoded = strict_canonical_json(receipt.to_dict())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT receipt_json,status FROM target_executions "
                "WHERE execution_id=?", (context.execution_id,),
            ).fetchone()
            if row is None:
                raise KeyError(context.execution_id)
            if row["receipt_json"] is not None and row["receipt_json"] != encoded:
                raise ValueError("terminal execution receipt identity conflict")
            if (row["status"] in (
                    TargetExecutionStatus.SUCCEEDED.value,
                    TargetExecutionStatus.FAILED.value,
                    TargetExecutionStatus.CANCELLED.value)
                    and row["status"] != status.value):
                raise ValueError("terminal execution state changed")
            connection.execute(
                "UPDATE target_executions SET status=?,run_state=?,"
                "receipt_json=?,error='',updated_at=CURRENT_TIMESTAMP "
                "WHERE execution_id=?",
                (status.value, run_state.value, encoded,
                 context.execution_id),
            )
            connection.commit()
        return self.state(context.target_request.target_id)

    def _record_error(self, execution_id: str, exc: BaseException) -> None:
        message = f"{type(exc).__name__}:{exc}"
        with self._connect() as connection:
            connection.execute(
                "UPDATE target_executions SET error=?,"
                "updated_at=CURRENT_TIMESTAMP WHERE execution_id=? "
                "AND status IN (?,?)",
                (message, execution_id,
                 TargetExecutionStatus.PREPARED.value,
                 TargetExecutionStatus.RUNNING.value),
            )


def _bound_invocation_binding(
        invocation: BoundInvocation) -> BoundInvocationBinding:
    component = invocation.implementation.verify_current()
    return BoundInvocationBinding(
        invocation_id=invocation.invocation_key,
        component_id=component.component_id,
        component_version=component.version,
        implementation_digest=component.implementation_digest,
        operation_key=component.operation_key,
        runtime_parameters=dict(invocation.parameters),
    )


def _deployment_binding(
        invocation: BoundInvocation, site_class_id: str, profile,
        walltime_s: int) -> InvocationDeploymentBinding:
    if profile.profile_id != invocation.execution_profile_id:
        raise ValueError("selected invocation names another execution profile")
    envelope = profile.placement.resources
    return InvocationDeploymentBinding(
        invocation_id=invocation.invocation_key,
        execution_profile_id=invocation.execution_profile_id,
        deployment_class_id=site_class_id,
        resource_request={
            "cpu_cores": envelope.min_cpu_cores,
            "memory_mb": envelope.min_memory_mb,
            "gpus": envelope.min_gpus,
            "mpi_ranks": 0,
            "walltime_s": walltime_s,
        },
    )


def _scientific_snapshot_refs(
        coordinator: ArtifactTargetCoordinator,
        outcome: ArtifactWorkflowOutcome) -> tuple[PlanSnapshotRef, ...]:
    resolver = coordinator.resolver
    values = [
        PlanSnapshotRef("artifact_registry", outcome.artifact_snapshot.snapshot_id),
        PlanSnapshotRef("capability_catalog", resolver.catalog.catalog_id),
        PlanSnapshotRef(
            "discovery_certificate",
            outcome.resolution.discovery_certificate.certificate_id),
        PlanSnapshotRef(
            "discovery_universe",
            outcome.resolution.discovery_universe.universe_id),
        PlanSnapshotRef("feasible_hypergraph",
                        outcome.resolution.hypergraph.graph_id),
    ]
    if resolver.evidence_snapshot is not None:
        values.append(PlanSnapshotRef(
            "evidence", resolver.evidence_snapshot.snapshot_id))
    return tuple(sorted(values, key=lambda value: value.name))


def _require_digest(value: str, label: str) -> None:
    if (not isinstance(value, str) or len(value) != 64
            or any(character not in "0123456789abcdef"
                   for character in value)):
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")


def _strict_object(value: str, label: str) -> dict[str, Any]:
    parsed = strict_json_loads(value)
    if not isinstance(parsed, dict):
        raise TypeError(f"{label} must be a JSON object")
    return parsed


__all__ = [
    "StaleTargetContextError",
    "TargetExecutionContext",
    "TargetExecutionReceipt",
    "TargetExecutionService",
    "TargetExecutionState",
    "TargetExecutionStatus",
]
