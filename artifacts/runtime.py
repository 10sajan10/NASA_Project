"""Stage-1 authoritative-commit to Stage-10B output-event bridge."""
from __future__ import annotations

from typing import TYPE_CHECKING, Iterable

from composition import CompilationAuthority
from contracts import ArtifactDescriptor
from engine.runtime import (
    InputArtifactSource,
    NATIVE_FILE_POINTER_VALIDATOR_KIND,
    NativeFilePointer,
)

from .coordinator import ArtifactTargetCoordinator, OutputEventStatus
from .records import ArtifactInput

if TYPE_CHECKING:
    from engine.runtime.controller import WorkflowController


class RuntimeArtifactEventBridge:
    """Observe committed Stage-1 native pointers and publish exact artifacts.

    Process exit is not authority. This callable is invoked only after the
    controller's fenced artifact commit (and on every terminal-run replay).
    Repeated scans are safe because both the runtime recipe and output event
    are content-addressed.
    """

    def __init__(
        self,
        coordinator: ArtifactTargetCoordinator,
        *,
        compilation_authorities: Iterable[CompilationAuthority] = (),
    ):
        if not isinstance(coordinator, ArtifactTargetCoordinator):
            raise TypeError("runtime artifact bridge requires a coordinator")
        self.coordinator = coordinator
        authorities = tuple(compilation_authorities)
        if not all(isinstance(value, CompilationAuthority)
                   for value in authorities):
            raise TypeError(
                "runtime artifact bridge requires compilation authorities")
        by_graph: dict[str, CompilationAuthority] = {}
        for authority in authorities:
            previous = by_graph.setdefault(
                authority.stage1_graph_id, authority)
            if previous != authority:
                raise ValueError(
                    "conflicting compilation authorities name one graph")
        # This registry is intentionally caller-supplied on every process
        # start.  Authority absence never degrades to trusting graph schema or
        # caller-authored ScientificArtifactBinding labels.
        self._authorities = by_graph

    def __call__(self, controller: "WorkflowController", run_id: str) -> None:
        graph = controller.store.load_graph_for_run(run_id)
        has_native_outputs = any(
            recipe.validation == {
                "kind": NATIVE_FILE_POINTER_VALIDATOR_KIND}
            for task in graph.tasks for recipe in task.outputs)
        if not has_native_outputs:
            return
        try:
            authority = self._authorities[graph.plan_id]
        except KeyError as exc:
            raise PermissionError(
                "native artifact publication requires the exact compiler-"
                "issued authority for this graph") from exc
        authority.verify(authority.record, graph)

        registry = self.coordinator.resolver.artifact_registry
        for task in _topological_tasks(graph.tasks):
            native_recipes = tuple(recipe for recipe in task.outputs if (
                recipe.validation == {
                    "kind": NATIVE_FILE_POINTER_VALIDATOR_KIND}))
            if not native_recipes:
                continue
            committed = tuple(
                (recipe, controller.store.committed_output(
                    run_id, task.task_id, recipe.output_name))
                for recipe in native_recipes)
            if not any(row is not None for _recipe, row in committed):
                continue
            if any(row is None for _recipe, row in committed):
                raise RuntimeError(
                    "native co-produced output set is only partially committed")
            producer_coordinates = {
                (
                    recipe.scientific_binding.capability_id,
                    recipe.scientific_binding.capability_version,
                    recipe.scientific_binding.invocation_id,
                    recipe.scientific_binding.evidence_profile_id,
                )
                for recipe in native_recipes
                if recipe.scientific_binding is not None
            }
            if (len(producer_coordinates) != 1
                    or any(recipe.scientific_binding is None
                           for recipe in native_recipes)):
                raise RuntimeError(
                    "native output recipes need one exact capability, version, "
                    "invocation, and evidence producer coordinate")
            (capability_id, capability_version, invocation_id,
             evidence_profile_id) = next(iter(producer_coordinates))
            runtime_inputs = controller.store.task_input_lineage(
                run_id, task.task_id)
            if (len({value.input_name for value in runtime_inputs})
                    != len(runtime_inputs)):
                raise RuntimeError(
                    "runtime task inputs are ambiguous by port")
            inputs = []
            for value in runtime_inputs:
                if value.source is InputArtifactSource.REGISTERED_ARTIFACT:
                    # This ID was verified directly from the frozen Stage-10
                    # registry record at run creation and again per attempt.
                    stage10_artifact_id = value.artifact_id
                else:
                    try:
                        stage10_artifact_id = registry.runtime_artifact_id(
                            run_id, value.artifact_id)
                    except KeyError as exc:
                        raise RuntimeError(
                            "native artifact input has no authoritative "
                            "Stage-10 lineage mapping") from exc
                inputs.append(ArtifactInput(
                    value.input_name, stage10_artifact_id))
            input_values = tuple(inputs)
            records = []
            for recipe, row in committed:
                binding = recipe.scientific_binding
                assert binding is not None and row is not None
                descriptor = ArtifactDescriptor.from_dict(binding.descriptor)
                pointer = NativeFilePointer.from_dict(controller.output_value(
                    run_id, task.task_id, recipe.output_name))
                if pointer.media_type != descriptor.representation:
                    raise ValueError(
                        "native pointer media type disagrees with output descriptor")
                metadata = dict(pointer.metadata)
                if "scientific_provenance" in metadata:
                    raise ValueError(
                        "native pointer metadata uses reserved "
                        "scientific_provenance field")
                # ArtifactRecord.artifact_id currently binds producer_id but
                # not metadata.  Keep producer_id as the exact invocation so
                # distinct parameterized derivations cannot collapse merely
                # because they produced identical bytes.  The stable
                # capability coordinate is retained separately in the
                # identity-bound ArtifactRecord metadata.
                metadata["scientific_provenance"] = {
                    "schema": "stage10d-scientific-provenance-v1",
                    "bound_plan_id": binding.bound_plan_id,
                    "invocation_id": invocation_id,
                    "capability_id": capability_id,
                    "capability_version": capability_version,
                    "evidence_profile_id": evidence_profile_id,
                }
                # Run occurrence belongs in the durable namespace mapping
                # below, not immutable artifact metadata.  Two runs that
                # produce the same scientific derivation must replay the same
                # ArtifactRecord instead of conflicting on a run label.
                metadata["runtime_provenance"] = {
                    "plan_id": graph.plan_id,
                    "task_id": task.task_id,
                    "recipe_id": recipe.recipe_id,
                    "stage1_artifact_id": row["artifact_id"],
                    "pointer_id": pointer.pointer_id,
                }
                record = registry.prepare_file(
                    pointer.path,
                    descriptor,
                    media_type=pointer.media_type,
                    producer_id=invocation_id,
                    producer_version=capability_version,
                    output_port_id=recipe.output_name,
                    inputs=input_values,
                    evidence_profile_id=evidence_profile_id,
                    metadata=metadata,
                    expected_content_sha256=pointer.content_sha256,
                )
                if record.size_bytes != pointer.size_bytes:
                    raise ValueError("native pointer size changed before event")
                records.append(record)
            event = self.coordinator.enqueue_records(invocation_id, records)
            status = self.coordinator.process_event(event.event_id)
            if status is not OutputEventStatus.APPLIED:
                raise RuntimeError(
                    "native artifact output event did not reach APPLIED")
            registry.bind_runtime_outputs(
                run_id,
                task.task_id,
                tuple(
                    (recipe.output_name, row["artifact_id"], record)
                    for (recipe, row), record in zip(committed, records)
                    if row is not None
                ),
            )


def _topological_tasks(tasks):
    """Return bound tasks in dependency order independent of key sorting."""
    by_key = {task.key: task for task in tasks}
    remaining = {
        task.key: {binding.upstream_task for binding in task.inputs}
        for task in tasks
    }
    ordered = []
    while remaining:
        ready = sorted(key for key, dependencies in remaining.items()
                       if not dependencies)
        if not ready:
            raise ValueError("runtime artifact graph contains a cycle")
        for key in ready:
            ordered.append(by_key[key])
            remaining.pop(key)
        for dependencies in remaining.values():
            dependencies.difference_update(ready)
    return tuple(ordered)


__all__ = ["RuntimeArtifactEventBridge"]
