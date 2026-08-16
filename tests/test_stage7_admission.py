"""Stage-7 admission: atomicity under crash, restart, and bounded memory.

The exit gates under test are the sharp ones: cursor advancement and task
insertion are one transaction, so an injected crash can neither skip nor
multiply a logical partition; a restart resumes the persisted cursor; and
controller memory stays bounded by the watermark rather than by the size of
the partition space.
"""
from __future__ import annotations

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


def _template() -> PartitionTaskTemplate:
    # A real resolved invocation, not a restated one: the template must carry
    # the science the resolver actually chose.
    from stage7.fixtures import resolve_one_selection
    return PartitionTaskTemplate.bind(resolve_one_selection(), retry_safe=True)


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
    first_store.record_outcomes(first.collection_id, [
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
        store.record_outcome(controller.collection_id, key,
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
        store.record_outcomes(controller.collection_id, outcomes)

    assert len(seen) == spec.total          # every partition, exactly once
    assert peak_in_flight <= policy.high_watermark
    assert controller.is_complete()


def test_a_committed_partition_is_never_un_committed(workspace):
    store, manifest, spec, template = workspace
    store.admit_window(manifest.collection_id, spec, template, 4)
    key, _index = next(iter(store.iter_admitted(manifest.collection_id, 1)))
    assert store.record_outcome(manifest.collection_id, key,
                                MemberOutcome.COMMITTED)
    # A duplicate or late packet result claiming failure must not win.
    assert not store.record_outcome(manifest.collection_id, key,
                                    MemberOutcome.FAILED)
    assert store.state(manifest.collection_id).committed == 1
    assert store.state(manifest.collection_id).failed == 0


def test_a_partial_collection_is_not_complete(workspace):
    store, manifest, spec, template = workspace
    store.admit_window(manifest.collection_id, spec, template, spec.total)
    keys = [key for key, _ in store.iter_admitted(
        manifest.collection_id, spec.total)]
    store.record_outcomes(manifest.collection_id, [
        (key, MemberOutcome.COMMITTED) for key in keys[:-1]])
    store.record_outcome(manifest.collection_id, keys[-1], MemberOutcome.FAILED)
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
