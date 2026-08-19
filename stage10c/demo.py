"""Runnable Stage-1 commit -> native artifact -> target re-plan proof."""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from artifacts import (
    ArtifactRegistry,
    ArtifactTargetCoordinator,
    ArtifactWorkflowResolver,
    RuntimeArtifactEventBridge,
)
from engine.runtime import (
    BoundExecutionGraph,
    NATIVE_FILE_POINTER_VALIDATOR_KIND,
    NativeFilePointer,
    OutputSpec,
    ResourceRequest,
    ScientificArtifactBinding,
    TaskTemplate,
    WorkflowController,
)
from engine.runtime.operations import operation_component
from resolution import DiscoveryCertificate, DiscoveryUniverseContract
from stage3.fixtures import _descriptor, make_composition_fixture


def run_demo(workspace: str | Path) -> dict[str, Any]:
    root = Path(workspace).resolve()
    root.mkdir(parents=True, exist_ok=True)
    fixture = make_composition_fixture()
    registry = ArtifactRegistry(root / "artifacts.sqlite")
    resolver = ArtifactWorkflowResolver(
        fixture.catalog, fixture.deployment_snapshot, registry,
        discovery_certificate=DiscoveryCertificate.for_base_catalog(
            fixture.catalog),
        discovery_universe=DiscoveryUniverseContract.declare(
            fixture.catalog.catalog_id),
    )
    coordinator = ArtifactTargetCoordinator(root / "targets.sqlite", resolver)
    before = coordinator.submit_target(fixture.root_uses)

    native = root / "native-sum.json"
    native.write_text("42", encoding="utf-8")
    content = native.read_bytes()
    pointer = NativeFilePointer.bind(
        native, "application/json",
        content_sha256=hashlib.sha256(content).hexdigest(),
        size_bytes=len(content), metadata={"variable": "sum"})
    descriptor = _descriptor("example.scalar.sum")
    invocation_id = "b" * 64
    graph = BoundExecutionGraph.bind(
        "stage10c-runtime-native-output",
        (TaskTemplate(
            key="native-sum-output",
            component=operation_component("native.file_pointer.v1"),
            parameters={"pointer": pointer.to_dict()},
            outputs=(OutputSpec(
                name="result", media_type="application/json",
                validation={"kind": NATIVE_FILE_POINTER_VALIDATOR_KIND},
                scientific_binding=ScientificArtifactBinding(
                    bound_plan_id="a" * 64,
                    invocation_id=invocation_id,
                    output_port="result",
                    descriptor_id=descriptor.descriptor_id,
                    descriptor=descriptor.to_dict())),),
            resources=ResourceRequest(
                cpu_cores=1, memory_mb=64, walltime_s=20),
        ),),
    )
    with WorkflowController(
            root / "runtime",
            artifact_commit_observer=RuntimeArtifactEventBridge(
                coordinator)) as controller:
        run_id = controller.create_run(graph)
        run_state = controller.run_until_terminal(run_id)

    after = coordinator.target(before.request.target_id)
    record = registry.records()[0]
    return {
        "schema": "stage10c-demo-result-v1",
        "run_id": run_id,
        "run_state": run_state.value,
        "before_status": before.status.value,
        "before_cost_units": before.workflow_manifest.total_cost_units,
        "after_status": after.status.value,
        "after_cost_units": after.workflow_manifest.total_cost_units,
        "artifact_record_id": record.record_id,
        "native_location": record.location,
        "native_location_unchanged": record.location == str(native),
        "runtime_provenance_bound":
            record.metadata["runtime_provenance"]["run_id"] == run_id,
        "native_payload_copied": False,
        "transformation_selected": False,
        "cube_write": False,
    }


__all__ = ["run_demo"]
