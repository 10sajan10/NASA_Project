"""Verified Stage-2 scientific-plan compiler into the Stage-1 kernel.

The compiler is intentionally narrow.  It accepts only the closed synthetic
operations used by the Stage-2 conformance slice and independently checks the
selected producer edges, implementation identities, output ports, deployment
bindings, and plan identities before creating a runtime graph.

Existing artifact leaves are never disguised as producer tasks.  A root-only
leaf needs no execution, but the compiler cannot authenticate its commit from
caller-supplied hashes and therefore reports that verification boundary.  A
leaf feeding a task remains unsupported until the Stage-1 runtime grows an
explicit committed-external-input binding.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable

from grid_convention import (
    FIELD_JSON_SCHEMA,
    FIELD_JSON_VALIDATOR_KIND,
    LEGACY_FIELD_JSON_SCHEMA,
)

from capabilities import (
    ArtifactLeaf,
    BoundInvocation,
    DeploymentCapabilitySnapshot,
    ExecutionProfile,
    artifact_evidence_subject,
    invocation_evidence_subject,
)
from engine.runtime.identity import strict_hash
from engine.runtime.operations import operation_component
from engine.runtime.types import (
    BoundExecutionGraph,
    InputBinding,
    OutputSpec,
    ResourceRequest,
    ScientificArtifactBinding,
    TaskTemplate,
)
from plans import (
    BoundDerivationPlan,
    DeploymentPlan,
    ProducerKind,
    SatisfactionKind,
)
from contracts import (
    EvidenceSnapshot,
    GRID_AFFINE_CONVENTION,
    RequirementUse,
    direct_match,
)
from transformations.model import verify_bound_transformation_authority
from acquisition.lowering import (
    ACQUISITION_OPERATION_KEY,
    verify_bound_acquisition_authority,
)

from .oracle import validate_compatibility_record


class CompilationStatus(str, Enum):
    COMPILED = "COMPILED"
    ARTIFACT_COMMIT_UNVERIFIED = "ARTIFACT_COMMIT_UNVERIFIED"
    BRIDGE_EXTERNAL_LEAF_UNSUPPORTED = "BRIDGE_EXTERNAL_LEAF_UNSUPPORTED"
    OPTIONAL_LOWERING_UNSUPPORTED = "OPTIONAL_LOWERING_UNSUPPORTED"


@dataclass(frozen=True)
class CompilationRecord:
    schema: str
    bound_plan_id: str
    deployment_plan_id: str | None
    status: CompilationStatus
    stage1_graph_id: str | None
    invocation_task_keys: tuple[tuple[str, str], ...]
    root_artifact_leaf_ids: tuple[str, ...]
    root_bindings: tuple[tuple[str, str, str, str], ...]
    output_descriptor_bindings: tuple[tuple[str, str, str], ...]
    message: str

    def __post_init__(self) -> None:
        if self.schema != "stage2-bound-to-stage1-compilation-v1":
            raise ValueError("unsupported compilation-record schema")
        if (self.output_descriptor_bindings
                != tuple(sorted(self.output_descriptor_bindings))
                or len({(invocation_id, port_id)
                        for invocation_id, port_id, _
                        in self.output_descriptor_bindings})
                != len(self.output_descriptor_bindings)):
            raise ValueError(
                "output descriptor bindings must be unique and canonical")
        if self.status is CompilationStatus.COMPILED:
            if (self.deployment_plan_id is None
                    or self.stage1_graph_id is None
                    or not self.invocation_task_keys):
                raise ValueError("compiled record requires graph and task mappings")
            if ({value[0] for value in self.invocation_task_keys}
                    != {value[0] for value in self.output_descriptor_bindings}):
                raise ValueError(
                    "compiled record must describe every invocation output")
        elif self.stage1_graph_id is not None:
            raise ValueError("non-compiled result cannot name a Stage-1 graph")

    @property
    def record_id(self) -> str:
        return strict_hash({
            "schema": self.schema,
            "bound_plan_id": self.bound_plan_id,
            "deployment_plan_id": self.deployment_plan_id,
            "status": self.status.value,
            "stage1_graph_id": self.stage1_graph_id,
            "invocation_task_keys": [list(value)
                                     for value in self.invocation_task_keys],
            "root_artifact_leaf_ids": list(self.root_artifact_leaf_ids),
            "root_bindings": [list(value) for value in self.root_bindings],
            "output_descriptor_bindings": [
                list(value) for value in self.output_descriptor_bindings],
            "message": self.message,
        })


@dataclass(frozen=True)
class CompilationResult:
    record: CompilationRecord
    graph: BoundExecutionGraph | None

    def __post_init__(self) -> None:
        if ((self.record.status is CompilationStatus.COMPILED)
                != (self.graph is not None)):
            raise ValueError("compilation record and graph disagree")
        if (self.graph is not None
                and self.record.stage1_graph_id != self.graph.plan_id):
            raise ValueError("compilation record names another graph")


def compile_bound_plan(
    plan: BoundDerivationPlan,
    invocations: Iterable[BoundInvocation],
    *,
    deployment_plan: DeploymentPlan | None = None,
    name: str = "stage2-composed-workflow",
    deployment_snapshot: DeploymentCapabilitySnapshot | None = None,
    execution_profiles: Iterable[ExecutionProfile] = (),
    root_uses: Iterable[RequirementUse] = (),
    artifact_leaves: Iterable[ArtifactLeaf] = (),
    evidence_snapshot: EvidenceSnapshot | None = None,
) -> CompilationResult:
    """Compile a validated bound derivation to exactly one task per invocation."""
    if not isinstance(plan, BoundDerivationPlan):
        raise TypeError("plan must be a BoundDerivationPlan")
    plan.validate_identity()
    plan.candidate_plan.validate_identity()

    invocation_values = tuple(invocations)
    invocation_by_id = {value.invocation_key: value
                        for value in invocation_values}
    if len(invocation_by_id) != len(invocation_values):
        raise ValueError("duplicate invocation identity supplied to compiler")
    selected = set(plan.candidate_plan.selected_invocation_ids)
    if set(invocation_by_id) != selected:
        raise ValueError("compiler invocation set does not match selected plan")
    if selected:
        if deployment_plan is None:
            raise ValueError("compilation requires an exact DeploymentPlan")
        if not isinstance(deployment_plan, DeploymentPlan):
            raise TypeError("deployment_plan must be a DeploymentPlan")
        deployment_plan.validate_identity()
        if deployment_plan.bound_plan_id != plan.bound_plan_id:
            raise ValueError(
                "deployment plan targets another bound derivation plan")
        if ({value.invocation_id for value
             in deployment_plan.invocation_bindings} != selected):
            raise ValueError(
                "deployment bindings do not match selected invocations")
    _validate_transformation_authorities(invocation_values)
    _validate_acquisition_authorities(invocation_values)
    _validate_executable_output_representations(invocation_values)

    leaf_ids = set(plan.candidate_plan.selected_artifact_leaf_ids)
    root_use_by_id = {
        value.requirement_use_id: value for value in tuple(root_uses)
    }
    if (set(root_use_by_id) != set(plan.candidate_plan.root_use_ids)
            or tuple(sorted(
                (use_id, value.requirement.requirement_id)
                for use_id, value in root_use_by_id.items()))
            != plan.candidate_plan.root_requirements):
        raise ValueError("compiler root requirements do not match the plan")
    artifact_leaf_by_id = {
        value.leaf_id: value for value in tuple(artifact_leaves)
    }
    if set(artifact_leaf_by_id) != leaf_ids:
        raise ValueError("compiler artifact leaf set does not match the plan")
    for binding in plan.artifact_bindings:
        leaf = artifact_leaf_by_id[binding.leaf_id]
        if (leaf.descriptor.descriptor_id != binding.descriptor_id
                or leaf.manifest_root_sha256 != binding.manifest_root):
            raise ValueError("artifact binding does not match the selected leaf")
    satisfactions = {
        value.use_id: value for value in plan.candidate_plan.satisfactions
    }
    _validate_plan_shape(
        plan, invocation_by_id, satisfactions, selected, leaf_ids,
        root_use_by_id, artifact_leaf_by_id, evidence_snapshot)
    leaf_consumed_by_invocation = False
    for invocation in invocation_by_id.values():
        for use in invocation.input_uses:
            binding = satisfactions.get(use.requirement_use_id)
            if binding is None:
                raise ValueError(
                    f"selected invocation input {use.requirement_use_id!r} "
                    "has no satisfaction")
            if binding.kind is not SatisfactionKind.PRODUCERS:
                return _noncompiled(
                    plan,
                    CompilationStatus.OPTIONAL_LOWERING_UNSUPPORTED,
                    "A selected invocation uses DEFAULT/OMIT, but its closed "
                    "binder has no verified runtime lowering.",
                    leaf_ids,
                    invocation_by_id,
                    deployment_plan,
                )
            if binding.kind is SatisfactionKind.PRODUCERS and any(
                    output.producer_kind is ProducerKind.ARTIFACT_LEAF
                    for output in binding.outputs):
                leaf_consumed_by_invocation = True

    if leaf_consumed_by_invocation:
        return _noncompiled(
            plan,
            CompilationStatus.BRIDGE_EXTERNAL_LEAF_UNSUPPORTED,
            "A selected ArtifactLeaf feeds an executable invocation; Stage 1 "
            "does not yet have an exact external-committed-input binding.",
            leaf_ids,
            invocation_by_id,
            deployment_plan,
        )
    if not selected:
        return _noncompiled(
            plan,
            CompilationStatus.ARTIFACT_COMMIT_UNVERIFIED,
            "Every root is bound to a declared artifact leaf, but caller-supplied "
            "identity and manifest hashes do not prove that the artifact is "
            "committed in a trusted store. External commit verification is "
            "required before treating the request as satisfied.",
            leaf_ids,
            invocation_by_id,
            deployment_plan,
        )

    execution_by_id = {
        value.invocation_id: value for value in plan.invocation_bindings
    }
    assert deployment_plan is not None
    deployment_by_id = {
        value.invocation_id: value
        for value in deployment_plan.invocation_bindings
    }
    profile_values = tuple(execution_profiles)
    profile_by_id = {value.profile_id: value for value in profile_values}
    if len(profile_by_id) != len(profile_values):
        raise ValueError("duplicate execution profile supplied to compiler")
    if deployment_snapshot is None or not profile_values:
        raise ValueError(
            "compilation requires a frozen deployment snapshot and profiles")
    if (deployment_plan.deployment_snapshot_ref.snapshot_id
            != deployment_snapshot.snapshot_id):
        raise ValueError(
            "deployment plan does not reference this deployment snapshot")
    task_key = {
        invocation_id: f"invocation-{invocation_id}"
        for invocation_id in sorted(selected)
    }
    templates: list[TaskTemplate] = []
    for invocation_id in _topological_invocations(
            invocation_by_id, satisfactions):
        invocation = invocation_by_id[invocation_id]
        execution = execution_by_id[invocation_id]
        placement = deployment_by_id[invocation_id]
        rule = invocation.binder.verify_current()
        current_component = operation_component(execution.operation_key)
        if current_component != invocation.implementation.verify_current():
            raise ValueError(
                f"IMPLEMENTATION_STALE for invocation {invocation_id}")
        if (rule.operation_key != current_component.operation_key
                or tuple(value.port_id for value in invocation.input_uses)
                != rule.input_ports
                or tuple(value.port_id for value in invocation.outputs)
                != rule.output_ports
                or tuple(sorted(invocation.parameters))
                != tuple(sorted(rule.parameter_names))):
            raise ValueError(
                f"closed binder no longer describes invocation {invocation_id}")
        if (execution.component_id != current_component.component_id
                or execution.component_version != current_component.version
                or execution.implementation_digest
                != current_component.implementation_digest
                or execution.runtime_parameters != invocation.parameters):
            raise ValueError(
                f"bound execution identity disagrees for invocation {invocation_id}")
        if placement.execution_profile_id != invocation.execution_profile_id:
            raise ValueError("deployment binding names another execution profile")
        try:
            profile = profile_by_id[placement.execution_profile_id]
        except KeyError as exc:
            raise ValueError("missing bound execution profile") from exc
        proof = deployment_snapshot.check(profile)
        if profile.implementation != invocation.implementation:
            raise ValueError(
                "execution profile covers another implementation")
        if (not proof.feasible
                or placement.deployment_class_id
                not in proof.feasible_site_class_ids):
            raise ValueError("selected deployment class is not feasible")
        request = _resource_request(placement.resource_request)
        envelope = profile.placement.resources
        site = next(value for value in deployment_snapshot.site_classes
                    if value.site_class_id == placement.deployment_class_id)
        if (request.cpu_cores < envelope.min_cpu_cores
                or request.cpu_cores > envelope.max_cpu_cores
                or request.memory_mb < envelope.min_memory_mb
                or request.memory_mb > envelope.max_memory_mb
                or request.gpus < envelope.min_gpus
                or request.gpus > envelope.max_gpus
                or request.cpu_cores > site.max_cpu_cores
                or request.memory_mb > site.max_memory_mb
                or request.gpus > site.max_gpus
                or request.mpi_ranks != 0):
            raise ValueError("attempt resources exceed the bound deployment envelope")

        input_bindings: list[InputBinding] = []
        for use in sorted(invocation.input_uses, key=lambda value: value.port_id):
            binding = satisfactions[use.requirement_use_id]
            if binding.kind is not SatisfactionKind.PRODUCERS:
                # Defaults/omissions are already frozen into invocation runtime
                # parameters and therefore have no Stage-1 artifact edge.
                continue
            if len(binding.outputs) != 1:
                raise ValueError(
                    "Stage-1 scalar compiler requires one output per runtime slot")
            source = binding.outputs[0]
            if source.producer_kind is not ProducerKind.INVOCATION:
                raise AssertionError("artifact leaves were rejected above")
            if source.producer_id not in selected:
                raise ValueError("input satisfaction references unselected producer")
            if source.output_port_id not in {
                    output.port_id
                    for output in invocation_by_id[source.producer_id].outputs}:
                raise ValueError("input satisfaction references undeclared output")
            input_bindings.append(InputBinding(
                input_name=use.port_id,
                upstream_task=task_key[source.producer_id],
                upstream_output=source.output_port_id,
            ))

        resource = _resource_request(placement.resource_request)
        templates.append(TaskTemplate(
            key=task_key[invocation_id],
            component=current_component,
            parameters=dict(invocation.parameters),
            inputs=tuple(input_bindings),
            outputs=tuple(OutputSpec(
                name=output.port_id,
                media_type="application/json",
                validation=_output_validation(invocation, output),
                scientific_binding=ScientificArtifactBinding(
                    bound_plan_id=plan.bound_plan_id,
                    invocation_id=invocation.invocation_key,
                    output_port=output.port_id,
                    descriptor_id=output.descriptor.descriptor_id,
                    descriptor=output.descriptor.to_dict(),
                ),
            ) for output in invocation.outputs),
            resources=resource,
        ))

    graph = BoundExecutionGraph.bind(
        f"stage2-bound-plan-{plan.bound_plan_id}-deployment-"
        f"{deployment_plan.deployment_plan_id}",
        templates,
        schema_version="stage2-bound-to-stage1-graph-v1",
    )
    mapping = tuple((invocation_id, task_key[invocation_id])
                    for invocation_id in sorted(selected))
    record = CompilationRecord(
        schema="stage2-bound-to-stage1-compilation-v1",
        bound_plan_id=plan.bound_plan_id,
        deployment_plan_id=deployment_plan.deployment_plan_id,
        status=CompilationStatus.COMPILED,
        stage1_graph_id=graph.plan_id,
        invocation_task_keys=mapping,
        root_artifact_leaf_ids=tuple(sorted(leaf_ids)),
        root_bindings=_root_bindings(plan, task_key),
        output_descriptor_bindings=_output_descriptor_bindings(
            invocation_by_id),
        message="The independently validated scientific plan compiled to "
                "one Stage-1 task per selected invocation; exact output "
                "descriptor bindings are retained by the compilation record.",
    )
    return CompilationResult(record, graph)


def _noncompiled(
    plan: BoundDerivationPlan,
    status: CompilationStatus,
    message: str,
    leaf_ids: set[str],
    invocations: dict[str, BoundInvocation],
    deployment_plan: DeploymentPlan | None,
) -> CompilationResult:
    return CompilationResult(CompilationRecord(
        schema="stage2-bound-to-stage1-compilation-v1",
        bound_plan_id=plan.bound_plan_id,
        deployment_plan_id=(None if deployment_plan is None
                            else deployment_plan.deployment_plan_id),
        status=status,
        stage1_graph_id=None,
        invocation_task_keys=(),
        root_artifact_leaf_ids=tuple(sorted(leaf_ids)),
        root_bindings=_root_bindings(plan, {}),
        output_descriptor_bindings=_output_descriptor_bindings(invocations),
        message=message,
    ), None)


def _resource_request(value: dict[str, object]) -> ResourceRequest:
    allowed = {"cpu_cores", "memory_mb", "gpus", "mpi_ranks", "walltime_s"}
    unknown = set(value) - allowed
    if unknown:
        raise ValueError(f"unsupported runtime resource fields: {sorted(unknown)}")
    return ResourceRequest(**value)


def _validate_plan_shape(
    plan: BoundDerivationPlan,
    invocations: dict[str, BoundInvocation],
    satisfactions: dict[str, object],
    selected_invocations: set[str],
    selected_leaves: set[str],
    root_uses: dict[str, RequirementUse],
    artifact_leaves: dict[str, ArtifactLeaf],
    evidence_snapshot: EvidenceSnapshot | None = None,
) -> None:
    """Recheck the selected subgraph without trusting optimizer/oracle output."""
    candidate = plan.candidate_plan
    active_uses = set(candidate.root_use_ids)
    typed_input_uses = {}
    declared_outputs = {
        invocation_id: {value.port_id for value in invocation.outputs}
        for invocation_id, invocation in invocations.items()
    }
    for invocation in invocations.values():
        for use in invocation.input_uses:
            active_uses.add(use.requirement_use_id)
            typed_input_uses[use.requirement_use_id] = use
    if set(satisfactions) != active_uses:
        raise ValueError(
            "selected plan satisfactions do not exactly cover active uses")

    proof_by_id = {
        value.proof_id: value for value in candidate.compatibility_proofs
    }
    output_consumers: dict[tuple[str, str, str], list[str]] = {}
    for use_id, binding in satisfactions.items():
        typed_use = typed_input_uses.get(use_id) or root_uses.get(use_id)
        if binding.kind is SatisfactionKind.PRODUCERS:
            if typed_use is not None and not (
                    typed_use.cardinality.minimum
                    <= len(binding.outputs)
                    <= typed_use.cardinality.maximum):
                raise ValueError("selected producer count violates cardinality")
            for output in binding.outputs:
                record = proof_by_id[output.proof_id]
                proof = validate_compatibility_record(record)
                evidence_bound = any(value is not None for value in (
                    proof.evidence_profile_id,
                    proof.evidence_snapshot_id,
                    proof.evidence_subject_id))
                if evidence_bound and evidence_snapshot is None:
                    # Failing closed remains the right answer when the caller
                    # cannot supply what direct_match actually used; replaying
                    # an evidence-bound proof without its evidence would check
                    # a weaker claim than the one that was selected.
                    raise ValueError(
                        "evidence-bound proof replay requires the frozen "
                        "evidence snapshot used by direct_match")
                if (not proof.satisfied
                        or (typed_use is not None
                            and proof.requirement_id
                            != typed_use.requirement.requirement_id)):
                    raise ValueError("selected edge has no matching satisfied proof")
                if output.producer_kind is ProducerKind.INVOCATION:
                    if output.producer_id not in selected_invocations:
                        raise ValueError("edge references an unselected invocation")
                    if output.output_port_id not in declared_outputs[
                            output.producer_id]:
                        raise ValueError("edge references an undeclared output port")
                    descriptor = next(
                        value.descriptor for value in
                        invocations[output.producer_id].outputs
                        if value.port_id == output.output_port_id)
                    if proof.descriptor_id != descriptor.descriptor_id:
                        raise ValueError("proof covers another artifact descriptor")
                    recomputed = direct_match(
                        descriptor, typed_use.requirement,
                        **_evidence_inputs(
                            proof, evidence_snapshot,
                            lambda: invocation_evidence_subject(
                                invocations[output.producer_id],
                                output.output_port_id)))
                elif output.producer_id not in selected_leaves:
                    raise ValueError("edge references an unselected artifact leaf")
                else:
                    artifact = next(
                        value for value in plan.artifact_bindings
                        if value.leaf_id == output.producer_id)
                    if proof.descriptor_id != artifact.descriptor_id:
                        raise ValueError("proof covers another artifact descriptor")
                    leaf = artifact_leaves[output.producer_id]
                    recomputed = direct_match(
                        leaf.descriptor, typed_use.requirement,
                        **_evidence_inputs(
                            proof, evidence_snapshot,
                            lambda: artifact_evidence_subject(
                                leaf, output.output_port_id)))
                if recomputed.to_dict() != proof.to_dict():
                    raise ValueError(
                        "selected proof does not equal independent direct_match")
                key = (output.producer_kind.value, output.producer_id,
                       output.output_port_id)
                output_consumers.setdefault(key, []).append(use_id)
        elif typed_use is not None:
            if not typed_use.optional:
                raise ValueError("required invocation input cannot be omitted/defaulted")
            if (binding.kind is SatisfactionKind.DEFAULT
                    and binding.default_id != typed_use.default_id):
                raise ValueError("selected default does not match the bound invocation")

    for consumers in output_consumers.values():
        if len(consumers) > 1 and any(
                not typed_input_uses[use_id].shareable
                for use_id in consumers if use_id in typed_input_uses):
            raise ValueError("non-shareable producer output is reused")


def _evidence_inputs(
    proof,
    evidence_snapshot,
    derive_subject,
) -> dict[str, Any]:
    """Rebuild the exact typed evidence inputs one selected proof was made with.

    The recorded identifiers are treated as claims to be checked, not as
    lookups to be trusted: the profile must really be in the frozen snapshot,
    the snapshot identity must match, and the subject is *derived* from the
    producer rather than read from the proof.  A forged evidence reference
    therefore fails here rather than replaying successfully.
    """
    if proof.evidence_profile_id is None:
        return {}
    assert evidence_snapshot is not None  # checked by the caller
    if proof.evidence_snapshot_id != evidence_snapshot.snapshot_id:
        raise ValueError(
            "selected proof cites another evidence snapshot")
    profile = next(
        (value for value in evidence_snapshot.profiles
         if value.profile_id == proof.evidence_profile_id), None)
    if profile is None:
        raise ValueError(
            "selected proof cites an evidence profile absent from the "
            "frozen snapshot")
    subject = derive_subject()
    if proof.evidence_subject_id != subject.identity:
        raise ValueError(
            "selected proof cites an evidence subject that this producer "
            "cannot have produced")
    return {
        "evidence_profile": profile,
        "evidence_snapshot": evidence_snapshot,
        "evidence_subject": subject,
    }


def _root_bindings(
    plan: BoundDerivationPlan,
    task_keys: dict[str, str],
) -> tuple[tuple[str, str, str, str], ...]:
    bindings = {value.use_id: value
                for value in plan.candidate_plan.satisfactions}
    artifact_by_id = {value.leaf_id: value for value in plan.artifact_bindings}
    result: list[tuple[str, str, str, str]] = []
    for use_id in sorted(plan.candidate_plan.root_use_ids):
        binding = bindings[use_id]
        if binding.kind is not SatisfactionKind.PRODUCERS or len(binding.outputs) != 1:
            raise ValueError("root must bind exactly one producer output")
        output = binding.outputs[0]
        if output.producer_kind is ProducerKind.INVOCATION:
            result.append((
                use_id,
                "TASK_OUTPUT" if output.producer_id in task_keys
                else "SELECTED_INVOCATION_OUTPUT",
                task_keys.get(output.producer_id, output.producer_id),
                output.output_port_id,
            ))
        else:
            artifact = artifact_by_id[output.producer_id]
            result.append((use_id, "DECLARED_ARTIFACT_MANIFEST_UNVERIFIED",
                           artifact.leaf_id, artifact.manifest_root))
    return tuple(result)


def _output_descriptor_bindings(
    invocations: dict[str, BoundInvocation],
) -> tuple[tuple[str, str, str], ...]:
    """Retain the scientific meaning of every lowered Stage-1 output."""
    return tuple(sorted(
        (invocation_id, output.port_id, output.descriptor.descriptor_id)
        for invocation_id, invocation in invocations.items()
        for output in invocation.outputs
    ))


def _validate_executable_output_representations(
    invocations: tuple[BoundInvocation, ...],
) -> None:
    """Reject descriptors the finite-JSON Stage-1 lowering cannot preserve."""
    for invocation in invocations:
        for output in invocation.outputs:
            if output.descriptor.representation != "application/json":
                raise ValueError(
                    "unsupported executable output representation "
                    f"{output.descriptor.representation!r} for invocation "
                    f"{invocation.invocation_key} port {output.port_id!r}; "
                    "Stage-2 finite_json lowering requires application/json")


def _validate_transformation_authorities(
    invocations: tuple[BoundInvocation, ...],
) -> None:
    """Replay semantic authority independently at the execution boundary."""
    for invocation in invocations:
        if not invocation.implementation.operation_key.startswith("transform."):
            if invocation.transformation_authority is not None:
                raise ValueError(
                    "non-transform invocation carries transformation authority")
            continue
        try:
            verify_bound_transformation_authority(invocation)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "transformation authority failed compiler replay for "
                f"invocation {invocation.invocation_key}: {exc}") from exc


def _validate_acquisition_authorities(
    invocations: tuple[BoundInvocation, ...],
) -> None:
    """Replay fetched-content authority independently before runtime lowering."""
    for invocation in invocations:
        if invocation.implementation.operation_key != ACQUISITION_OPERATION_KEY:
            if invocation.acquisition_authority is not None:
                raise ValueError(
                    "non-acquisition invocation carries acquisition authority")
            continue
        try:
            verify_bound_acquisition_authority(invocation)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "acquisition authority failed compiler replay for "
                f"invocation {invocation.invocation_key}: {exc}") from exc


def _output_validation(invocation: BoundInvocation, output) -> dict[str, object]:
    """Lower a typed field descriptor to the narrow Stage-4 commit validator."""
    descriptor = output.descriptor
    if descriptor.schema_version == LEGACY_FIELD_JSON_SCHEMA:
        raise ValueError(
            "legacy field-json-v1 lacks the canonical axis/grid contract and "
            "cannot be lowered for execution; publish field-json-v2")
    if descriptor.schema_version != FIELD_JSON_SCHEMA:
        return {"kind": "finite_json"}
    if descriptor.grid is None:
        raise ValueError(
            "field-json-v2 executable output requires an exact grid descriptor")
    if (descriptor.grid.crs != descriptor.spatial_support.crs
            or descriptor.grid.axis_order
            != descriptor.spatial_support.axis_order):
        raise ValueError(
            "field-json-v2 grid and spatial support CRS/axes disagree")
    descriptor.grid.require_support(descriptor.spatial_support)
    temporal = descriptor.temporal_support
    if not descriptor.component_names:
        raise ValueError(
            "field-json-v2 executable output requires an exact component "
            "contract in its artifact descriptor")
    component_names = list(descriptor.component_names)
    return {
        "kind": FIELD_JSON_VALIDATOR_KIND,
        "descriptor_id": descriptor.descriptor_id,
        "crs": descriptor.spatial_support.crs,
        "grid_affine_convention": GRID_AFFINE_CONVENTION,
        "grid_axis_order": list(descriptor.grid.axis_order),
        "grid_shape": list(descriptor.grid.shape),
        "grid_affine": list(descriptor.grid.affine),
        "grid_support_bounds": list(descriptor.spatial_support.bounds),
        "temporal": {
            "kind": temporal.kind.value,
            "start": temporal.start,
            "end": temporal.end,
            "cadence_s": temporal.cadence_s,
        },
        "component_names": component_names,
    }


def _topological_invocations(
    invocations: dict[str, BoundInvocation],
    satisfactions: dict[str, object],
) -> tuple[str, ...]:
    dependencies: dict[str, set[str]] = {key: set() for key in invocations}
    for consumer_id, invocation in invocations.items():
        for use in invocation.input_uses:
            binding = satisfactions[use.requirement_use_id]
            if binding.kind is SatisfactionKind.PRODUCERS:
                dependencies[consumer_id].update(
                    output.producer_id for output in binding.outputs
                    if output.producer_kind is ProducerKind.INVOCATION)
    ordered: list[str] = []
    remaining = {key: set(value) for key, value in dependencies.items()}
    while remaining:
        ready = sorted(key for key, deps in remaining.items() if not deps)
        if not ready:
            raise ValueError("selected derivation contains a cycle")
        for key in ready:
            ordered.append(key)
            remaining.pop(key)
        for deps in remaining.values():
            deps.difference_update(ready)
    return tuple(ordered)
