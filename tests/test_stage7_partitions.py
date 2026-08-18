"""Stage-7 partition space, template identity, packets, and completeness.

The claims under test are that a partition space is addressed without being
materialised, that logical task identity is stable against deployment churn,
that fusion never merges identity, and that a partial result cannot pass as a
complete collection.
"""
from __future__ import annotations

import pytest

from partitions import (
    AxisKind,
    CollectionManifest,
    CollectionState,
    CompletionPolicy,
    MemberOutcome,
    PacketAttempt,
    PacketResult,
    PacketMember,
    PartitionAxis,
    PartitionKey,
    PartitionSetSpec,
    PartitionRetryPolicy,
    PartitionTaskTemplate,
    WorkPacket,
    fuse_members,
)


def _spec(tiles: int = 4, windows: int = 3) -> PartitionSetSpec:
    return PartitionSetSpec.bind((
        PartitionAxis("tile", AxisKind.SPATIAL,
                      tuple(f"t{i}" for i in range(tiles))),
        PartitionAxis("window", AxisKind.TEMPORAL,
                      tuple(f"w{i}" for i in range(windows))),
    ))


def _invocation(index: int = 0):
    """One of the genuinely selected invocations, by stable order."""
    from stage7.fixtures import resolve_all_selected
    return resolve_all_selected()[index]


def _template(index: int = 0, **overrides) -> PartitionTaskTemplate:
    base = dict(estimated_cost_units=1, retry_safe=True)
    if "retry_policy" in overrides:
        base.pop("retry_safe")
    base.update(overrides)
    return PartitionTaskTemplate.bind(_invocation(index), **base)


# -- the partition space --------------------------------------------------


def test_a_space_is_counted_arithmetically_not_by_listing():
    spec = _spec(100, 100)
    assert spec.total == 10_000
    # There is deliberately no method that returns every key.
    assert not hasattr(spec, "keys")
    assert not hasattr(spec, "all_keys")


def test_indices_decode_to_stable_ordered_coordinates():
    spec = _spec(4, 3)
    assert spec.key_at(0).label == "tile=t0/window=w0"
    # The last axis varies fastest, so adjacent indices are neighbours.
    assert spec.key_at(1).label == "tile=t0/window=w1"
    assert spec.key_at(3).label == "tile=t1/window=w0"
    assert spec.key_at(11).label == "tile=t3/window=w2"


def test_every_index_decodes_to_a_distinct_partition():
    spec = _spec(7, 5)
    labels = [spec.key_at(index).label for index in range(spec.total)]
    assert len(set(labels)) == spec.total


def test_out_of_range_indices_are_refused():
    spec = _spec(2, 2)
    with pytest.raises(IndexError):
        spec.key_at(4)
    with pytest.raises(IndexError):
        spec.key_at(-1)


def test_windows_are_bounded_and_start_where_asked():
    spec = _spec(100, 100)
    window = list(spec.iter_keys(9_990, 20))
    # Clamped at the end of the space rather than overrunning it.
    assert len(window) == 10
    assert window[0].index == 9_990
    assert window[-1].index == 9_999


def test_axis_order_is_part_of_identity():
    tile = PartitionAxis("tile", AxisKind.SPATIAL, ("t0", "t1"))
    window = PartitionAxis("window", AxisKind.TEMPORAL, ("w0", "w1"))
    assert (PartitionSetSpec.bind((tile, window)).set_id
            != PartitionSetSpec.bind((window, tile)).set_id)


def test_axes_reject_duplicates_and_emptiness():
    with pytest.raises(ValueError, match="repeats a label"):
        PartitionAxis("tile", AxisKind.SPATIAL, ("t0", "t0"))
    with pytest.raises(TypeError):
        PartitionAxis("tile", AxisKind.SPATIAL, ())
    with pytest.raises(ValueError, match="cannot repeat a name"):
        PartitionSetSpec.bind((
            PartitionAxis("tile", AxisKind.SPATIAL, ("t0",)),
            PartitionAxis("tile", AxisKind.TEMPORAL, ("w0",))))


def test_space_round_trips():
    spec = _spec()
    assert PartitionSetSpec.from_dict(spec.to_dict()) == spec
    key = spec.key_at(5)
    assert PartitionKey.from_dict(key.to_dict()) == key


# -- template identity ----------------------------------------------------


def test_one_template_gives_every_partition_a_distinct_stable_key():
    spec = _spec(10, 10)
    template = _template()
    keys = [template.logical_task_key(spec.key_at(index))
            for index in range(spec.total)]
    assert len(set(keys)) == spec.total
    # Stable across recomputation.
    assert keys[7] == template.logical_task_key(spec.key_at(7))


def test_the_same_partition_under_a_different_selection_is_a_different_task():
    spec = _spec()
    first = _template(0)
    second = _template(1)
    assert (first.logical_task_key(spec.key_at(0))
            != second.logical_task_key(spec.key_at(0)))


def test_logical_keys_ignore_deployment_and_cost_churn():
    """Revising a resource envelope must not rename unaffected logical tasks."""
    spec = _spec()
    cheap = _template(estimated_cost_units=1)
    dear = _template(estimated_cost_units=64)
    # Cost is part of the template's own identity...
    assert cheap.template_id != dear.template_id
    # ...but a logical task key carries no deployment binding or attempt.
    key = cheap.logical_task_key(spec.key_at(0))
    assert "binding" not in key and "attempt" not in key


def test_template_round_trips():
    template = _template()
    assert PartitionTaskTemplate.from_dict(template.to_dict()) == template


def test_retry_policy_is_typed_and_part_of_collection_identity():
    spec = _spec()
    three = _template(
        retry_policy=PartitionRetryPolicy(True, 3))
    five = _template(
        retry_policy=PartitionRetryPolicy(True, 5))

    assert three.retry_policy == PartitionRetryPolicy(True, 3)
    assert three.template_id != five.template_id
    first = CollectionManifest.bind(
        set_id=spec.set_id, template_id=three.template_id,
        expected=spec.total, policy=CompletionPolicy.ALL)
    second = CollectionManifest.bind(
        set_id=spec.set_id, template_id=five.template_id,
        expected=spec.total, policy=CompletionPolicy.ALL)
    assert first.collection_id != second.collection_id


def test_retry_policy_roundtrip_is_strict_and_tamper_evident():
    policy = PartitionRetryPolicy(True, 4)
    assert PartitionRetryPolicy.from_dict(policy.to_dict()) == policy
    template = _template(retry_policy=policy)
    forged = template.to_dict()
    forged["retry_policy"]["max_attempts"] = 40

    with pytest.raises(ValueError, match="identity does not verify"):
        PartitionTaskTemplate.from_dict(forged)
    with pytest.raises(ValueError, match="max_attempts=1"):
        PartitionRetryPolicy(False, 2)


# -- packets and fusion ---------------------------------------------------

_COLLECTION_ID = "c" * 64


def _members(count: int, template: PartitionTaskTemplate,
             spec: PartitionSetSpec) -> tuple[PacketMember, ...]:
    return tuple(
        PacketMember(template.logical_task_key(spec.key_at(index)), index,
                     "binding:example")
        for index in range(count))


def test_fusion_bundles_neighbours_without_merging_identity():
    spec = _spec(10, 10)
    template = _template()
    packets = fuse_members(_members(64, template, spec), _COLLECTION_ID,
                           template.template_id,
                           max_members=8, cost_per_member=1,
                           target_packet_cost=8)
    assert len(packets) == 8
    assert all(len(packet.members) == 8 for packet in packets)
    # Every partition keeps its own logical task key inside the bundle.
    everything = [key for packet in packets for key in packet.logical_task_keys]
    assert len(set(everything)) == 64
    # Members stay in partition order, so lineage is reconstructible.
    for packet in packets:
        indices = [item.partition_index for item in packet.members]
        assert indices == sorted(indices)


def test_work_already_large_enough_is_not_bundled():
    spec = _spec(10, 10)
    template = _template(estimated_cost_units=32)
    packets = fuse_members(_members(4, template, spec), _COLLECTION_ID,
                           template.template_id,
                           max_members=8, cost_per_member=32,
                           target_packet_cost=8)
    assert len(packets) == 4
    assert all(len(packet.members) == 1 for packet in packets)


def test_a_packet_refuses_duplicate_or_unordered_members():
    spec = _spec()
    template = _template()
    member = PacketMember(template.logical_task_key(spec.key_at(0)), 0, "b")
    with pytest.raises(ValueError, match="cannot repeat a logical task"):
        WorkPacket.bind(_COLLECTION_ID, template.template_id, (member, member))
    later = PacketMember(template.logical_task_key(spec.key_at(1)), 1, "b")
    with pytest.raises(ValueError, match="partition order"):
        WorkPacket.bind(_COLLECTION_ID, template.template_id, (later, member))


def test_a_partial_packet_keeps_committed_members_and_retries_the_rest():
    spec = _spec(10, 10)
    template = _template()
    packet = WorkPacket.bind(
        _COLLECTION_ID, template.template_id, _members(4, template, spec))
    keys = packet.logical_task_keys
    attempt = PacketAttempt.bind(packet, 1, fence_token="fence-1")
    result = PacketResult.bind(attempt, packet, (
        (keys[0], MemberOutcome.COMMITTED),
        (keys[1], MemberOutcome.FAILED),
        (keys[2], MemberOutcome.COMMITTED),
        (keys[3], MemberOutcome.NOT_ATTEMPTED)))

    assert result.committed_keys() == tuple(sorted((keys[0], keys[2])))
    # Only the uncommitted members come back, and committed work is never
    # recomputed because one sibling in the bundle failed.
    assert result.retryable_keys(retry_safe=True) == tuple(
        sorted((keys[1], keys[3])))
    assert set(result.retryable_keys(retry_safe=True)).isdisjoint(
        result.committed_keys())


def test_a_non_retry_safe_template_retries_nothing_automatically():
    spec = _spec(10, 10)
    template = _template(retry_safe=False)
    packet = WorkPacket.bind(
        _COLLECTION_ID, template.template_id, _members(2, template, spec))
    attempt = PacketAttempt.bind(packet, 1, fence_token="fence-1")
    result = PacketResult.bind(attempt, packet, tuple(
        (key, MemberOutcome.FAILED) for key in packet.logical_task_keys))
    assert result.retryable_keys(retry_safe=False) == ()


def test_an_attempt_must_report_exactly_its_packet():
    spec = _spec(10, 10)
    template = _template()
    packet = WorkPacket.bind(
        _COLLECTION_ID, template.template_id, _members(2, template, spec))
    attempt = PacketAttempt.bind(packet, 1, fence_token="fence-1")
    with pytest.raises(ValueError, match="exactly its packet's members"):
        PacketResult.bind(attempt, packet, (
            (packet.logical_task_keys[0], MemberOutcome.COMMITTED),))


def test_packet_records_round_trip():
    spec = _spec(10, 10)
    template = _template()
    packet = WorkPacket.bind(
        _COLLECTION_ID, template.template_id, _members(3, template, spec))
    assert WorkPacket.from_dict(packet.to_dict()) == packet
    attempt = PacketAttempt.bind(packet, 1, fence_token="fence-1")
    assert PacketAttempt.from_dict(attempt.to_dict()) == attempt
    result = PacketResult.bind(attempt, packet, tuple(
        (key, MemberOutcome.COMMITTED) for key in packet.logical_task_keys))
    assert PacketResult.from_dict(result.to_dict()) == result

    # A relabelled outcome must not survive deserialisation: the audit found
    # FAILED -> COMMITTED was accepted when identity was not re-verified.
    forged = result.to_dict()
    forged["outcomes"] = [[key, "COMMITTED"] for key, _ in result.outcomes]
    if forged["outcomes"] != [[key, outcome.value]
                              for key, outcome in result.outcomes]:
        with pytest.raises(ValueError, match="identity does not verify"):
            PacketResult.from_dict(forged)


def test_packet_result_identity_is_independent_of_mapping_iteration_order():
    spec = _spec(10, 10)
    template = _template()
    packet = WorkPacket.bind(
        _COLLECTION_ID, template.template_id, _members(3, template, spec))
    attempt = PacketAttempt.bind(packet, 1, fence_token="fence-1")
    forward = tuple(
        (key, MemberOutcome.FAILED) for key in packet.logical_task_keys)
    reverse = tuple(reversed(forward))

    assert PacketResult.bind(attempt, packet, forward) == PacketResult.bind(
        attempt, packet, reverse)


# -- collection completeness ----------------------------------------------


def _manifest(expected: int = 100, **overrides) -> CollectionManifest:
    spec = _spec(10, 10)
    template = _template()
    base = dict(set_id=spec.set_id, template_id=template.template_id,
                expected=expected, policy=CompletionPolicy.ALL)
    base.update(overrides)
    return CollectionManifest.bind(**base)


def test_all_policy_needs_every_partition_and_tolerates_no_failure():
    manifest = _manifest(100)
    assert CollectionState(100, 100, 100, 0).satisfies(manifest)
    # One short is not complete.
    assert not CollectionState(100, 100, 99, 0).satisfies(manifest)
    # And one failure is not complete even if everything else committed.
    assert not CollectionState(100, 100, 99, 1).satisfies(manifest)


def test_at_least_policy_admits_a_shortfall_it_declared():
    manifest = _manifest(100, policy=CompletionPolicy.AT_LEAST,
                         minimum_committed=90)
    assert CollectionState(100, 100, 90, 10).satisfies(manifest)
    assert not CollectionState(100, 100, 89, 11).satisfies(manifest)


def test_fraction_policy_rounds_the_requirement_up():
    manifest = _manifest(100, policy=CompletionPolicy.FRACTION,
                         minimum_fraction="0.955")
    # 95.5 partitions required means 96, never 95.
    assert manifest.required_committed == 96
    assert not CollectionState(100, 100, 95, 5).satisfies(manifest)
    assert CollectionState(100, 100, 96, 4).satisfies(manifest)


def test_policies_reject_thresholds_they_do_not_use():
    with pytest.raises(ValueError, match="ALL takes no threshold"):
        _manifest(100, minimum_committed=50)
    with pytest.raises(ValueError, match="requires a minimum within"):
        _manifest(100, policy=CompletionPolicy.AT_LEAST, minimum_committed=200)
    with pytest.raises(ValueError, match="fraction must be in"):
        _manifest(100, policy=CompletionPolicy.FRACTION, minimum_fraction="0")


def test_state_cannot_describe_an_impossible_collection():
    with pytest.raises(ValueError, match="more partitions admitted"):
        CollectionState(10, 11, 0, 0)
    with pytest.raises(ValueError, match="more partitions resolved"):
        CollectionState(10, 5, 4, 2)
    with pytest.raises(ValueError, match="different partition space"):
        CollectionState(50, 50, 50, 0).satisfies(_manifest(100))


def test_manifest_round_trips():
    manifest = _manifest()
    assert CollectionManifest.from_dict(manifest.to_dict()) == manifest
