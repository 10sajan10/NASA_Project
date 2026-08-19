"""Runnable target -> producer -> artifact -> target proof."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from artifacts import ArtifactRegistry, ArtifactWorkflowResolver
from cube.entries import DatasetRef
from resolution import DiscoveryCertificate, DiscoveryUniverseContract
from stage3.fixtures import _descriptor, make_composition_fixture


def run_demo(workspace: str | Path) -> dict[str, Any]:
    root = Path(workspace).resolve()
    root.mkdir(parents=True, exist_ok=True)
    fixture = make_composition_fixture()
    registry = ArtifactRegistry(root / "artifacts.sqlite")
    service = ArtifactWorkflowResolver(
        fixture.catalog,
        fixture.deployment_snapshot,
        registry,
        discovery_certificate=DiscoveryCertificate.for_base_catalog(
            fixture.catalog),
        discovery_universe=DiscoveryUniverseContract.declare(
            fixture.catalog.catalog_id),
    )
    before = service.resolve(fixture.root_uses)

    native = root / "sum.json"
    native.write_text("42", encoding="utf-8")
    descriptor = _descriptor("example.scalar.sum")
    service.output_arrived(
        producer_id="stage10a-sum-file-producer",
        outputs={"result": DatasetRef(
            str(native),
            "application/json",
            descriptor=descriptor,
            producer_version="1.0.0",
            output_port_id="result",
        )},
    )
    after = service.resolve(fixture.root_uses)

    before_plan = before.resolution.selection.plan
    after_plan = after.resolution.selection.plan
    assert before_plan is not None and after_plan is not None
    selected = after.selected_artifacts[0]
    return {
        "schema": "stage10a-demo-result-v1",
        "before_cost_units": before_plan.total_cost_units,
        "before_selected_invocation_count": len(
            before_plan.selected_invocation_ids),
        "after_cost_units": after_plan.total_cost_units,
        "after_selected_invocation_count": len(
            after_plan.selected_invocation_ids),
        "artifact_workflow_id": after.workflow_id,
        "artifact_record_id": selected.record_id,
        "artifact_id": selected.artifact_id,
        "native_location": selected.location,
        "native_location_unchanged": selected.location == str(native),
        "array_payload_written": False,
        "transformations_selected": False,
    }


__all__ = ["run_demo"]
