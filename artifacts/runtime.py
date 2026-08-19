"""Stage-1 authoritative-commit to Stage-10B output-event bridge."""
from __future__ import annotations

from typing import TYPE_CHECKING

from contracts import ArtifactDescriptor
from engine.runtime import NATIVE_FILE_POINTER_VALIDATOR_KIND, NativeFilePointer

from .coordinator import ArtifactTargetCoordinator
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

    def __init__(self, coordinator: ArtifactTargetCoordinator):
        if not isinstance(coordinator, ArtifactTargetCoordinator):
            raise TypeError("runtime artifact bridge requires a coordinator")
        self.coordinator = coordinator

    def __call__(self, controller: "WorkflowController", run_id: str) -> None:
        graph = controller.store.load_graph_for_run(run_id)
        for task in graph.tasks:
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
            producer_ids = {
                recipe.scientific_binding.invocation_id
                for recipe in native_recipes
                if recipe.scientific_binding is not None
            }
            if (len(producer_ids) != 1
                    or any(recipe.scientific_binding is None
                           for recipe in native_recipes)):
                raise RuntimeError(
                    "native output recipes need one exact scientific producer")
            producer_id = next(iter(producer_ids))
            inputs = tuple(ArtifactInput(port, artifact_id) for port, artifact_id
                           in controller.store.task_input_artifact_ids(
                               run_id, task.task_id))
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
                metadata["runtime_provenance"] = {
                    "run_id": run_id,
                    "plan_id": graph.plan_id,
                    "task_id": task.task_id,
                    "recipe_id": recipe.recipe_id,
                    "stage1_artifact_id": row["artifact_id"],
                    "pointer_id": pointer.pointer_id,
                }
                record = self.coordinator.resolver.artifact_registry.prepare_file(
                    pointer.path,
                    descriptor,
                    media_type=pointer.media_type,
                    producer_id=producer_id,
                    producer_version=task.component.version,
                    output_port_id=recipe.output_name,
                    inputs=inputs,
                    metadata=metadata,
                    expected_content_sha256=pointer.content_sha256,
                )
                if record.size_bytes != pointer.size_bytes:
                    raise ValueError("native pointer size changed before event")
                records.append(record)
            event = self.coordinator.enqueue_records(producer_id, records)
            self.coordinator.process_event(event.event_id)


__all__ = ["RuntimeArtifactEventBridge"]
