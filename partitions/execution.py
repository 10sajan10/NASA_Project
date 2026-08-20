"""Compiling partitions into real Stage-1 tasks and running them.

Until now a Stage-7 "commit" was a durable state transition: the control plane
admitted a partition, packetised it, and recorded an outcome that a caller had
decided. Nothing executed. That made the 10^4-partition demonstration a
lifecycle exercise rather than partitioned science, and the README said so.

This module closes that gap for the executable case. A :class:`WorkPacket` is
compiled into a :class:`BoundExecutionGraph` -- one Stage-1 task per member,
keyed by the partition's own logical task key -- run through the durable
controller, and its *real* per-member outcome is fed back into the partition
store, where the existing retry and completeness rules apply unchanged.

Input-consuming partitions use a deliberately narrow bridge: a packet-sized,
content-addressed :class:`PartitionInputManifest` binds each invocation port
to an artifact that this same Stage-1 runtime has already committed.  The
compiler checks the authoritative recipe's complete scientific descriptor
against the selected ``RequirementUse``; run registration independently
rechecks the commit before any worker can start.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import uuid4

from contracts import ArtifactDescriptor, direct_match
from engine.runtime import (
    ExternalArtifactInputBinding,
    RunState,
    ScientificArtifactBinding,
    WorkflowController,
)
from engine.runtime.operations import operation_component
from engine.runtime.state import RuntimeStore
from engine.runtime.types import (
    BoundExecutionGraph,
    OutputSpec,
    ResourceRequest,
    TaskTemplate,
)

from .packet import WorkPacket
from .inputs import PartitionInputManifest
from .space import PartitionSetSpec
from .store import PartitionStore, RetryDecision
from .template import PartitionTaskTemplate


class PartitionNotExecutable(RuntimeError):
    """The template's invocation cannot run as a standalone partition task."""


def compile_packet(template: PartitionTaskTemplate,
                   packet: WorkPacket, *,
                   partition_spec: PartitionSetSpec | None = None,
                   input_manifest: PartitionInputManifest | None = None,
                   runtime_store: RuntimeStore | None = None,
                   ) -> BoundExecutionGraph:
    """Compile one packet into a Stage-1 graph, one task per partition.

    The task key *is* the partition's logical task key, so runtime identity
    and partition identity are the same thing and results map back without a
    side table.
    """
    if packet.template_id != template.template_id:
        raise ValueError("packet does not belong to this template")
    invocation = template.invocation
    if invocation.input_uses and (
            partition_spec is None or input_manifest is None
            or runtime_store is None):
        raise PartitionNotExecutable(
            f"capability {template.capability_id!r} consumes "
            f"{[use.port_id for use in invocation.input_uses]}; partition "
            "execution requires that inputs are already bound by an exact "
            "committed-artifact input manifest, partition spec, and "
            "authoritative runtime store")
    if not invocation.input_uses and input_manifest is not None:
        raise PartitionNotExecutable(
            "an input-free partition template cannot take an input manifest")
    if input_manifest is not None:
        assert partition_spec is not None
        if not isinstance(runtime_store, RuntimeStore):
            raise TypeError(
                "partition input compilation requires a RuntimeStore authority")
        input_manifest.verify_for(partition_spec, template, packet)
    component = operation_component(template.operation_key)
    if component != invocation.implementation.verify_current():
        raise PartitionNotExecutable(
            "the template's implementation is stale against the closed registry")
    resources = ResourceRequest.from_dict(
        dict(template.deployment_binding.resource_request))

    parameters = dict(template.parameters)
    outputs = tuple(OutputSpec(
        name=port.port_id,
        scientific_binding=ScientificArtifactBinding(
            bound_plan_id=template.template_id,
            invocation_id=invocation.invocation_key,
            capability_id=invocation.capability_id,
            capability_version=invocation.capability_version,
            evidence_profile_id=invocation.evidence_profile_id,
            output_port=port.port_id,
            descriptor_id=port.descriptor.descriptor_id,
            descriptor=port.descriptor.to_dict(),
        ),
    ) for port in invocation.outputs)
    uses = {value.requirement_use_id: value
            for value in invocation.input_uses}
    tasks: list[TaskTemplate] = []
    for member in packet.members:
        external_inputs: tuple[ExternalArtifactInputBinding, ...] = ()
        if input_manifest is not None:
            assignment = input_manifest.assignment_for(
                member.logical_task_key)
            compiled_inputs: list[ExternalArtifactInputBinding] = []
            for binding in assignment.inputs:
                use = uses[binding.requirement_use_id]
                # RuntimeStore is intentionally duck-typed here to keep the
                # partition identity module independent of SQLite internals;
                # accepting the returned record is not authority.  The exact
                # graph artifact ID is re-resolved inside create_run().
                try:
                    committed = runtime_store.committed_external_artifact(
                        binding.artifact_id)
                except (AttributeError, KeyError, RuntimeError, ValueError) as exc:
                    raise PartitionNotExecutable(
                        f"partition input {binding.input_name!r} is not an "
                        "authoritatively committed artifact") from exc
                scientific = committed.recipe.scientific_binding
                if scientific is None:
                    raise PartitionNotExecutable(
                        f"partition input {binding.input_name!r} has no "
                        "authoritative scientific descriptor")
                if scientific.descriptor_id != binding.descriptor_id:
                    raise PartitionNotExecutable(
                        f"partition input {binding.input_name!r} descriptor "
                        "does not match its frozen manifest binding")
                proof = direct_match(
                    # ScientificArtifactBinding construction has already
                    # replayed ArtifactDescriptor.from_dict and its identity.
                    ArtifactDescriptor.from_dict(scientific.descriptor),
                    use.requirement,
                )
                if not proof.satisfied:
                    codes = ",".join(
                        value.value for value in proof.rejection_codes)
                    raise PartitionNotExecutable(
                        f"committed artifact for input {binding.input_name!r} "
                        f"does not satisfy its selected RequirementUse: {codes}")
                compiled_inputs.append(ExternalArtifactInputBinding(
                    binding.input_name, binding.artifact_id))
            external_inputs = tuple(sorted(
                compiled_inputs, key=lambda value: value.input_name))
        tasks.append(TaskTemplate(
            key=member.logical_task_key,
            component=component,
            parameters=parameters,
            external_inputs=external_inputs,
            outputs=outputs,
            resources=resources,
            max_attempts=template.retry_policy.max_attempts,
        ))
    return BoundExecutionGraph.bind(
        f"partition-packet-{packet.packet_id[:12]}", tuple(tasks))


def execute_packet(
    store: PartitionStore,
    collection_id: str,
    template: PartitionTaskTemplate,
    packet: WorkPacket,
    *,
    runtime_root: Path | str,
    attempt_number: int | None = None,
    timeout_s: float = 120.0,
    lease_duration_s: float | None = None,
    controller_kwargs: dict[str, Any] | None = None,
    partition_spec: PartitionSetSpec | None = None,
    input_manifest: PartitionInputManifest | None = None,
) -> tuple[RetryDecision, RunState]:
    """Run one packet for real and record what actually happened.

    Each member's outcome comes from its own Stage-1 task state, so a packet
    where one partition fails records exactly that: the siblings stay
    committed and only the failure is considered for retry.
    """
    with WorkflowController(Path(runtime_root),
                            **(controller_kwargs or {})) as controller:
        effective_spec = partition_spec
        if input_manifest is not None:
            registered_spec, registered_template = store.collection_contract(
                collection_id)
            if registered_template != template:
                raise PartitionNotExecutable(
                    "partition execution template differs from collection authority")
            if (partition_spec is not None
                    and partition_spec != registered_spec):
                raise PartitionNotExecutable(
                    "partition input spec differs from collection authority")
            effective_spec = registered_spec
        graph = compile_packet(
            template, packet, partition_spec=effective_spec,
            input_manifest=input_manifest, runtime_store=controller.store)
        # Run registration writes only immutable control rows; no provider is
        # touched until run_until_terminal().  The packet intent can therefore
        # bind the exact run+plan before any worker submission.
        run_id = controller.create_run(graph)
        attempt = store.register_packet_attempt(
            collection_id,
            packet,
            fence_token=uuid4().hex,
            runtime_run_id=run_id,
            runtime_plan_id=graph.plan_id,
            lease_duration_s=(timeout_s + 60.0 if lease_duration_s is None
                              else lease_duration_s),
            expected_attempt_number=attempt_number,
        )
        run_state = controller.run_until_terminal(run_id, timeout_s=timeout_s)
        decision = store.record_runtime_packet_completion(
            collection_id, packet, attempt, controller.store)
    return decision, run_state


__all__ = ["PartitionNotExecutable", "compile_packet", "execute_packet"]
