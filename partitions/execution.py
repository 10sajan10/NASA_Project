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

What is still not general: a partition's *inputs* are not yet bound per
partition. Every partition of a template runs the same resolved invocation, so
this executes real science identically across the space rather than tiling a
dataset across it. Per-partition input binding is the acquisition bridge and is
not built.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from engine.runtime import RunState, WorkflowController
from engine.runtime.operations import operation_component
from engine.runtime.types import (
    BoundExecutionGraph,
    OutputSpec,
    ResourceRequest,
    TaskTemplate,
)

from .packet import MemberOutcome, PacketAttempt, PacketResult, WorkPacket
from .store import PartitionStore, RetryDecision
from .template import PartitionTaskTemplate


class PartitionNotExecutable(RuntimeError):
    """The template's invocation cannot run as a standalone partition task."""


def compile_packet(template: PartitionTaskTemplate,
                   packet: WorkPacket) -> BoundExecutionGraph:
    """Compile one packet into a Stage-1 graph, one task per partition.

    The task key *is* the partition's logical task key, so runtime identity
    and partition identity are the same thing and results map back without a
    side table.
    """
    if packet.template_id != template.template_id:
        raise ValueError("packet does not belong to this template")
    invocation = template.invocation
    if invocation.input_uses:
        # A partition task with unbound upstream inputs would have to invent
        # them.  Refuse rather than fabricate: binding inputs per partition is
        # the acquisition bridge, and it does not exist yet.
        raise PartitionNotExecutable(
            f"capability {template.capability_id!r} consumes "
            f"{[use.port_id for use in invocation.input_uses]}; partition "
            "execution requires an invocation whose inputs are already bound")
    component = operation_component(template.operation_key)
    if component != invocation.implementation.verify_current():
        raise PartitionNotExecutable(
            "the template's implementation is stale against the closed registry")

    parameters = dict(template.parameters)
    outputs = tuple(OutputSpec(name=port.port_id) for port in invocation.outputs)
    tasks = tuple(
        TaskTemplate(
            key=member.logical_task_key,
            component=component,
            parameters=parameters,
            outputs=outputs,
            resources=ResourceRequest(cpu_cores=1, memory_mb=128,
                                      walltime_s=60),
        )
        for member in packet.members
    )
    return BoundExecutionGraph.bind(
        f"partition-packet-{packet.packet_id[:12]}", tasks)


def execute_packet(
    store: PartitionStore,
    collection_id: str,
    template: PartitionTaskTemplate,
    packet: WorkPacket,
    *,
    runtime_root: Path | str,
    attempt_number: int = 1,
    max_attempts: int = 3,
    timeout_s: float = 120.0,
    controller_kwargs: dict[str, Any] | None = None,
) -> tuple[RetryDecision, RunState]:
    """Run one packet for real and record what actually happened.

    Each member's outcome comes from its own Stage-1 task state, so a packet
    where one partition fails records exactly that: the siblings stay
    committed and only the failure is considered for retry.
    """
    graph = compile_packet(template, packet)
    attempt = PacketAttempt.bind(
        packet, attempt_number,
        fence_token=f"{packet.packet_id[:12]}-{attempt_number}")

    by_key: dict[str, MemberOutcome] = {}
    with WorkflowController(Path(runtime_root),
                            **(controller_kwargs or {})) as controller:
        run_id = controller.create_run(graph)
        run_state = controller.run_until_terminal(run_id, timeout_s=timeout_s)
        states = {row["task_key"]: row["state"]
                  for row in controller.store.task_rows(run_id)}
    for member in packet.members:
        state = states.get(member.logical_task_key)
        by_key[member.logical_task_key] = (
            MemberOutcome.COMMITTED if state == "SUCCEEDED"
            else MemberOutcome.FAILED if state in (
                "FAILED", "INVALID_OUTPUT", "CANCELLED", "LOST")
            else MemberOutcome.NOT_ATTEMPTED)

    result = PacketResult.bind(attempt, packet, tuple(by_key.items()))
    decision = store.record_packet_result(
        collection_id, packet, attempt, result,
        retry_safe=template.retry_safe, max_attempts=max_attempts)
    return decision, run_state


__all__ = ["PartitionNotExecutable", "compile_packet", "execute_packet"]
