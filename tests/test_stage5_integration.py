"""Stage-5 end to end: metadata, binding, transfer, execution, commit.

The central claims are that an acquired remote artifact competes with a pinned
local one inside the same global selector, that no payload byte moves before a
manifest is bound, that a transient outage retries the same binding instead of
replanning, and that secrets never reach a serialized record.
"""
from __future__ import annotations

import dataclasses
import json
import tempfile
from pathlib import Path

import pytest

from acquisition import (
    AcquisitionDiscoveryReplay,
    AcquisitionLimits,
    AcquisitionSearch,
    BindingStaleError,
    FetchLeaseBusyError,
    FetchedContentBinding,
    ManifestShardStore,
    PayloadFetcher,
    PayloadStore,
    PlanningSessionStore,
    ProviderQuota,
    QuotaExceededError,
    lower_fetched_content_to_capability,
    lower_manifest_to_capability,
    verify_fetch_receipt,
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


def _fetch_content(workspace, tmp_path, connector, bound):
    session_store, shards = workspace
    payloads = PayloadStore(tmp_path / "assets")
    receipt = PayloadFetcher(
        session_store, payloads, quota=ProviderQuota()).fetch(
            bound, connector, shards)
    return FetchedContentBinding.bind(bound, receipt, payloads, shards)


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


def test_acquisition_replay_reconstructs_coverage_from_durable_rows():
    with tempfile.TemporaryDirectory(prefix="stage5-replay-") as raw_root:
        root = Path(raw_root)
        demo = build_demo_plan(root)
        forged_bound = dataclasses.replace(
            demo.coarse_bound,
            coverage=dataclasses.replace(
                demo.coarse_bound.coverage,
                detail="caller-authored false coverage detail"),
        )
        forged_manifests = tuple(
            forged_bound if item.manifest_root == forged_bound.manifest_root
            else item
            for item in demo.acquisition.bound_manifests
        )
        # The legacy expansion ID binds roots only. Independent durable replay,
        # rather than that self-hash, must reject the altered coverage claim.
        forged = dataclasses.replace(
            demo.acquisition, bound_manifests=forged_manifests)
        assert forged.expansion_id == demo.acquisition.expansion_id
        layer = next(
            item for item in demo.discovery_certificate.layers
            if item.layer_kind == "ACQUISITION_EXPANSION")

        with pytest.raises(ValueError, match="manifests disagree"):
            AcquisitionDiscoveryReplay(
                forged,
                demo.acquisition_base_catalog,
                PlanningSessionStore(root / "planning.sqlite3"),
                ManifestShardStore(root / "manifest-shards"),
            ).verify(demo.catalog, layer)


def test_every_compiled_stage5_field_uses_the_semantic_v2_commit_gate():
    """The canonical grid check is on the real executable path, not a helper."""
    with tempfile.TemporaryDirectory(prefix="stage5-field-gate-") as root:
        demo = build_demo_plan(Path(root))
    graph = demo.compilation.graph
    assert graph is not None
    validations = [
        output.validation
        for task in graph.tasks
        for output in task.outputs
    ]
    assert validations
    assert {value["kind"] for value in validations} == {"field_json_v2"}
    assert all(tuple(value["component_names"]) == ("speed",)
               for value in validations)
    assert all(value["grid_affine_convention"]
               == "sample-centres-axis-aligned-v1"
               for value in validations)


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

    roots = {
        binding.runtime_parameters.get("content_binding", {}).get(
            "manifest_root")
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


def test_resolver_certificate_binds_both_discovery_layers_to_its_catalog():
    with tempfile.TemporaryDirectory(prefix="stage5-certificate-") as root:
        demo = build_demo_plan(Path(root) / "planning")

    certificate = demo.discovery_certificate
    assert certificate.subject_catalog_id == demo.catalog.catalog_id
    assert certificate.complete
    assert certificate.certificate_id == certificate.expected_id()
    assert {item.layer_kind for item in certificate.layers} == {
        "ACQUISITION_EXPANSION", "TRANSFORMATION_EXPANSION"}
    assert {item.expansion_id for item in certificate.layers} == {
        demo.acquisition.expansion_id,
        next(item.snapshot_id for item in demo.bound_plan.scientific_snapshot_refs
             if item.name == "transformation_expansion"),
    }
    assert (demo.resolution.discovery_certificate.certificate_id
            == certificate.certificate_id)
    universe = demo.discovery_universe
    assert universe.universe_id == universe.expected_id()
    assert universe.base_catalog_id == certificate.base_catalog_id
    assert {item.layer_kind for item in universe.required_layers} == {
        "ACQUISITION_EXPANSION", "TRANSFORMATION_EXPANSION"}
    universe.verify_coverage(certificate)
    assert demo.resolution.discovery_universe == universe


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


def test_known_quota_is_reserved_before_the_first_payload_byte(
        workspace, tmp_path):
    session_store, shards = workspace
    remote = fx.make_remote_connector()
    bound = _coarse(_discover(workspace, (fx.make_local_connector(), remote)))
    payloads = PayloadStore(tmp_path / "assets")
    with pytest.raises(QuotaExceededError, match="byte"):
        PayloadFetcher(
            session_store,
            payloads,
            quota=ProviderQuota(max_bytes=bound.manifest.total_bytes - 1),
        ).fetch(bound, remote, shards)
    assert remote.payload_calls == 0
    assert remote.bytes_transferred == 0
    assert session_store.quota_for(bound.source_id).bytes_transferred == 0
    assert not payloads.receipt_path(bound.manifest_root).exists()


def test_mid_transfer_crash_cannot_reuse_quota_for_an_extra_provider_call(
        workspace, tmp_path):
    """A missing checkpoint is a newly charged attempt after restart."""
    session_store, shards = workspace
    remote = fx.make_remote_connector()
    bound = _coarse(_discover(workspace, (fx.make_local_connector(), remote)))
    payloads = PayloadStore(tmp_path / "assets")
    quota = ProviderQuota(
        max_payload_calls=len(bound.asset_ids),
        max_bytes=bound.manifest.total_bytes)

    def crash_after_second_blob(event):
        if event == "after_blob_persisted:remote-tile-west":
            raise RuntimeError("simulated process death before checkpoint")

    with pytest.raises(RuntimeError, match="simulated process death"):
        PayloadFetcher(
            session_store, payloads, quota=quota,
            failpoint=crash_after_second_blob,
        ).fetch(bound, remote, shards)

    assert remote.payload_calls == 2
    assert session_store.fetch_checkpoint(
        bound.manifest_root, "remote-tile-east").complete
    assert not session_store.fetch_checkpoint(
        bound.manifest_root, "remote-tile-west").complete

    # Reopening the process/store reuses the east checkpoint. Reopening west
    # would be a third provider call, so the tight two-call budget refuses it
    # before the connector is invoked.
    restarted = PlanningSessionStore(session_store.db_path)
    with pytest.raises(QuotaExceededError, match="payload call"):
        PayloadFetcher(restarted, payloads, quota=quota).fetch(
            bound, remote, shards)
    assert remote.payload_calls == 2
    assert restarted.quota_for(bound.source_id).payload_calls == 2


def test_restart_reuses_completed_assets_and_charges_only_the_missing_attempt(
        workspace, tmp_path):
    session_store, shards = workspace
    remote = fx.make_remote_connector()
    bound = _coarse(_discover(workspace, (fx.make_local_connector(), remote)))
    payloads = PayloadStore(tmp_path / "assets")
    west_size = next(
        item.byte_size for item in bound.manifest.iter_assets(shards)
        if item.asset_id == "remote-tile-west")
    quota = ProviderQuota(
        max_payload_calls=len(bound.asset_ids) + 1,
        max_bytes=bound.manifest.total_bytes + west_size)

    def crash_after_second_blob(event):
        if event == "after_blob_persisted:remote-tile-west":
            raise RuntimeError("simulated process death before checkpoint")

    with pytest.raises(RuntimeError):
        PayloadFetcher(
            session_store, payloads, quota=quota,
            failpoint=crash_after_second_blob,
        ).fetch(bound, remote, shards)
    receipt = PayloadFetcher(
        PlanningSessionStore(session_store.db_path), payloads, quota=quota,
    ).fetch(bound, remote, shards)

    assert receipt.asset_ids == bound.asset_ids
    assert receipt.transient_retries == 1
    assert remote.payload_calls == 3
    usage = session_store.quota_for(bound.source_id)
    assert usage.payload_calls == remote.payload_calls
    assert usage.bytes_transferred == quota.max_bytes


def test_a_live_same_node_fetch_owner_prevents_concurrent_transfer(
        workspace, tmp_path):
    session_store, shards = workspace
    remote = fx.make_remote_connector()
    bound = _coarse(_discover(workspace, (fx.make_local_connector(), remote)))
    payloads = PayloadStore(tmp_path / "assets")

    with payloads.fetch_lock(bound.manifest_root):
        with pytest.raises(FetchLeaseBusyError, match="active fetch owner"):
            PayloadFetcher(session_store, payloads).fetch(
                bound, remote, shards)
    assert remote.payload_calls == 0


def test_restart_closes_receipt_before_intent_ack_without_refetch(
        workspace, tmp_path):
    session_store, shards = workspace
    remote = fx.make_remote_connector()
    bound = _coarse(_discover(workspace, (fx.make_local_connector(), remote)))
    payloads = PayloadStore(tmp_path / "assets")

    def crash_after_receipt(event):
        if event == "after_receipt_persisted":
            raise RuntimeError("crash before transfer intent acknowledgement")

    with pytest.raises(RuntimeError, match="intent acknowledgement"):
        PayloadFetcher(
            session_store, payloads, failpoint=crash_after_receipt,
        ).fetch(bound, remote, shards)
    moved = remote.payload_calls

    receipt = PayloadFetcher(
        PlanningSessionStore(session_store.db_path), payloads,
    ).fetch(bound, remote, shards)
    assert receipt.asset_ids == bound.asset_ids
    assert remote.payload_calls == moved
    with session_store.connect() as connection:
        status = connection.execute(
            "SELECT status FROM fetch_transfer_intents WHERE manifest_root=?",
            (bound.manifest_root,),
        ).fetchone()[0]
    assert status == "COMPLETE"


def test_cached_receipt_replays_source_set_root_and_blob_checks(
        workspace, tmp_path):
    session_store, shards = workspace
    remote = fx.make_remote_connector()
    bound = _coarse(_discover(workspace, (fx.make_local_connector(), remote)))
    payloads = PayloadStore(tmp_path / "assets")
    fetcher = PayloadFetcher(session_store, payloads, quota=ProviderQuota())
    receipt = fetcher.fetch(bound, remote, shards)

    with pytest.raises(ValueError, match="source"):
        verify_fetch_receipt(
            bound, dataclasses.replace(receipt, source_id="foreign-source"),
            payloads, shards)
    partial = type(receipt).bind(
        receipt.manifest_root, receipt.source_id, receipt.assets[:-1],
        receipt.transient_retries)
    with pytest.raises(ValueError, match="asset order/set"):
        verify_fetch_receipt(bound, partial, payloads, shards)

    blob = payloads.path_for(receipt.assets[0].blob_sha256)
    payload = blob.read_bytes()
    blob.write_bytes(bytes([payload[0] ^ 1]) + payload[1:])
    with pytest.raises(ValueError, match="content check"):
        fetcher.fetch(bound, remote, shards)


def test_changed_bytes_change_content_and_capability_identity(tmp_path):
    def acquire(root: Path, remote):
        workspace = (
            PlanningSessionStore(root / "planning.sqlite3"),
            ManifestShardStore(root / "shards"),
        )
        bound = _coarse(_discover(
            workspace, (fx.make_local_connector(), remote)))
        content = _fetch_content(workspace, root, remote, bound)
        capability = lower_fetched_content_to_capability(
            content, fx.acquisition_profile(), cost_units=3)
        return bound, content, capability

    original_remote = fx.make_remote_connector()
    first_bound, first_content, first_capability = acquire(
        tmp_path / "first", original_remote)

    changed_remote = fx.make_remote_connector()
    original = changed_remote._entries["remote-tile-west"].payload
    changed = original.replace(b"10.0", b"11.0")
    assert changed != original and len(changed) == len(original)
    changed_remote.replace_payload_without_version(
        "remote-tile-west", changed)
    second_bound, second_content, second_capability = acquire(
        tmp_path / "second", changed_remote)

    # Metadata, ETags, sizes, query, and exact manifest are unchanged. Only
    # the fetched bytes differ, and that difference reaches producer identity.
    assert first_bound.manifest_root == second_bound.manifest_root
    assert first_content.content_root != second_content.content_root
    assert first_content.binding_id != second_content.binding_id
    assert first_capability.spec_id != second_capability.spec_id


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
    remote = fx.make_remote_connector()
    bound = _coarse(_discover(workspace, (fx.make_local_connector(), remote)))
    content = _fetch_content(workspace, tmp_path, remote, bound)
    lower_fetched_content_to_capability(
        content, fx.acquisition_profile(), cost_units=3)
    # Coverage proves the requested subset.  The post-fetch descriptor names
    # the complete, exact payload lattice, including deterministic tile
    # overhang, so commit validation can compare bytes to sample centres.
    assert content.descriptor.spatial_support.bounds == \
        fx.field_grid().support_bounds
    assert content.descriptor.grid == fx.field_grid()

    forged = fx.descriptor(fx.FLOW_CONCEPT, fx.TARGET_UNITS,
                           fx.field_grid().support_bounds,
                           OriginClass.OBSERVATION)
    with pytest.raises(TypeError, match="manifest/descriptor lowering"):
        lower_manifest_to_capability(
            bound, forged, fx.acquisition_profile(), cost_units=3)


def test_acquisition_capability_must_use_the_closed_operation(workspace):
    import tempfile
    remote = fx.make_remote_connector()
    bound = _coarse(_discover(workspace, (fx.make_local_connector(), remote)))
    with tempfile.TemporaryDirectory(prefix="stage5-profile-") as root:
        content = _fetch_content(workspace, Path(root), remote, bound)
    with pytest.raises(ValueError, match="closed materialize operation"):
        lower_fetched_content_to_capability(
            content, fx.transform_profile(), cost_units=3)


def test_exact_fetched_bytes_are_a_scientific_parameter(workspace, tmp_path):
    remote = fx.make_remote_connector()
    bound = _coarse(_discover(workspace, (fx.make_local_connector(), remote)))
    content = _fetch_content(workspace, tmp_path, remote, bound)
    capability = lower_fetched_content_to_capability(
        content, fx.acquisition_profile(), cost_units=3)
    parameters = capability.parameterizations[0].parameters
    serialized = parameters["content_binding"]
    assert serialized["manifest_root"] == bound.manifest_root
    assert serialized["binding_id"] == content.binding_id
    assert serialized["content_root"] == content.content_root
    assert [item["asset_id"] for item in serialized["assets"]] \
        == list(bound.asset_ids)
