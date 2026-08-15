"""Stage-5 binding: what may be pinned, and what happens when it moves.

Three refusals are the subject here.  An asset with no conditional identity is
``UNBINDABLE`` and may only be bootstrapped through quarantine.  A gap refuses
to bind at all.  And an asset that changed or vanished after binding produces
``BINDING_STALE`` plus a child plan recording the exclusion — never a silent
runtime substitution.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from acquisition import (
    AssetCandidate,
    AssetConditionalIdentity,
    AssetExtent,
    BindingRejectionCode,
    BindingStatus,
    ConditionalIdentityKind,
    FetchAuthorization,
    IngestionStatus,
    ManifestShardStore,
    MetadataQuery,
    SnapshotIngestionPlan,
    StaleReasonCode,
    bind_manifest,
    derive_exclusion_plan,
    ingest_snapshot,
    verify_binding,
)
from contracts import (
    ArtifactDescriptor,
    BBoxSupport,
    Missingness,
    MissingnessStatus,
    OriginClass,
    SampleSemantics,
    TemporalKind,
    TemporalSupport,
)

CRS = "EPSG:4326"
AXES = ("x", "y")
START = "2026-01-01T00:00:00Z"
END = "2026-01-01T02:00:00Z"


def _window() -> TemporalSupport:
    return TemporalSupport(
        TemporalKind.SERIES, start=START, end=END, cadence_s="3600",
        max_gap_s="0", sample_semantics=SampleSemantics.INSTANTANEOUS)


def _candidate(asset_id: str, bounds, *, etag: str | None = "v1",
               size: int = 10) -> AssetCandidate:
    identity = (None if etag is None else AssetConditionalIdentity(
        ConditionalIdentityKind.ETAG, etag))
    return AssetCandidate(
        asset_id=asset_id,
        locator=f"https://example.invalid/{asset_id}",
        extent=AssetExtent(BBoxSupport(CRS, AXES, bounds), _window()),
        byte_size=size,
        conditional_identity=identity,
    )


def _query() -> MetadataQuery:
    return MetadataQuery(
        source_id="src", concept_id="example.field.flow_speed",
        schema_version="example-field-v1", units="m.s-1",
        representation="application/json",
        spatial=BBoxSupport(CRS, AXES, ("0", "0", "4", "4")),
        temporal=_window())


@pytest.fixture()
def store(tmp_path: Path) -> ManifestShardStore:
    return ManifestShardStore(tmp_path / "shards")


def _bind(store, candidates, bounds=("0", "0", "4", "4")):
    return bind_manifest(
        candidates, query=_query(), store=store,
        target_spatial=BBoxSupport(CRS, AXES, bounds),
        target_temporal=_window())


def test_two_tiles_bind_into_one_exact_manifest(store):
    result = _bind(store, (_candidate("west", ("0", "0", "2", "4")),
                           _candidate("east", ("2", "0", "4", "4"))))
    assert result.ok
    bound = result.bound
    assert bound.asset_ids == ("east", "west")
    assert bound.coverage.complete
    assert bound.manifest.asset_count == 2
    assert bound.manifest.query_id == _query().query_id


def test_asset_without_conditional_identity_is_unbindable(store):
    result = _bind(store, (_candidate("west", ("0", "0", "2", "4")),
                           _candidate("east", ("2", "0", "4", "4"),
                                      etag=None)))
    # The bindable half still binds only if it covers the request on its own;
    # here it does not, so the whole binding fails and says why.
    assert not result.ok
    assert result.unbindable_asset_ids == ("east",)
    codes = {item.code for item in result.rejections}
    assert BindingRejectionCode.UNBINDABLE_NO_CONDITIONAL_IDENTITY in codes
    assert BindingRejectionCode.COVERAGE_GAP in codes


def test_unbindable_assets_are_named_even_when_binding_succeeds(store):
    result = _bind(store, (
        _candidate("whole", ("0", "0", "4", "4")),
        _candidate("unversioned", ("0", "0", "4", "4"), etag=None)))
    assert result.ok
    assert result.bound.asset_ids == ("whole",)
    # Discovery does not hide data that exists but cannot be pinned.
    assert result.unbindable_asset_ids == ("unversioned",)
    assert result.rejections[0].code is \
        BindingRejectionCode.UNBINDABLE_NO_CONDITIONAL_IDENTITY


def test_a_gap_refuses_to_bind(store):
    result = _bind(store, (_candidate("west", ("0", "0", "1.5", "4")),
                           _candidate("east", ("2", "0", "4", "4"))))
    assert not result.ok
    assert result.bound is None
    assert [item.code for item in result.rejections] == [
        BindingRejectionCode.COVERAGE_GAP]


def test_no_candidates_is_its_own_rejection(store):
    result = _bind(store, ())
    assert not result.ok
    assert result.rejections[0].code is BindingRejectionCode.NO_CANDIDATES


def test_payload_authorization_cannot_be_forged(store):
    result = _bind(store, (_candidate("whole", ("0", "0", "4", "4")),))
    authorization = result.bound.authorization()
    assert authorization.authorizes("src", "whole")
    assert not authorization.authorizes("src", "somewhere-else")
    assert not authorization.authorizes("other-source", "whole")
    with pytest.raises(PermissionError, match="minted from a bound manifest"):
        FetchAuthorization(object(), "a" * 64, "src", frozenset({"whole"}))


def test_fresh_binding_verifies_clean(store):
    result = _bind(store, (_candidate("whole", ("0", "0", "4", "4")),))
    verification = verify_binding(
        result.bound, store, {"whole": "v1"})
    assert verification.fresh
    assert verification.reasons == ()


def test_mutated_and_missing_assets_produce_binding_stale(store):
    result = _bind(store, (_candidate("west", ("0", "0", "2", "4")),
                           _candidate("east", ("2", "0", "4", "4"))))
    verification = verify_binding(
        result.bound, store, {"west": "v2", "east": None})
    assert verification.status is BindingStatus.BINDING_STALE
    assert {item.asset_id: item.code for item in verification.reasons} == {
        "east": StaleReasonCode.ASSET_MISSING,
        "west": StaleReasonCode.ASSET_MUTATED,
    }
    mutated = next(item for item in verification.reasons
                   if item.asset_id == "west")
    assert (mutated.expected, mutated.observed) == ("v1", "v2")


def test_exclusion_child_plan_records_rather_than_substitutes(store):
    result = _bind(store, (_candidate("west", ("0", "0", "2", "4")),
                           _candidate("east", ("2", "0", "4", "4"))))
    verification = verify_binding(result.bound, store,
                                  {"west": "v2", "east": "v1"})
    child = derive_exclusion_plan(result.bound, verification)

    assert child.parent_manifest_root == result.bound.manifest_root
    assert child.excluded_asset_ids == ("west",)
    # The child plan names what may not be used again.  It offers no
    # replacement, because choosing one is a planning decision.
    assert not hasattr(child, "replacement_asset_ids")
    assert child.child_plan_id == child.expected_id()

    with pytest.raises(ValueError, match="nothing to exclude"):
        derive_exclusion_plan(
            result.bound, verify_binding(result.bound, store,
                                         {"west": "v1", "east": "v1"}))


def test_quarantine_content_addresses_weakly_identified_bytes(store):
    candidates = (_candidate("loose", ("0", "0", "4", "4"), etag=None),)
    plan = SnapshotIngestionPlan.bind(
        source_id="src", candidates=candidates,
        reason="provider publishes no ETag, version, or checksum")
    payload = b'{"value": 1}'
    descriptor = ArtifactDescriptor(
        concept_id="example.field.flow_speed",
        schema_version="example-field-v1",
        representation="application/json",
        units="m.s-1",
        spatial_support=BBoxSupport(CRS, AXES, ("0", "0", "4", "4")),
        temporal_support=_window(),
        vertical_support=None, grid=None, native_resolution=None,
        origin=OriginClass.OBSERVATION,
        missingness=Missingness(MissingnessStatus.COMPLETE),
    )
    result = ingest_snapshot(
        plan, candidates, {"loose": payload}, descriptor,
        shard_store=store, artifact_id="a" * 64)

    assert result.status is IngestionStatus.COMMITTED
    # The bytes are now pinned by *our* observation, not by provider promises.
    assert result.leaf.manifest_root_sha256 == result.manifest.manifest_root
    stored = next(result.manifest.iter_assets(store))
    assert stored.conditional_identity.kind is \
        ConditionalIdentityKind.CHECKSUM_SHA256
    assert stored.conditional_identity.value == \
        hashlib.sha256(payload).hexdigest()


def test_quarantine_refuses_already_pinnable_assets():
    with pytest.raises(ValueError, match="weakly identified assets only"):
        SnapshotIngestionPlan.bind(
            source_id="src",
            candidates=(_candidate("pinnable", ("0", "0", "4", "4")),),
            reason="should not be quarantined")


def test_quarantine_without_payload_is_rejected(store):
    candidates = (_candidate("loose", ("0", "0", "4", "4"), etag=None),)
    plan = SnapshotIngestionPlan.bind(
        source_id="src", candidates=candidates, reason="unversioned source")
    descriptor = ArtifactDescriptor(
        concept_id="c", schema_version="v", representation="application/json",
        units="m.s-1",
        spatial_support=BBoxSupport(CRS, AXES, ("0", "0", "4", "4")),
        temporal_support=_window(), vertical_support=None, grid=None,
        native_resolution=None, origin=OriginClass.OBSERVATION,
        missingness=Missingness(MissingnessStatus.COMPLETE))
    result = ingest_snapshot(plan, candidates, {}, descriptor,
                             shard_store=store, artifact_id="a" * 64)
    assert result.status is IngestionStatus.REJECTED
    assert result.leaf is None
