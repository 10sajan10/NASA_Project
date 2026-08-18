"""Stage-5 discovery: the fixed point, restartability, and truncation.

The load-bearing claims are that discovery keeps going while producers keep
revealing new questions, that a restarted search resumes rather than repeats,
and that every bound which cuts discovery short is typed, persisted, and
surfaced through the *existing* upstream completeness channel.
"""
from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from acquisition import (
    AcquisitionLimitCode,
    AcquisitionLimits,
    AcquisitionRequest,
    AcquisitionSearch,
    ManifestShardStore,
    PlanningSessionStore,
    ProviderQuota,
    SecondOrderQuerySpec,
    second_order_rule,
    second_order_rule_keys,
)
from resolution import DiscoveryLayerCertificate, UpstreamCompleteness
from stage5 import fixtures as fx


@pytest.fixture()
def workspace(tmp_path: Path):
    return (PlanningSessionStore(tmp_path / "planning.sqlite3"),
            ManifestShardStore(tmp_path / "shards"))


def _search(workspace, connectors, **kwargs):
    session_store, shards = workspace
    return AcquisitionSearch(session_store, shards, connectors, **kwargs)


def test_discovery_reaches_a_fixed_point_over_two_rounds(workspace):
    local, remote = fx.make_local_connector(), fx.make_remote_connector()
    expansion = _search(workspace, (local, remote)).discover(
        "s1", fx.acquisition_requests(), second_order=(fx.support_rule(),))

    assert expansion.complete
    assert expansion.limit_reasons == ()
    # Round 1 asks the two declared questions; round 2 asks the one that only
    # round 1's answers could have produced.
    assert expansion.rounds == 2
    rounds = {item.query_id: item.round_index for item in expansion.outcomes}
    assert sorted(rounds.values()) == [1, 1, 2]
    assert len(expansion.bound_manifests) == 3


def test_second_order_query_uses_the_discovered_extent_not_the_request(
        workspace):
    remote = fx.make_remote_connector()
    expansion = _search(workspace, (remote,)).discover(
        "s1",
        (AcquisitionRequest(query=fx.remote_query(),
                            target_spatial=fx.target_bbox(),
                            target_temporal=fx.target_window()),),
        second_order=(fx.support_rule(),))

    support = next(item for item in expansion.bound_manifests
                   if item.query_payload["concept_id"] == fx.SUPPORT_CONCEPT)
    # The tiles overhang the request, so the follow-up asks about a strictly
    # larger region than anything derivable before round one.
    assert list(support.query_payload["spatial"]["bounds"]) == \
        ["-1.5", "-1", "4.5", "5"]
    assert fx.target_bbox().bounds == ("0", "0", "4", "4")


def test_no_payload_bytes_move_during_discovery(workspace):
    local, remote = fx.make_local_connector(), fx.make_remote_connector()
    _search(workspace, (local, remote)).discover(
        "s1", fx.acquisition_requests(), second_order=(fx.support_rule(),))
    assert local.bytes_transferred == 0
    assert remote.bytes_transferred == 0
    assert local.payload_calls == 0
    assert remote.payload_calls == 0


def test_query_schema_facts_must_be_authorized_before_provider_access(
        workspace):
    remote = fx.make_remote_connector()
    forged = dataclasses.replace(fx.remote_query(), units=fx.TARGET_UNITS)
    with pytest.raises(ValueError, match="frozen schema|source schema"):
        _search(workspace, (remote,)).discover(
            "s1", (AcquisitionRequest(
                query=forged,
                target_spatial=fx.target_bbox(),
                target_temporal=fx.target_window()),))
    assert remote.search_calls == 0
    assert remote.bytes_transferred == 0


def test_restart_resumes_from_the_persisted_cursor_without_erasing_truncation(
        tmp_path):
    session_store = PlanningSessionStore(tmp_path / "planning.sqlite3")
    shards = ManifestShardStore(tmp_path / "shards")
    request = (AcquisitionRequest(query=fx.remote_query(),
                                  target_spatial=fx.target_bbox(),
                                  target_temporal=fx.target_window()),)

    # Crash after page one is durably stored, before the second call returns.
    # The two-page bound is frozen from the start and remains unchanged.
    first_remote = fx.make_remote_connector(page_size=1)
    original_search = first_remote.search_metadata

    def crash_after_first_page(query, cursor, limit):
        if first_remote.search_calls:
            raise RuntimeError("simulated controller crash")
        return original_search(query, cursor, limit)

    first_remote.search_metadata = crash_after_first_page
    limits = AcquisitionLimits(max_pages_per_query=2)
    with pytest.raises(RuntimeError, match="simulated controller crash"):
        AcquisitionSearch(
            session_store, shards, (first_remote,), limits=limits,
        ).discover("s1", request)
    assert first_remote.search_calls == 1
    state = session_store.cursor_for("s1", fx.remote_query().query_id)
    assert state.cursor == "1" and state.pages_read == 1

    # A new process: new store object, new connector, same durable session.
    reopened = PlanningSessionStore(tmp_path / "planning.sqlite3")
    second_remote = fx.make_remote_connector(page_size=1)
    second = AcquisitionSearch(
        reopened, shards, (second_remote,), limits=limits).discover(
            "s1", request)

    assert second.complete
    assert not second.limit_codes
    # Only the *remaining* page was fetched; the first was not paid for twice.
    assert second_remote.search_calls == 1
    assert reopened.cursor_for("s1", fx.remote_query().query_id).exhausted
    outcome = second.outcomes[0]
    assert outcome.resumed is True
    assert outcome.pages_read == 2
    assert outcome.candidate_count == 2


def test_acquisition_scope_round_trip_and_identity_bind_the_exact_universe(
        workspace):
    from acquisition import AcquisitionScope, ProviderQuota

    request = AcquisitionRequest(
        query=fx.local_query(),
        target_spatial=fx.target_bbox(),
        target_temporal=fx.target_window(),
    )
    limits = AcquisitionLimits(max_pages_per_query=3)
    quota = ProviderQuota(max_metadata_calls=7, max_payload_calls=11,
                          max_bytes=4096)
    descriptor = fx.make_local_connector().descriptor
    scope = AcquisitionScope.bind(
        (request,), (), (descriptor,), limits, quota)

    assert AcquisitionScope.from_dict(scope.to_dict()) == scope
    assert scope.scope_id == scope.expected_id()

    changed = AcquisitionScope.bind(
        (request,), (), (descriptor,),
        AcquisitionLimits(max_pages_per_query=4), quota)
    assert changed.scope_id != scope.scope_id

    tampered = scope.to_dict()
    tampered["quota"]["max_bytes"] += 1
    with pytest.raises(ValueError, match="identity does not verify"):
        AcquisitionScope.from_dict(tampered)


def test_expansion_identity_cannot_conflate_different_empty_query_scopes(
        tmp_path):
    from acquisition import ProviderQuota

    limits = AcquisitionLimits(max_pages_per_query=1)
    quota = ProviderQuota()
    query = fx.remote_query()
    # Both searches activate the same page limit before they can bind a
    # complete manifest.  Their exact query scopes must still distinguish the
    # availability snapshots.
    def run(tag, *, bind):
        store = PlanningSessionStore(tmp_path / f"{tag}.sqlite3")
        shards = ManifestShardStore(tmp_path / f"{tag}-shards")
        connector = fx.make_remote_connector(page_size=1)
        return AcquisitionSearch(
            store, shards, (connector,), limits=limits, quota=quota,
        ).discover(
            "same-session-id",
            (AcquisitionRequest(
                query=query,
                target_spatial=fx.target_bbox(),
                target_temporal=fx.target_window(), bind=bind),),
        )

    first = run("first", bind=True)
    second = run("second", bind=False)
    assert first.scope.scope_id != second.scope.scope_id
    assert first.expansion_id != second.expansion_id


def test_an_exhausted_query_is_not_re_paginated_on_a_later_pass(tmp_path):
    session_store = PlanningSessionStore(tmp_path / "planning.sqlite3")
    shards = ManifestShardStore(tmp_path / "shards")
    request = (AcquisitionRequest(query=fx.local_query(),
                                  target_spatial=fx.target_bbox(),
                                  target_temporal=fx.target_window()),)
    AcquisitionSearch(session_store, shards, (fx.make_local_connector(),)
                      ).discover("s1", request)
    before = session_store.quota_for(fx.LOCAL_SOURCE_ID).metadata_calls

    again = fx.make_local_connector()
    AcquisitionSearch(session_store, shards, (again,)).discover("s1", request)
    assert again.search_calls == 0
    assert session_store.quota_for(fx.LOCAL_SOURCE_ID).metadata_calls == before


def test_exhausted_quota_truncates_discovery_with_a_typed_reason(workspace):
    remote = fx.make_remote_connector(page_size=1)
    expansion = _search(
        workspace, (remote,),
        quota=ProviderQuota(max_metadata_calls=1)).discover(
            "s1", (AcquisitionRequest(query=fx.remote_query(),
                                      target_spatial=fx.target_bbox(),
                                      target_temporal=fx.target_window()),))
    assert not expansion.complete
    assert AcquisitionLimitCode.PROVIDER_QUOTA.value in expansion.limit_codes


def test_a_transient_outage_cools_the_provider_and_marks_incompleteness(
        workspace):
    session_store, _shards = workspace
    remote = fx.make_remote_connector()
    remote.transient_searches = 1
    expansion = _search(workspace, (remote,)).discover(
        "s1", (AcquisitionRequest(query=fx.remote_query(),
                                  target_spatial=fx.target_bbox(),
                                  target_temporal=fx.target_window()),))
    assert not expansion.complete
    assert AcquisitionLimitCode.PROVIDER_COOLDOWN.value in expansion.limit_codes
    # A connector timeout means "we did not look", never "nothing is there".
    assert session_store.cooldown_for(fx.REMOTE_SOURCE_ID) is not None


def test_a_missing_connector_is_incompleteness_not_absence(workspace):
    expansion = _search(workspace, (fx.make_local_connector(),)).discover(
        "s1", (AcquisitionRequest(query=fx.remote_query(),
                                  target_spatial=fx.target_bbox(),
                                  target_temporal=fx.target_window()),))
    assert not expansion.complete
    assert AcquisitionLimitCode.CONNECTOR_UNAVAILABLE.value \
        in expansion.limit_codes


def test_round_cap_truncates_the_fixed_point(workspace):
    local, remote = fx.make_local_connector(), fx.make_remote_connector()
    expansion = _search(workspace, (local, remote),
                        limits=AcquisitionLimits(max_rounds=1)).discover(
        "s1", fx.acquisition_requests(), second_order=(fx.support_rule(),))
    assert expansion.rounds == 1
    assert not expansion.complete
    assert AcquisitionLimitCode.MAX_ROUNDS.value in expansion.limit_codes


def test_asset_cap_truncates_and_names_the_query(workspace):
    remote = fx.make_remote_connector(page_size=1)
    expansion = _search(
        workspace, (remote,),
        limits=AcquisitionLimits(max_assets_per_query=1)).discover(
            "s1", (AcquisitionRequest(query=fx.remote_query(),
                                      target_spatial=fx.target_bbox(),
                                      target_temporal=fx.target_window()),))
    assert not expansion.complete
    reason = next(item for item in expansion.limit_reasons
                  if item.code is AcquisitionLimitCode.MAX_ASSETS_PER_QUERY)
    assert reason.subject_ids == (fx.remote_query().query_id,)


def test_truncation_is_persisted_so_a_restart_cannot_forget_it(workspace):
    session_store, _shards = workspace
    remote = fx.make_remote_connector(page_size=1)
    _search(workspace, (remote,),
            limits=AcquisitionLimits(max_pages_per_query=1)).discover(
        "s1", (AcquisitionRequest(query=fx.remote_query(),
                                  target_spatial=fx.target_bbox(),
                                  target_temporal=fx.target_window()),))
    assert ("MAX_PAGES_PER_QUERY", fx.remote_query().query_id) \
        in session_store.limits_for("s1")


def test_a_restart_cannot_relabel_a_truncated_session_complete(workspace):
    """Later exhaustion does not erase an earlier omitted frontier."""
    session_store, _shards = workspace
    remote = fx.make_remote_connector(page_size=1)
    request = AcquisitionRequest(
        query=fx.remote_query(),
        target_spatial=fx.target_bbox(),
        target_temporal=fx.target_window(),
    )
    first = _search(
        workspace, (remote,),
        limits=AcquisitionLimits(max_pages_per_query=1),
    ).discover("s1", (request,))
    assert not first.complete

    # A fresh process may not widen the pre-provider durable scope and relabel
    # those prior pages as a complete search.
    calls_before = remote.search_calls
    with pytest.raises(ValueError, match="another acquisition scope"):
        _search(
            workspace, (remote,),
            limits=AcquisitionLimits(max_pages_per_query=50),
        ).discover("s1", (request,))
    assert remote.search_calls == calls_before
    assert session_store.snapshot_id_for("s1") is None


def test_expansion_completeness_and_reasons_cannot_disagree(workspace):
    expansion = _search(workspace, (fx.make_local_connector(),)).discover(
        "s1", (AcquisitionRequest(query=fx.local_query(),
                                  target_spatial=fx.target_bbox(),
                                  target_temporal=fx.target_window()),))
    assert expansion.complete is (not expansion.limit_reasons)
    assert expansion.expansion_id == expansion.expected_id()


def test_truncated_discovery_binds_identity_limits_and_reasons_in_a_layer(
        workspace):
    remote = fx.make_remote_connector(page_size=1)
    expansion = _search(workspace, (remote,),
                        limits=AcquisitionLimits(max_pages_per_query=1)
                        ).discover(
        "s1", (AcquisitionRequest(query=fx.remote_query(),
                                  target_spatial=fx.target_bbox(),
                                  target_temporal=fx.target_window()),))
    layer = expansion.discovery_layer()

    assert DiscoveryLayerCertificate.from_dict(layer.to_dict()) == layer
    assert layer.expansion_id == expansion.expansion_id
    assert layer.source_ids == (fx.REMOTE_SOURCE_ID,)
    assert layer.complete is False
    assert "MAX_PAGES_PER_QUERY" in layer.limit_codes
    assert {item.name for item in layer.limits} == {
        "connector_deadline_s",
        "max_assets_per_query",
        "max_pages_per_query",
        "max_queries",
        "max_rounds",
    }


def test_upstream_layers_merge_by_conjunction_and_union():
    acquisition = UpstreamCompleteness.from_layer(False, ("MAX_PAGES_PER_QUERY",))
    closure = UpstreamCompleteness.from_layer(False, ("MAX_DEPTH",))
    merged = UpstreamCompleteness.merge_all((acquisition, closure))
    assert merged.complete is False
    assert merged.limit_codes == ("MAX_DEPTH", "MAX_PAGES_PER_QUERY")

    whole = UpstreamCompleteness.merge_all(
        (UpstreamCompleteness.whole(), UpstreamCompleteness.whole()))
    assert whole.complete is True and whole.limit_codes == ()
    # One truncated layer is enough to end the claim for all of them.
    assert not UpstreamCompleteness.whole().merge(acquisition).complete


def test_inconsistent_upstream_pairs_are_refused():
    with pytest.raises(ValueError, match="cannot report limit codes"):
        UpstreamCompleteness(True, ("MAX_DEPTH",))
    with pytest.raises(ValueError, match="must name its limit codes"):
        UpstreamCompleteness(False, ())


def test_second_order_rules_are_a_closed_registry():
    assert second_order_rule_keys() == (
        "second_order.support_over_discovered_extent.v1",)
    with pytest.raises(KeyError, match="unknown closed Stage-5"):
        second_order_rule("module:function")
    with pytest.raises(KeyError):
        SecondOrderQuerySpec(
            rule_id="module:function",
            trigger_query_id="a" * 64, source_id="src", concept_id="c",
            schema_version="v", units="m", representation="application/json")


def test_discovery_needs_at_least_one_request(workspace):
    with pytest.raises(ValueError, match="at least one request"):
        _search(workspace, (fx.make_local_connector(),)).discover("s1", ())


def test_a_search_freezes_its_session(workspace):
    session_store, _shards = workspace
    expansion = _search(workspace, (fx.make_local_connector(),)).discover(
        "s1", (AcquisitionRequest(query=fx.local_query(),
                                  target_spatial=fx.target_bbox(),
                                  target_temporal=fx.target_window()),))
    assert session_store.is_frozen("s1")
    assert session_store.snapshot_id_for("s1") == expansion.expansion_id
