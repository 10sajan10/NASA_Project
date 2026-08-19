"""Crash/restart proof for durable target-to-artifact automation."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from artifacts import (
    ArtifactRegistry,
    ArtifactTargetCoordinator,
    ArtifactWorkflowResolver,
)
from cube.entries import DatasetRef
from resolution import DiscoveryCertificate, DiscoveryUniverseContract
from stage3.fixtures import _descriptor, make_composition_fixture


def _services(root: Path):
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
    return fixture, registry, coordinator


def run_demo(workspace: str | Path) -> dict[str, Any]:
    root = Path(workspace).resolve()
    root.mkdir(parents=True, exist_ok=True)
    fixture, registry, coordinator = _services(root)
    before = coordinator.submit_target(fixture.root_uses)

    native = root / "sum.json"
    native.write_text("42", encoding="utf-8")
    event = coordinator.enqueue_output_event(
        producer_id="stage10b-sum-file-producer",
        outputs={"result": DatasetRef(
            str(native), "application/json",
            descriptor=_descriptor("example.scalar.sum"),
            producer_version="1.0.0", output_port_id="result")},
    )

    # Deliberately stop between the two durable databases.
    registry.register_records(event.records)
    fixture2, registry2, restarted = _services(root)
    assert fixture2.root_uses == fixture.root_uses
    replayed = restarted.recover()
    after = restarted.target(before.request.target_id)
    manifest = after.workflow_manifest
    assert manifest is not None
    return {
        "schema": "stage10b-demo-result-v1",
        "target_id": before.request.target_id,
        "event_id": event.event_id,
        "replayed_event_ids": list(replayed),
        "before_status": before.status.value,
        "before_cost_units": before.workflow_manifest.total_cost_units,
        "after_status": after.status.value,
        "after_cost_units": manifest.total_cost_units,
        "workflow_manifest_id": manifest.manifest_id,
        "artifact_record_count": len(registry2.records()),
        "native_location": manifest.selected_artifacts[0].location,
        "native_location_unchanged":
            manifest.selected_artifacts[0].location == str(native),
        "payload_copied": False,
        "transformation_selected": False,
        "cube_write": False,
    }


__all__ = ["run_demo"]
