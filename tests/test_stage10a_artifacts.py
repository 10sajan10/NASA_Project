"""Stage 10A: native artifact pointers automatically enter planning."""
from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path

import pytest

from artifacts import (
    ArtifactAvailability,
    ArtifactInput,
    ArtifactMatchStatus,
    ArtifactRecord,
    ArtifactRegistry,
    ArtifactRegistrySnapshot,
    ArtifactWorkflowResolver,
)
from contracts import ArtifactDescriptor
from cube.entries import DatasetRef
from cube.grid import SimulationGrid
from cube.store import Cube
from engine.contracts import ProducerV2, VarSpec
from resolution import (
    DiscoveryCertificate,
    DiscoveryUniverseContract,
    ResolutionStatus,
)
from stage3.fixtures import (
    _descriptor,
    _requirement,
    make_composition_fixture,
)


def _service(fixture, registry):
    return ArtifactWorkflowResolver(
        fixture.catalog,
        fixture.deployment_snapshot,
        registry,
        discovery_certificate=DiscoveryCertificate.for_base_catalog(
            fixture.catalog),
        discovery_universe=DiscoveryUniverseContract.declare(
            fixture.catalog.catalog_id),
    )


def test_a_target_request_automatically_reuses_a_registered_artifact(tmp_path):
    fixture = make_composition_fixture()
    registry = ArtifactRegistry(tmp_path / "artifacts.sqlite")
    service = _service(fixture, registry)

    before = service.resolve(fixture.root_uses)
    assert before.resolution.status is ResolutionStatus.READY
    assert before.resolution.selection.plan.total_cost_units == 3
    assert before.selected_artifacts == ()

    native = tmp_path / "sum.json"
    native.write_text("42", encoding="utf-8")
    record = service.register_output(
        DatasetRef(
            str(native),
            "application/json",
            descriptor=_descriptor("example.scalar.sum"),
            producer_version="1.0.0",
            output_port_id="result",
        ),
        producer_id="external-sum-producer",
    )

    after = service.resolve(fixture.root_uses)
    assert after.resolution.status is ResolutionStatus.READY
    assert after.resolution.eligible_for_binding
    assert after.resolution.selection.plan.total_cost_units == 0
    assert after.resolution.selection.plan.selected_invocation_ids == ()
    assert after.selected_artifacts == (record,)
    assert after.to_dict()["selected_artifacts"][0]["location"] == \
        str(native.resolve())
    assert native.read_text(encoding="utf-8") == "42"
    assert before.artifact_snapshot.snapshot_id != \
        after.artifact_snapshot.snapshot_id


def test_every_resolution_refreshes_file_availability(tmp_path):
    fixture = make_composition_fixture()
    registry = ArtifactRegistry(tmp_path / "artifacts.sqlite")
    service = _service(fixture, registry)
    native = tmp_path / "sum.json"
    native.write_text("42", encoding="utf-8")
    service.register_output(
        DatasetRef(
            str(native), "application/json",
            descriptor=_descriptor("example.scalar.sum")),
        producer_id="external-sum-producer",
    )
    selected = service.resolve(fixture.root_uses)
    assert selected.selected_artifacts

    original_stat = native.stat()
    native.write_text("43", encoding="utf-8")
    # Restoring size and mtime used to fool a stat-only planning snapshot.
    os.utime(native, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    refreshed = service.resolve(fixture.root_uses)
    assert refreshed.selected_artifacts == ()
    assert refreshed.resolution.selection.plan.total_cost_units == 3
    assert refreshed.artifact_snapshot.entries[0].availability is \
        ArtifactAvailability.UNAVAILABLE
    assert refreshed.artifact_snapshot.entries[0].reason == "SIZE_CHANGED" or \
        refreshed.artifact_snapshot.entries[0].reason == "CONTENT_CHANGED"


def test_registry_search_uses_the_same_direct_match_contract(tmp_path):
    registry = ArtifactRegistry(tmp_path / "artifacts.sqlite")
    native = tmp_path / "left.json"
    native.write_text("20", encoding="utf-8")
    record = registry.register_file(
        native,
        _descriptor("example.scalar.left"),
        media_type="application/json",
        producer_id="left-source",
        producer_version="1",
        output_port_id="result",
    )

    accepted = registry.search(_requirement("example.scalar.left"))
    assert len(accepted) == 1
    assert accepted[0].status is \
        ArtifactMatchStatus.COMPATIBLE_WITH_CAVEATS
    assert accepted[0].record == record
    rejected = registry.search(
        _requirement("example.scalar.sum"), include_incompatible=True)
    assert rejected == ()  # concept index prunes unrelated artifacts

    wrong_units = dataclasses.replace(
        _requirement("example.scalar.left"),
        units=type(_requirement("example.scalar.left").units).exact("m"))
    mismatch = registry.search(wrong_units, include_incompatible=True)
    assert mismatch[0].status is ArtifactMatchStatus.INCOMPATIBLE
    assert "UNITS_MISMATCH" in mismatch[0].reason


def test_record_snapshot_round_trip_and_identity_tamper_rejection(tmp_path):
    registry = ArtifactRegistry(tmp_path / "artifacts.sqlite")
    native = tmp_path / "value.json"
    native.write_text("20", encoding="utf-8")
    record = registry.register_file(
        native,
        _descriptor("example.scalar.left"),
        media_type="application/json",
        producer_id="source",
        producer_version="1",
        output_port_id="result",
        inputs=(ArtifactInput("forcing", "a" * 64),),
        metadata={"variable": "left"},
    )
    assert ArtifactRecord.from_dict(record.to_dict()) == record
    snapshot = registry.snapshot()
    assert ArtifactRegistrySnapshot.from_dict(snapshot.to_dict()) == snapshot

    changed = record.to_dict()
    changed["location"] = str(tmp_path / "other.json")
    with pytest.raises(ValueError, match="manifest identity"):
        ArtifactRecord.from_dict(changed)


def test_registry_restart_is_idempotent_and_does_not_copy_payload(tmp_path):
    path = tmp_path / "native.nc"
    path.write_bytes(b"native-file")
    db = tmp_path / "artifacts.sqlite"
    first = ArtifactRegistry(db).register_file(
        path,
        _descriptor("example.scalar.left"),
        media_type="application/json",
        producer_id="source",
        producer_version="1",
        output_port_id="result",
    )
    second_registry = ArtifactRegistry(db)
    second = second_registry.register_file(
        path,
        _descriptor("example.scalar.left"),
        media_type="application/json",
        producer_id="source",
        producer_version="1",
        output_port_id="result",
    )
    assert first == second
    assert second_registry.records() == (first,)
    assert Path(first.location) == path.resolve()
    assert path.read_bytes() == b"native-file"


def test_registration_refuses_a_symlink_pointer(tmp_path):
    target = tmp_path / "target.json"
    link = tmp_path / "link.json"
    target.write_text("20", encoding="utf-8")
    link.symlink_to(target)
    registry = ArtifactRegistry(tmp_path / "artifacts.sqlite")
    with pytest.raises(ValueError, match="symbolic link"):
        registry.register_file(
            link,
            _descriptor("example.scalar.left"),
            media_type="application/json",
            producer_id="source",
            producer_version="1",
            output_port_id="result",
        )


def test_datasetref_output_is_registered_automatically_when_registry_attached(
        tmp_path):
    registry = ArtifactRegistry(tmp_path / "artifacts.sqlite")
    cube = Cube(
        tmp_path / "cube-root",
        SimulationGrid(4326, 1.0, 2, 2, 0.0, 2.0),
        artifact_registry=registry,
    )
    native = tmp_path / "native.json"
    native.write_text("20", encoding="utf-8")
    descriptor = _descriptor("example.scalar.left")
    spec = VarSpec(
        "example.scalar.left", kind="static", dtype="float32", units="1")

    class NativeProducer(ProducerV2):
        name = "native-producer"
        produces = (spec,)
        requires = ()

        def compute(self, inputs, request):
            return {
                spec.name: DatasetRef(
                    str(native),
                    "application/json",
                    descriptor=descriptor,
                    producer_version="2.0",
                    output_port_id="result",
                    detail={"variable": "left"},
                )
            }

    request = type("Request", (), {"context": {}})()
    producer = NativeProducer()
    output = producer.compute({}, request)
    producer.update(cube, output, request)

    records = registry.records()
    assert len(records) == 1
    assert records[0].descriptor == descriptor
    assert records[0].location == str(native.resolve())
    assert records[0].metadata["variable"] == "left"
    assert not cube.has(spec.name)  # no Zarr/array payload was written
    cube.close()


def test_automatic_output_refuses_missing_scientific_metadata(tmp_path):
    registry = ArtifactRegistry(tmp_path / "artifacts.sqlite")
    cube = Cube(
        tmp_path / "cube-root",
        SimulationGrid(4326, 1.0, 2, 2, 0.0, 2.0),
        artifact_registry=registry,
    )
    native = tmp_path / "native.json"
    native.write_text("20", encoding="utf-8")
    spec = VarSpec(
        "example.scalar.left", kind="static", dtype="float32", units="1")

    class NativeProducer(ProducerV2):
        name = "native-producer"
        produces = (spec,)
        requires = ()

        def compute(self, inputs, request):
            return {spec.name: DatasetRef(
                str(native), "application/json")}

    request = type("Request", (), {"context": {}})()
    producer = NativeProducer()
    with pytest.raises(ValueError, match="requires DatasetRef.descriptor"):
        producer.update(cube, producer.compute({}, request), request)
    assert registry.records() == ()
    assert cube.catalog.search() == []
    cube.close()


def test_producer_arrival_is_visible_to_the_next_target_request(tmp_path):
    """The complete Stage-10A loop needs no manual catalog operation."""
    fixture = make_composition_fixture()
    registry = ArtifactRegistry(tmp_path / "artifacts.sqlite")
    service = _service(fixture, registry)
    assert service.resolve(
        fixture.root_uses).resolution.selection.plan.total_cost_units == 3

    cube = Cube(
        tmp_path / "cube-root",
        SimulationGrid(4326, 1.0, 2, 2, 0.0, 2.0),
    )
    service.attach_output_registry(cube)
    native = tmp_path / "sum.json"
    native.write_text("42", encoding="utf-8")
    descriptor = _descriptor("example.scalar.sum")
    spec = VarSpec(
        "example.scalar.sum", kind="static", dtype="float32", units="1")

    class SumProducer(ProducerV2):
        name = "sum-file-producer"
        produces = (spec,)
        requires = ()

        def compute(self, inputs, request):
            return {spec.name: DatasetRef(
                str(native), "application/json", descriptor=descriptor,
                producer_version="1.0.0", output_port_id="result")}

    request = type("Request", (), {"context": {}})()
    producer = SumProducer()
    producer.update(cube, producer.compute({}, request), request)

    refreshed = service.resolve(fixture.root_uses)
    assert refreshed.resolution.selection.plan.total_cost_units == 0
    assert refreshed.resolution.selection.plan.selected_invocation_ids == ()
    assert len(refreshed.selected_artifacts) == 1
    assert refreshed.selected_artifacts[0].location == str(native.resolve())
    assert not cube.has(spec.name)
    cube.close()


def test_cube_catalog_registration_binds_the_exact_location(tmp_path):
    from cube.catalog import Catalog
    from cube.entries import CubeEntry
    actual = tmp_path / "actual.bin"
    wrong = tmp_path / "wrong.bin"
    actual.write_bytes(b"bytes")
    digest = __import__("hashlib").sha256(actual.read_bytes()).hexdigest()
    entry = CubeEntry.create(
        concept="value", kind="static", producer="source",
        content_sha256=digest, location=str(wrong.resolve()),
        media_type="application/octet-stream")
    catalog = Catalog(tmp_path / "catalog.duckdb")
    with pytest.raises(ValueError, match="must equal"):
        catalog.register_dataset(entry, actual)
    catalog.close()


def test_direct_only_service_refuses_a_transformation_catalog(tmp_path):
    from stage4.fixtures import make_unit_bridge_fixture
    from transformations import expand_transform_catalog

    fixture = make_unit_bridge_fixture()
    expansion = expand_transform_catalog(
        fixture.base_catalog, fixture.transformations)
    with pytest.raises(ValueError, match="direct-only"):
        ArtifactWorkflowResolver(
            expansion.augmented_catalog,
            fixture.deployment_snapshot,
            ArtifactRegistry(tmp_path / "artifacts.sqlite"),
            discovery_certificate=expansion.discovery_certificate(),
            discovery_universe=DiscoveryUniverseContract.declare(
                fixture.base_catalog.catalog_id),
        )


def test_runnable_demo_proves_the_automatic_loop(tmp_path):
    from stage10a import run_demo

    result = run_demo(tmp_path / "demo")
    assert result["before_cost_units"] == 3
    assert result["after_cost_units"] == 0
    assert result["after_selected_invocation_count"] == 0
    assert result["native_location_unchanged"]
    assert not result["array_payload_written"]
    assert not result["transformations_selected"]


def test_coproduced_output_event_is_registered_atomically(tmp_path):
    fixture = make_composition_fixture()
    registry = ArtifactRegistry(tmp_path / "artifacts.sqlite")
    service = _service(fixture, registry)
    left = tmp_path / "left.json"
    right = tmp_path / "right.json"
    left.write_text("20", encoding="utf-8")
    right.write_text("22", encoding="utf-8")

    # Freeze a conflicting planning record for the same right-hand leaf. The
    # new event can prepare both records, but its transaction must publish
    # neither when the second record conflicts.
    existing = registry.register_file(
        right,
        _descriptor("example.scalar.right"),
        media_type="application/json",
        producer_id="pair-file-producer",
        producer_version="1",
        output_port_id="right",
        metadata={"revision": "old"},
    )
    with pytest.raises(ValueError, match="leaf identity has conflicting"):
        service.output_arrived(
            producer_id="pair-file-producer",
            outputs={
                "left": DatasetRef(
                    str(left), "application/json",
                    descriptor=_descriptor("example.scalar.left"),
                    producer_version="1", output_port_id="left"),
                "right": DatasetRef(
                    str(right), "application/json",
                    descriptor=_descriptor("example.scalar.right"),
                    producer_version="1", output_port_id="right",
                    detail={"revision": "new"}),
            },
        )
    assert registry.records() == (existing,)
