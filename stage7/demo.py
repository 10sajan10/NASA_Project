"""Runnable Stage-7 demonstration: 10^4 partitions under a bounded window.

Five properties are demonstrated, each of which is an exit gate:

1. one resolved scientific selection drives every partition;
2. the whole 10,000-partition space executes while controller memory stays
   bounded by the admission window — and stays *flat* as the space grows;
3. a restarted controller resumes the persisted cursor;
4. a crash injected inside admission can neither skip nor multiply a logical
   partition; and
5. a partially failed packet keeps its committed members, and a partially
   complete collection is not complete.
"""
from __future__ import annotations

import tracemalloc
from pathlib import Path
from typing import Any

from partitions import (
    AdmissionPolicy,
    execute_packet,
    BoundedAdmissionController,
    CollectionManifest,
    CompletionPolicy,
    MemberOutcome,
    PacketAttempt,
    PacketResult,
    PartitionStore,
)

from . import fixtures as fx


class _InjectedCrash(RuntimeError):
    """Raised inside an admission transaction to simulate a controller death."""


def _run_to_completion(store: PartitionStore, fixture: fx.Stage7Fixture,
                       policy: AdmissionPolicy) -> dict[str, Any]:
    """Drive the whole space, committing every partition, bounded throughout."""
    controller = BoundedAdmissionController(
        store, fixture.manifest, fixture.spec, fixture.template, policy=policy)
    peak_in_flight = 0
    packets = 0
    committed = 0
    for batch in controller.drain():
        peak_in_flight = max(
            peak_in_flight, store.in_flight(controller.collection_id))
        outcomes: list[tuple[str, MemberOutcome]] = []
        for packet in batch:
            packets += 1
            attempt = PacketAttempt.bind(
                packet, 1, fence_token=f"fence-{packet.packet_id[:12]}")
            result = PacketResult.bind(
                attempt, packet,
                tuple((key, MemberOutcome.COMMITTED)
                      for key in packet.logical_task_keys))
            outcomes.extend(
                (key, MemberOutcome.COMMITTED)
                for key in result.committed_keys())
        # One flush per batch rather than one per partition.
        committed += store._record_outcomes_for_test_fixture(
            controller.collection_id, outcomes)
    state = controller.state()
    return {
        "packets": packets,
        "committed": committed,
        "peak_in_flight": peak_in_flight,
        "state": state.to_dict(),
        "is_complete": controller.is_complete(),
    }


def _memory_for_space(root: Path, tiles: int, windows: int,
                      policy: AdmissionPolicy) -> tuple[int, dict[str, Any]]:
    """Peak Python allocation while driving a space of the given size."""
    fixture = fx.make_stage7_fixture(tiles=tiles, windows=windows)
    store = PartitionStore(root / f"partitions-{tiles}x{windows}.sqlite3")
    tracemalloc.start()
    try:
        summary = _run_to_completion(store, fixture, policy)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    return peak, summary


def run_demo(runtime_root: Path | str) -> dict[str, Any]:
    root = Path(runtime_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    policy = AdmissionPolicy(window_size=256, low_watermark=128,
                            high_watermark=512, max_packet_members=32,
                            target_packet_cost=16)

    # -- 1 + 2: the full space, bounded, with a memory scaling curve ----
    # Tiles are held fixed and only the temporal axis grows, so partition count
    # rises 100x while declared axis labels rise ~1.8x.  If peak memory tracked
    # partition count this would show a 100x rise.
    peaks: dict[int, int] = {}
    full = None
    for windows in (1, 10, 100):
        peak, summary = _memory_for_space(root, fx.TILE_COUNT, windows, policy)
        peaks[fx.TILE_COUNT * windows] = peak
        if windows == fx.WINDOW_COUNT:
            full = summary
    assert full is not None
    smallest = min(peaks)
    largest = max(peaks)

    fixture = fx.make_stage7_fixture()

    # -- 3: restart resumes the persisted cursor ------------------------
    restart_root = root / "restart.sqlite3"
    first_store = PartitionStore(restart_root)
    first = BoundedAdmissionController(
        first_store, fixture.manifest, fixture.spec, fixture.template,
        policy=policy)
    first.top_up()
    # Commit what was admitted so in-flight falls below the low watermark;
    # otherwise a correct controller admits nothing and resumption is untested.
    first_store._record_outcomes_for_test_fixture(first.collection_id, [
        (key, MemberOutcome.COMMITTED)
        for key, _index in first_store.iter_admitted(
            first.collection_id, policy.high_watermark)])
    cursor_before = first_store.cursor(fixture.manifest.collection_id)
    admitted_before = first_store.state(fixture.manifest.collection_id).admitted

    # A brand new store object over the same file is a fresh process.
    reopened = PartitionStore(restart_root)
    resumed = BoundedAdmissionController(
        reopened, fixture.manifest, fixture.spec, fixture.template,
        policy=policy)
    resumed_result = resumed.top_up()
    cursor_after = reopened.cursor(fixture.manifest.collection_id)
    resumed_indices = [
        index for _key, index in reopened.iter_admitted(
            resumed.collection_id, policy.high_watermark)]

    # -- 4: a crash inside admission neither skips nor multiplies -------
    crash_root = root / "crash.sqlite3"
    crash_store = PartitionStore(crash_root)
    crash_controller = BoundedAdmissionController(
        crash_store, fixture.manifest, fixture.spec, fixture.template,
        policy=policy)
    crash_controller.top_up()
    before_crash = crash_store.cursor(fixture.manifest.collection_id)
    admitted_at_crash = crash_store.state(fixture.manifest.collection_id).admitted

    def crash_before_advance(point: str) -> None:
        if point == "before_cursor_advance":
            raise _InjectedCrash("controller died mid-window")

    crashed = False
    try:
        crash_store.admit_window(
            fixture.manifest.collection_id, fixture.spec, fixture.template,
            policy.window_size, fault=crash_before_advance)
    except _InjectedCrash:
        crashed = True
    after_crash = crash_store.cursor(fixture.manifest.collection_id)
    # Retry the same window cleanly and confirm it lands exactly once.
    recovered = crash_store.admit_window(
        fixture.manifest.collection_id, fixture.spec, fixture.template,
        policy.window_size)
    indices = crash_store.partition_indices(fixture.manifest.collection_id)

    # -- 5: partial failure and partial completeness --------------------
    partial_root = root / "partial.sqlite3"
    partial_store = PartitionStore(partial_root)
    partial = BoundedAdmissionController(
        partial_store, fixture.manifest, fixture.spec, fixture.template,
        policy=policy)
    partial.top_up()
    packet = partial.next_packets(limit=8)[0]
    keys = packet.logical_task_keys
    partial_attempt = PacketAttempt.bind(
        packet, 1, fence_token="fence-partial")
    mixed = PacketResult.bind(partial_attempt, packet, tuple(
        (key, MemberOutcome.COMMITTED if index % 2 == 0
         else MemberOutcome.FAILED)
        for index, key in enumerate(keys)))
    for key in mixed.committed_keys():
        partial_store._record_outcome_for_test_fixture(
            partial.collection_id, key, MemberOutcome.COMMITTED)
    for key in mixed.retryable_keys(retry_safe=fixture.template.retry_safe):
        partial_store._record_outcome_for_test_fixture(
            partial.collection_id, key, MemberOutcome.FAILED)
    partial_state = partial_store.state(partial.collection_id)

    # A committed member survives a later duplicate result claiming failure.
    survivor = mixed.committed_keys()[0]
    partial_store._record_outcome_for_test_fixture(
        partial.collection_id, survivor, MemberOutcome.FAILED)
    survived = partial_store.state(partial.collection_id).committed == \
        partial_state.committed

    # -- 6: partitions that actually execute ----------------------------
    executed = _execute_for_real(root, fixture, policy)

    return {
        "schema": "stage7-demo-result-v1",
        "real_execution": executed,
        "one_selection_reused": {
            "invocation_key": fixture.invocation_key,
            "selected_capability_id": fixture.selected_capability_id,
            "partitions_sharing_it": fixture.spec.total,
        },
        "bounded_execution": {
            "partitions": fixture.spec.total,
            "packets": full["packets"],
            "committed": full["committed"],
            "peak_in_flight": full["peak_in_flight"],
            "high_watermark": policy.high_watermark,
            "within_watermark": full["peak_in_flight"] <= policy.high_watermark,
            "is_complete": full["is_complete"],
        },
        "memory_scaling": {
            "note": (
                "peak Python allocation is bounded by the admission window and "
                "the declared axis labels, not by partition count. The 100 -> "
                "1000 step is window saturation: below the high watermark the "
                "window never fills, so that step is not an apples-to-apples "
                "comparison. The saturated 1000 -> 10000 step is."),
            "peak_bytes_by_partition_count": {
                str(count): peaks[count] for count in sorted(peaks)},
            "high_watermark": policy.high_watermark,
            "saturated_partition_growth": largest // 1000,
            "saturated_memory_growth": round(
                peaks[largest] / max(peaks[1000], 1), 2),
            "memory_grew_far_less_than_partitions": (
                peaks[largest] / max(peaks[1000], 1)
                < (largest / 1000) / 4),
        },
        "restart": {
            "cursor_before_restart": cursor_before.next_index,
            "admitted_before_restart": admitted_before,
            "cursor_after_resume": cursor_after.next_index,
            "resumed_windows": resumed_result.windows,
            "resumed_admitted": resumed_result.admitted,
            "first_partition_after_restart": (
                min(resumed_indices) if resumed_indices else None),
            "resumed_at_persisted_cursor": (
                bool(resumed_indices)
                and min(resumed_indices) == cursor_before.next_index),
            "no_partition_re_admitted": (
                bool(resumed_indices)
                and min(resumed_indices) >= cursor_before.next_index),
        },
        "crash_atomicity": {
            "crash_injected": crashed,
            "cursor_before": before_crash.next_index,
            "cursor_after_crash": after_crash.next_index,
            "cursor_unchanged_by_crash": (
                before_crash.next_index == after_crash.next_index),
            "admitted_unchanged_by_crash": (
                admitted_at_crash
                == crash_store.state(fixture.manifest.collection_id).admitted
                - recovered.admitted),
            "recovered_window": recovered.admitted,
            "no_duplicate_partitions": len(indices) == len(set(indices)),
            "no_skipped_partitions": list(indices) == list(range(len(indices))),
        },
        "partial_failure": {
            "packet_members": len(keys),
            "committed": len(mixed.committed_keys()),
            "retryable": len(mixed.retryable_keys(retry_safe=True)),
            "retryable_when_not_retry_safe": len(
                mixed.retryable_keys(retry_safe=False)),
            "committed_member_survives_duplicate_failure": survived,
        },
        "partial_is_not_complete": {
            "expected": partial_state.expected,
            "committed": partial_state.committed,
            "failed": partial_state.failed,
            "satisfies_all_policy": partial_state.satisfies(fixture.manifest),
        },
    }


def _execute_for_real(root: Path, fixture: fx.Stage7Fixture,
                      policy: AdmissionPolicy) -> dict[str, Any]:
    """Run a bounded slice of partitions as real Stage-1 tasks.

    The 10,000-partition figures above measure the control plane. This runs a
    small slice for real, so "committed" means a subprocess executed and an
    artifact was published rather than a row being set.
    """
    small = fx.make_stage7_fixture(tiles=2, windows=4)
    store = PartitionStore(root / "executed.sqlite3")
    controller = BoundedAdmissionController(
        store, small.manifest, small.spec, small.template, policy=policy)
    controller.top_up()
    packets = controller.next_packets(limit=8)
    committed = 0
    states = []
    for index, packet in enumerate(packets, start=1):
        decision, run_state = execute_packet(
            store, controller.collection_id, small.template, packet,
            runtime_root=root / f"runtime-{index}")
        committed += len(decision.committed)
        states.append(run_state.value)
    state = store.state(controller.collection_id)
    return {
        "partitions": small.spec.total,
        "packets_executed": len(packets),
        "run_states": states,
        "committed": committed,
        "collection_state": state.to_dict(),
        "collection_complete": state.satisfies(small.manifest),
        "capability_executed": small.template.capability_id,
        "operation_executed": small.template.operation_key,
    }


def collection_with_policy(fixture: fx.Stage7Fixture,
                           policy: CompletionPolicy, **thresholds
                           ) -> CollectionManifest:
    """Rebuild the fixture's manifest under a different completion policy."""
    return CollectionManifest.bind(
        set_id=fixture.spec.set_id, template_id=fixture.template.template_id,
        expected=fixture.spec.total, policy=policy, **thresholds)


__all__ = ["collection_with_policy", "run_demo"]
