"""Frozen counterexamples and closed regressions for Stage-8R tracks.

Open tests are strict expected failures.  They are not quarantines: an XPASS
fails the suite so the implementing track must remove the marker and retain
the counterexample as an ordinary passing regression.

No test contacts a network or scheduler, launches MPI, or runs WRF-SFIRE.
"""
from __future__ import annotations

import dataclasses
import hashlib
import time

import pytest

from acquisition import ManifestShardStore, lower_manifest_to_capability
from contracts import (
    ArtifactDescriptor,
    BBoxSupport,
    Missingness,
    MissingnessStatus,
    OriginClass,
    IntrinsicUncertainty,
)
from cube.catalog import Catalog
from cube.entries import CubeEntry
from engine.runtime import RunState, WorkflowController
from engine.runtime.operations import operation_component
from engine.runtime.slurm import SlurmProvider, SlurmSubmitOptions, SlurmUnavailable
from engine.runtime.types import (
    BoundExecutionGraph,
    InputBinding,
    OutputSpec,
    ResourceRequest,
    TaskTemplate,
)
from partitions import (
    AdmissionPolicy,
    AxisKind,
    BoundedAdmissionController,
    CollectionManifest,
    CompletionPolicy,
    MemberOutcome,
    PacketAttempt,
    PacketResult,
    PartitionAxis,
    PartitionSetSpec,
    PartitionStore,
    PartitionTaskTemplate,
)
from scheduling import PriorityPolicy
from stage5.fixtures import acquisition_profile
from stage4.fixtures import make_unit_bridge_fixture
from transformations import TransformationSpec


OPEN = pytest.mark.xfail(
    strict=True,
    reason="Stage 8R gate is frozen but its implementation track is open",
)


def test_transform_uncertainty_policy_gate():
    """Unit scaling cannot silently retain a known untransformed error model."""
    bridge = make_unit_bridge_fixture().transformations[0]
    known = IntrinsicUncertainty.known(
        "uncertainty:absolute-metres-v1", "f" * 64)
    source = dataclasses.replace(
        bridge.input_ports[0].descriptor, intrinsic_uncertainty=known)
    result = dataclasses.replace(
        bridge.output_ports[0].descriptor, intrinsic_uncertainty=known)

    with pytest.raises(ValueError, match="uncertainty|loss"):
        TransformationSpec.bind_unit_affine(
            transformation_id="stage8r-known-uncertainty",
            transformation_version="1.0.0",
            execution_profile=bridge.execution_profile,
            source=source,
            result=result,
            cost_units=1,
        )


def test_acquisition_identity_gate(tmp_path):
    """A query cannot be lowered under spatial facts it never established."""
    from tests.test_stage5_binding import _bind, _candidate, _window

    shard_store = ManifestShardStore(tmp_path / "shards")
    binding = _bind(
        shard_store,
        (_candidate("whole", ("0", "0", "4", "4")),),
    )
    assert binding.ok
    query = binding.bound.query_payload
    forged = ArtifactDescriptor(
        concept_id=query["concept_id"],
        schema_version=query["schema_version"],
        representation=query["representation"],
        units=query["units"],
        spatial_support=BBoxSupport(
            "EPSG:4326", ("x", "y"),
            ("-100", "-100", "100", "100")),
        temporal_support=_window(),
        vertical_support=None,
        grid=None,
        native_resolution=None,
        origin=OriginClass.OBSERVATION,
        missingness=Missingness(MissingnessStatus.COMPLETE),
        component_names=("value",),
    )

    with pytest.raises(TypeError, match="descriptor"):
        lower_manifest_to_capability(
            binding.bound, forged, acquisition_profile(), cost_units=1)


def test_grid_contract_gate(tmp_path):
    """A valid north-up signed-y affine cannot pass planning then fail commit."""
    from tests.test_stage4_artifact_validation import _configuration, _field, _graph

    configuration = _configuration()
    configuration["grid_affine"] = ["1", "0", "0", "0", "-1", "10"]
    configuration["grid_support_bounds"] = ["-0.5", "8.5", "1.5", "10.5"]
    field = _field()
    field["y"] = [10.0, 9.0]
    graph = _graph(field, configuration=configuration)
    with WorkflowController(tmp_path / "runtime") as controller:
        run_id = controller.create_run(graph)
        assert controller.run_until_terminal(run_id) is RunState.SUCCEEDED


def _partition_fixture(tmp_path):
    from stage7.fixtures import resolve_one_selection

    spec = PartitionSetSpec.bind((
        PartitionAxis("tile", AxisKind.SPATIAL, ("t0", "t1")),
    ))
    template = PartitionTaskTemplate.bind(
        resolve_one_selection(), retry_safe=True)
    manifest = CollectionManifest.bind(
        set_id=spec.set_id,
        template_id=template.template_id,
        expected=spec.total,
        policy=CompletionPolicy.ALL,
    )
    store = PartitionStore(tmp_path / "partitions.sqlite3")
    controller = BoundedAdmissionController(
        store,
        manifest,
        spec,
        template,
        policy=AdmissionPolicy(
            window_size=2, low_watermark=1, high_watermark=2),
    )
    controller.top_up()
    return store, manifest, controller.next_packets(limit=2)[0]


def test_packet_result_replay_gate(tmp_path):
    """The same provider result is one attempt, however often it is delivered."""
    store, manifest, packet = _partition_fixture(tmp_path)
    attempt = store.register_packet_attempt(
        manifest.collection_id,
        packet,
        fence_token="stable-fence",
        runtime_run_id="fixture-stage8r-replay",
        runtime_plan_id="f" * 64,
        expected_attempt_number=1,
    )
    result = PacketResult.bind(
        attempt,
        packet,
        tuple((key, MemberOutcome.FAILED)
              for key in packet.logical_task_keys),
    )
    for _ in range(2):
        store._record_packet_result_for_test_fixture(
            manifest.collection_id,
            packet,
            attempt,
            result,
        )
    for key in packet.logical_task_keys:
        assert store.attempt_count(manifest.collection_id, key) == 1
        assert store.attempts_for(manifest.collection_id, key) == (
            (1, "FAILED"),)


def _sleep_graph() -> BoundExecutionGraph:
    sleep = operation_component("synthetic.sleep.v1")
    return BoundExecutionGraph.bind(
        "stage8r-reservation-restart",
        tuple(TaskTemplate(
            key=f"task-{index}",
            component=sleep,
            parameters={"seconds": 3.0, "value": index},
            outputs=(OutputSpec(),),
            resources=ResourceRequest(
                cpu_cores=1, memory_mb=64, walltime_s=30),
        ) for index in range(2)),
    )


def test_reservation_restart_gate(tmp_path):
    """A new in-memory ledger cannot forget a durable active attempt."""
    from tests.test_stage9_runtime_bridge import _ledger

    root = tmp_path / "runtime"
    graph = _sleep_graph()
    first = WorkflowController(
        root,
        max_inflight=2,
        poll_interval_s=0.01,
        ledger=_ledger(1),
        site_id="node",
        priority_policy=PriorityPolicy(),
    )
    run_id = first.create_run(graph)
    first.tick(run_id)
    assert first._global_active_attempt_count() == 1
    first.close()  # durable subprocess keeps running; reservation does not

    second = WorkflowController(
        root,
        max_inflight=2,
        poll_interval_s=0.01,
        ledger=_ledger(1),
        site_id="node",
        priority_policy=PriorityPolicy(),
    )
    try:
        durable = second.store.active_attempt_reservations()
        assert len(durable) == 1
        assert durable[0].site_id == "node"
        assert durable[0].envelope["cpu_cores"] == 1
        assert second.ledger.live_task_keys() == (
            durable[0].spec.attempt_id,)
        second.tick(run_id)
        assert second._global_active_attempt_count() <= 1
    finally:
        for task in graph.tasks:
            second.cancel_task(run_id, task.task_id)
        second.close()


def test_reservation_restart_fails_closed_when_capacity_shrinks(tmp_path):
    """Recovery cannot forget work merely because the new ledger is smaller."""
    from scheduling import ExecutionSite, ReservationLedger, ResourceEnvelopeSpec
    from tests.test_stage9_runtime_bridge import _ledger

    root = tmp_path / "runtime"
    graph = _sleep_graph()
    first = WorkflowController(
        root, max_inflight=2, ledger=_ledger(1), site_id="node")
    run_id = first.create_run(graph)
    first.tick(run_id)
    first.close()

    too_small = ReservationLedger((
        ExecutionSite("node", ResourceEnvelopeSpec(0, 4096)),
    ))
    with pytest.raises(RuntimeError, match="cannot reconstruct"):
        WorkflowController(
            root, max_inflight=2, ledger=too_small, site_id="node")

    recovery = WorkflowController(
        root, max_inflight=2, ledger=_ledger(1), site_id="node")
    try:
        for task in graph.tasks:
            recovery.cancel_task(run_id, task.task_id)
    finally:
        recovery.close()


def test_startup_sweeps_terminal_unreleased_crash_state(tmp_path):
    """Terminal attempt state wins after a crash before ledger release."""
    from tests.test_stage9_runtime_bridge import _ledger

    root = tmp_path / "runtime"
    graph = _sleep_graph()
    ledger = _ledger(1)
    first = WorkflowController(
        root, max_inflight=2, ledger=ledger, site_id="node")
    run_id = first.create_run(graph)
    first.tick(run_id)
    active = first.store.active_attempts(run_id)
    assert len(active) == 1
    attempt_id = active[0].spec.attempt_id

    cancelled = first.store.request_cancel(
        run_id, active[0].spec.task.task_id)
    assert cancelled is not None
    first._stop_cancelled_attempt(cancelled, error="crash-window fixture")
    with first.store.connect() as connection:
        row = connection.execute(
            "SELECT released_at FROM attempt_resource_reservations "
            "WHERE attempt_id=?", (attempt_id,),
        ).fetchone()
    assert row is not None and row[0] is None
    assert ledger.reservation(attempt_id) is not None

    # Simulate process loss without WorkflowController.close(), whose normal
    # terminal cleanup would close this exact crash window.
    first.provider.close()
    first.store.stop_controller_session(first.controller_id)
    first._lock.close()
    first._closed = True

    second = WorkflowController(
        root, max_inflight=2, ledger=ledger, site_id="node")
    try:
        assert ledger.reservation(attempt_id) is None
        with second.store.connect() as connection:
            repaired = connection.execute(
                "SELECT released_at FROM attempt_resource_reservations "
                "WHERE attempt_id=?", (attempt_id,),
            ).fetchone()
        assert repaired is not None and repaired[0] is not None
        assert second.store.sweep_terminal_attempt_reservations() == ()
        for task in graph.tasks:
            second.cancel_task(run_id, task.task_id)
    finally:
        second.close()


def test_reservations_use_attempt_fences_not_cross_run_task_ids(tmp_path):
    """The same bound task in two runs owns two independent capacity claims."""
    from tests.test_stage9_runtime_bridge import _ledger

    sleep = operation_component("synthetic.sleep.v1")
    graph = BoundExecutionGraph.bind(
        "stage8r-cross-run-reservations",
        (TaskTemplate(
            key="same-scientific-task",
            component=sleep,
            parameters={"seconds": 3.0, "value": 1},
            outputs=(OutputSpec(),),
            resources=ResourceRequest(
                cpu_cores=1, memory_mb=64, walltime_s=30),
        ),),
    )
    ledger = _ledger(2)
    controller = WorkflowController(
        tmp_path / "runtime",
        max_inflight=2,
        ledger=ledger,
        site_id="node",
    )
    run_ids = (controller.create_run(graph), controller.create_run(graph))
    try:
        controller.tick(run_ids[0])
        controller.tick(run_ids[1])
        records = controller.store.active_attempt_reservations()
        assert len(records) == 2
        assert len({record.spec.task.task_id for record in records}) == 1
        attempt_ids = tuple(sorted(
            record.spec.attempt_id for record in records))
        assert ledger.live_task_keys() == attempt_ids
        assert ledger.used("node").cpu_cores == 2
    finally:
        for run_id in run_ids:
            controller.cancel_task(run_id, graph.tasks[0].task_id)
        controller.close()


def test_ready_aging_cache_prunes_non_ready_tasks(tmp_path):
    """Priority timestamps are bounded by the durable global READY set."""
    graph = _sleep_graph()
    controller = WorkflowController(
        tmp_path / "runtime",
        max_inflight=1,
        priority_policy=PriorityPolicy(),
    )
    run_id = controller.create_run(graph)
    try:
        controller.tick(run_id)
        assert len(controller._ready_since) == 2

        # One task is RUNNING and the other remains READY. The second tick
        # cannot dispatch, but must still prune the task that left READY.
        controller.tick(run_id)
        assert len(controller._ready_since) == 1
        remaining = next(iter(controller._ready_since))
        assert remaining[0] == run_id
        assert controller.store.task_state(
            remaining[0], remaining[1]).value == "READY"

        for task in graph.tasks:
            controller.cancel_task(run_id, task.task_id)
        controller.tick(run_id)
        assert controller._ready_since == {}
    finally:
        controller.close()


def test_full_graph_rank_gate(tmp_path):
    """Live priority must see descendants, not only the two ready heads."""
    from tests.test_stage9_runtime_bridge import _ledger

    sleep = operation_component("synthetic.sleep.v1")
    output = (OutputSpec(),)
    graph = BoundExecutionGraph.bind(
        "stage8r-full-rank",
        (
            TaskTemplate(
                "a-head", sleep, {"seconds": 3.0, "value": "a"},
                outputs=output,
                resources=ResourceRequest(
                    cpu_cores=1, memory_mb=64, walltime_s=1)),
            TaskTemplate(
                "a-middle", sleep, {"seconds": 0.0, "value": "m"},
                inputs=(InputBinding("ignored", "a-head"),), outputs=output,
                resources=ResourceRequest(
                    cpu_cores=1, memory_mb=64, walltime_s=10)),
            TaskTemplate(
                "a-tail", sleep, {"seconds": 0.0, "value": "t"},
                inputs=(InputBinding("ignored", "a-middle"),), outputs=output,
                resources=ResourceRequest(
                    cpu_cores=1, memory_mb=64, walltime_s=10)),
            TaskTemplate(
                "b-independent", sleep, {"seconds": 3.0, "value": "b"},
                outputs=output,
                resources=ResourceRequest(
                    cpu_cores=1, memory_mb=64, walltime_s=5)),
        ),
    )
    controller = WorkflowController(
        tmp_path / "runtime",
        max_inflight=2,
        ledger=_ledger(1),
        site_id="node",
        priority_policy=PriorityPolicy(),
    )
    run_id = controller.create_run(graph)
    try:
        controller.tick(run_id)
        active = controller.store.active_attempts(run_id)
        assert len(active) == 1
        assert active[0].spec.task.key == "a-head"
    finally:
        for task in graph.tasks:
            controller.cancel_task(run_id, task.task_id)
        controller.close()


def test_slurm_partial_visibility_gate(tmp_path):
    """Queue absence plus unavailable accounting is not confirmed absence."""
    from tests.test_stage9a_slurm import FakeSlurm, _spec

    fake = FakeSlurm()
    provider = SlurmProvider(
        tmp_path / "slurm",
        runner=fake,
        require_tools=False,
        options=SlurmSubmitOptions(time_limit="00:05:00"),
    )
    spec = _spec(provider, "partial-visibility")
    handle = provider.submit(spec)
    fake.finish(handle.external_id, "COMPLETED")
    fake.sacct_broken = True

    with pytest.raises(SlurmUnavailable):
        provider.submit(spec)
    assert fake.submissions == 1


def test_cube_authority_gate(tmp_path):
    """A caller digest alone is not an authoritative artifact publication."""
    catalog = Catalog(tmp_path / "catalog.duckdb")
    try:
        uncommitted = CubeEntry.create(
            concept="example.result",
            kind="static",
            producer="free-text-producer",
            content_sha256=hashlib.sha256(b"bytes that were never committed").hexdigest(),
        )
        with pytest.raises(PermissionError, match="artifact|authoritative"):
            catalog.commit_entry(uncommitted)
    finally:
        catalog.close()
