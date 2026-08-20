"""Verified Stage-2 scientific-plan compiler into the Stage-1 kernel.

The compiler is intentionally narrow.  It accepts only the closed synthetic
operations used by the Stage-2 conformance slice and independently checks the
selected producer edges, implementation identities, output ports, deployment
bindings, and plan identities before creating a runtime graph.

Existing artifact leaves are never disguised as producer tasks.  A leaf may
feed a task only when its exact record is uniquely COMMITTED in the supplied
registry snapshot and its producer-owned bytes survive a fresh stable,
no-follow hash.  The resulting external binding remains distinct from a
Stage-1 artifact commit and is replayed independently by the runtime.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any, Iterable

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
from engine.runtime.native import (
    NATIVE_FILE_POINTER_VALIDATOR_KIND,
    NativeFilePointer,
    verify_native_file,
)
from engine.runtime.operations import operation_component
from engine.runtime.types import (
    BoundExecutionGraph,
    InputBinding,
    OutputSpec,
    ResourceRequest,
    RegisteredArtifactDelivery,
    RegisteredArtifactInputBinding,
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
    ArtifactDescriptor,
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

if TYPE_CHECKING:
    from artifacts.records import ArtifactRecord, ArtifactRegistrySnapshot


class CompilationStatus(str, Enum):
    COMPILED = "COMPILED"
    ARTIFACT_COMMIT_UNVERIFIED = "ARTIFACT_COMMIT_UNVERIFIED"
    BRIDGE_EXTERNAL_LEAF_UNSUPPORTED = "BRIDGE_EXTERNAL_LEAF_UNSUPPORTED"
    OPTIONAL_LOWERING_UNSUPPORTED = "OPTIONAL_LOWERING_UNSUPPORTED"


_COMPILATION_AUTHORITY_MINT = object()
_NATIVE_POINTER_OPERATION = "native.file_pointer.v1"
_NATIVE_POINTER_IDENTITY_OPERATION = "native.file_pointer_identity.v1"
_NATIVE_POINTER_OPERATIONS = frozenset({
    _NATIVE_POINTER_OPERATION,
    _NATIVE_POINTER_IDENTITY_OPERATION,
})


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
    registered_root_bindings: tuple[tuple[str, str, str, str], ...]
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
        if (self.registered_root_bindings
                != tuple(sorted(self.registered_root_bindings))
                or len({value[0] for value in self.registered_root_bindings})
                != len(self.registered_root_bindings)):
            raise ValueError(
                "registered root bindings must be unique and canonical")
        root_by_use = {value[0]: value for value in self.root_bindings}
        for use_id, snapshot_id, record_id, manifest_root \
                in self.registered_root_bindings:
            if (any(len(value) != 64
                    or any(char not in "0123456789abcdef" for char in value)
                    for value in (
                        use_id, snapshot_id, record_id, manifest_root))
                    or root_by_use.get(use_id) != (
                        use_id, "VERIFIED_REGISTERED_ARTIFACT",
                        record_id, manifest_root)):
                raise ValueError(
                    "registered root coordinate conflicts with root binding")
        if self.status is CompilationStatus.COMPILED:
            if (self.deployment_plan_id is None
                    or self.stage1_graph_id is None
                    or not self.invocation_task_keys):
                raise ValueError("compiled record requires graph and task mappings")
            if ({value[0] for value in self.invocation_task_keys}
                    != {value[0] for value in self.output_descriptor_bindings}):
                raise ValueError(
                    "compiled record must describe every invocation output")
        elif (self.stage1_graph_id is not None
              or self.registered_root_bindings):
            raise ValueError(
                "non-compiled result cannot name runtime artifact authority")

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
            "registered_root_bindings": [
                list(value) for value in self.registered_root_bindings],
            "output_descriptor_bindings": [
                list(value) for value in self.output_descriptor_bindings],
            "message": self.message,
        })


@dataclass(frozen=True, init=False)
class CompilationAuthority:
    """Trusted-local receipt issued with one exact compiled runtime graph.

    The private mint makes accidental caller-authored scientific labels
    structurally insufficient at the Stage-10 publication boundary.  This is
    deliberately not a cryptographic signature or a defense against code that
    already executes inside this trusted Python process; verification replays
    the content-addressed compilation record and graph instead.
    """

    authority_id: str
    record: CompilationRecord

    def __init__(
            self, mint: object, *, authority_id: str,
            record: CompilationRecord,
    ) -> None:
        if mint is not _COMPILATION_AUTHORITY_MINT:
            raise PermissionError(
                "CompilationAuthority is issued only by compile_bound_plan")
        if (not isinstance(record, CompilationRecord)
                or record.status is not CompilationStatus.COMPILED):
            raise TypeError(
                "compilation authority requires a compiled record")
        object.__setattr__(self, "authority_id", authority_id)
        object.__setattr__(self, "record", record)
        if authority_id != self.expected_id():
            raise ValueError("compilation authority identity does not verify")

    def expected_id(self) -> str:
        return strict_hash({
            "schema": "stage10c-compilation-authority-v1",
            "compilation_record_id": self.record.record_id,
            "stage1_graph_id": self.record.stage1_graph_id,
        })

    @property
    def stage1_graph_id(self) -> str:
        assert self.record.stage1_graph_id is not None
        return self.record.stage1_graph_id

    def verify(
            self, record: CompilationRecord,
            graph: BoundExecutionGraph,
    ) -> None:
        """Replay the exact compiler record against a loaded runtime graph."""
        if self.authority_id != self.expected_id():
            raise ValueError("compilation authority identity does not verify")
        if record != self.record or record.record_id != self.record.record_id:
            raise ValueError("compilation authority covers another record")
        if not isinstance(graph, BoundExecutionGraph):
            raise TypeError("compilation authority requires a runtime graph")
        graph.validate_identity()
        if (graph.plan_id != self.stage1_graph_id
                or graph.schema_version
                != "stage2-bound-to-stage1-graph-v1"):
            raise ValueError("compilation authority covers another graph")

        mappings = self.record.invocation_task_keys
        if (mappings != tuple(sorted(mappings))
                or len({value[0] for value in mappings}) != len(mappings)
                or len({value[1] for value in mappings}) != len(mappings)):
            raise ValueError("compilation task mappings are not canonical")
        invocation_by_task = {
            task_key: invocation_id for invocation_id, task_key in mappings}
        if set(invocation_by_task) != {task.key for task in graph.tasks}:
            raise ValueError(
                "compiled graph tasks disagree with the compilation record")
        expected = {
            (invocation_id, port_id): descriptor_id
            for invocation_id, port_id, descriptor_id
            in self.record.output_descriptor_bindings
        }
        observed: dict[tuple[str, str], str] = {}
        for task in graph.tasks:
            invocation_id = invocation_by_task[task.key]
            for recipe in task.outputs:
                binding = recipe.scientific_binding
                if binding is None:
                    raise ValueError(
                        "compiled output lacks a scientific binding")
                coordinate = (invocation_id, recipe.output_name)
                descriptor_id = expected.get(coordinate)
                if (descriptor_id is None
                        or binding.bound_plan_id != self.record.bound_plan_id
                        or binding.invocation_id != invocation_id
                        or binding.output_port != recipe.output_name
                        or binding.descriptor_id != descriptor_id
                        or ArtifactDescriptor.from_dict(
                            binding.descriptor).descriptor_id != descriptor_id):
                    raise ValueError(
                        "compiled output binding disagrees with the "
                        "compilation record")
                observed[coordinate] = descriptor_id
        if observed != expected:
            raise ValueError(
                "compiled graph outputs disagree with the compilation record")


@dataclass(frozen=True)
class CompilationResult:
    record: CompilationRecord
    graph: BoundExecutionGraph | None
    authority: CompilationAuthority | None = None

    def __post_init__(self) -> None:
        if ((self.record.status is CompilationStatus.COMPILED)
                != (self.graph is not None)):
            raise ValueError("compilation record and graph disagree")
        if ((self.record.status is CompilationStatus.COMPILED)
                != (self.authority is not None)):
            raise ValueError("compiled result and authority disagree")
        if (self.graph is not None
                and self.record.stage1_graph_id != self.graph.plan_id):
            raise ValueError("compilation record names another graph")
        if self.authority is not None:
            self.authority.verify(self.record, self.graph)


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
    artifact_snapshot: ArtifactRegistrySnapshot | None = None,
    artifact_records: Iterable[ArtifactRecord] = (),
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
    _validate_native_pointer_invocations(invocation_values)
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
    artifact_record_values = tuple(artifact_records)
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
    # Every artifact selected beside executable work is part of the scientific
    # result, even when it satisfies another root directly rather than feeding
    # a task.  Authenticate the complete selected leaf set so a mixed-root run
    # cannot report success after an unverified direct artifact disappears.
    if selected and leaf_ids:
        if artifact_snapshot is None or not artifact_record_values:
            return _noncompiled(
                plan,
                CompilationStatus.BRIDGE_EXTERNAL_LEAF_UNSUPPORTED,
                "Executable work was selected alongside ArtifactLeaf results, "
                "but the compiler was not given their exact committed "
                "registry snapshot and ArtifactRecord receipts.",
                leaf_ids,
                invocation_by_id,
                deployment_plan,
            )
        artifact_record_by_leaf = _verify_external_artifacts(
            plan,
            artifact_leaf_by_id,
            artifact_snapshot,
            artifact_record_values,
        )
    else:
        if artifact_snapshot is not None or artifact_record_values:
            raise ValueError(
                "artifact registry receipts were supplied but no selected "
                "leaf accompanies executable work")
        artifact_record_by_leaf = {}
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
        external_bindings: list[RegisteredArtifactInputBinding] = []
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
            if source.producer_kind is ProducerKind.ARTIFACT_LEAF:
                try:
                    record = artifact_record_by_leaf[source.producer_id]
                except KeyError as exc:
                    raise ValueError(
                        "artifact input has no verified registry receipt") from exc
                delivery = (
                    RegisteredArtifactDelivery.NATIVE_FILE_POINTER
                    if (current_component.operation_key
                        == _NATIVE_POINTER_IDENTITY_OPERATION
                        and use.port_id == "source")
                    else RegisteredArtifactDelivery.JSON_VALUE
                )
                if (delivery is RegisteredArtifactDelivery.JSON_VALUE
                        and record.media_type != "application/json"):
                    raise ValueError(
                        "JSON_VALUE delivery requires an application/json "
                        "ArtifactRecord")
                external_bindings.append(RegisteredArtifactInputBinding(
                    input_name=use.port_id,
                    snapshot_id=artifact_snapshot.snapshot_id,
                    record=record.to_dict(),
                    delivery=delivery,
                ))
                continue
            if source.producer_kind is not ProducerKind.INVOCATION:
                raise ValueError("input references an unknown producer kind")
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
            external_inputs=tuple(external_bindings),
            outputs=tuple(OutputSpec(
                name=output.port_id,
                media_type="application/json",
                validation=_output_validation(invocation, output),
                scientific_binding=ScientificArtifactBinding(
                    bound_plan_id=plan.bound_plan_id,
                    invocation_id=invocation.invocation_key,
                    capability_id=invocation.capability_id,
                    capability_version=invocation.capability_version,
                    evidence_profile_id=invocation.evidence_profile_id,
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
        external_manifest_roots=tuple(sorted(
            value.manifest_root_sha256
            for value in artifact_record_by_leaf.values())),
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
        root_bindings=_root_bindings(
            plan, task_key, artifact_record_by_leaf),
        registered_root_bindings=_registered_root_bindings(
            plan,
            artifact_record_by_leaf,
            artifact_snapshot.snapshot_id if artifact_record_by_leaf else None,
        ),
        output_descriptor_bindings=_output_descriptor_bindings(
            invocation_by_id),
        message="The independently validated scientific plan compiled to "
                "one Stage-1 task per selected invocation; exact output "
                "descriptor bindings are retained by the compilation record.",
    )
    authority = CompilationAuthority(
        _COMPILATION_AUTHORITY_MINT,
        authority_id=strict_hash({
            "schema": "stage10c-compilation-authority-v1",
            "compilation_record_id": record.record_id,
            "stage1_graph_id": graph.plan_id,
        }),
        record=record,
    )
    authority.verify(record, graph)
    return CompilationResult(record, graph, authority)


def _verify_external_artifacts(
    plan: BoundDerivationPlan,
    leaves: dict[str, ArtifactLeaf],
    snapshot: ArtifactRegistrySnapshot,
    records: tuple[ArtifactRecord, ...],
) -> dict[str, ArtifactRecord]:
    """Authenticate every selected external leaf and its current native bytes."""
    from artifacts.records import (
        ArtifactAvailability,
        ArtifactRecord,
        ArtifactRegistrySnapshot,
    )
    if not isinstance(snapshot, ArtifactRegistrySnapshot):
        raise TypeError("artifact_snapshot must be an ArtifactRegistrySnapshot")
    if (not all(isinstance(value, ArtifactRecord) for value in records)
            or len({value.record_id for value in records}) != len(records)):
        raise ValueError("artifact_records must be unique ArtifactRecords")
    snapshot_leaves: dict[str, list[object]] = {}
    for entry in snapshot.entries:
        snapshot_leaves.setdefault(
            entry.record.leaf.leaf_id, []).append(entry)
    by_leaf: dict[str, ArtifactRecord] = {}
    for record in records:
        leaf = record.leaf
        if leaf.leaf_id in by_leaf:
            raise ValueError("artifact records are ambiguous for one leaf")
        by_leaf[leaf.leaf_id] = record
        memberships = snapshot_leaves.get(leaf.leaf_id, [])
        if (len(memberships) != 1
                or memberships[0].record != record
                or memberships[0].availability
                is not ArtifactAvailability.COMMITTED):
            raise ValueError(
                "selected ArtifactRecord is not exactly COMMITTED in the "
                "frozen registry snapshot")
    if set(by_leaf) != set(leaves):
        raise ValueError(
            "artifact_records do not exactly cover selected artifact leaves")

    plan_bindings = {value.leaf_id: value for value in plan.artifact_bindings}
    if set(plan_bindings) != set(leaves):
        raise ValueError("bound artifact records do not match selected leaves")
    for leaf_id, declared_leaf in leaves.items():
        record = by_leaf[leaf_id]
        binding = plan_bindings[leaf_id]
        if record.leaf != declared_leaf:
            raise ValueError(
                "ArtifactRecord does not reproduce the selected ArtifactLeaf")
        if (binding.descriptor_id != record.descriptor.descriptor_id
                or binding.manifest_root != record.manifest_root_sha256
                or binding.content_digest != record.content_sha256):
            raise ValueError(
                "ArtifactLeafBinding does not match the selected record's "
                "descriptor, manifest, and content digest")
        verify_native_file(
            record.location,
            content_sha256=record.content_sha256,
            size_bytes=record.size_bytes,
        )
    return by_leaf


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
        registered_root_bindings=(),
        output_descriptor_bindings=_output_descriptor_bindings(invocations),
        message=message,
    ), None, None)


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
    registered: dict[str, ArtifactRecord] | None = None,
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
            record = (registered or {}).get(output.producer_id)
            if record is None:
                result.append((
                    use_id, "DECLARED_ARTIFACT_MANIFEST_UNVERIFIED",
                    artifact.leaf_id, artifact.manifest_root))
            else:
                result.append((
                    use_id, "VERIFIED_REGISTERED_ARTIFACT",
                    record.record_id, record.manifest_root_sha256))
    return tuple(result)


def _registered_root_bindings(
    plan: BoundDerivationPlan,
    registered: dict[str, ArtifactRecord],
    snapshot_id: str | None,
) -> tuple[tuple[str, str, str, str], ...]:
    """Retain snapshot/record/manifest coordinates for artifact roots."""
    if not registered:
        return ()
    if snapshot_id is None:
        raise ValueError("verified registered roots require a snapshot identity")
    satisfactions = {
        value.use_id: value for value in plan.candidate_plan.satisfactions}
    result = []
    for use_id in sorted(plan.candidate_plan.root_use_ids):
        binding = satisfactions[use_id]
        if (binding.kind is not SatisfactionKind.PRODUCERS
                or len(binding.outputs) != 1):
            continue
        output = binding.outputs[0]
        if output.producer_kind is not ProducerKind.ARTIFACT_LEAF:
            continue
        record = registered[output.producer_id]
        result.append((
            use_id,
            snapshot_id,
            record.record_id,
            record.manifest_root_sha256,
        ))
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
            if (invocation.implementation.operation_key
                    not in _NATIVE_POINTER_OPERATIONS
                    and output.descriptor.representation
                    != "application/json"):
                raise ValueError(
                    "unsupported executable output representation "
                    f"{output.descriptor.representation!r} for invocation "
                    f"{invocation.invocation_key} port {output.port_id!r}; "
                    "Stage-2 finite_json lowering requires application/json")


def _validate_native_pointer_invocations(
    invocations: tuple[BoundInvocation, ...],
) -> None:
    """Replay the two closed no-copy pointer contracts before graph minting."""
    for invocation in invocations:
        operation = invocation.implementation.operation_key
        if operation == _NATIVE_POINTER_OPERATION:
            if (len(invocation.outputs) != 1
                    or invocation.outputs[0].port_id != "result"):
                raise ValueError(
                    "native pointer producer has an invalid output contract")
            try:
                pointer = NativeFilePointer.from_dict(
                    invocation.parameters["pointer"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(
                    "native pointer producer has an invalid bound pointer") \
                    from exc
            if (pointer.media_type
                    != invocation.outputs[0].descriptor.representation):
                raise ValueError(
                    "native pointer media type disagrees with its output "
                    "descriptor")
        elif operation == _NATIVE_POINTER_IDENTITY_OPERATION:
            if (len(invocation.input_uses) != 1
                    or len(invocation.outputs) != 1
                    or invocation.input_uses[0].port_id != "source"
                    or invocation.outputs[0].port_id != "result"
                    or invocation.input_uses[0].requirement.exact_descriptor_id
                    != invocation.outputs[0].descriptor.descriptor_id):
                raise ValueError(
                    "native pointer identity must preserve one exact "
                    "descriptor")


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
    operation_key = getattr(
        getattr(invocation, "implementation", None), "operation_key", None)
    if operation_key in _NATIVE_POINTER_OPERATIONS:
        return {"kind": NATIVE_FILE_POINTER_VALIDATOR_KIND}
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
