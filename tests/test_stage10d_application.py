"""Stage 10D: one durable target reaches one authorized runtime run."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import replace

import pytest

from artifacts import (
    ArtifactAvailability,
    ArtifactInput,
    ArtifactRegistry,
    ArtifactSnapshotQuery,
    ArtifactSnapshotQueryResult,
    ArtifactTargetCoordinator,
    ArtifactWorkflowResolver,
    SnapshotArtifactCatalog,
    TargetStatus,
)
from capabilities import BoundInvocation
from engine.runtime import RunState, WorkflowController
from engine.runtime.identity import strict_canonical_json, strict_hash
from plans import ProducerKind
from resolution import (
    ProducerSelectionRef,
    SatisfactionArcSelectionRef,
    SelectionConstraints,
)
from stage10c import build_demo_plan
from stage10d import (
    StaleTargetContextError,
    TargetExecutionService,
    TargetExecutionStatus,
)
from stage3.fixtures import _descriptor


def _system(tmp_path):
    native = tmp_path / "native-sum.json"
    native.write_text("42", encoding="utf-8")
    demo = build_demo_plan(native)
    registry = ArtifactRegistry(tmp_path / "artifacts.sqlite")
    source_record = registry.register_file(
        native,
        _descriptor("example.scalar.sum"),
        media_type="application/json",
        producer_id="external.seed",
        producer_version="1",
        output_port_id="result",
        metadata={"role": "selected-native-input"},
    )
    incompatible_path = tmp_path / "incompatible.json"
    incompatible_path.write_text("99", encoding="utf-8")
    incompatible_record = registry.register_file(
        incompatible_path,
        replace(_descriptor("example.scalar.sum"), units="m"),
        media_type="application/json",
        producer_id="external.incompatible",
        producer_version="1",
        output_port_id="result",
        metadata={"role": "must-not-match"},
    )
    resolver = ArtifactWorkflowResolver(
        demo.catalog,
        demo.deployment_snapshot,
        registry,
        discovery_certificate=demo.discovery_certificate,
        discovery_universe=demo.discovery_universe,
    )
    coordinator = ArtifactTargetCoordinator(
        tmp_path / "targets.sqlite", resolver)
    invocations = tuple(sorted((
        BoundInvocation.bind(spec, parameterization)
        for spec in demo.catalog.capabilities
        for parameterization in spec.parameterizations
    ), key=lambda value: value.invocation_key))
    identity = next(
        value for value in invocations
        if value.implementation.operation_key
        == "native.file_pointer_identity.v1")
    main_constraints = SelectionConstraints.bind(
        required_satisfactions=(SatisfactionArcSelectionRef(
            demo.root_use.requirement_use_id,
            ProducerKind.INVOCATION,
            identity.invocation_key,
            "result",
        ),),
    )
    main = coordinator.submit_target(
        (demo.root_use,), constraints=main_constraints)
    assert main.status is TargetStatus.PLANNED_WORKFLOW

    # This target refuses the original artifact and every executable producer.
    # It is unsatisfiable until the identity run registers a new derived leaf.
    waiting_constraints = SelectionConstraints.bind(exclude=(
        ProducerSelectionRef(
            ProducerKind.ARTIFACT_LEAF, source_record.leaf.leaf_id),
        *(ProducerSelectionRef(
            ProducerKind.INVOCATION, value.invocation_key)
          for value in invocations),
    ))
    waiting = coordinator.submit_target(
        (demo.root_use,), constraints=waiting_constraints)
    assert waiting.status is TargetStatus.UNSATISFIABLE
    return {
        "native": native,
        "demo": demo,
        "registry": registry,
        "coordinator": coordinator,
        "source": source_record,
        "incompatible": incompatible_record,
        "identity": identity,
        "main": main,
        "waiting": waiting,
    }


def _attempt_count(runtime_root, run_id):
    with sqlite3.connect(
            str(runtime_root / "control" / "runtime.sqlite3")) as connection:
        return int(connection.execute(
            "SELECT COUNT(*) FROM attempts WHERE run_id=?", (run_id,),
        ).fetchone()[0])


def test_target_automatically_compiles_runs_registers_replans_and_queries(
        tmp_path):
    system = _system(tmp_path)
    runtime = tmp_path / "runtime"
    service_db = tmp_path / "executions.sqlite"
    service = TargetExecutionService(
        service_db, runtime, system["coordinator"])

    completed = service.execute(system["main"].request)
    assert completed.status is TargetExecutionStatus.SUCCEEDED
    assert completed.run_state is RunState.SUCCEEDED
    assert completed.receipt is not None
    assert completed.receipt.run_id == completed.context.execution_id
    assert _attempt_count(runtime, completed.context.run_id) == 1

    records = system["registry"].records()
    produced = tuple(
        value for value in records
        if value.producer_id == system["identity"].invocation_key)
    assert len(produced) == 1
    output = produced[0]
    # ArtifactRecord.artifact_id currently binds producer_id, so it remains
    # the exact invocation coordinate to prevent distinct parameterized
    # derivations with equal bytes from collapsing. Stable capability identity
    # is separately queryable from identity-bound scientific_provenance.
    assert output.producer_id == system["identity"].invocation_key
    assert output.producer_version == system["identity"].capability_version
    assert output.evidence_profile_id == (
        system["identity"].evidence_profile_id)
    assert output.metadata["scientific_provenance"] == {
        "schema": "stage10d-scientific-provenance-v1",
        "bound_plan_id": completed.context.bound_plan.bound_plan_id,
        "invocation_id": system["identity"].invocation_key,
        "capability_id": system["identity"].capability_id,
        "capability_version": system["identity"].capability_version,
        "evidence_profile_id": system["identity"].evidence_profile_id,
    }
    assert output.location == str(system["native"].resolve())
    assert output.content_sha256 == system["source"].content_sha256
    assert output.inputs == (ArtifactInput(
        "source", system["source"].artifact_id),)
    assert system["native"].read_text(encoding="utf-8") == "42"
    assert system["coordinator"].target(
        system["waiting"].request.target_id).status \
        is TargetStatus.SATISFIED_BY_ARTIFACT

    # Reconstruct every artifact/target service from only the durable paths,
    # then recompute the waiting target. This is a real coordinator/registry
    # restart, not a replay through the original Python objects.
    reopened_registry = ArtifactRegistry(system["registry"].path)
    reopened_resolver = ArtifactWorkflowResolver(
        system["demo"].catalog,
        system["demo"].deployment_snapshot,
        reopened_registry,
        discovery_certificate=system["demo"].discovery_certificate,
        discovery_universe=system["demo"].discovery_universe,
    )
    reopened_coordinator = ArtifactTargetCoordinator(
        system["coordinator"].path, reopened_resolver)
    waiting_after_restart = reopened_coordinator.resolve_target(
        system["waiting"].request.target_id)
    assert waiting_after_restart.status is TargetStatus.SATISFIED_BY_ARTIFACT
    assert waiting_after_restart.workflow_manifest is not None
    assert waiting_after_restart.workflow_manifest.selected_artifacts == (
        output,)

    # The read surface can constrain every non-null scientific, provenance,
    # location, availability, and direct-lineage field in this scalar record.
    snapshot = reopened_registry.snapshot()
    descriptor = output.descriptor
    query = ArtifactSnapshotQuery(
        record_id=output.record_id,
        artifact_id=output.artifact_id,
        content_sha256=output.content_sha256,
        descriptor_id=descriptor.descriptor_id,
        concept_id=descriptor.concept_id,
        representation=descriptor.representation,
        schema_version=descriptor.schema_version,
        units=descriptor.units,
        spatial_support=descriptor.spatial_support,
        spatial_crs=descriptor.spatial_support.crs,
        intersects_bounds=("-0.5", "-0.5", "0.5", "0.5"),
        temporal_kind=descriptor.temporal_support.kind,
        temporal_support=descriptor.temporal_support,
        origin=descriptor.origin,
        missingness=descriptor.missingness,
        missingness_status=descriptor.missingness.status,
        intrinsic_uncertainty=descriptor.intrinsic_uncertainty,
        uncertainty_status=descriptor.intrinsic_uncertainty.status,
        required_components=descriptor.component_names,
        evidence_profile_id=output.evidence_profile_id,
        invocation_id=system["identity"].invocation_key,
        capability_id=system["identity"].capability_id,
        producer_id=output.producer_id,
        producer_version=output.producer_version,
        output_port_id=output.output_port_id,
        media_type=output.media_type,
        location=output.location,
        record_metadata=output.metadata,
        lineage_inputs=output.inputs,
        has_lineage=True,
        availability=ArtifactAvailability.COMMITTED,
    )
    catalog = SnapshotArtifactCatalog(snapshot)
    result = catalog.search(query)
    assert result.records == (output,)
    assert ArtifactSnapshotQuery.from_dict(query.to_dict()) == query
    assert ArtifactSnapshotQueryResult.from_dict(
        result.to_dict(), query=query, snapshot=snapshot) == result
    assert catalog.search(ArtifactSnapshotQuery(
        lineage_artifact_ids=(system["source"].artifact_id,),
    )).records == (output,)
    assert catalog.search(ArtifactSnapshotQuery(
        lineage_port_ids=("source",),
    )).records == (output,)
    assert catalog.search(ArtifactSnapshotQuery(
        invocation_id=system["identity"].invocation_key,
    )).records == (output,)
    assert catalog.search(ArtifactSnapshotQuery(
        capability_id=system["identity"].capability_id,
    )).records == (output,)
    assert catalog.search(ArtifactSnapshotQuery(
        capability_id="capability:does-not-match",
    )).records == ()

    # Reopening and executing the same durable request returns the same
    # receipt. It neither creates another run nor another worker attempt.
    reopened = TargetExecutionService(
        service_db, runtime, reopened_coordinator)
    replayed = reopened.execute(system["main"].request.target_id)
    assert replayed == completed
    assert _attempt_count(runtime, completed.context.run_id) == 1


def test_restart_reconstructs_authority_for_terminal_run_without_relaunch(
        tmp_path):
    system = _system(tmp_path)
    runtime = tmp_path / "runtime"
    service_db = tmp_path / "executions.sqlite"
    first = TargetExecutionService(
        service_db, runtime, system["coordinator"])
    target_state, outcome = first._current_ready_outcome(
        system["main"].request)
    context = first._build_context(
        system["main"].request, target_state.workflow_manifest, outcome)
    first._insert_or_load_context(context)
    compilation = first._recompile(context)
    assert compilation.graph is not None

    # Simulate a process dying after the authoritative runtime commit but
    # before the application observer and receipt update.
    with WorkflowController(runtime) as controller:
        controller.create_run(compilation.graph, run_id=context.run_id)
        assert controller.run_until_terminal(context.run_id) \
            is RunState.SUCCEEDED
    assert first.state(system["main"].request.target_id).status \
        is TargetExecutionStatus.PREPARED
    assert len(system["registry"].records()) == 2

    recovered = TargetExecutionService(
        service_db, runtime, system["coordinator"])
    state = recovered.execute(system["main"].request.target_id)
    assert state.status is TargetExecutionStatus.SUCCEEDED
    assert len(system["registry"].records()) == 3
    assert _attempt_count(runtime, context.run_id) == 1


def test_terminal_receipt_survives_input_archival_but_not_receipt_relabelling(
        tmp_path):
    system = _system(tmp_path)
    service_db = tmp_path / "executions.sqlite"
    runtime = tmp_path / "runtime"
    service = TargetExecutionService(
        service_db, runtime, system["coordinator"])
    completed = service.execute(system["main"].request.target_id)
    assert completed.status is TargetExecutionStatus.SUCCEEDED

    archived = tmp_path / "archived-native-sum.json"
    system["native"].rename(archived)
    assert TargetExecutionService(
        service_db, runtime, system["coordinator"],
    ).execute(system["main"].request.target_id) == completed

    # Recompute a self-consistent receipt identity after changing its target.
    # Row/context replay, rather than the receipt's own hash alone, refuses it.
    with sqlite3.connect(str(service_db)) as connection:
        raw = json.loads(connection.execute(
            "SELECT receipt_json FROM target_executions WHERE target_id=?",
            (system["main"].request.target_id,),
        ).fetchone()[0])
        raw["target_id"] = "f" * 64
        raw["receipt_id"] = strict_hash({
            key: value for key, value in raw.items() if key != "receipt_id"
        })
        connection.execute(
            "UPDATE target_executions SET receipt_json=? WHERE target_id=?",
            (strict_canonical_json(raw), system["main"].request.target_id),
        )
    with pytest.raises(ValueError, match="exact context"):
        service.state(system["main"].request.target_id)


def test_stale_manifest_and_portable_manifest_are_not_launch_authority(tmp_path):
    system = _system(tmp_path)
    service = TargetExecutionService(
        tmp_path / "executions.sqlite", tmp_path / "runtime",
        system["coordinator"])
    with pytest.raises(TypeError, match="not executable authority"):
        service.execute(system["main"].workflow_manifest)

    # Direct registry publication changes the exact planning snapshot without
    # rewriting the coordinator's durable manifest.
    extra = tmp_path / "new-incompatible.json"
    extra.write_text("100", encoding="utf-8")
    system["registry"].register_file(
        extra,
        replace(_descriptor("example.scalar.sum"), units="s"),
        media_type="application/json",
        producer_id="external.new-incompatible",
        producer_version="1",
        output_port_id="result",
    )
    with pytest.raises(StaleTargetContextError, match="stale"):
        service.execute(system["main"].request.target_id)
    assert service.states() == ()


def test_service_authorities_and_persisted_context_are_fail_closed(tmp_path):
    system = _system(tmp_path)
    service_db = tmp_path / "executions.sqlite"
    runtime = tmp_path / "runtime"
    service = TargetExecutionService(
        service_db, runtime, system["coordinator"])
    target_state, outcome = service._current_ready_outcome(
        system["main"].request)
    context = service._build_context(
        system["main"].request, target_state.workflow_manifest, outcome)
    service._insert_or_load_context(context)

    with pytest.raises(ValueError, match="another runtime_root"):
        TargetExecutionService(
            service_db, tmp_path / "other-runtime", system["coordinator"])

    other_registry = ArtifactRegistry(tmp_path / "other-artifacts.sqlite")
    other_resolver = ArtifactWorkflowResolver(
        system["demo"].catalog,
        system["demo"].deployment_snapshot,
        other_registry,
        discovery_certificate=system["demo"].discovery_certificate,
        discovery_universe=system["demo"].discovery_universe,
    )
    other_coordinator = ArtifactTargetCoordinator(
        tmp_path / "other-targets.sqlite", other_resolver)
    with pytest.raises(ValueError, match="another coordinator_path"):
        TargetExecutionService(service_db, runtime, other_coordinator)

    with sqlite3.connect(str(service_db)) as connection:
        raw = json.loads(connection.execute(
            "SELECT context_json FROM target_executions WHERE target_id=?",
            (system["main"].request.target_id,),
        ).fetchone()[0])
        raw["resolution_id"] = "0" * 64
        connection.execute(
            "UPDATE target_executions SET context_json=? WHERE target_id=?",
            (strict_canonical_json(raw), system["main"].request.target_id),
        )
    with pytest.raises(ValueError, match="disagree|identity"):
        service.state(system["main"].request.target_id)


def test_cancelled_runtime_is_persisted_as_a_terminal_receipt(tmp_path):
    system = _system(tmp_path)
    runtime = tmp_path / "runtime"
    service = TargetExecutionService(
        tmp_path / "executions.sqlite", runtime, system["coordinator"])
    target_state, outcome = service._current_ready_outcome(
        system["main"].request)
    context = service._build_context(
        system["main"].request, target_state.workflow_manifest, outcome)
    service._insert_or_load_context(context)
    compilation = service._recompile(context)
    assert compilation.graph is not None
    with WorkflowController(runtime) as controller:
        controller.create_run(compilation.graph, run_id=context.run_id)
        assert controller.cancel_task(
            context.run_id, compilation.graph.tasks[0].task_id)
        assert controller.store.run_state(context.run_id) \
            is RunState.CANCELLED
    state = service.execute(system["main"].request.target_id)
    assert state.status is TargetExecutionStatus.CANCELLED
    assert state.run_state is RunState.CANCELLED
    assert state.receipt is not None
