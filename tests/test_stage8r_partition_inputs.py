"""Stage-8R gate: partition inputs are exact committed scientific artifacts."""
from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path

import pytest

from contracts import direct_match
from engine.runtime import (
    BoundExecutionGraph,
    OutputSpec,
    RunState,
    ScientificArtifactBinding,
    TaskTemplate,
    WorkflowController,
)
from engine.runtime.identity import strict_hash
from engine.runtime.operations import operation_component
from engine.runtime.state import RuntimeStore
from partitions import (
    AdmissionPolicy,
    AxisKind,
    BoundedAdmissionController,
    CollectionManifest,
    CompletionPolicy,
    MemberOutcome,
    PacketAttemptAuthorityError,
    PacketResult,
    PartitionArtifactInput,
    PartitionAxis,
    PartitionInputAssignment,
    PartitionInputManifest,
    PartitionNotExecutable,
    PartitionSetSpec,
    PartitionStore,
    PartitionTaskTemplate,
    compile_packet,
    execute_packet,
)
from stage3.fixtures import make_composition_fixture
from stage7.fixtures import deployment_binding_for, resolve_all_selected


def _add_template() -> PartitionTaskTemplate:
    invocation = next(
        value for value in resolve_all_selected()
        if value.capability_id == "example-add")
    return PartitionTaskTemplate.bind(
        invocation, deployment_binding_for(invocation), retry_safe=True)


def _collection(tmp_path):
    spec = PartitionSetSpec.bind((
        PartitionAxis("tile", AxisKind.SPATIAL, ("p0", "p1")),
    ))
    template = _add_template()
    manifest = CollectionManifest.bind(
        set_id=spec.set_id, template_id=template.template_id,
        expected=spec.total, policy=CompletionPolicy.ALL)
    store = PartitionStore(tmp_path / "partition-control.sqlite3")
    admission = BoundedAdmissionController(
        store, manifest, spec, template,
        policy=AdmissionPolicy(
            window_size=2, low_watermark=2, high_watermark=2,
            max_packet_members=2, target_packet_cost=16))
    assert admission.top_up().admitted == 2
    packets = admission.next_packets(limit=2)
    assert len(packets) == 1 and len(packets[0].members) == 2
    return spec, template, manifest, store, packets[0]


def _descriptors(template: PartitionTaskTemplate):
    fixture = make_composition_fixture()
    result = {}
    for use in template.invocation.input_uses:
        matches = [
            output.descriptor
            for capability in fixture.catalog.capabilities
            for output in capability.output_ports
            if direct_match(output.descriptor, use.requirement).satisfied
        ]
        assert matches
        result[use.port_id] = matches[0]
    return result


def _seed_committed_inputs(runtime_root, template):
    """Run four real producers and return their committed artifact IDs."""
    descriptors = _descriptors(template)
    values = {
        (0, "left"): 1,
        (0, "right"): 10,
        (1, "left"): 2,
        (1, "right"): 20,
    }
    scientific_plan_id = strict_hash({
        "schema": "stage8r-partition-input-seed-plan-v1",
        "template_id": template.template_id,
    })
    tasks = []
    for (partition_index, port), value in sorted(values.items()):
        key = f"seed-p{partition_index}-{port}"
        descriptor = descriptors[port]
        tasks.append(TaskTemplate(
            key=key,
            component=operation_component("synthetic.constant.v1"),
            parameters={"value": value},
            outputs=(OutputSpec(
                name="result",
                scientific_binding=ScientificArtifactBinding(
                    bound_plan_id=scientific_plan_id,
                    invocation_id=strict_hash({
                        "schema": "stage8r-seed-invocation-v1",
                        "key": key,
                    }),
                    output_port="result",
                    descriptor_id=descriptor.descriptor_id,
                    descriptor=descriptor.to_dict(),
                ),
            ),),
        ))
    graph = BoundExecutionGraph.bind("stage8r-partition-input-seeds", tasks)
    with WorkflowController(runtime_root) as controller:
        run_id = controller.create_run(graph)
        assert controller.run_until_terminal(run_id) is RunState.SUCCEEDED
        with controller.store.connect() as connection:
            rows = connection.execute(
                "SELECT t.task_key,s.artifact_id FROM task_output_slots s "
                "JOIN tasks t ON t.run_id=s.run_id AND t.task_id=s.task_id "
                "WHERE s.run_id=?", (run_id,)).fetchall()
    artifacts = {}
    for task_key, artifact_id in rows:
        _, partition, port = task_key.split("-")
        index = int(partition[1:])
        artifacts[(index, port)] = artifact_id
    assert set(artifacts) == set(values)
    return artifacts, descriptors


def _input_manifest(spec, template, collection, packet,
                    artifacts, descriptors):
    uses = {value.port_id: value for value in template.invocation.input_uses}
    assignments = []
    for member in packet.members:
        bindings = tuple(PartitionArtifactInput(
            requirement_use_id=uses[port].requirement_use_id,
            input_name=port,
            artifact_id=artifacts[(member.partition_index, port)],
            descriptor_id=descriptors[port].descriptor_id,
        ) for port in ("left", "right"))
        assignments.append(PartitionInputAssignment.bind(
            member.logical_task_key, member.partition_index, bindings))
    return PartitionInputManifest.bind(
        collection.collection_id, spec, template, packet, assignments)


def _prepared(tmp_path):
    spec, template, collection, store, packet = _collection(tmp_path)
    runtime_root = tmp_path / "runtime"
    artifacts, descriptors = _seed_committed_inputs(runtime_root, template)
    manifest = _input_manifest(
        spec, template, collection, packet, artifacts, descriptors)
    return (spec, template, collection, store, packet, runtime_root,
            artifacts, descriptors, manifest)


def test_two_partitions_consume_distinct_committed_manifests_and_commit_outputs(
        tmp_path):
    (spec, template, collection, store, packet, runtime_root,
     _artifacts, _descriptors_by_port, manifest) = _prepared(tmp_path)
    runtime_store = RuntimeStore(runtime_root / "control" / "runtime.sqlite3")
    graph = compile_packet(
        template, packet, partition_spec=spec,
        input_manifest=manifest, runtime_store=runtime_store)

    assert BoundExecutionGraph.from_dict(graph.to_dict()) == graph
    assert len(graph.tasks) == 2
    assert all(len(task.external_inputs) == 2 for task in graph.tasks)
    assert len({tuple(value.artifact_id for value in task.external_inputs)
                for task in graph.tasks}) == 2

    decision, state = execute_packet(
        store, collection.collection_id, template, packet,
        runtime_root=runtime_root, input_manifest=manifest)
    assert state is RunState.SUCCEEDED
    assert set(decision.committed) == set(packet.logical_task_keys)

    with runtime_store.connect() as connection:
        run_id = connection.execute(
            "SELECT run_id FROM runs WHERE plan_id=? ORDER BY created_at DESC "
            "LIMIT 1", (graph.plan_id,)).fetchone()[0]
        rows = connection.execute(
            "SELECT task_id,task_key FROM tasks WHERE run_id=?",
            (run_id,)).fetchall()
        output_ids = connection.execute(
            "SELECT artifact_id FROM task_output_slots WHERE run_id=?",
            (run_id,)).fetchall()
        assert connection.execute(
            "SELECT COUNT(*) FROM task_external_inputs WHERE run_id=?",
            (run_id,)).fetchone()[0] == 4
    expected = {
        packet.members[0].logical_task_key: 11,
        packet.members[1].logical_task_key: 22,
    }
    with WorkflowController(runtime_root) as controller:
        observed = {
            task_key: controller.output_value(run_id, task_id)
            for task_id, task_key in rows
        }
    assert observed == expected
    assert len({row[0] for row in output_ids}) == 2


def test_packet_completion_is_derived_from_bound_runtime_commits(tmp_path):
    (spec, template, collection, store, packet, runtime_root,
     _artifacts, _descriptors_by_port, manifest) = _prepared(tmp_path)
    with WorkflowController(runtime_root) as controller:
        graph = compile_packet(
            template, packet, partition_spec=spec,
            input_manifest=manifest, runtime_store=controller.store)
        run_id = controller.create_run(graph)
        attempt = store.register_packet_attempt(
            collection.collection_id,
            packet,
            fence_token="runtime-authority",
            runtime_run_id=run_id,
            runtime_plan_id=graph.plan_id,
        )
        assert controller.run_until_terminal(run_id) is RunState.SUCCEEDED

        forged = PacketResult.bind(attempt, packet, tuple(
            (key, MemberOutcome.FAILED)
            for key in packet.logical_task_keys))
        with pytest.raises(PermissionError, match="caller packet results"):
            store.record_packet_result(
                collection.collection_id, packet, attempt, forged)
        assert store.state(collection.collection_id).committed == 0

        decision = store.record_runtime_packet_completion(
            collection.collection_id, packet, attempt, controller.store)
        assert set(decision.committed) == set(packet.logical_task_keys)
        assert store.record_runtime_packet_completion(
            collection.collection_id, packet, attempt,
            controller.store) == decision
        with store.connect() as connection:
            bound = connection.execute(
                "SELECT runtime_run_id,runtime_plan_id,status "
                "FROM packet_attempt_intents WHERE collection_id=? "
                "AND attempt_id=?",
                (collection.collection_id, attempt.attempt_id),
            ).fetchone()
        assert tuple(bound) == (run_id, graph.plan_id, "COMPLETED")

        with pytest.raises(PacketAttemptAuthorityError, match="only currently admitted"):
            store.register_packet_attempt(
                collection.collection_id,
                packet,
                fence_token="duplicate-runtime",
                runtime_run_id="must-not-submit",
                runtime_plan_id=graph.plan_id,
            )


def test_raw_partition_outcome_apis_refuse_scientific_claims(tmp_path):
    _spec_value, _template_value, collection, store, packet, *_ = _prepared(
        tmp_path)
    key = packet.logical_task_keys[0]
    with pytest.raises(PermissionError, match="raw partition outcomes"):
        store.record_outcome(
            collection.collection_id, key, MemberOutcome.COMMITTED)
    with pytest.raises(PermissionError, match="raw partition outcomes"):
        store.record_outcomes(
            collection.collection_id, ((key, MemberOutcome.COMMITTED),))
    assert store.state(collection.collection_id).committed == 0


def test_runtime_plan_with_packet_keys_but_wrong_operation_is_rejected(tmp_path):
    (_spec_value, _template_value, collection, store, packet, runtime_root,
     *_rest) = _prepared(tmp_path)
    forged_graph = BoundExecutionGraph.bind(
        f"partition-packet-{packet.packet_id[:12]}",
        tuple(TaskTemplate(
            key=member.logical_task_key,
            component=operation_component("synthetic.constant.v1"),
            parameters={"value": 99},
        ) for member in packet.members),
    )
    with WorkflowController(runtime_root) as controller:
        run_id = controller.create_run(forged_graph)
        attempt = store.register_packet_attempt(
            collection.collection_id,
            packet,
            fence_token="wrong-operation-plan",
            runtime_run_id=run_id,
            runtime_plan_id=forged_graph.plan_id,
        )
        assert controller.run_until_terminal(run_id) is RunState.SUCCEEDED
        with pytest.raises(
                PacketAttemptAuthorityError,
                match="registered scientific invocation|recompile"):
            store.record_runtime_packet_completion(
                collection.collection_id, packet, attempt, controller.store)
    assert store.state(collection.collection_id).committed == 0


def test_external_input_bindings_survive_controller_restart(tmp_path):
    (spec, template, _collection, _store, packet, runtime_root,
     _artifacts, _descriptors_by_port, manifest) = _prepared(tmp_path)
    runtime_store = RuntimeStore(runtime_root / "control" / "runtime.sqlite3")
    graph = compile_packet(
        template, packet, partition_spec=spec,
        input_manifest=manifest, runtime_store=runtime_store)

    first = WorkflowController(runtime_root)
    run_id = first.create_run(graph)
    first.close()  # no attempt exists; only the durable external bindings do

    with WorkflowController(runtime_root) as recovered:
        assert recovered.run_until_terminal(run_id) is RunState.SUCCEEDED
        assert {
            recovered.output_value(run_id, task.task_id)
            for task in graph.tasks
        } == {11, 22}
        with recovered.store.connect() as connection:
            attempts = connection.execute(
                "SELECT spec_json FROM attempts WHERE run_id=?",
                (run_id,)).fetchall()
    assert len(attempts) == 2
    assert all(len(json.loads(row[0])["input_artifacts"]) == 2
               for row in attempts)
    for row in attempts:
        for receipt in json.loads(row[0])["input_artifacts"].values():
            assert set(receipt) == {
                "artifact_id", "recipe_id", "content_sha256",
                "size_bytes", "manifest_path",
            }
            assert len(receipt["artifact_id"]) == 64
            assert len(receipt["recipe_id"]) == 64
            assert len(receipt["content_sha256"]) == 64
            assert receipt["size_bytes"] > 0
            assert Path(receipt["manifest_path"]).is_absolute()


def test_input_manifest_tampering_and_packet_swap_are_rejected(tmp_path):
    (spec, template, collection, _store, packet, runtime_root,
     artifacts, descriptors, manifest) = _prepared(tmp_path)
    forged = manifest.to_dict()
    left0 = forged["assignments"][0]["inputs"][0]["artifact_id"]
    left1 = forged["assignments"][1]["inputs"][0]["artifact_id"]
    forged["assignments"][0]["inputs"][0]["artifact_id"] = left1
    forged["assignments"][1]["inputs"][0]["artifact_id"] = left0
    with pytest.raises(ValueError, match="identity does not verify"):
        PartitionInputManifest.from_dict(forged)

    # Even an intact manifest is specific to one immutable packet membership.
    shortened = type(packet).bind(
        packet.collection_id, packet.template_id, packet.members[:1])
    with pytest.raises(ValueError, match="another collection, space, template, or packet"):
        manifest.verify_for(spec, template, shortened)

    # Rebinding is explicit: a newly hashed manifest with the two compatible
    # left artifacts swapped produces a different executable graph identity.
    assignments = list(manifest.assignments)
    first_inputs = list(assignments[0].inputs)
    second_inputs = list(assignments[1].inputs)
    first_inputs[0] = replace(first_inputs[0], artifact_id=artifacts[(1, "left")])
    second_inputs[0] = replace(second_inputs[0], artifact_id=artifacts[(0, "left")])
    assignments[0] = PartitionInputAssignment.bind(
        assignments[0].logical_task_key, 0, first_inputs)
    assignments[1] = PartitionInputAssignment.bind(
        assignments[1].logical_task_key, 1, second_inputs)
    rebound = PartitionInputManifest.bind(
        collection.collection_id, spec, template, packet, assignments)
    authority = RuntimeStore(runtime_root / "control" / "runtime.sqlite3")
    original_graph = compile_packet(
        template, packet, partition_spec=spec,
        input_manifest=manifest, runtime_store=authority)
    rebound_graph = compile_packet(
        template, packet, partition_spec=spec,
        input_manifest=rebound, runtime_store=authority)
    assert rebound.manifest_id != manifest.manifest_id
    assert rebound_graph.plan_id != original_graph.plan_id


def test_uncommitted_and_wrong_descriptor_inputs_fail_closed(tmp_path):
    (spec, template, collection, _store, packet, runtime_root,
     artifacts, descriptors, manifest) = _prepared(tmp_path)
    authority = RuntimeStore(runtime_root / "control" / "runtime.sqlite3")

    unknown_assignments = list(manifest.assignments)
    inputs = list(unknown_assignments[0].inputs)
    inputs[0] = replace(inputs[0], artifact_id="f" * 64)
    unknown_assignments[0] = PartitionInputAssignment.bind(
        unknown_assignments[0].logical_task_key, 0, inputs)
    unknown = PartitionInputManifest.bind(
        collection.collection_id, spec, template, packet,
        unknown_assignments)
    with pytest.raises(PartitionNotExecutable, match="not an authoritatively committed"):
        compile_packet(
            template, packet, partition_spec=spec,
            input_manifest=unknown, runtime_store=authority)

    wrong_assignments = list(manifest.assignments)
    inputs = list(wrong_assignments[0].inputs)
    right_position = next(
        index for index, value in enumerate(inputs)
        if value.input_name == "right")
    inputs[right_position] = replace(
        inputs[right_position],
        artifact_id=artifacts[(0, "left")],
        descriptor_id=descriptors["left"].descriptor_id)
    wrong_assignments[0] = PartitionInputAssignment.bind(
        wrong_assignments[0].logical_task_key, 0, inputs)
    wrong = PartitionInputManifest.bind(
        collection.collection_id, spec, template, packet, wrong_assignments)
    with pytest.raises(PartitionNotExecutable, match="does not satisfy"):
        compile_packet(
            template, packet, partition_spec=spec,
            input_manifest=wrong, runtime_store=authority)


def test_run_registration_rechecks_commit_in_its_own_runtime(tmp_path):
    (spec, template, _collection, _store, packet, runtime_root,
     _artifacts, _descriptors_by_port, manifest) = _prepared(tmp_path)
    authority = RuntimeStore(runtime_root / "control" / "runtime.sqlite3")
    graph = compile_packet(
        template, packet, partition_spec=spec,
        input_manifest=manifest, runtime_store=authority)

    # A compiler lookup is not a transferable attestation.  The same graph is
    # powerless in a different runtime whose artifact_commits table does not
    # contain those identities.
    with WorkflowController(tmp_path / "foreign-runtime") as foreign:
        with pytest.raises(ValueError, match="no authoritative commit"):
            foreign.create_run(graph)
