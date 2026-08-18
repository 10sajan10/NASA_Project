"""Stage-7 admission: atomicity under crash, restart, and bounded memory.

The exit gates under test are the sharp ones: cursor advancement and task
insertion are one transaction, so an injected crash can neither skip nor
multiply a logical partition; a restart resumes the persisted cursor; and
controller memory stays bounded by the watermark rather than by the size of
the partition space.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from partitions import (
    AdmissionPolicy,
    AxisKind,
    BoundedAdmissionController,
    CollectionManifest,
    CompletionPolicy,
    MemberOutcome,
    PartitionAxis,
    PartitionSetSpec,
    PartitionStore,
    PartitionRetryPolicy,
    PartitionTaskTemplate,
)


class InjectedCrash(RuntimeError):
    """Stands in for the controller process dying mid-transaction."""


def _spec(tiles: int = 10, windows: int = 10) -> PartitionSetSpec:
    return PartitionSetSpec.bind((
        PartitionAxis("tile", AxisKind.SPATIAL,
                      tuple(f"t{i}" for i in range(tiles))),
        PartitionAxis("window", AxisKind.TEMPORAL,
                      tuple(f"w{i}" for i in range(windows))),
    ))


def _template(**policy) -> PartitionTaskTemplate:
    # A real resolved invocation, not a restated one: the template must carry
    # the science the resolver actually chose.
    from stage7.fixtures import resolve_one_selection
    if not policy:
        policy = {"retry_safe": True}
    return PartitionTaskTemplate.bind(resolve_one_selection(), **policy)


def _manifest(spec: PartitionSetSpec,
              template: PartitionTaskTemplate) -> CollectionManifest:
    return CollectionManifest.bind(
        set_id=spec.set_id, template_id=template.template_id,
        expected=spec.total, policy=CompletionPolicy.ALL)


@pytest.fixture()
def workspace(tmp_path: Path):
    spec, template = _spec(), _template()
    store = PartitionStore(tmp_path / "partitions.sqlite3")
    manifest = _manifest(spec, template)
    store.open_collection(manifest, spec, template)
    return store, manifest, spec, template


# -- atomic admission -----------------------------------------------------


def test_admission_advances_the_cursor_exactly_once_per_window(workspace):
    store, manifest, spec, template = workspace
    first = store.admit_window(manifest.collection_id, spec, template, 30)
    assert (first.admitted, first.first_index, first.next_index) == (30, 0, 30)
    second = store.admit_window(manifest.collection_id, spec, template, 30)
    assert (second.admitted, second.first_index, second.next_index) == (30, 30, 60)
    assert store.cursor(manifest.collection_id).next_index == 60
    assert store.partition_indices(manifest.collection_id) == tuple(range(60))


def test_admission_clamps_at_the_end_of_the_space(workspace):
    store, manifest, spec, template = workspace
    store.admit_window(manifest.collection_id, spec, template, 90)
    last = store.admit_window(manifest.collection_id, spec, template, 90)
    assert last.admitted == 10 and last.exhausted
    empty = store.admit_window(manifest.collection_id, spec, template, 90)
    assert empty.admitted == 0 and empty.exhausted
    assert store.state(manifest.collection_id).admitted == spec.total


@pytest.mark.parametrize("point", [
    "after_cursor_read", "before_cursor_advance", "after_cursor_advance"])
def test_a_crash_anywhere_in_admission_rolls_the_whole_window_back(
        workspace, point):
    store, manifest, spec, template = workspace
    store.admit_window(manifest.collection_id, spec, template, 30)
    before_cursor = store.cursor(manifest.collection_id)
    before_indices = store.partition_indices(manifest.collection_id)

    def crash(reached: str) -> None:
        if reached == point:
            raise InjectedCrash(point)

    with pytest.raises(InjectedCrash):
        store.admit_window(manifest.collection_id, spec, template, 30,
                           fault=crash)

    # Neither half of the transaction survived: no advanced cursor, no
    # orphaned tasks.
    assert store.cursor(manifest.collection_id) == before_cursor
    assert store.partition_indices(manifest.collection_id) == before_indices


@pytest.mark.parametrize("point", [
    "after_cursor_read", "before_cursor_advance", "after_cursor_advance"])
def test_retrying_after_a_crash_admits_each_partition_exactly_once(
        workspace, point):
    store, manifest, spec, template = workspace

    def crash(reached: str) -> None:
        if reached == point:
            raise InjectedCrash(point)

    admitted = 0
    crashed_at: set[int] = set()
    while admitted < spec.total:
        # Crash once at each window offset, then let the retry through, so the
        # loop advances exactly as a repeatedly-restarted controller would.
        fault = None if admitted in crashed_at else crash
        try:
            result = store.admit_window(
                manifest.collection_id, spec, template, 20, fault=fault)
            admitted = result.next_index
        except InjectedCrash:
            crashed_at.add(admitted)
            continue          # a restarted controller simply tries again

    # Every window really did crash once before succeeding.
    assert crashed_at == {0, 20, 40, 60, 80}
    indices = store.partition_indices(manifest.collection_id)
    assert len(indices) == spec.total          # nothing multiplied
    assert list(indices) == list(range(spec.total))   # nothing skipped


def test_a_repeated_window_is_idempotent_rather_than_duplicating(workspace):
    store, manifest, spec, template = workspace
    store.admit_window(manifest.collection_id, spec, template, 40)
    # Rewind the cursor as a crash-after-upsert-before-commit would *not*, then
    # re-admit: the stable-key upserts must absorb the repeat.
    with store.connect() as connection:
        connection.execute(
            "UPDATE cursors SET next_index=0 WHERE collection_id=?",
            (manifest.collection_id,))
    store.admit_window(manifest.collection_id, spec, template, 40)
    indices = store.partition_indices(manifest.collection_id)
    assert len(indices) == 40 and len(set(indices)) == 40


def test_an_unknown_collection_is_refused(workspace):
    store, _manifest, spec, template = workspace
    with pytest.raises(KeyError):
        store.admit_window("f" * 64, spec, template, 10)


def test_a_collection_must_match_its_space_and_template(tmp_path):
    spec, template = _spec(), _template()
    store = PartitionStore(tmp_path / "p.sqlite3")
    wrong = CollectionManifest.bind(
        set_id=spec.set_id, template_id=template.template_id,
        expected=spec.total + 1, policy=CompletionPolicy.ALL)
    with pytest.raises(ValueError, match="manifest expects"):
        store.open_collection(wrong, spec, template)


# -- restart --------------------------------------------------------------


def test_a_restarted_controller_resumes_the_persisted_cursor(tmp_path):
    spec, template = _spec(), _template()
    manifest = _manifest(spec, template)
    policy = AdmissionPolicy(window_size=20, low_watermark=10,
                             high_watermark=40)

    first_store = PartitionStore(tmp_path / "p.sqlite3")
    first = BoundedAdmissionController(
        first_store, manifest, spec, template, policy=policy)
    first.top_up()
    first_store._record_outcomes_for_test_fixture(first.collection_id, [
        (key, MemberOutcome.COMMITTED) for key, _index
        in first_store.iter_admitted(first.collection_id, 40)])
    cursor = first_store.cursor(manifest.collection_id).next_index
    assert cursor == 40

    # A new store object over the same file is a fresh process.
    reopened = PartitionStore(tmp_path / "p.sqlite3")
    resumed = BoundedAdmissionController(
        reopened, manifest, spec, template, policy=policy)
    resumed.top_up()

    indices = [index for _key, index
               in reopened.iter_admitted(resumed.collection_id, 40)]
    assert min(indices) == cursor          # resumed exactly where it stopped
    assert reopened.cursor(manifest.collection_id).next_index == 80
    assert reopened.state(manifest.collection_id).committed == 40


def test_opening_a_collection_twice_does_not_reset_it(workspace):
    store, manifest, spec, template = workspace
    store.admit_window(manifest.collection_id, spec, template, 25)
    store.open_collection(manifest, spec, template)
    assert store.cursor(manifest.collection_id).next_index == 25


# -- watermarks and boundedness ------------------------------------------


def test_top_up_respects_the_high_watermark(workspace):
    store, manifest, spec, template = workspace
    policy = AdmissionPolicy(window_size=8, low_watermark=4, high_watermark=24)
    controller = BoundedAdmissionController(
        store, manifest, spec, template, policy=policy)
    result = controller.top_up()
    assert result.admitted == 24
    assert store.in_flight(manifest.collection_id) == 24
    # Already above the low watermark, so nothing more is admitted.
    assert controller.top_up().admitted == 0


def test_top_up_refills_only_after_falling_to_the_low_watermark(workspace):
    store, manifest, spec, template = workspace
    policy = AdmissionPolicy(window_size=8, low_watermark=4, high_watermark=16)
    controller = BoundedAdmissionController(
        store, manifest, spec, template, policy=policy)
    controller.top_up()
    # Commit down to the low watermark.
    for key, _index in list(store.iter_admitted(controller.collection_id, 12)):
        store._record_outcome_for_test_fixture(controller.collection_id, key,
                             MemberOutcome.COMMITTED)
    assert store.in_flight(controller.collection_id) == 4
    assert controller.top_up().admitted == 12


def test_the_whole_space_drains_within_the_watermark(tmp_path):
    spec = _spec(40, 40)                    # 1,600 partitions
    template = _template()
    manifest = _manifest(spec, template)
    store = PartitionStore(tmp_path / "p.sqlite3")
    policy = AdmissionPolicy(window_size=64, low_watermark=32,
                             high_watermark=128, max_packet_members=16,
                             target_packet_cost=16)
    controller = BoundedAdmissionController(
        store, manifest, spec, template, policy=policy)

    peak_in_flight = 0
    seen: set[int] = set()
    for batch in controller.drain():
        peak_in_flight = max(peak_in_flight,
                             store.in_flight(controller.collection_id))
        outcomes = []
        for packet in batch:
            for member in packet.members:
                seen.add(member.partition_index)
                outcomes.append(
                    (member.logical_task_key, MemberOutcome.COMMITTED))
        store._record_outcomes_for_test_fixture(controller.collection_id, outcomes)

    assert len(seen) == spec.total          # every partition, exactly once
    assert peak_in_flight <= policy.high_watermark
    assert controller.is_complete()


def test_a_committed_partition_is_never_un_committed(workspace):
    store, manifest, spec, template = workspace
    store.admit_window(manifest.collection_id, spec, template, 4)
    key, _index = next(iter(store.iter_admitted(manifest.collection_id, 1)))
    assert store._record_outcome_for_test_fixture(manifest.collection_id, key,
                                MemberOutcome.COMMITTED)
    # A duplicate or late packet result claiming failure must not win.
    assert not store._record_outcome_for_test_fixture(manifest.collection_id, key,
                                    MemberOutcome.FAILED)
    assert store.state(manifest.collection_id).committed == 1
    assert store.state(manifest.collection_id).failed == 0


def test_a_partial_collection_is_not_complete(workspace):
    store, manifest, spec, template = workspace
    store.admit_window(manifest.collection_id, spec, template, spec.total)
    keys = [key for key, _ in store.iter_admitted(
        manifest.collection_id, spec.total)]
    store._record_outcomes_for_test_fixture(manifest.collection_id, [
        (key, MemberOutcome.COMMITTED) for key in keys[:-1]])
    store._record_outcome_for_test_fixture(
        manifest.collection_id, keys[-1], MemberOutcome.FAILED)
    state = store.state(manifest.collection_id)
    assert state.committed == spec.total - 1
    assert not state.satisfies(manifest)


# -- the demonstration ----------------------------------------------------


def test_the_demo_executes_ten_thousand_partitions_bounded():
    from stage7.demo import run_demo
    with tempfile.TemporaryDirectory(prefix="stage7-demo-") as root:
        result = run_demo(Path(root))

    execution = result["bounded_execution"]
    assert execution["partitions"] == 10_000
    assert execution["committed"] == 10_000
    assert execution["within_watermark"] is True
    assert execution["peak_in_flight"] <= execution["high_watermark"]
    assert execution["is_complete"] is True

    memory = result["memory_scaling"]
    # 10x the partitions must not cost anything like 10x the memory.
    assert memory["saturated_partition_growth"] == 10
    assert memory["memory_grew_far_less_than_partitions"] is True

    restart = result["restart"]
    assert restart["resumed_at_persisted_cursor"] is True
    assert restart["no_partition_re_admitted"] is True

    crash = result["crash_atomicity"]
    assert crash["crash_injected"] is True
    assert crash["cursor_unchanged_by_crash"] is True
    assert crash["no_duplicate_partitions"] is True
    assert crash["no_skipped_partitions"] is True

    assert result["partial_failure"][
        "committed_member_survives_duplicate_failure"] is True
    assert result["partial_is_not_complete"]["satisfies_all_policy"] is False


def test_one_selection_is_reused_by_every_partition():
    from stage7 import fixtures as fx
    fixture = fx.make_stage7_fixture(tiles=20, windows=20)
    # The template carries exactly one resolved invocation, and every partition
    # key derives from it, so partitions cannot drift onto other producers.
    assert fixture.template.invocation_key == fixture.invocation_key
    keys = {fixture.template.logical_task_key(fixture.spec.key_at(index))
            for index in range(fixture.spec.total)}
    assert len(keys) == fixture.spec.total == 400


# -- foreign definitions and durable retry (audit findings) ---------------


def test_admission_refuses_a_foreign_partition_set_or_template(tmp_path):
    """The collection's registered definitions are authoritative.

    The audit registered one collection, admitted with a different template,
    and watched the cursor advance while the wrong task was stored permanently.
    """
    from stage7.fixtures import resolve_all_selected
    spec, template = _spec(), _template()
    store = PartitionStore(tmp_path / "p.sqlite3")
    manifest = _manifest(spec, template)
    store.open_collection(manifest, spec, template)

    foreign_template = PartitionTaskTemplate.bind(
        resolve_all_selected()[0], estimated_cost_units=7)
    assert foreign_template.template_id != template.template_id
    with pytest.raises(ValueError, match="registered against template"):
        store.admit_window(manifest.collection_id, spec, foreign_template, 4)

    foreign_spec = _spec(3, 3)
    with pytest.raises(ValueError, match="registered against partition set"):
        store.admit_window(manifest.collection_id, foreign_spec, template, 4)

    # Nothing was admitted and the cursor never moved.
    assert store.cursor(manifest.collection_id).next_index == 0
    assert store.partition_indices(manifest.collection_id) == ()


def _one_packet(store, controller):
    controller.top_up()
    return controller.next_packets(limit=4)[0]


def _registered_attempt(store, manifest, packet, number, fence, **kwargs):
    """Use the same durable pre-submission authority as a real executor."""
    return store.register_packet_attempt(
        manifest.collection_id,
        packet,
        fence_token=fence,
        runtime_run_id=f"fixture-run-{fence}",
        runtime_plan_id="f" * 64,
        expected_attempt_number=number,
        **kwargs,
    )


def test_a_failed_retry_safe_member_comes_back_for_another_attempt(tmp_path):
    from partitions import PacketAttempt, PacketResult
    spec, template = _spec(2, 2), _template()
    store = PartitionStore(tmp_path / "p.sqlite3")
    manifest = _manifest(spec, template)
    controller = BoundedAdmissionController(
        store, manifest, spec, template,
        policy=AdmissionPolicy(window_size=4, low_watermark=1,
                               high_watermark=4))
    packet = _one_packet(store, controller)
    failing = packet.logical_task_keys[0]

    attempt = _registered_attempt(
        store, manifest, packet, 1, "fence-1")
    result = PacketResult.bind(attempt, packet, tuple(
        (key, MemberOutcome.FAILED if key == failing else
         MemberOutcome.COMMITTED) for key in packet.logical_task_keys))
    decision = store._record_packet_result_for_test_fixture(
        manifest.collection_id, packet, attempt, result)

    assert decision.requeued == (failing,)
    assert decision.exhausted == ()
    # The retry claim is now actionable: it really is offered again.
    offered = [member.logical_task_key
               for item in controller.next_packets(limit=4)
               for member in item.members]
    assert failing in offered
    assert store.attempts_for(manifest.collection_id, failing) == (
        (1, "FAILED"),)


def test_retries_stop_at_the_attempt_ceiling(tmp_path):
    from partitions import PacketAttempt, PacketResult
    spec, template = _spec(2, 2), _template()
    store = PartitionStore(tmp_path / "p.sqlite3")
    manifest = _manifest(spec, template)
    controller = BoundedAdmissionController(
        store, manifest, spec, template,
        policy=AdmissionPolicy(window_size=4, low_watermark=1,
                               high_watermark=4))
    packet = _one_packet(store, controller)
    failing = packet.logical_task_keys[0]

    decision = None
    for number, packet_attempt_number in enumerate((1, 1, 2), start=1):
        if number > 1:
            packet = controller.next_packets(limit=4)[0]
            assert packet.logical_task_keys == (failing,)
        attempt = _registered_attempt(
            store, manifest, packet, packet_attempt_number,
            f"fence-{number}")
        result = PacketResult.bind(attempt, packet, tuple(
            (key, MemberOutcome.FAILED if key == failing else
             MemberOutcome.COMMITTED) for key in packet.logical_task_keys))
        decision = store._record_packet_result_for_test_fixture(
            manifest.collection_id, packet, attempt, result)

    assert decision.exhausted == (failing,)
    assert store.attempt_count(manifest.collection_id, failing) == 3
    assert store.attempts_for(manifest.collection_id, failing) == (
        (1, "FAILED"), (2, "FAILED"), (3, "FAILED"))
    offered = [member.logical_task_key
               for item in controller.next_packets(limit=4)
               for member in item.members]
    assert failing not in offered


def test_a_non_retry_safe_failure_is_terminal_immediately(tmp_path):
    from partitions import PacketAttempt, PacketResult
    spec, template = _spec(2, 2), _template(
        retry_policy=PartitionRetryPolicy(False, 1))
    store = PartitionStore(tmp_path / "p.sqlite3")
    manifest = _manifest(spec, template)
    controller = BoundedAdmissionController(
        store, manifest, spec, template,
        policy=AdmissionPolicy(window_size=4, low_watermark=1,
                               high_watermark=4))
    packet = _one_packet(store, controller)
    failing = packet.logical_task_keys[0]

    attempt = _registered_attempt(
        store, manifest, packet, 1, "fence-1")
    result = PacketResult.bind(attempt, packet, tuple(
        (key, MemberOutcome.FAILED if key == failing else
         MemberOutcome.COMMITTED) for key in packet.logical_task_keys))
    decision = store._record_packet_result_for_test_fixture(
        manifest.collection_id, packet, attempt, result)

    # Re-running a non-idempotent operation is a human decision, not a default.
    assert decision.requeued == ()
    assert decision.exhausted == (failing,)


def test_caller_cannot_widen_registered_retry_safety(tmp_path):
    """Result ingestion derives policy from immutable collection state."""
    from partitions import PacketAttempt, PacketResult
    spec = _spec(2, 2)
    template = _template(
        retry_policy=PartitionRetryPolicy(False, 1))
    store = PartitionStore(tmp_path / "p.sqlite3")
    manifest = _manifest(spec, template)
    controller = BoundedAdmissionController(
        store, manifest, spec, template,
        policy=AdmissionPolicy(window_size=4, low_watermark=1,
                               high_watermark=4))
    packet = _one_packet(store, controller)
    attempt = _registered_attempt(
        store, manifest, packet, 1, "unsafe-widen")
    failed = PacketResult.bind(attempt, packet, tuple(
        (key, MemberOutcome.FAILED) for key in packet.logical_task_keys))

    decision = store._record_packet_result_for_test_fixture(
        manifest.collection_id, packet, attempt, failed,
        retry_safe=True, max_attempts=100)
    assert decision.requeued == ()
    assert set(decision.exhausted) == set(packet.logical_task_keys)
    assert controller.next_packets(limit=4) == ()


def test_caller_cannot_widen_registered_attempt_ceiling(tmp_path):
    from partitions import PacketAttempt, PacketResult
    spec = _spec(2, 2)
    template = _template(
        retry_policy=PartitionRetryPolicy(True, 2))
    store = PartitionStore(tmp_path / "p.sqlite3")
    manifest = _manifest(spec, template)
    controller = BoundedAdmissionController(
        store, manifest, spec, template,
        policy=AdmissionPolicy(window_size=4, low_watermark=1,
                               high_watermark=4))
    packet = _one_packet(store, controller)

    first = _registered_attempt(
        store, manifest, packet, 1, "ceiling-1")
    failed_first = PacketResult.bind(first, packet, tuple(
        (key, MemberOutcome.FAILED) for key in packet.logical_task_keys))
    first_decision = store._record_packet_result_for_test_fixture(
        manifest.collection_id, packet, first, failed_first,
        max_attempts=20)
    assert set(first_decision.requeued) == set(packet.logical_task_keys)

    second = _registered_attempt(
        store, manifest, packet, 2, "ceiling-2")
    failed_second = PacketResult.bind(second, packet, tuple(
        (key, MemberOutcome.FAILED) for key in packet.logical_task_keys))
    second_decision = store._record_packet_result_for_test_fixture(
        manifest.collection_id, packet, second, failed_second,
        max_attempts=20)
    assert second_decision.requeued == ()
    assert set(second_decision.exhausted) == set(packet.logical_task_keys)


def test_registered_retry_policy_tamper_fails_closed(tmp_path):
    """The store re-verifies its serialized template before each decision."""
    from partitions import PacketAttempt, PacketResult
    spec, template = _spec(2, 2), _template()
    store = PartitionStore(tmp_path / "p.sqlite3")
    manifest = _manifest(spec, template)
    controller = BoundedAdmissionController(
        store, manifest, spec, template,
        policy=AdmissionPolicy(window_size=4, low_watermark=1,
                               high_watermark=4))
    packet = _one_packet(store, controller)
    attempt = _registered_attempt(
        store, manifest, packet, 1, "tampered-policy")
    failed = PacketResult.bind(attempt, packet, tuple(
        (key, MemberOutcome.FAILED) for key in packet.logical_task_keys))

    forged = template.to_dict()
    forged["retry_policy"]["max_attempts"] = 300
    with store.connect() as connection:
        connection.execute(
            "UPDATE collections SET template_json=? WHERE collection_id=?",
            (json.dumps(forged, sort_keys=True, separators=(",", ":")),
             manifest.collection_id))

    with pytest.raises(ValueError, match="identity does not verify"):
        store._record_packet_result_for_test_fixture(
            manifest.collection_id, packet, attempt, failed)
    assert all(store.attempt_count(manifest.collection_id, key) == 0
               for key in packet.logical_task_keys)


def test_committed_members_cannot_be_resubmitted(tmp_path):
    from partitions import (
        PacketAttemptAuthorityError,
        PacketResult,
    )
    spec, template = _spec(2, 2), _template()
    store = PartitionStore(tmp_path / "p.sqlite3")
    manifest = _manifest(spec, template)
    controller = BoundedAdmissionController(
        store, manifest, spec, template,
        policy=AdmissionPolicy(window_size=4, low_watermark=1,
                               high_watermark=4))
    packet = _one_packet(store, controller)

    first = _registered_attempt(
        store, manifest, packet, 1, "fence-1")
    store._record_packet_result_for_test_fixture(
        manifest.collection_id, packet, first,
        PacketResult.bind(first, packet, tuple(
            (key, MemberOutcome.COMMITTED)
            for key in packet.logical_task_keys)))
    with pytest.raises(PacketAttemptAuthorityError, match="currently admitted"):
        _registered_attempt(store, manifest, packet, 2, "stale-fence")
    assert store.state(manifest.collection_id).committed == len(
        packet.logical_task_keys)


def test_packet_result_replay_is_idempotent_and_conflicts_fail_closed(tmp_path):
    from partitions import (
        PacketAttempt, PacketResult, PacketResultConflictError,
    )
    spec, template = _spec(2, 2), _template()
    clock = [10.0]
    store = PartitionStore(tmp_path / "p.sqlite3", clock=lambda: clock[0])
    manifest = _manifest(spec, template)
    controller = BoundedAdmissionController(
        store, manifest, spec, template,
        policy=AdmissionPolicy(window_size=4, low_watermark=1,
                               high_watermark=4))
    packet = _one_packet(store, controller)
    attempt = _registered_attempt(
        store, manifest, packet, 1, "stable-fence",
        lease_duration_s=1.0)
    failed = PacketResult.bind(attempt, packet, tuple(
        (key, MemberOutcome.FAILED) for key in packet.logical_task_keys))

    clock[0] = 10.5
    first = store._record_packet_result_for_test_fixture(
        manifest.collection_id, packet, attempt, failed)
    # A completed result remains replayable after the former lease horizon.
    clock[0] = 100.0
    replay = store._record_packet_result_for_test_fixture(
        manifest.collection_id, packet, attempt, failed)
    assert replay == first
    assert all(store.attempt_count(manifest.collection_id, key) == 1
               for key in packet.logical_task_keys)

    contradictory = PacketResult.bind(attempt, packet, tuple(
        (key, MemberOutcome.COMMITTED) for key in packet.logical_task_keys))
    with pytest.raises(PacketResultConflictError, match="different result"):
        store._record_packet_result_for_test_fixture(
            manifest.collection_id, packet, attempt, contradictory)
    assert store.state(manifest.collection_id).committed == 0


def test_not_attempted_does_not_consume_retry_budget(tmp_path):
    from partitions import PacketAttempt, PacketResult
    spec, template = _spec(2, 2), _template()
    store = PartitionStore(tmp_path / "p.sqlite3")
    manifest = _manifest(spec, template)
    controller = BoundedAdmissionController(
        store, manifest, spec, template,
        policy=AdmissionPolicy(window_size=4, low_watermark=1,
                               high_watermark=4))
    packet = _one_packet(store, controller)
    attempt = _registered_attempt(
        store, manifest, packet, 1, "never-launched")
    result = PacketResult.bind(attempt, packet, tuple(
        (key, MemberOutcome.NOT_ATTEMPTED)
        for key in packet.logical_task_keys))

    decision = store._record_packet_result_for_test_fixture(
        manifest.collection_id, packet, attempt, result)
    assert set(decision.requeued) == set(packet.logical_task_keys)
    assert decision.exhausted == ()
    for key in packet.logical_task_keys:
        assert store.attempt_count(manifest.collection_id, key) == 0
        assert store.attempts_for(manifest.collection_id, key) == ()

    # A provider that never began any member does not advance even the packet
    # execution sequence. A new fenced submission may still be attempt 1.
    retried = _registered_attempt(
        store, manifest, packet, 1, "actually-launched")
    failed = PacketResult.bind(retried, packet, tuple(
        (key, MemberOutcome.FAILED) for key in packet.logical_task_keys))
    store._record_packet_result_for_test_fixture(
        manifest.collection_id, packet, retried, failed)
    for key in packet.logical_task_keys:
        assert store.attempt_count(manifest.collection_id, key) == 1


def test_packet_identity_cannot_cross_collection_boundaries(tmp_path):
    from partitions import PacketAttempt, PacketResult
    spec, template = _spec(2, 2), _template()
    store = PartitionStore(tmp_path / "p.sqlite3")
    first = _manifest(spec, template)
    second = CollectionManifest.bind(
        set_id=spec.set_id, template_id=template.template_id,
        expected=spec.total, policy=CompletionPolicy.AT_LEAST,
        minimum_committed=1)
    first_controller = BoundedAdmissionController(
        store, first, spec, template,
        policy=AdmissionPolicy(window_size=4, low_watermark=1,
                               high_watermark=4))
    second_controller = BoundedAdmissionController(
        store, second, spec, template,
        policy=AdmissionPolicy(window_size=4, low_watermark=1,
                               high_watermark=4))
    first_packet = _one_packet(store, first_controller)
    second_controller.top_up()
    attempt = PacketAttempt.bind(first_packet, 1, fence_token="collection-a")
    result = PacketResult.bind(attempt, first_packet, tuple(
        (key, MemberOutcome.COMMITTED)
        for key in first_packet.logical_task_keys))

    with pytest.raises(ValueError, match="does not belong to this collection"):
        store._record_packet_result_for_test_fixture(
            second.collection_id, first_packet, attempt, result)
    assert store.state(second.collection_id).committed == 0


def test_packet_attempt_number_must_follow_the_durable_sequence(tmp_path):
    spec, template = _spec(2, 2), _template()
    store = PartitionStore(tmp_path / "p.sqlite3")
    manifest = _manifest(spec, template)
    controller = BoundedAdmissionController(
        store, manifest, spec, template,
        policy=AdmissionPolicy(window_size=4, low_watermark=1,
                               high_watermark=4))
    packet = _one_packet(store, controller)
    with pytest.raises(ValueError, match="expected 1, observed 99"):
        store.register_packet_attempt(
            manifest.collection_id,
            packet,
            fence_token="bad-sequence",
            runtime_run_id="fixture-bad-sequence",
            runtime_plan_id="f" * 64,
            expected_attempt_number=99,
        )
    for key in packet.logical_task_keys:
        assert store.attempt_count(manifest.collection_id, key) == 0


def test_a_self_minted_attempt_arriving_first_has_no_authority(tmp_path):
    """Frozen counterexample: a plausible hash is not a submission lease."""
    from partitions import (
        PacketAttempt, PacketAttemptAuthorityError, PacketResult,
    )
    spec, template = _spec(2, 2), _template()
    store = PartitionStore(tmp_path / "p.sqlite3")
    manifest = _manifest(spec, template)
    controller = BoundedAdmissionController(
        store, manifest, spec, template,
        policy=AdmissionPolicy(window_size=4, low_watermark=1,
                               high_watermark=4))
    packet = _one_packet(store, controller)
    minted = PacketAttempt.bind(packet, 1, fence_token="caller-invented")
    result = PacketResult.bind(minted, packet, tuple(
        (key, MemberOutcome.COMMITTED) for key in packet.logical_task_keys))

    with pytest.raises(PacketAttemptAuthorityError, match="never registered"):
        store._record_packet_result_for_test_fixture(
            manifest.collection_id, packet, minted, result)

    # The rejected delivery consumed neither packet sequence nor member budget.
    registered = _registered_attempt(
        store, manifest, packet, 1, "controller-issued")
    assert registered.attempt_number == 1
    for key in packet.logical_task_keys:
        assert store.attempt_count(manifest.collection_id, key) == 0


def test_a_live_lease_is_idempotent_but_fences_repacketised_members(tmp_path):
    from partitions import PacketAttemptConflictError, WorkPacket
    spec, template = _spec(2, 2), _template()
    clock = [10.0]
    store = PartitionStore(tmp_path / "p.sqlite3", clock=lambda: clock[0])
    manifest = _manifest(spec, template)
    controller = BoundedAdmissionController(
        store, manifest, spec, template,
        policy=AdmissionPolicy(window_size=4, low_watermark=1,
                               high_watermark=4))
    packet = _one_packet(store, controller)
    first = _registered_attempt(
        store, manifest, packet, 1, "provider-fence")
    clock[0] = 10.5
    replay = _registered_attempt(
        store, manifest, packet, 1, "provider-fence")
    assert replay == first

    with pytest.raises(PacketAttemptConflictError, match="unexpired"):
        _registered_attempt(
            store, manifest, packet, 1, "competing-fence")

    # A caller cannot evade the packet lease by regrouping the same member.
    subset = WorkPacket.bind(
        manifest.collection_id, template.template_id, (packet.members[0],))
    with pytest.raises(PacketAttemptConflictError, match="already belongs"):
        store.register_packet_attempt(
            manifest.collection_id, subset,
            fence_token="repacketised-fence",
            runtime_run_id="fixture-repacketised",
            runtime_plan_id="f" * 64)


def test_packet_members_are_reconstructed_not_trusted(tmp_path):
    """Frozen counterexamples for forged partition index and deployment."""
    from partitions import (
        PacketAttemptAuthorityError, PacketMember, WorkPacket,
    )
    spec, template = _spec(2, 2), _template()
    store = PartitionStore(tmp_path / "p.sqlite3")
    manifest = _manifest(spec, template)
    controller = BoundedAdmissionController(
        store, manifest, spec, template,
        policy=AdmissionPolicy(window_size=4, low_watermark=1,
                               high_watermark=4))
    packet = _one_packet(store, controller)
    member = packet.members[0]

    wrong_index = WorkPacket.bind(
        manifest.collection_id,
        template.template_id,
        (PacketMember(member.logical_task_key, member.partition_index + 1,
                      member.deployment_binding_id),),
    )
    with pytest.raises(PacketAttemptAuthorityError, match="claims partition"):
        store.register_packet_attempt(
            manifest.collection_id, wrong_index, fence_token="wrong-index",
            runtime_run_id="fixture-wrong-index",
            runtime_plan_id="f" * 64)

    wrong_binding = WorkPacket.bind(
        manifest.collection_id,
        template.template_id,
        (PacketMember(member.logical_task_key, member.partition_index,
                      "caller-supplied-deployment"),),
    )
    with pytest.raises(PacketAttemptAuthorityError, match="forged deployment"):
        store.register_packet_attempt(
            manifest.collection_id, wrong_binding, fence_token="wrong-binding",
            runtime_run_id="fixture-wrong-binding",
            runtime_plan_id="f" * 64)

    # Neither forged packet created authority for the legitimate packet.
    attempt = _registered_attempt(
        store, manifest, packet, 1, "legitimate-binding")
    assert attempt.attempt_number == 1


def test_an_expired_packet_lease_stays_fenced_pending_reconciliation(tmp_path):
    from partitions import (
        PacketAttemptAuthorityError,
        PacketAttemptConflictError,
        PacketResult,
    )
    spec, template = _spec(2, 2), _template()
    clock = [10.0]
    store = PartitionStore(tmp_path / "p.sqlite3", clock=lambda: clock[0])
    manifest = _manifest(spec, template)
    controller = BoundedAdmissionController(
        store, manifest, spec, template,
        policy=AdmissionPolicy(window_size=4, low_watermark=1,
                               high_watermark=4))
    packet = _one_packet(store, controller)
    expired = _registered_attempt(
        store, manifest, packet, 1, "old-worker",
        lease_duration_s=1.0)
    expired_result = PacketResult.bind(expired, packet, tuple(
        (key, MemberOutcome.COMMITTED) for key in packet.logical_task_keys))

    clock[0] = 11.0
    with pytest.raises(PacketAttemptAuthorityError, match="expired"):
        store._record_packet_result_for_test_fixture(
            manifest.collection_id, packet, expired, expired_result)

    with pytest.raises(PacketAttemptConflictError, match="reconciled"):
        _registered_attempt(
            store, manifest, packet, 1, "replacement-worker",
            lease_duration_s=10.0)
    assert all(store.attempt_count(manifest.collection_id, key) == 0
               for key in packet.logical_task_keys)


def test_expired_packet_releases_only_after_exact_run_proves_never_launched(
        tmp_path):
    from engine.runtime.state import RuntimeStore
    from engine.runtime.types import RunState
    from partitions import PacketAttemptConflictError, compile_packet

    spec, template = _spec(2, 2), _template()
    clock = [10.0]
    store = PartitionStore(tmp_path / "p.sqlite3", clock=lambda: clock[0])
    manifest = _manifest(spec, template)
    controller = BoundedAdmissionController(
        store, manifest, spec, template,
        policy=AdmissionPolicy(window_size=4, low_watermark=1,
                               high_watermark=4))
    packet = _one_packet(store, controller)
    graph = compile_packet(template, packet)
    runtime = RuntimeStore(tmp_path / "runtime" / "control" / "runtime.sqlite3")
    run_id = runtime.create_run(graph)
    expired = store.register_packet_attempt(
        manifest.collection_id, packet, fence_token="dead-before-submit",
        runtime_run_id=run_id, runtime_plan_id=graph.plan_id,
        lease_duration_s=1.0, expected_attempt_number=1)

    clock[0] = 11.0
    released = store.reconcile_expired_packet_not_launched(
        manifest.collection_id, packet, expired, runtime)
    assert released == packet.logical_task_keys
    assert runtime.run_state(run_id) is RunState.CANCELLED
    assert all(store.attempt_count(manifest.collection_id, key) == 0
               for key in packet.logical_task_keys)

    # No scientific attempt was consumed. A new exact run/fence reuses packet
    # sequence number 1 rather than being mislabeled as retry 2.
    replacement_run = runtime.create_run(graph)
    replacement = store.register_packet_attempt(
        manifest.collection_id, packet, fence_token="replacement",
        runtime_run_id=replacement_run, runtime_plan_id=graph.plan_id,
        lease_duration_s=10.0, expected_attempt_number=1)
    assert replacement.attempt_number == 1
    assert replacement.attempt_id != expired.attempt_id


def test_never_launched_reconciliation_refuses_while_a_controller_lock_is_live(
        tmp_path):
    from engine.runtime.state import ControllerLock, RuntimeStore
    from partitions import PacketAttemptConflictError, compile_packet

    spec, template = _spec(2, 2), _template()
    clock = [10.0]
    store = PartitionStore(tmp_path / "p.sqlite3", clock=lambda: clock[0])
    manifest = _manifest(spec, template)
    controller = BoundedAdmissionController(
        store, manifest, spec, template,
        policy=AdmissionPolicy(window_size=4, low_watermark=1,
                               high_watermark=4))
    packet = _one_packet(store, controller)
    graph = compile_packet(template, packet)
    runtime = RuntimeStore(tmp_path / "runtime" / "control" / "runtime.sqlite3")
    run_id = runtime.create_run(graph)
    expired = store.register_packet_attempt(
        manifest.collection_id, packet, fence_token="possibly-live",
        runtime_run_id=run_id, runtime_plan_id=graph.plan_id,
        lease_duration_s=1.0)
    clock[0] = 11.0

    lock = ControllerLock(runtime.db_path.parent / "controller.lock")
    try:
        with pytest.raises(RuntimeError, match="another WorkflowController"):
            store.reconcile_expired_packet_not_launched(
                manifest.collection_id, packet, expired, runtime)
    finally:
        lock.close()

    with pytest.raises(PacketAttemptConflictError, match="reconciled"):
        store.register_packet_attempt(
            manifest.collection_id, packet, fence_token="unsafe-replacement",
            runtime_run_id="unsafe", runtime_plan_id=graph.plan_id,
            lease_duration_s=10.0)


# -- real partition execution --------------------------------------------


def test_partitions_compile_into_real_stage_1_tasks(tmp_path):
    from partitions import compile_packet
    spec, template = _spec(2, 2), _template()
    store = PartitionStore(tmp_path / "p.sqlite3")
    manifest = _manifest(spec, template)
    controller = BoundedAdmissionController(
        store, manifest, spec, template,
        policy=AdmissionPolicy(window_size=4, low_watermark=1,
                               high_watermark=4))
    packet = _one_packet(store, controller)

    graph = compile_packet(template, packet)
    # One task per partition, and the runtime key *is* the partition's logical
    # key, so results map back without a side table.
    assert len(graph.tasks) == len(packet.members)
    assert {task.key for task in graph.tasks} == set(packet.logical_task_keys)
    assert all(task.component.operation_key == template.operation_key
               for task in graph.tasks)


def test_a_template_with_unbound_inputs_refuses_to_execute(tmp_path):
    """Refuse rather than invent inputs the acquisition bridge would supply."""
    from partitions import PartitionNotExecutable, compile_packet
    from stage7.fixtures import resolve_all_selected

    consuming = next(item for item in resolve_all_selected()
                     if item.input_uses)
    template = PartitionTaskTemplate.bind(consuming)
    spec = _spec(2, 2)
    store = PartitionStore(tmp_path / "p.sqlite3")
    manifest = CollectionManifest.bind(
        set_id=spec.set_id, template_id=template.template_id,
        expected=spec.total, policy=CompletionPolicy.ALL)
    controller = BoundedAdmissionController(
        store, manifest, spec, template,
        policy=AdmissionPolicy(window_size=4, low_watermark=1,
                               high_watermark=4))
    packet = _one_packet(store, controller)

    with pytest.raises(PartitionNotExecutable, match="inputs are already bound"):
        compile_packet(template, packet)


def test_partitions_execute_and_their_real_outcomes_drive_the_store(tmp_path):
    """A partition commit is now an execution, not an asserted state."""
    from engine.runtime import RunState
    from partitions import execute_packet

    spec, template = _spec(2, 2), _template()
    store = PartitionStore(tmp_path / "p.sqlite3")
    manifest = _manifest(spec, template)
    controller = BoundedAdmissionController(
        store, manifest, spec, template,
        policy=AdmissionPolicy(window_size=4, low_watermark=1,
                               high_watermark=4))
    packet = _one_packet(store, controller)

    decision, run_state = execute_packet(
        store, manifest.collection_id, template, packet,
        runtime_root=tmp_path / "runtime")

    assert run_state is RunState.SUCCEEDED
    assert set(decision.committed) == set(packet.logical_task_keys)
    assert decision.requeued == () and decision.exhausted == ()
    state = store.state(manifest.collection_id)
    assert state.committed == len(packet.members)
    assert state.satisfies(manifest)
    # Each partition has a durable attempt recording the real outcome.
    for key in packet.logical_task_keys:
        assert store.attempts_for(manifest.collection_id, key) == (
            (1, "COMMITTED"),)


def test_expired_packet_accepts_only_its_exact_terminal_runtime_result(tmp_path):
    """Lease expiry cannot erase a result already committed by the bound run."""
    from engine.runtime import RunState, WorkflowController
    from partitions import compile_packet

    spec, template = _spec(2, 2), _template()
    clock = [10.0]
    store = PartitionStore(tmp_path / "p.sqlite3", clock=lambda: clock[0])
    manifest = _manifest(spec, template)
    controller = BoundedAdmissionController(
        store, manifest, spec, template,
        policy=AdmissionPolicy(window_size=4, low_watermark=1,
                               high_watermark=4))
    packet = _one_packet(store, controller)

    with WorkflowController(tmp_path / "runtime") as runtime_controller:
        graph = compile_packet(template, packet)
        run_id = runtime_controller.create_run(graph)
        attempt = store.register_packet_attempt(
            manifest.collection_id, packet, fence_token="terminal-after-expiry",
            runtime_run_id=run_id, runtime_plan_id=graph.plan_id,
            lease_duration_s=1.0)
        assert runtime_controller.run_until_terminal(run_id) is RunState.SUCCEEDED
        clock[0] = 11.0
        decision = store.record_runtime_packet_completion(
            manifest.collection_id, packet, attempt,
            runtime_controller.store)

    assert set(decision.committed) == set(packet.logical_task_keys)
    with store.connect() as connection:
        intent = connection.execute(
            "SELECT status,reconciliation_kind FROM packet_attempt_intents "
            "WHERE collection_id=? AND attempt_id=?",
            (manifest.collection_id, attempt.attempt_id),
        ).fetchone()
    assert intent == ("COMPLETED", "RUNTIME_TERMINAL")


def test_a_packet_refuses_a_foreign_template(tmp_path):
    from partitions import compile_packet
    from stage7.fixtures import resolve_all_selected
    spec, template = _spec(2, 2), _template()
    store = PartitionStore(tmp_path / "p.sqlite3")
    manifest = _manifest(spec, template)
    controller = BoundedAdmissionController(
        store, manifest, spec, template,
        policy=AdmissionPolicy(window_size=4, low_watermark=1,
                               high_watermark=4))
    packet = _one_packet(store, controller)

    other = PartitionTaskTemplate.bind(
        resolve_all_selected()[0], estimated_cost_units=99)
    with pytest.raises(ValueError, match="does not belong to this template"):
        compile_packet(other, packet)
