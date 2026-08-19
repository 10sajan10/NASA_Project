"""Stage 10B durable target and artifact-event acceptance tests."""
from __future__ import annotations

import json

import pytest

from artifacts import (
    ArtifactQuery,
    ArtifactRegistry,
    ArtifactTargetCoordinator,
    ArtifactWorkflowResolver,
    OutputEventStatus,
    TargetRequest,
    TargetStatus,
    VerificationPolicy,
    WorkflowManifest,
)
from cube.entries import DatasetRef
from resolution import DiscoveryCertificate, DiscoveryUniverseContract
from stage3.fixtures import _descriptor, make_composition_fixture


def _system(tmp_path):
    fixture = make_composition_fixture()
    registry = ArtifactRegistry(tmp_path / "artifacts.sqlite")
    resolver = ArtifactWorkflowResolver(
        fixture.catalog,
        fixture.deployment_snapshot,
        registry,
        discovery_certificate=DiscoveryCertificate.for_base_catalog(
            fixture.catalog),
        discovery_universe=DiscoveryUniverseContract.declare(
            fixture.catalog.catalog_id),
    )
    coordinator = ArtifactTargetCoordinator(
        tmp_path / "targets.sqlite", resolver)
    return fixture, registry, resolver, coordinator


def _sum_output(path):
    return DatasetRef(
        str(path), "application/json",
        descriptor=_descriptor("example.scalar.sum"),
        producer_version="1.0.0", output_port_id="result",
        detail={"variable": "sum"},
    )


def test_target_request_and_portable_manifest_are_identity_checked(tmp_path):
    fixture, _registry, _resolver, coordinator = _system(tmp_path)
    request = TargetRequest.bind(fixture.root_uses)
    assert TargetRequest.from_dict(request.to_dict()) == request

    state = coordinator.submit_target(fixture.root_uses)
    assert state.status is TargetStatus.PLANNED_WORKFLOW
    assert state.workflow_manifest is not None
    manifest = state.workflow_manifest
    assert WorkflowManifest.from_dict(manifest.to_dict()) == manifest
    exported = manifest.write_json(tmp_path / "workflow-manifest.json")
    assert WorkflowManifest.read_json(exported) == manifest
    assert manifest.root_uses == tuple(sorted(
        fixture.root_uses, key=lambda value: value.requirement_use_id))
    assert manifest.total_cost_units == 3
    assert len(manifest.selected_invocations) == 2

    altered = manifest.to_dict()
    altered["total_cost_units"] = 999
    with pytest.raises(ValueError, match="identity"):
        WorkflowManifest.from_dict(altered)


def test_output_event_automatically_reresolves_a_durable_target(tmp_path):
    fixture, registry, _resolver, coordinator = _system(tmp_path)
    before = coordinator.submit_target(fixture.root_uses)
    assert before.status is TargetStatus.PLANNED_WORKFLOW
    assert before.workflow_manifest.total_cost_units == 3

    native = tmp_path / "sum.json"
    native.write_text("42", encoding="utf-8")
    event = coordinator.output_arrived(
        producer_id="sum-file-producer",
        outputs={"result": _sum_output(native)},
    )

    after = coordinator.target(before.request.target_id)
    assert coordinator.event_status(event.event_id) is OutputEventStatus.APPLIED
    assert after.status is TargetStatus.SATISFIED_BY_ARTIFACT
    assert after.revision == before.revision + 1
    assert after.workflow_manifest.total_cost_units == 0
    assert after.workflow_manifest.selected_invocations == ()
    assert after.workflow_manifest.selected_artifacts == registry.records()
    assert after.workflow_manifest.selected_artifacts[0].location == \
        str(native.resolve())


def test_crash_after_registry_commit_replays_event_and_target_once(tmp_path):
    fixture, registry, resolver, coordinator = _system(tmp_path)
    initial = coordinator.submit_target(fixture.root_uses)
    native = tmp_path / "sum.json"
    native.write_text("42", encoding="utf-8")
    event = coordinator.enqueue_output_event(
        producer_id="sum-file-producer",
        outputs={"result": _sum_output(native)},
    )

    # Crash window: the authoritative registry transaction commits, but the
    # coordination DB still says PENDING.
    registry.register_records(event.records)
    assert coordinator.event_status(event.event_id) is OutputEventStatus.PENDING

    restarted = ArtifactTargetCoordinator(
        tmp_path / "targets.sqlite", resolver)
    assert restarted.recover() == (event.event_id,)
    recovered = restarted.target(initial.request.target_id)
    assert recovered.status is TargetStatus.SATISFIED_BY_ARTIFACT
    assert recovered.workflow_manifest.total_cost_units == 0
    assert len(registry.records()) == 1
    assert restarted.recover() == ()
    assert restarted.process_event(event.event_id) is OutputEventStatus.APPLIED
    assert len(registry.records()) == 1
    assert restarted.resolve_target(initial.request.target_id).revision == \
        recovered.revision


def test_registered_event_replays_target_refresh_after_restart(tmp_path):
    fixture, registry, resolver, coordinator = _system(tmp_path)
    initial = coordinator.submit_target(fixture.root_uses)
    native = tmp_path / "sum.json"
    native.write_text("42", encoding="utf-8")
    event = coordinator.enqueue_output_event(
        producer_id="sum-file-producer",
        outputs={"result": _sum_output(native)},
    )
    records = registry.register_records(event.records)

    # Second crash window: output is registered and the event records that
    # fact, but target refresh has not happened.
    import sqlite3
    with sqlite3.connect(str(tmp_path / "targets.sqlite")) as connection:
        connection.execute(
            "UPDATE artifact_output_events SET status=?,"
            "registered_record_ids_json=? WHERE event_id=?",
            (OutputEventStatus.REGISTERED.value,
             json.dumps([value.record_id for value in records]),
             event.event_id),
        )
    assert coordinator.target(initial.request.target_id).status is \
        TargetStatus.PLANNED_WORKFLOW

    restarted = ArtifactTargetCoordinator(
        tmp_path / "targets.sqlite", resolver)
    restarted.recover()
    assert restarted.target(initial.request.target_id).status is \
        TargetStatus.SATISFIED_BY_ARTIFACT


def test_indexed_metadata_query_does_not_require_payload_open(tmp_path):
    registry = ArtifactRegistry(tmp_path / "artifacts.sqlite")
    native = tmp_path / "sum.json"
    native.write_text("42", encoding="utf-8")
    record = registry.register_file(
        native, _descriptor("example.scalar.sum"),
        media_type="application/json", producer_id="sum-source",
        producer_version="2", output_port_id="result")

    native.unlink()
    assert registry.query_records(ArtifactQuery(
        concept_id="example.scalar.sum", schema_version="example-scalar-v1",
        representation="application/json", units="1",
        producer_id="sum-source", producer_version="2",
        output_port_id="result", spatial_crs="EPSG:4326",
        intersects_bounds=("0", "0", "1", "1"))) == (record,)
    assert registry.query_records(ArtifactQuery(
        concept_id="example.scalar.sum", spatial_crs="EPSG:4326",
        intersects_bounds=("20", "20", "21", "21"))) == ()
    snapshot = registry.snapshot()
    assert snapshot.committed_records == ()


def test_stat_receipt_avoids_rehash_until_fingerprint_changes(
        tmp_path, monkeypatch):
    import artifacts.registry as registry_module

    registry = ArtifactRegistry(tmp_path / "artifacts.sqlite")
    native = tmp_path / "sum.json"
    native.write_text("42", encoding="utf-8")
    registry.register_file(
        native, _descriptor("example.scalar.sum"),
        media_type="application/json", producer_id="source",
        producer_version="1", output_port_id="result")

    real = registry_module._hash_file
    calls = []
    monkeypatch.setattr(
        registry_module, "_hash_file",
        lambda path: (calls.append(path), real(path))[1])
    assert registry.snapshot().committed_records
    assert calls == []
    assert registry.snapshot(
        verification_policy=VerificationPolicy.ALWAYS_REHASH).committed_records
    assert len(calls) == 1


def test_changed_bytes_fail_pending_event_without_false_target_refresh(tmp_path):
    fixture, registry, _resolver, coordinator = _system(tmp_path)
    target = coordinator.submit_target(fixture.root_uses)
    native = tmp_path / "sum.json"
    native.write_text("42", encoding="utf-8")
    event = coordinator.enqueue_output_event(
        producer_id="sum-file-producer",
        outputs={"result": _sum_output(native)},
    )
    native.write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError, match="no longer available"):
        coordinator.process_event(event.event_id)
    assert coordinator.event_status(event.event_id) is OutputEventStatus.FAILED
    assert registry.records() == ()
    assert coordinator.target(target.request.target_id).status is \
        TargetStatus.PLANNED_WORKFLOW
