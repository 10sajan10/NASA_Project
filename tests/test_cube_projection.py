"""Stage-8R authoritative artifact -> Cube projection and recovery."""
from __future__ import annotations

import os
from dataclasses import replace

import pytest

from contracts.types import ArtifactDescriptor
from cube.catalog import Catalog
from cube.projection import (
    CubeEntryProjection,
    CubeProjector,
    ProjectionFailpoint,
)
from engine.runtime import RunState, WorkflowController
from engine.runtime.identity import strict_hash
from engine.runtime.operations import operation_component
from engine.runtime.types import (
    BoundExecutionGraph,
    ExternalArtifactInputBinding,
    OutputSpec,
    ScientificArtifactBinding,
    TaskTemplate,
)
from stage4.demo import build_demo_plan


class SimulatedProjectorCrash(BaseException):
    pass


class CrashOnce:
    def __init__(self, point: ProjectionFailpoint) -> None:
        self.point = point.value
        self.hit = False

    def __call__(self, point: str) -> None:
        if point == self.point and not self.hit:
            self.hit = True
            raise SimulatedProjectorCrash(point)


def _execute_chain(tmp_path):
    demo = build_demo_plan()
    graph = demo.compilation.graph
    assert graph is not None
    controller = WorkflowController(tmp_path)
    run_id = controller.create_run(graph)
    assert controller.run_until_terminal(run_id) is RunState.SUCCEEDED
    return demo, graph, controller, run_id


def test_compiler_binds_full_descriptor_and_plan_coordinates_to_recipes():
    demo = build_demo_plan()
    graph = demo.compilation.graph
    assert graph is not None
    invocation_by_id = {
        value.invocation_key: value for value in demo.selected_invocations}
    task_key_by_invocation = dict(
        demo.compilation.record.invocation_task_keys)

    for invocation_id, invocation in invocation_by_id.items():
        task = graph.task_by_key(task_key_by_invocation[invocation_id])
        recipe_by_port = {value.output_name: value for value in task.outputs}
        for output in invocation.outputs:
            binding = recipe_by_port[output.port_id].scientific_binding
            assert binding is not None
            assert binding.bound_plan_id == demo.bound_plan.bound_plan_id
            assert binding.invocation_id == invocation_id
            assert binding.capability_id == invocation.capability_id
            assert binding.capability_version == invocation.capability_version
            assert binding.evidence_profile_id == invocation.evidence_profile_id
            assert binding.output_port == output.port_id
            assert binding.descriptor_id == output.descriptor.descriptor_id
            assert binding.to_dict()["descriptor"] == output.descriptor.to_dict()


def test_tiny_same_concept_chain_executes_commits_and_projects(tmp_path):
    demo, graph, controller, run_id = _execute_chain(tmp_path)
    catalog = Catalog(tmp_path / "cube.duckdb")
    try:
        projector = CubeProjector(tmp_path, controller.store, catalog)
        assert projector.project_pending() == 2
        assert projector.project_pending() == 0

        entries = catalog.entries_for("example.scalar.length")
        assert sorted(value.depth for value in entries) == [0, 1]
        derived = catalog.resolve("example.scalar.length")
        assert derived.depth == 1
        assert len(derived.inputs) == 1
        baseline = catalog.entry(derived.inputs[0].entry_id)
        assert baseline is not None and baseline.depth == 0
        # Both stages produce the same concept; lineage, not write order,
        # distinguishes the transformed value from its input.
        assert baseline.concept == derived.concept

        with controller.store.connect() as connection:
            rows = connection.execute(
                "SELECT state,projection_id,entry_id FROM cube_projection_outbox "
                "WHERE run_id=? ORDER BY recipe_id", (run_id,),
            ).fetchall()
        assert len(rows) == 2
        assert all(row[0] == "PROJECTED" for row in rows)
        derived_receipt = None
        for row in rows:
            receipt = catalog.projection(row[1])
            assert receipt is not None and receipt.entry_id == row[2]
            assert receipt.descriptor_id in {
                output.descriptor.descriptor_id
                for invocation in demo.selected_invocations
                for output in invocation.outputs}
            assert receipt.invocation_id in {
                value.invocation_key for value in demo.selected_invocations}
            if receipt.entry_id == derived.entry_id:
                derived_receipt = receipt
        assert derived_receipt is not None
        assert len(derived_receipt.inputs) == 1
        parent_edge = derived_receipt.inputs[0]
        parent_receipt = catalog.projection_for_artifact(
            run_id, parent_edge.artifact_id)
        assert parent_receipt is not None
        assert parent_receipt.recipe_id == parent_edge.recipe_id
        assert parent_receipt.entry_id == parent_edge.entry_id == baseline.entry_id
    finally:
        catalog.close()
        controller.close()


def test_fresh_catalog_rebuilds_after_global_outbox_is_already_projected(
        tmp_path):
    """A global SQLite ack cannot stand in for target-catalog contents."""
    _, _, controller, run_id = _execute_chain(tmp_path)
    first = Catalog(tmp_path / "first-cube.duckdb")
    try:
        assert CubeProjector(
            tmp_path, controller.store, first).project_pending() == 2
    finally:
        first.close()
    with controller.store.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM cube_projection_outbox "
            "WHERE run_id=? AND state='PROJECTED'", (run_id,),
        ).fetchone()[0] == 2

    fresh = Catalog(tmp_path / "fresh-cube.duckdb")
    try:
        projector = CubeProjector(tmp_path, controller.store, fresh)
        assert projector.project_pending() == 2
        assert len(fresh.entries_for("example.scalar.length")) == 2
        assert fresh.con.execute(
            "SELECT COUNT(*) FROM entry_projections").fetchone()[0] == 2
        assert projector.project_pending() == 0
    finally:
        fresh.close()
        controller.close()


def test_crash_after_duckdb_commit_replays_without_duplicate(tmp_path):
    _, _, controller, run_id = _execute_chain(tmp_path)
    catalog = Catalog(tmp_path / "cube.duckdb")
    try:
        crash = CrashOnce(ProjectionFailpoint.AFTER_CUBE_COMMIT)
        projector = CubeProjector(
            tmp_path, controller.store, catalog, failpoint=crash)
        try:
            projector.project_pending()
        except SimulatedProjectorCrash:
            pass
        else:  # pragma: no cover - failpoint contract
            raise AssertionError("projection failpoint did not fire")
        assert crash.hit
        assert len(catalog.entries_for("example.scalar.length")) == 1
        with controller.store.connect() as connection:
            assert connection.execute(
                "SELECT COUNT(*) FROM cube_projection_outbox "
                "WHERE run_id=? AND state='PENDING'", (run_id,),
            ).fetchone()[0] == 2

        recovered = CubeProjector(tmp_path, controller.store, catalog)
        assert recovered.project_pending() == 2
        assert len(catalog.entries_for("example.scalar.length")) == 2
        assert recovered.project_pending() == 0
    finally:
        catalog.close()
        controller.close()


def test_crash_after_sqlite_ack_converges_from_remaining_outbox(tmp_path):
    _, _, controller, run_id = _execute_chain(tmp_path)
    catalog = Catalog(tmp_path / "cube.duckdb")
    try:
        crash = CrashOnce(ProjectionFailpoint.AFTER_OUTBOX_ACK)
        projector = CubeProjector(
            tmp_path, controller.store, catalog, failpoint=crash)
        try:
            projector.project_pending()
        except SimulatedProjectorCrash:
            pass
        else:  # pragma: no cover - failpoint contract
            raise AssertionError("projection failpoint did not fire")
        with controller.store.connect() as connection:
            states = [row[0] for row in connection.execute(
                "SELECT state FROM cube_projection_outbox WHERE run_id=?",
                (run_id,),
            ).fetchall()]
        assert sorted(states) == ["PENDING", "PROJECTED"]
        assert CubeProjector(
            tmp_path, controller.store, catalog).project_pending() == 1
        assert len(catalog.entries_for("example.scalar.length")) == 2
    finally:
        catalog.close()
        controller.close()


def test_identical_derivation_in_another_run_reuses_entry_with_new_receipt(
        tmp_path):
    _, graph, controller, first_run = _execute_chain(tmp_path)
    second_run = controller.create_run(graph)
    assert controller.run_until_terminal(second_run) is RunState.SUCCEEDED
    catalog = Catalog(tmp_path / "cube.duckdb")
    try:
        assert CubeProjector(
            tmp_path, controller.store, catalog).project_pending() == 4
        # Entry identity deliberately excludes run_id, while each run retains
        # its own authoritative projection receipt.
        assert len(catalog.entries_for("example.scalar.length")) == 2
        assert catalog.con.execute(
            "SELECT COUNT(*) FROM entry_projections").fetchone()[0] == 4
        with controller.store.connect() as connection:
            assert connection.execute(
                "SELECT COUNT(*) FROM cube_projection_outbox WHERE state='PROJECTED' "
                "AND run_id IN (?,?)", (first_run, second_run),
            ).fetchone()[0] == 4
    finally:
        catalog.close()
        controller.close()


def test_precommitted_external_artifact_keeps_source_run_lineage(tmp_path):
    _, first_graph, controller, source_run = _execute_chain(tmp_path)
    catalog = Catalog(tmp_path / "cube.duckdb")
    try:
        projector = CubeProjector(tmp_path, controller.store, catalog)
        assert projector.project_pending() == 2
        source_task = next(task for task in first_graph.tasks if task.inputs)
        source_recipe = source_task.outputs[0]
        assert source_recipe.scientific_binding is not None
        source_artifact = controller.store.committed_output(
            source_run, source_task.task_id, source_recipe.output_name)
        assert source_artifact is not None

        invocation_id = strict_hash({"external-consumer": "identity-v1"})
        binding = ScientificArtifactBinding(
            bound_plan_id=strict_hash({"bound-plan": "external-fixture-v1"}),
            invocation_id=invocation_id,
            capability_id="fixture.synthetic.identity",
            capability_version="1.0.0",
            evidence_profile_id="evidence:unknown",
            output_port="result",
            descriptor_id=source_recipe.scientific_binding.descriptor_id,
            descriptor=source_recipe.scientific_binding.to_dict()["descriptor"],
        )
        external_graph = BoundExecutionGraph.bind(
            "authoritative-external-artifact-consumer",
            (TaskTemplate(
                key="identity",
                component=operation_component("synthetic.identity.v1"),
                external_inputs=(ExternalArtifactInputBinding(
                    "value", source_artifact["artifact_id"]),),
                outputs=(OutputSpec(
                    "result", "application/json", {"kind": "finite_json"},
                    binding),),
            ),),
            schema_version="stage8r-external-cube-lineage-v1",
        )
        consumer_run = controller.create_run(external_graph)
        assert controller.run_until_terminal(consumer_run) is RunState.SUCCEEDED
        assert projector.project_pending() == 1
        with controller.store.connect() as connection:
            row = connection.execute(
                "SELECT projection_id FROM cube_projection_outbox "
                "WHERE run_id=?", (consumer_run,),
            ).fetchone()
        receipt = catalog.projection(row[0])
        assert receipt is not None and len(receipt.inputs) == 1
        edge = receipt.inputs[0]
        assert edge.source_run_id == source_run
        assert edge.artifact_id == source_artifact["artifact_id"]
        parent = catalog.projection_for_artifact(
            edge.source_run_id, edge.artifact_id)
        assert parent is not None and parent.entry_id == edge.entry_id
    finally:
        catalog.close()
        controller.close()


def test_conflicting_projection_receipt_is_not_silently_replayed(tmp_path):
    _, _, controller, _ = _execute_chain(tmp_path)
    catalog = Catalog(tmp_path / "cube.duckdb")
    try:
        crash = CrashOnce(ProjectionFailpoint.AFTER_CUBE_COMMIT)
        try:
            CubeProjector(
                tmp_path, controller.store, catalog,
                failpoint=crash).project_pending()
        except SimulatedProjectorCrash:
            pass
        row = catalog.con.execute(
            "SELECT projection_id FROM entry_projections").fetchone()
        assert row is not None
        catalog.con.execute(
            "UPDATE entry_projections SET projection_json='{}' "
            "WHERE projection_id=?", [row[0]])

        with pytest.raises(RuntimeError, match="projection identity conflict"):
            CubeProjector(
                tmp_path, controller.store, catalog).project_pending()
        assert len(catalog.entries_for("example.scalar.length")) == 1
    finally:
        catalog.close()
        controller.close()


def test_stale_runtime_recipe_cannot_be_projected(tmp_path):
    _, _, controller, run_id = _execute_chain(tmp_path)
    catalog = Catalog(tmp_path / "cube.duckdb")
    try:
        with controller.store.transaction() as connection:
            recipe_id = connection.execute(
                "SELECT recipe_id FROM cube_projection_outbox WHERE run_id=? "
                "ORDER BY created_at LIMIT 1", (run_id,),
            ).fetchone()[0]
            connection.execute(
                "UPDATE artifact_recipes SET recipe_json='{}' WHERE recipe_id=?",
                (recipe_id,),
            )
        with pytest.raises((RuntimeError, ValueError), match="ArtifactRecipe|fields"):
            CubeProjector(
                tmp_path, controller.store, catalog).project_pending()
        assert catalog.entries_for("example.scalar.length") == []
    finally:
        catalog.close()
        controller.close()


def test_manifest_file_drift_cannot_be_projected(tmp_path):
    _, _, controller, run_id = _execute_chain(tmp_path)
    catalog = Catalog(tmp_path / "cube.duckdb")
    try:
        with controller.store.connect() as connection:
            manifest_path = connection.execute(
                "SELECT a.manifest_path FROM cube_projection_outbox o "
                "JOIN artifacts a ON a.artifact_id=o.artifact_id "
                "WHERE o.run_id=? ORDER BY o.created_at LIMIT 1", (run_id,),
            ).fetchone()[0]
        os.chmod(manifest_path, 0o600)
        with open(manifest_path, "w", encoding="utf-8") as stream:
            stream.write("{}")
        with pytest.raises(RuntimeError, match="manifest file differs"):
            CubeProjector(
                tmp_path, controller.store, catalog).project_pending()
        assert catalog.entries_for("example.scalar.length") == []
    finally:
        catalog.close()
        controller.close()


def test_a_self_consistent_receipt_is_not_authority(tmp_path):
    """Internal consistency is not proof the artifact exists.

    The public `commit_entry` already refuses caller-authored publication, but
    the private authoritative committer used to replay only the projection's
    own identity hash.  A receipt built by hand satisfies that check while
    naming an artifact that was never committed, so publication now requires a
    `ProjectionAuthority` minted only after the manifest is re-read and the
    object re-hashed from disk.
    """
    demo, graph, controller, run_id = _execute_chain(tmp_path)
    catalog = Catalog(tmp_path / "cube.duckdb")
    try:
        projector = CubeProjector(tmp_path, controller.store, catalog)
        with controller.store.connect() as connection:
            key = connection.execute(
                "SELECT run_id,recipe_id,artifact_id FROM "
                "cube_projection_outbox ORDER BY created_at LIMIT 1"
            ).fetchone()
        projection, authority = projector._reconstruct(
            str(key[0]), str(key[1]), str(key[2]))

        # The receipt still verifies against itself, exactly as before.
        assert projection.expected_id() == projection.projection_id

        for impostor in (None, object()):
            with pytest.raises(PermissionError, match="minted"):
                catalog._commit_authoritative_projection(projection, impostor)

        # The genuine pairing publishes.
        entry = catalog._commit_authoritative_projection(projection, authority)
        assert entry.entry_id == projection.entry_id
    finally:
        catalog.close()


def test_an_authority_is_bound_to_the_exact_projection(tmp_path):
    """Verified bytes cannot authorize forged scientific coordinates."""
    demo, graph, controller, run_id = _execute_chain(tmp_path)
    catalog = Catalog(tmp_path / "cube.duckdb")
    try:
        projector = CubeProjector(tmp_path, controller.store, catalog)
        with controller.store.connect() as connection:
            key = connection.execute(
                "SELECT run_id,recipe_id,artifact_id FROM "
                "cube_projection_outbox ORDER BY created_at LIMIT 1"
            ).fetchone()
        projection, authority = projector._reconstruct(
            *[str(value) for value in key])
        binding = ScientificArtifactBinding(
            projection.bound_plan_id,
            projection.invocation_id,
            "fixture.forgery-probe",
            "1.0.0",
            "evidence:unknown",
            projection.output_port,
            projection.descriptor_id,
            projection.descriptor,
        )
        descriptor = ArtifactDescriptor.from_dict(projection.descriptor)
        forged_descriptor = replace(
            descriptor, concept_id="forged.scientific.concept")
        forged_bindings = (
            replace(binding, invocation_id="f" * 64),
            replace(
                binding,
                descriptor_id=forged_descriptor.descriptor_id,
                descriptor=forged_descriptor.to_dict(),
            ),
        )
        forged = [
            CubeEntryProjection.bind(
                run_id=projection.run_id,
                runtime_plan_id=projection.runtime_plan_id,
                binding=value,
                recipe_id=projection.recipe_id,
                artifact_id=projection.artifact_id,
                content_sha256=projection.content_sha256,
                inputs=projection.inputs,
            )
            for value in forged_bindings
        ]
        forged.append(CubeEntryProjection.bind(
            run_id=projection.run_id,
            runtime_plan_id=projection.runtime_plan_id,
            binding=binding,
            recipe_id="e" * 64,
            artifact_id=projection.artifact_id,
            content_sha256=projection.content_sha256,
            inputs=projection.inputs,
        ))

        assert all(value.expected_id() == value.projection_id
                   for value in forged)
        for value in forged:
            with pytest.raises(PermissionError, match="different projection"):
                catalog._commit_authoritative_projection(value, authority)
    finally:
        catalog.close()


def test_authority_cannot_be_constructed_without_the_mint():
    from cube.entries import ProjectionAuthority

    with pytest.raises(PermissionError, match="minted only"):
        ProjectionAuthority(object(), "projection", "{}")
