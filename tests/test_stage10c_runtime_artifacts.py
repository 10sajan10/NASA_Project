"""Stage 10C: authoritative Stage-1 commit emits durable artifact events."""
from __future__ import annotations

import hashlib

import pytest

from artifacts import (
    ArtifactRegistry,
    ArtifactTargetCoordinator,
    ArtifactWorkflowResolver,
    RuntimeArtifactEventBridge,
    TargetStatus,
)
from engine.runtime import (
    BoundExecutionGraph,
    NATIVE_FILE_POINTER_VALIDATOR_KIND,
    NativeFilePointer,
    OutputSpec,
    ResourceRequest,
    RunState,
    ScientificArtifactBinding,
    TaskTemplate,
    WorkflowController,
)
from engine.runtime.operations import operation_component
from resolution import DiscoveryCertificate, DiscoveryUniverseContract
from stage3.fixtures import _descriptor, make_composition_fixture


def _system(tmp_path):
    fixture = make_composition_fixture()
    registry = ArtifactRegistry(tmp_path / "artifacts.sqlite")
    resolver = ArtifactWorkflowResolver(
        fixture.catalog, fixture.deployment_snapshot, registry,
        discovery_certificate=DiscoveryCertificate.for_base_catalog(
            fixture.catalog),
        discovery_universe=DiscoveryUniverseContract.declare(
            fixture.catalog.catalog_id),
    )
    coordinator = ArtifactTargetCoordinator(
        tmp_path / "targets.sqlite", resolver)
    return fixture, registry, coordinator


def _native_graph(native, *, media_type="application/json"):
    payload = native.read_bytes()
    pointer = NativeFilePointer.bind(
        native, media_type,
        content_sha256=hashlib.sha256(payload).hexdigest(),
        size_bytes=len(payload), metadata={"variable": "sum"})
    descriptor = _descriptor("example.scalar.sum")
    binding = ScientificArtifactBinding(
        bound_plan_id="a" * 64,
        invocation_id="b" * 64,
        output_port="result",
        descriptor_id=descriptor.descriptor_id,
        descriptor=descriptor.to_dict(),
    )
    graph = BoundExecutionGraph.bind(
        "stage10c-native-output",
        (TaskTemplate(
            key="native-sum-output",
            component=operation_component("native.file_pointer.v1"),
            parameters={"pointer": pointer.to_dict()},
            outputs=(OutputSpec(
                name="result", media_type="application/json",
                validation={"kind": NATIVE_FILE_POINTER_VALIDATOR_KIND},
                scientific_binding=binding),),
            resources=ResourceRequest(
                cpu_cores=1, memory_mb=64, walltime_s=20),
        ),),
    )
    return graph, pointer


def test_runtime_commit_automatically_registers_and_reresolves_target(tmp_path):
    fixture, registry, coordinator = _system(tmp_path)
    target = coordinator.submit_target(fixture.root_uses)
    assert target.status is TargetStatus.PLANNED_WORKFLOW

    native = tmp_path / "native-sum.json"
    native.write_text("42", encoding="utf-8")
    graph, pointer = _native_graph(native)
    bridge = RuntimeArtifactEventBridge(coordinator)
    with WorkflowController(
            tmp_path / "runtime",
            artifact_commit_observer=bridge) as controller:
        run_id = controller.create_run(graph)
        assert controller.run_until_terminal(run_id) is RunState.SUCCEEDED
        assert NativeFilePointer.from_dict(controller.output_value(
            run_id, graph.tasks[0].task_id)) == pointer

    refreshed = coordinator.target(target.request.target_id)
    assert refreshed.status is TargetStatus.SATISFIED_BY_ARTIFACT
    assert refreshed.workflow_manifest.total_cost_units == 0
    assert len(registry.records()) == 1
    record = registry.records()[0]
    assert record.location == str(native.resolve())
    assert record.producer_id == "b" * 64
    assert record.metadata["runtime_provenance"]["run_id"] == run_id
    assert native.read_text(encoding="utf-8") == "42"


def test_restart_scans_a_terminal_runtime_commit_missed_before_observer(tmp_path):
    fixture, registry, coordinator = _system(tmp_path)
    target = coordinator.submit_target(fixture.root_uses)
    native = tmp_path / "native-sum.json"
    native.write_text("42", encoding="utf-8")
    graph, _pointer = _native_graph(native)
    runtime = tmp_path / "runtime"

    # Crash boundary: Stage-1 commit wins, but no event observer was attached.
    with WorkflowController(runtime) as first:
        run_id = first.create_run(graph)
        assert first.run_until_terminal(run_id) is RunState.SUCCEEDED
    assert registry.records() == ()
    # A terminal tick on restart replays authoritative commits into the outbox.
    bridge = RuntimeArtifactEventBridge(coordinator)
    with WorkflowController(
            runtime, artifact_commit_observer=bridge) as recovered:
        assert recovered.tick(run_id) is RunState.SUCCEEDED
        assert recovered.tick(run_id) is RunState.SUCCEEDED
    assert len(registry.records()) == 1
    state = coordinator.target(target.request.target_id)
    assert state.status is TargetStatus.SATISFIED_BY_ARTIFACT
    revision = state.revision

    with WorkflowController(
            runtime, artifact_commit_observer=bridge) as recovered_again:
        recovered_again.tick(run_id)
    assert coordinator.target(target.request.target_id).revision == revision
    assert len(registry.records()) == 1


def test_runtime_pointer_descriptor_mismatch_fails_before_registration(tmp_path):
    fixture, registry, coordinator = _system(tmp_path)
    target = coordinator.submit_target(fixture.root_uses)
    native = tmp_path / "native-sum.bin"
    native.write_bytes(b"42")
    graph, _pointer = _native_graph(
        native, media_type="application/octet-stream")
    bridge = RuntimeArtifactEventBridge(coordinator)
    with WorkflowController(
            tmp_path / "runtime",
            artifact_commit_observer=bridge) as controller:
        run_id = controller.create_run(graph)
        with pytest.raises(ValueError, match="media type disagrees"):
            controller.run_until_terminal(run_id)
        assert controller.store.run_state(run_id) is RunState.SUCCEEDED
    assert registry.records() == ()
    assert coordinator.target(target.request.target_id).status is \
        TargetStatus.PLANNED_WORKFLOW


def test_worker_refuses_native_file_changed_before_execution(tmp_path):
    fixture, registry, coordinator = _system(tmp_path)
    native = tmp_path / "native-sum.json"
    native.write_text("42", encoding="utf-8")
    graph, _pointer = _native_graph(native)
    native.write_text("tampered", encoding="utf-8")
    with WorkflowController(
            tmp_path / "runtime",
            artifact_commit_observer=RuntimeArtifactEventBridge(
                coordinator)) as controller:
        run_id = controller.create_run(graph)
        assert controller.run_until_terminal(run_id) is RunState.FAILED
    assert registry.records() == ()
