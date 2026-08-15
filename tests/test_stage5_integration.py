"""Stage-5 end to end: metadata, binding, transfer, execution, commit.

The central claims are that an acquired remote artifact competes with a pinned
local one inside the same global selector, that no payload byte moves before a
manifest is bound, that a transient outage retries the same binding instead of
replanning, and that secrets never reach a serialized record.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from acquisition import (
    AcquisitionLimits,
    AcquisitionSearch,
    BindingStaleError,
    ManifestShardStore,
    PayloadFetcher,
    PayloadStore,
    PlanningSessionStore,
    ProviderQuota,
    lower_manifest_to_capability,
)
from acquisition.connector import EnvironmentSecretResolver
from contracts import OriginClass
from engine.runtime.identity import strict_canonical_json
from stage5 import fixtures as fx
from stage5.demo import build_demo_plan, run_demo


@pytest.fixture()
def workspace(tmp_path: Path):
    return (PlanningSessionStore(tmp_path / "planning.sqlite3"),
            ManifestShardStore(tmp_path / "shards"))


def _discover(workspace, connectors, session="s1"):
    session_store, shards = workspace
    return AcquisitionSearch(
        session_store, shards, connectors, limits=AcquisitionLimits()
    ).discover(session, fx.acquisition_requests(),
               second_order=(fx.support_rule(),))


def _coarse(expansion):
    return expansion.manifest_for(fx.remote_query().query_id)


# -- the vertical slice ---------------------------------------------------


def test_demo_selects_the_acquired_and_converted_path():
    with tempfile.TemporaryDirectory(prefix="stage5-demo-") as root:
        result = run_demo(Path(root))

    assert result["resolution_status"] == "READY"
    assert result["validation_passed"] is True
    assert result["globally_optimal"] is True
    # Acquisition (3) plus the declared conversion (2) beats the pinned
    # local artifact (8), and the selector had to prove coverage to get there.
    assert result["selected_cost_units"] == 5
    assert result["rejected_local_alternative_cost"] == fx.LOCAL_COST
    assert any(item.startswith("acquire:")
               for item in result["selected_capabilities"])
    assert "transform:example-mps-to-kmph" in result["selected_capabilities"]
    assert result["coverage_status"] == "COMPLETE"
    assert sorted(result["coarse_asset_ids"]) == [
        "remote-tile-east", "remote-tile-west"]
    assert result["run_state"] == "SUCCEEDED"
    assert result["task_count"] == 2
    assert result["attempt_count"] == 2
    assert result["result_matches_expected"] is True


def test_payload_transfer_begins_only_after_binding():
    with tempfile.TemporaryDirectory(prefix="stage5-order-") as root:
        result = run_demo(Path(root))
    # Planning asked two providers what exists and read none of it.
    assert result["bytes_transferred_during_planning"] == 0
    assert result["bytes_transferred_after_binding"] > 0
    assert result["binding_status"] == "FRESH"


def test_the_bound_derivation_carries_the_manifest_root():
    with tempfile.TemporaryDirectory(prefix="stage5-root-") as root:
        demo = build_demo_plan(Path(root) / "planning")
        result_root = demo.coarse_bound.manifest_root

    roots = {binding.runtime_parameters.get("manifest_root")
             for binding in demo.bound_plan.invocation_bindings}
    assert result_root in roots
    # Reading different bytes would change the parameters, hence the
    # invocation identity, hence the plan.
    encoded = strict_canonical_json(demo.bound_plan.to_dict())
    assert result_root in encoded


def test_second_order_discovery_runs_before_the_snapshot_freezes():
    with tempfile.TemporaryDirectory(prefix="stage5-rounds-") as root:
        demo = build_demo_plan(Path(root) / "planning")
    assert demo.acquisition.rounds == 2
    assert demo.acquisition.complete
    assert len(demo.acquisition.bound_manifests) == 3


def test_two_real_direct_alternatives_are_present_in_the_graph():
    with tempfile.TemporaryDirectory(prefix="stage5-alts-") as root:
        demo = build_demo_plan(Path(root) / "planning")
    acquired = sorted(
        item.capability_id for item in demo.catalog.capabilities
        if item.capability_id.startswith("acquire:"))
    # Local pinned, remote coarse, and the second-order support source.
    assert len(acquired) == 3
    assert any(fx.LOCAL_SOURCE_ID in item for item in acquired)
    assert any(fx.REMOTE_SOURCE_ID in item for item in acquired)
    # The losing model alternative is admissible, not absent.
    assert any(item.capability_id == "example-downscale-model"
               for item in demo.catalog.capabilities)


# -- transfer semantics ---------------------------------------------------


def test_a_transient_outage_retries_the_same_binding(workspace, tmp_path):
    session_store, shards = workspace
    remote = fx.make_remote_connector()
    expansion = _discover(workspace, (fx.make_local_connector(), remote))
    bound = _coarse(expansion)

    searches_before = remote.search_calls
    remote.transient_payloads = 2
    payloads = PayloadStore(tmp_path / "assets")
    receipt = PayloadFetcher(session_store, payloads,
                             quota=ProviderQuota()).fetch(bound, remote, shards)

    assert receipt.transient_retries == 2
    # The same binding was retried: identical manifest, identical asset set.
    assert receipt.manifest_root == bound.manifest_root
    assert set(receipt.asset_ids) == set(bound.asset_ids)
    # Retrying is not replanning, so no new metadata search ran.
    assert remote.search_calls == searches_before


def test_a_mutated_asset_ends_the_plan_with_binding_stale(workspace, tmp_path):
    session_store, shards = workspace
    remote = fx.make_remote_connector()
    bound = _coarse(_discover(workspace, (fx.make_local_connector(), remote)))

    remote.mutate("remote-tile-west", "etag-west-2")
    fetcher = PayloadFetcher(session_store, PayloadStore(tmp_path / "assets"),
                             quota=ProviderQuota())
    with pytest.raises(BindingStaleError) as excinfo:
        fetcher.fetch(bound, remote, shards)

    verification = excinfo.value.verification
    assert verification.status.value == "BINDING_STALE"
    assert [item.asset_id for item in verification.reasons] == [
        "remote-tile-west"]
    # No receipt was written, so nothing downstream can materialize.
    assert not PayloadStore(tmp_path / "assets").receipt_path(
        bound.manifest_root).exists()


def test_a_missing_asset_is_stale_rather_than_substituted(workspace, tmp_path):
    session_store, shards = workspace
    remote = fx.make_remote_connector()
    bound = _coarse(_discover(workspace, (fx.make_local_connector(), remote)))

    remote.remove("remote-tile-east")
    with pytest.raises(BindingStaleError) as excinfo:
        PayloadFetcher(session_store, PayloadStore(tmp_path / "assets"),
                       quota=ProviderQuota()).fetch(bound, remote, shards)
    reasons = excinfo.value.verification.reasons
    assert [item.code.value for item in reasons] == ["ASSET_MISSING"]
    # The still-present neighbour was never promoted to cover for it.
    assert bound.asset_ids == ("remote-tile-east", "remote-tile-west")


def test_transfer_is_idempotent_for_an_immutable_binding(workspace, tmp_path):
    session_store, shards = workspace
    remote = fx.make_remote_connector()
    bound = _coarse(_discover(workspace, (fx.make_local_connector(), remote)))
    payloads = PayloadStore(tmp_path / "assets")
    fetcher = PayloadFetcher(session_store, payloads, quota=ProviderQuota())

    first = fetcher.fetch(bound, remote, shards)
    moved = remote.bytes_transferred
    second = fetcher.fetch(bound, remote, shards)
    assert first == second
    assert remote.bytes_transferred == moved  # nothing re-transferred


def test_transfer_debits_the_same_provider_budget_as_planning(
        workspace, tmp_path):
    session_store, shards = workspace
    remote = fx.make_remote_connector()
    bound = _coarse(_discover(workspace, (fx.make_local_connector(), remote)))
    after_planning = session_store.quota_for(fx.REMOTE_SOURCE_ID)
    assert after_planning.metadata_calls > 0
    assert after_planning.payload_calls == 0

    PayloadFetcher(session_store, PayloadStore(tmp_path / "assets"),
                   quota=ProviderQuota()).fetch(bound, remote, shards)
    after_transfer = session_store.quota_for(fx.REMOTE_SOURCE_ID)
    assert after_transfer.metadata_calls == after_planning.metadata_calls
    assert after_transfer.payload_calls == len(bound.asset_ids)
    assert after_transfer.bytes_transferred > 0


def test_a_connector_refuses_bytes_without_an_authorization(workspace):
    _session_store, _shards = workspace
    remote = fx.make_remote_connector()
    with pytest.raises(PermissionError, match="minted authorization"):
        remote.open_payload(None, "remote-tile-west", "loc", None)


def test_a_connector_refuses_assets_outside_the_bound_manifest(
        workspace, tmp_path):
    _session_store, shards = workspace
    remote = fx.make_remote_connector()
    bound = _coarse(_discover(workspace, (fx.make_local_connector(), remote)))
    authorization = bound.authorization()
    asset = next(bound.manifest.iter_assets(shards))
    with pytest.raises(PermissionError, match="not covered by the bound"):
        remote.open_payload(authorization, "remote-tile-far",
                            "loc", asset.conditional_identity)


# -- secret hygiene -------------------------------------------------------


def test_secrets_are_referenced_and_never_serialized(workspace, monkeypatch):
    secret = "super-secret-archive-token"
    monkeypatch.setenv("STAGE5_ARCHIVE_TOKEN", secret)
    remote = fx.make_remote_connector()
    expansion = _discover(workspace, (fx.make_local_connector(), remote))

    assert remote.descriptor.credential_ref.reference == \
        "env:STAGE5_ARCHIVE_TOKEN"
    assert EnvironmentSecretResolver().resolve(
        remote.descriptor.credential_ref) == secret

    serialized = json.dumps([
        expansion.to_dict(),
        remote.descriptor.to_dict(),
        *[item.to_dict() for item in expansion.bound_manifests],
    ])
    assert secret not in serialized
    # The reference itself is fine to record; the value is what must not leak.
    assert "STAGE5_ARCHIVE_TOKEN" in json.dumps(remote.descriptor.to_dict())


def test_a_missing_credential_fails_loudly(monkeypatch):
    monkeypatch.delenv("STAGE5_ARCHIVE_TOKEN", raising=False)
    remote = fx.make_remote_connector()
    with pytest.raises(PermissionError, match="is not present"):
        EnvironmentSecretResolver().resolve(remote.descriptor.credential_ref)


# -- lowering guards ------------------------------------------------------


def test_a_manifest_cannot_be_relabelled_on_the_way_into_the_catalog(
        workspace, tmp_path):
    _session_store, shards = workspace
    bound = _coarse(_discover(workspace, (fx.make_local_connector(),
                                          fx.make_remote_connector())))
    honest = fx.descriptor(fx.FLOW_CONCEPT, fx.COARSE_UNITS,
                           ("-1", "-1", "5", "5"), OriginClass.OBSERVATION)
    lower_manifest_to_capability(
        bound, honest, fx.acquisition_profile(), cost_units=3)

    forged = fx.descriptor(fx.FLOW_CONCEPT, fx.TARGET_UNITS,
                           ("-1", "-1", "5", "5"), OriginClass.OBSERVATION)
    with pytest.raises(ValueError, match="does not match the bound query"):
        lower_manifest_to_capability(
            bound, forged, fx.acquisition_profile(), cost_units=3)

    wrong_concept = fx.descriptor(fx.SUPPORT_CONCEPT, fx.COARSE_UNITS,
                                  ("-1", "-1", "5", "5"),
                                  OriginClass.OBSERVATION)
    with pytest.raises(ValueError, match="does not match the bound query"):
        lower_manifest_to_capability(
            bound, wrong_concept, fx.acquisition_profile(), cost_units=3)


def test_acquisition_capability_must_use_the_closed_operation(workspace):
    bound = _coarse(_discover(workspace, (fx.make_local_connector(),
                                          fx.make_remote_connector())))
    honest = fx.descriptor(fx.FLOW_CONCEPT, fx.COARSE_UNITS,
                           ("-1", "-1", "5", "5"), OriginClass.OBSERVATION)
    with pytest.raises(ValueError, match="closed materialize operation"):
        lower_manifest_to_capability(
            bound, honest, fx.transform_profile(), cost_units=3)


def test_the_manifest_root_is_a_scientific_parameter(workspace):
    bound = _coarse(_discover(workspace, (fx.make_local_connector(),
                                          fx.make_remote_connector())))
    honest = fx.descriptor(fx.FLOW_CONCEPT, fx.COARSE_UNITS,
                           ("-1", "-1", "5", "5"), OriginClass.OBSERVATION)
    capability = lower_manifest_to_capability(
        bound, honest, fx.acquisition_profile(), cost_units=3)
    parameters = capability.parameterizations[0].parameters
    assert parameters["manifest_root"] == bound.manifest_root
    assert list(parameters["asset_ids"]) == list(bound.asset_ids)
