"""Stage-5 manifest identity, bounded streaming, and coverage refusal.

The claims under test are that a manifest pins an exact asset set by content,
that reading a large one does not cost the controller memory proportional to
its size, and that a hole in coverage is reported rather than smoothed over.
"""
from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from acquisition import (
    AssetConditionalIdentity,
    AssetExtent,
    AssetManifest,
    AssetRef,
    AssemblyMode,
    ConditionalIdentityKind,
    CoverageStatus,
    ManifestShard,
    ManifestShardStore,
    assess_coverage,
    expand_bbox,
)
from acquisition.manifest import bbox_union_covers, temporal_union_covers
from contracts import BBoxSupport, SampleSemantics, TemporalKind, TemporalSupport

CRS = "EPSG:4326"
AXES = ("x", "y")
START = "2026-01-01T00:00:00Z"
END = "2026-01-01T02:00:00Z"


@pytest.fixture()
def store(tmp_path: Path) -> ManifestShardStore:
    return ManifestShardStore(tmp_path / "shards")


def _window(start: str = START, end: str = END) -> TemporalSupport:
    return TemporalSupport(
        TemporalKind.SERIES, start=start, end=end, cadence_s="3600",
        max_gap_s="0", sample_semantics=SampleSemantics.INSTANTANEOUS)


def _asset(asset_id: str, bounds, *, source: str = "src",
           start: str = START, end: str = END, size: int = 10) -> AssetRef:
    return AssetRef(
        asset_id=asset_id,
        source_id=source,
        locator=f"https://example.invalid/{asset_id}",
        conditional_identity=AssetConditionalIdentity(
            ConditionalIdentityKind.ETAG, f"etag-{asset_id}"),
        extent=AssetExtent(BBoxSupport(CRS, AXES, bounds), _window(start, end)),
        byte_size=size,
    )


def test_manifest_root_pins_content_and_order(store):
    west = _asset("west", ("0", "0", "2", "4"))
    east = _asset("east", ("2", "0", "4", "4"))
    first = AssetManifest.freeze(
        source_id="src", query_id="a" * 64, assets=(west, east), store=store)
    same = AssetManifest.freeze(
        source_id="src", query_id="a" * 64, assets=(west, east), store=store)
    reordered = AssetManifest.freeze(
        source_id="src", query_id="a" * 64, assets=(east, west), store=store)

    assert first.manifest_root == same.manifest_root
    # Assembly order is scientifically meaningful, so it is part of identity.
    assert first.manifest_root != reordered.manifest_root
    assert first.asset_count == 2
    assert first.total_bytes == 20


def test_manifest_rejects_foreign_source_and_duplicates(store):
    west = _asset("west", ("0", "0", "2", "4"))
    foreign = _asset("other", ("2", "0", "4", "4"), source="elsewhere")
    with pytest.raises(ValueError, match="exactly one source"):
        AssetManifest.freeze(source_id="src", query_id="a" * 64,
                             assets=(west, foreign), store=store)
    with pytest.raises(ValueError, match="duplicate asset"):
        AssetManifest.freeze(source_id="src", query_id="a" * 64,
                             assets=(west, west), store=store)
    with pytest.raises(ValueError, match="empty manifest"):
        AssetManifest.freeze(source_id="src", query_id="a" * 64, assets=(),
                             store=store)


def test_manifest_streams_shards_without_retaining_assets(store):
    assets = tuple(
        _asset(f"tile-{index:05d}", ("0", "0", "2", "4"))
        for index in range(2_000))
    manifest = AssetManifest.freeze(
        source_id="src", query_id="a" * 64, assets=assets, store=store,
        shard_size=128)

    # The manifest itself holds only shard digests and counters: its resident
    # size tracks shard count, not asset count.
    assert len(manifest.shard_digests) == 2_000 // 128 + 1
    assert not hasattr(manifest, "assets")
    assert manifest.asset_count == 2_000

    peak = 0
    seen = 0
    for shard in manifest.iter_shards(store):
        peak = max(peak, len(shard.assets))
        seen += len(shard.assets)
    assert seen == 2_000
    assert peak <= 128  # never more than one shard resident at a time


def test_shard_store_is_content_addressed_and_verifies(store, tmp_path):
    shard = ManifestShard(0, (_asset("west", ("0", "0", "2", "4")),))
    digest = store.put(shard)
    assert store.put(shard) == digest  # idempotent
    assert store.get(digest) == shard
    with pytest.raises(KeyError):
        store.get("f" * 64)

    corrupted = store._path(digest)
    corrupted.write_text(
        corrupted.read_text().replace("west", "east"), encoding="utf-8")
    with pytest.raises(ValueError, match="digest check"):
        store.get(digest)


def test_manifest_round_trips_through_json(store):
    manifest = AssetManifest.freeze(
        source_id="src", query_id="a" * 64,
        assets=(_asset("west", ("0", "0", "2", "4")),), store=store)
    assert AssetManifest.from_dict(manifest.to_dict()) == manifest


def test_two_tiles_cover_a_request_but_a_hole_does_not():
    target = BBoxSupport(CRS, AXES, ("0", "0", "4", "4"))
    tiles = (_asset("west", ("0", "0", "2", "4")),
             _asset("east", ("2", "0", "4", "4")))
    assert bbox_union_covers(tiles, target)

    holed = (_asset("west", ("0", "0", "1.5", "4")),
             _asset("east", ("2", "0", "4", "4")))
    assert not bbox_union_covers(holed, target)


def test_partial_width_tiles_do_not_count_as_a_sweep():
    target = BBoxSupport(CRS, AXES, ("0", "0", "4", "4"))
    # Each tile spans only half the other axis, so no single-axis sweep closes.
    quarters = (_asset("sw", ("0", "0", "2", "2")),
                _asset("se", ("2", "0", "4", "2")))
    assert not bbox_union_covers(quarters, target)


def test_coverage_gap_is_reported_not_smoothed():
    target = BBoxSupport(CRS, AXES, ("0", "0", "4", "4"))
    assessment = assess_coverage(
        (_asset("west", ("0", "0", "1.5", "4")),
         _asset("east", ("2", "0", "4", "4"))),
        target_spatial=target, target_temporal=_window())
    assert assessment.status is CoverageStatus.SPATIAL_GAP
    assert not assessment.complete
    assert "hole" in assessment.detail
    assert assessment.selected_asset_ids == ()


def test_temporal_gap_is_reported():
    target = BBoxSupport(CRS, AXES, ("0", "0", "4", "4"))
    early = _asset("early", ("0", "0", "4", "4"),
                   start=START, end="2026-01-01T00:30:00Z")
    assessment = assess_coverage(
        (early,), target_spatial=target, target_temporal=_window())
    assert assessment.status is CoverageStatus.TEMPORAL_GAP


@pytest.mark.parametrize(
    ("temporal", "label"),
    (
        (TemporalSupport(
            TemporalKind.SERIES, start=START, end=END, cadence_s="1800",
            max_gap_s="0",
            sample_semantics=SampleSemantics.INSTANTANEOUS), "cadence"),
        (TemporalSupport(
            TemporalKind.SERIES, start=START, end=END, cadence_s="3600",
            anchor="2026-01-01T00:30:00Z", max_gap_s="0",
            sample_semantics=SampleSemantics.INSTANTANEOUS), "alignment"),
        (TemporalSupport(
            TemporalKind.SERIES, start=START, end=END, cadence_s="3600",
            max_gap_s="600",
            sample_semantics=SampleSemantics.INSTANTANEOUS), "maximum-gap"),
    ),
)
def test_coverage_jointly_enforces_timeline_contract(temporal, label):
    target = BBoxSupport(CRS, AXES, ("0", "0", "4", "4"))
    asset = _asset("whole", ("0", "0", "4", "4"))
    asset = dataclasses.replace(
        asset, extent=AssetExtent(asset.extent.spatial, temporal))
    assessment = assess_coverage(
        (asset,), target_spatial=target, target_temporal=_window())
    assert assessment.status is CoverageStatus.TEMPORAL_CONTRACT_MISMATCH
    assert label in assessment.detail


def test_covered_layout_is_rejected_when_runtime_cannot_assemble_it():
    target = BBoxSupport(CRS, AXES, ("0", "0", "4", "4"))
    horizontal_stripes = (
        _asset("north", ("0", "2", "4", "4")),
        _asset("south", ("0", "0", "4", "2")),
    )
    assessment = assess_coverage(
        horizontal_stripes,
        target_spatial=target,
        target_temporal=_window(),
        assembly_mode=AssemblyMode.FIELD_JSON_X_TILES_V1,
    )
    assert assessment.status is CoverageStatus.ASSEMBLY_UNSUPPORTED
    assert "cannot be assembled" in assessment.detail


def test_crs_mismatch_is_refused_rather_than_reprojected():
    target = BBoxSupport("EPSG:3857", AXES, ("0", "0", "4", "4"))
    assessment = assess_coverage(
        (_asset("west", ("0", "0", "4", "4")),),
        target_spatial=target, target_temporal=_window())
    assert assessment.status is CoverageStatus.CRS_MISMATCH
    assert "does not reproject" in assessment.detail


def test_no_intersecting_asset_is_distinct_from_a_gap():
    target = BBoxSupport(CRS, AXES, ("0", "0", "4", "4"))
    assessment = assess_coverage(
        (_asset("far", ("10", "10", "12", "12")),),
        target_spatial=target, target_temporal=_window())
    assert assessment.status is CoverageStatus.NO_ASSETS


def test_halo_admits_neighbours_and_reports_them_separately():
    target = BBoxSupport(CRS, AXES, ("1", "1", "3", "3"))
    # The halo-expanded target is (-0.5, -0.5) .. (4.5, 4.5), and the assets
    # must actually cover all of it -- a requested halo is mandatory input
    # coverage, not a hint.
    core = _asset("core", ("-1", "-1", "4", "5"))
    neighbour = _asset("halo", ("4", "-1", "6", "5"))
    assessment = assess_coverage(
        (core, neighbour), target_spatial=target, target_temporal=_window(),
        halo="1.5")
    assert assessment.complete
    assert set(assessment.selected_asset_ids) == {"core", "halo"}
    # The neighbour supports the edges; it is not part of the answer itself.
    assert assessment.halo_asset_ids == ("halo",)


def test_a_requested_halo_must_actually_be_covered():
    """Covering the target but not its halo is a gap, not a success.

    A later interpolation reads outside the target cells; if those inputs were
    never acquired it would silently invent edge values.
    """
    target = BBoxSupport(CRS, AXES, ("1", "1", "3", "3"))
    exactly_the_target = _asset("core", ("1", "1", "3", "3"))
    assert assess_coverage(
        (exactly_the_target,), target_spatial=target,
        target_temporal=_window(), halo="0").complete
    gapped = assess_coverage(
        (exactly_the_target,), target_spatial=target,
        target_temporal=_window(), halo="1.5")
    assert gapped.status is CoverageStatus.SPATIAL_GAP
    assert "halo" in gapped.detail


def test_expand_bbox_grows_symmetrically_and_rejects_negative():
    box = BBoxSupport(CRS, AXES, ("0", "0", "4", "4"))
    grown = expand_bbox(box, "1")
    assert grown.bounds == ("-1", "-1", "5", "5")
    with pytest.raises(ValueError, match="halo cannot be negative"):
        expand_bbox(box, "-1")


def test_temporal_union_needs_a_continuous_sweep():
    target = _window(START, END)
    contiguous = (
        _asset("a", ("0", "0", "4", "4"), start=START,
               end="2026-01-01T01:00:00Z"),
        _asset("b", ("0", "0", "4", "4"), start="2026-01-01T01:00:00Z",
               end=END),
    )
    assert temporal_union_covers(contiguous, target)
    broken = (
        _asset("a", ("0", "0", "4", "4"), start=START,
               end="2026-01-01T00:30:00Z"),
        _asset("b", ("0", "0", "4", "4"), start="2026-01-01T01:00:00Z",
               end=END),
    )
    assert not temporal_union_covers(broken, target)


def test_conditional_identity_kinds_are_typed_and_checked():
    assert AssetConditionalIdentity(
        ConditionalIdentityKind.CHECKSUM_SHA256, "a" * 64).content_addressed
    assert not AssetConditionalIdentity(
        ConditionalIdentityKind.ETAG, "W/\"v1\"").content_addressed
    with pytest.raises(ValueError):
        AssetConditionalIdentity(
            ConditionalIdentityKind.CHECKSUM_SHA256, "not-a-digest")
