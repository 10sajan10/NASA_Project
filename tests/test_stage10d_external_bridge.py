"""Stage 10D: exact registry artifacts cross the execution boundary."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from artifacts import (
    ArtifactAvailability,
    ArtifactRecord,
    ArtifactRegistry,
    ArtifactRegistrySnapshot,
    ArtifactSnapshotEntry,
    ArtifactWorkflowResolver,
)
from capabilities import BoundInvocation
from composition import CompilationStatus, compile_bound_plan
from engine.runtime import (
    InputArtifactSource,
    NativeFilePointer,
    RegisteredArtifactDelivery,
    RegisteredArtifactInputBinding,
    ResourceRequest,
    RunState,
    TaskTemplate,
    TaskInputLineage,
    BoundExecutionGraph,
    AttemptSpec,
    ExternalArtifactInputBinding,
    OutputSpec,
    WorkflowController,
)
from engine.runtime.operations import operation_component
from engine.runtime.types import RegisteredArtifactInputReceipt
from engine.runtime.types import attempt_id
from engine.runtime.worker import (
    _load_registered_input,
    _verify_invocation,
)
from plans import (
    ArtifactLeafBinding,
    BoundDerivationPlan,
    DeploymentPlan,
    PlanSnapshotRef,
    ProducerKind,
)
from resolution import (
    ResolutionStatus,
    SatisfactionArcSelectionRef,
    SelectionConstraints,
)
from stage10c import build_demo_plan
from stage10c.demo import _bound_invocation_binding, _deployment_binding
from stage3.fixtures import _descriptor


def _record_and_snapshot(tmp_path, value="41"):
    native = tmp_path / "native.json"
    native.write_text(value, encoding="utf-8")
    registry = ArtifactRegistry(tmp_path / "registry.sqlite")
    record = registry.register_file(
        native,
        _descriptor("example.scalar.sum"),
        media_type="application/json",
        producer_id="external.fixture",
        producer_version="1",
        output_port_id="result",
    )
    return native, record, registry.snapshot()


def test_runtime_consumes_registered_json_without_stage1_copy(tmp_path):
    native, record, snapshot = _record_and_snapshot(tmp_path)
    graph = BoundExecutionGraph.bind("registered-json", (TaskTemplate(
        key="scale",
        component=operation_component("synthetic.scale.v1"),
        parameters={"factor": 2},
        external_inputs=(RegisteredArtifactInputBinding(
            "value", snapshot.snapshot_id, record.to_dict(),
            RegisteredArtifactDelivery.JSON_VALUE),),
        outputs=(OutputSpec(),),
        resources=ResourceRequest(memory_mb=64),
    ),))
    task = graph.tasks[0]

    with WorkflowController(tmp_path / "runtime") as controller:
        run_id = controller.create_run(graph)
        assert controller.run_until_terminal(run_id) is RunState.SUCCEEDED
        assert controller.output_value(run_id, task.task_id) == 82
        assert controller.store.task_input_lineage(run_id, task.task_id) == (
            # The ID is already in the Stage-10 namespace; it must never be
            # looked up as a Stage-1 commit ID.
            TaskInputLineage(
                "value", InputArtifactSource.REGISTERED_ARTIFACT,
                record.artifact_id),
        )
    assert native.read_text(encoding="utf-8") == "41"


def test_runtime_rehashes_registered_bytes_at_run_and_attempt(tmp_path):
    native, record, snapshot = _record_and_snapshot(tmp_path)
    binding = RegisteredArtifactInputBinding(
        "value", snapshot.snapshot_id, record.to_dict(),
        RegisteredArtifactDelivery.JSON_VALUE)
    graph = BoundExecutionGraph.bind("registered-replay", (TaskTemplate(
        key="identity",
        component=operation_component("synthetic.identity.v1"),
        external_inputs=(binding,),
        outputs=(OutputSpec(),),
    ),))

    native.write_text("changed-before-run", encoding="utf-8")
    with WorkflowController(tmp_path / "runtime-a") as controller:
        with pytest.raises(ValueError, match="does not verify"):
            controller.create_run(graph)

    native.write_text("41", encoding="utf-8")
    with WorkflowController(tmp_path / "runtime-b") as controller:
        run_id = controller.create_run(graph)
        native.write_text("changed-before-attempt", encoding="utf-8")
        with pytest.raises(ValueError, match="does not verify"):
            controller.tick(run_id)

    native.write_text("41", encoding="utf-8")
    moved = tmp_path / "relocated-without-new-record.json"
    native.rename(moved)
    with WorkflowController(tmp_path / "runtime-c") as controller:
        with pytest.raises(FileNotFoundError):
            controller.create_run(graph)


def test_runtime_refuses_non_json_registered_media(tmp_path):
    native = tmp_path / "native.bin"
    native.write_bytes(b"41")
    registry = ArtifactRegistry(tmp_path / "registry.sqlite")
    descriptor = replace(
        _descriptor("example.scalar.sum"),
        representation="application/octet-stream")
    record = registry.register_file(
        native,
        descriptor,
        media_type="application/octet-stream",
        producer_id="external.fixture",
        producer_version="1",
        output_port_id="result",
    )
    snapshot = registry.snapshot()
    graph = BoundExecutionGraph.bind("unsupported-native-media", (
        TaskTemplate(
            key="identity",
            component=operation_component("synthetic.identity.v1"),
            external_inputs=(RegisteredArtifactInputBinding(
                "value", snapshot.snapshot_id, record.to_dict(),
                RegisteredArtifactDelivery.JSON_VALUE),),
            outputs=(OutputSpec(),),
        ),
    ))
    with WorkflowController(tmp_path / "runtime") as controller:
        with pytest.raises(ValueError, match="application/json only"):
            controller.create_run(graph)

    pointer_graph = BoundExecutionGraph.bind("native-media-pointer", (
        TaskTemplate(
            key="pointer-identity",
            component=operation_component("native.file_pointer_identity.v1"),
            external_inputs=(RegisteredArtifactInputBinding(
                "source", snapshot.snapshot_id, record.to_dict(),
                RegisteredArtifactDelivery.NATIVE_FILE_POINTER),),
            outputs=(OutputSpec(),),
        ),
    ))
    with WorkflowController(tmp_path / "pointer-runtime") as controller:
        run_id = controller.create_run(pointer_graph)
        assert controller.run_until_terminal(run_id) is RunState.SUCCEEDED
        pointer = NativeFilePointer.from_dict(controller.output_value(
            run_id, pointer_graph.tasks[0].task_id))
    assert pointer.path == str(native)
    assert pointer.media_type == "application/octet-stream"
    assert pointer.content_sha256 == record.content_sha256


def test_runtime_refuses_symlink_and_pointer_mode_on_ordinary_operation(
        tmp_path):
    native, record, snapshot = _record_and_snapshot(tmp_path)
    moved = tmp_path / "moved.json"
    native.rename(moved)
    native.symlink_to(moved)
    symlink_binding = RegisteredArtifactInputBinding(
        "value", snapshot.snapshot_id, record.to_dict(),
        RegisteredArtifactDelivery.JSON_VALUE)
    symlink_graph = BoundExecutionGraph.bind("bad-symlink", (TaskTemplate(
        key="identity",
        component=operation_component("synthetic.identity.v1"),
        external_inputs=(symlink_binding,),
        outputs=(OutputSpec(),),
    ),))
    binding = RegisteredArtifactInputBinding(
        "value", snapshot.snapshot_id, record.to_dict(),
        RegisteredArtifactDelivery.NATIVE_FILE_POINTER)
    graph = BoundExecutionGraph.bind("bad-pointer-delivery", (TaskTemplate(
        key="identity",
        component=operation_component("synthetic.identity.v1"),
        external_inputs=(binding,),
        outputs=(OutputSpec(),),
    ),))
    with WorkflowController(tmp_path / "runtime") as controller:
        with pytest.raises(ValueError, match="symbolic link"):
            controller.create_run(symlink_graph)

    native.unlink()
    moved.rename(native)
    with WorkflowController(tmp_path / "runtime-2") as controller:
        with pytest.raises(ValueError, match="restricted"):
            controller.create_run(graph)


def test_pointer_delivery_never_materializes_native_payload(
        tmp_path, monkeypatch):
    _native, record, snapshot = _record_and_snapshot(tmp_path)
    receipt = RegisteredArtifactInputReceipt(
        snapshot.snapshot_id,
        record.to_dict(),
        RegisteredArtifactDelivery.NATIVE_FILE_POINTER,
    )

    def forbidden_read(*_args, **_kwargs):
        raise AssertionError("pointer delivery loaded the native payload")

    monkeypatch.setattr(
        "engine.runtime.worker.read_verified_native_bytes", forbidden_read)
    pointer = NativeFilePointer.from_dict(
        _load_registered_input("source", receipt))
    assert pointer.path == record.location
    assert pointer.content_sha256 == record.content_sha256


def test_worker_refuses_registered_receipt_on_stage1_commit_port(tmp_path):
    _native, record, snapshot = _record_and_snapshot(tmp_path)
    graph = BoundExecutionGraph.bind("receipt-type-confusion", (TaskTemplate(
        key="identity",
        component=operation_component("synthetic.identity.v1"),
        external_inputs=(ExternalArtifactInputBinding("value", "a" * 64),),
        outputs=(OutputSpec(),),
    ),))
    task = graph.tasks[0]
    deployment_id = "b" * 64
    aid = attempt_id("run", task.task_id, deployment_id, 1, 1)
    spec = AttemptSpec(
        run_id="run",
        deployment_id=deployment_id,
        task=task,
        attempt_id=aid,
        attempt_number=1,
        fencing_token=1,
        provider="stage1-local-subprocess",
        input_artifacts={"value": RegisteredArtifactInputReceipt(
            snapshot.snapshot_id,
            record.to_dict(),
            RegisteredArtifactDelivery.JSON_VALUE,
        )},
        stage_dir=str((tmp_path / "stage").resolve()),
        created_at=1.0,
    )
    with pytest.raises(RuntimeError, match="registered-artifact receipt"):
        _verify_invocation(
            spec,
            spec.attempt_token,
            Path(spec.stage_dir),
            "stage1-local-subprocess",
        )


def test_compiler_lowers_exact_selected_record_to_pointer_delivery(tmp_path):
    native = tmp_path / "native-sum.json"
    native.write_text("42", encoding="utf-8")
    demo = build_demo_plan(native)
    descriptor = _descriptor("example.scalar.sum")
    registry = ArtifactRegistry(tmp_path / "artifacts.sqlite")
    record = registry.register_file(
        native,
        descriptor,
        media_type="application/json",
        producer_id="external.fixture",
        producer_version="1",
        output_port_id="result",
    )
    identity_spec = next(
        value for value in demo.catalog.capabilities
        if value.implementation.operation_key
        == "native.file_pointer_identity.v1")
    identity_invocation = BoundInvocation.bind(
        identity_spec, identity_spec.parameterizations[0])
    constraints = SelectionConstraints.bind(required_satisfactions=(
        SatisfactionArcSelectionRef(
            demo.root_use.requirement_use_id,
            ProducerKind.INVOCATION,
            identity_invocation.invocation_key,
            "result",
        ),
    ))
    resolved = ArtifactWorkflowResolver(
        demo.catalog,
        demo.deployment_snapshot,
        registry,
        discovery_certificate=demo.discovery_certificate,
        discovery_universe=demo.discovery_universe,
    ).resolve((demo.root_use,), constraints=constraints)
    assert resolved.resolution.status is ResolutionStatus.READY
    plan = resolved.resolution.selection.plan
    assert plan is not None
    assert plan.selected_artifact_leaf_ids == (record.leaf.leaf_id,)
    selected_ids = set(plan.selected_invocation_ids)
    selected = tuple(
        node.invocation for node in resolved.resolution.hypergraph.invocation_nodes
        if node.invocation_id in selected_ids)
    assert len(selected) == 1
    bound = BoundDerivationPlan.bind(
        plan,
        invocation_bindings=tuple(
            _bound_invocation_binding(value) for value in selected),
        artifact_bindings=(ArtifactLeafBinding(
            record.leaf.leaf_id,
            record.descriptor.descriptor_id,
            record.manifest_root_sha256,
            record.content_sha256,
        ),),
        scientific_snapshot_refs=(
            PlanSnapshotRef("capability_catalog", demo.catalog.catalog_id),
            PlanSnapshotRef(
                "feasible_hypergraph",
                resolved.resolution.hypergraph.graph_id),
        ),
    )
    profiles = {value.profile_id: value
                for value in demo.catalog.execution_profiles}
    sites = dict(resolved.resolution.selection.deployment_choices)
    deployment = DeploymentPlan.bind(
        bound,
        deployment_snapshot_ref=PlanSnapshotRef(
            "deployment", demo.deployment_snapshot.snapshot_id),
        invocation_bindings=tuple(
            _deployment_binding(
                value, sites[value.invocation_key],
                profiles[value.execution_profile_id])
            for value in selected),
    )
    compilation = compile_bound_plan(
        bound,
        selected,
        deployment_plan=deployment,
        deployment_snapshot=demo.deployment_snapshot,
        execution_profiles=demo.catalog.execution_profiles,
        root_uses=(demo.root_use,),
        artifact_leaves=(record.leaf,),
        artifact_snapshot=resolved.artifact_snapshot,
        artifact_records=resolved.selected_artifacts,
    )
    assert compilation.record.status is CompilationStatus.COMPILED
    assert compilation.graph is not None
    task = compilation.graph.tasks[0]
    assert len(task.external_inputs) == 1
    assert task.external_inputs[0].delivery is (
        RegisteredArtifactDelivery.NATIVE_FILE_POINTER)

    with WorkflowController(tmp_path / "runtime") as controller:
        run_id = controller.create_run(compilation.graph)
        assert controller.run_until_terminal(run_id) is RunState.SUCCEEDED
        pointer = NativeFilePointer.from_dict(
            controller.output_value(run_id, task.task_id))
    assert pointer.path == str(native)
    assert pointer.content_sha256 == record.content_sha256

    conflicting_record = ArtifactRecord.bind(
        descriptor=record.descriptor,
        location=record.location,
        media_type=record.media_type,
        content_sha256=record.content_sha256,
        size_bytes=record.size_bytes,
        producer_id=record.producer_id,
        producer_version=record.producer_version,
        output_port_id=record.output_port_id,
        inputs=record.inputs,
        evidence_profile_id=record.evidence_profile_id,
        metadata={"conflicting-record": True},
    )
    assert conflicting_record.leaf == record.leaf
    ambiguous_snapshot = ArtifactRegistrySnapshot.freeze((
        ArtifactSnapshotEntry(record, ArtifactAvailability.COMMITTED),
        ArtifactSnapshotEntry(
            conflicting_record, ArtifactAvailability.COMMITTED),
    ))
    with pytest.raises(ValueError, match="exactly COMMITTED"):
        compile_bound_plan(
            bound,
            selected,
            deployment_plan=deployment,
            deployment_snapshot=demo.deployment_snapshot,
            execution_profiles=demo.catalog.execution_profiles,
            root_uses=(demo.root_use,),
            artifact_leaves=(record.leaf,),
            artifact_snapshot=ambiguous_snapshot,
            artifact_records=(record,),
        )

    bad_binding = replace(
        bound.artifact_bindings[0], content_digest="0" * 64)
    bad_bound = BoundDerivationPlan.bind(
        plan,
        invocation_bindings=bound.invocation_bindings,
        artifact_bindings=(bad_binding,),
        scientific_snapshot_refs=bound.scientific_snapshot_refs,
    )
    bad_deployment = DeploymentPlan.bind(
        bad_bound,
        deployment_snapshot_ref=deployment.deployment_snapshot_ref,
        invocation_bindings=deployment.invocation_bindings,
    )
    with pytest.raises(ValueError, match="content digest"):
        compile_bound_plan(
            bad_bound,
            selected,
            deployment_plan=bad_deployment,
            deployment_snapshot=demo.deployment_snapshot,
            execution_profiles=demo.catalog.execution_profiles,
            root_uses=(demo.root_use,),
            artifact_leaves=(record.leaf,),
            artifact_snapshot=resolved.artifact_snapshot,
            artifact_records=resolved.selected_artifacts,
        )
