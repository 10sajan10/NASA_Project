"""Stage 10D: native publication proposals and snapshot-bound queries."""
from __future__ import annotations

import dataclasses
import hashlib
from pathlib import Path

import pytest

from artifacts import (
    ArtifactAvailability,
    ArtifactInput,
    ArtifactRecord,
    ArtifactRegistrySnapshot,
    ArtifactSnapshotEntry,
    ArtifactSnapshotQuery,
    NativePublicationDeclaration,
    NativePublicationProposal,
    PublicationProducerRole,
    SnapshotArtifactCatalog,
    prepare_native_publication,
)
from contracts import (
    ArtifactDescriptor,
    BBoxSupport,
    GridDescriptor,
    IntrinsicUncertainty,
    Missingness,
    MissingnessStatus,
    OriginClass,
    SampleSemantics,
    SpatialScale,
    TemporalKind,
    TemporalSupport,
    UncertaintyStatus,
    VerticalKind,
    VerticalSupport,
)


def _descriptor(
    *, units: str = "s", ensemble_member: str = "member-01",
) -> ArtifactDescriptor:
    spacing = SpatialScale.isotropic("100", "m")
    grid = GridDescriptor(
        crs="EPSG:32613",
        axis_order=("x", "y"),
        shape=(2, 2),
        affine=("100", "0", "50", "0", "-100", "150"),
        spacing=spacing,
    )
    return ArtifactDescriptor(
        concept_id="hazard.fire.arrival_time",
        schema_version="wrf-sfire-output-v1",
        representation="application/x-netcdf",
        units=units,
        spatial_support=BBoxSupport(
            "EPSG:32613", ("x", "y"), grid.support_bounds),
        temporal_support=TemporalSupport(
            TemporalKind.SERIES,
            start="2032-01-01T00:00:00Z",
            end="2032-01-01T01:00:00Z",
            cadence_s="60",
            anchor="2032-01-01T00:00:00Z",
            max_gap_s="0",
            sample_semantics=SampleSemantics.INSTANTANEOUS,
            reference_time="2031-12-31T18:00:00Z",
        ),
        vertical_support=VerticalSupport(
            VerticalKind.HEIGHT_AGL, "m", "ground", ("0",)),
        grid=grid,
        native_resolution=spacing,
        origin=OriginClass.MODEL,
        missingness=Missingness(
            MissingnessStatus.BOUNDED, "0.01", ("_FillValue",)),
        ensemble_member=ensemble_member,
        intrinsic_uncertainty=IntrinsicUncertainty.known(
            "uncertainty:wrf-sfire:1", "manifest:uncertainty:run-7"),
        component_names=("arrival_time", "fire_intensity"),
    )


def _declaration(
    path: Path,
    *,
    descriptor: ArtifactDescriptor | None = None,
    role: PublicationProducerRole = PublicationProducerRole.DERIVED,
    inputs: tuple[ArtifactInput, ...] | None = None,
    declared_ports: tuple[str, ...] | None = None,
    expected_digest: str | None = None,
) -> NativePublicationDeclaration:
    if inputs is None:
        inputs = (ArtifactInput("meteorology", "a" * 64),)
    if declared_ports is None:
        declared_ports = ("meteorology",)
    return NativePublicationDeclaration(
        descriptor=descriptor or _descriptor(),
        location=str(path),
        media_type="application/x-netcdf",
        producer_id="wrf-sfire",
        producer_version="4.5.2",
        invocation_id="invocation:wrf-sfire:run-7",
        output_port_id="wrfout",
        producer_role=role,
        declared_input_ports=declared_ports,
        inputs=inputs,
        evidence_profile_id="evidence:wrf-sfire:validated-v2",
        expected_content_sha256=expected_digest,
        metadata={"native_variable": "TIGN_G", "domain": "d03"},
    )


def test_native_publication_is_verified_but_not_published_or_copied(tmp_path):
    native = tmp_path / "wrfout_d03"
    native.write_bytes(b"native-netcdf-content")
    digest = hashlib.sha256(native.read_bytes()).hexdigest()

    proposal = prepare_native_publication(
        _declaration(native, expected_digest=digest))

    assert proposal.record.content_sha256 == digest
    assert proposal.record.size_bytes == len(b"native-netcdf-content")
    assert proposal.record.location == str(native)
    assert proposal.record.descriptor == _descriptor()
    assert proposal.record.inputs == (
        ArtifactInput("meteorology", "a" * 64),)
    assert proposal.invocation_id == "invocation:wrf-sfire:run-7"
    assert proposal.producer_role is PublicationProducerRole.DERIVED
    assert proposal.proposal_id == proposal.expected_id()
    assert native.read_bytes() == b"native-netcdf-content"
    assert tuple(tmp_path.iterdir()) == (native,)


def test_publication_proposal_cannot_be_self_minted(tmp_path):
    native = tmp_path / "native.nc"
    native.write_bytes(b"content")
    verified = prepare_native_publication(_declaration(native))
    with pytest.raises(PermissionError, match="content-verified"):
        NativePublicationProposal(
            proposal_id=verified.proposal_id,
            declaration_id=verified.declaration_id,
            invocation_id=verified.invocation_id,
            producer_role=verified.producer_role,
            record=verified.record,
            _mint=object(),
        )


def test_publication_requires_complete_lineage_or_an_explicit_source(tmp_path):
    native = tmp_path / "native.nc"
    native.write_bytes(b"content")
    with pytest.raises(ValueError, match="cover every declared input"):
        _declaration(native, declared_ports=("fuel", "meteorology"))
    with pytest.raises(ValueError, match="only a declared source"):
        _declaration(native, inputs=(), declared_ports=())
    with pytest.raises(ValueError, match="source producer cannot"):
        _declaration(native, role=PublicationProducerRole.SOURCE)

    source = _declaration(
        native,
        role=PublicationProducerRole.SOURCE,
        inputs=(),
        declared_ports=(),
    )
    assert prepare_native_publication(source).record.inputs == ()


def test_publication_refuses_relative_symlink_and_wrong_content(tmp_path):
    native = tmp_path / "native.nc"
    native.write_bytes(b"content")
    with pytest.raises(ValueError, match="absolute path"):
        dataclasses.replace(_declaration(native), location="relative.nc")

    link = tmp_path / "linked.nc"
    link.symlink_to(native)
    with pytest.raises(ValueError, match="symbolic links"):
        prepare_native_publication(_declaration(link))

    with pytest.raises(ValueError, match="expected content digest"):
        prepare_native_publication(
            _declaration(native, expected_digest="b" * 64))


def _record(
    path: Path,
    descriptor: ArtifactDescriptor,
    *,
    content: bytes,
    producer_version: str = "4.5.2",
) -> ArtifactRecord:
    path.write_bytes(content)
    return ArtifactRecord.bind(
        descriptor=descriptor,
        location=str(path),
        media_type=descriptor.representation,
        content_sha256=hashlib.sha256(content).hexdigest(),
        size_bytes=len(content),
        producer_id="wrf-sfire",
        producer_version=producer_version,
        output_port_id="wrfout",
        inputs=(ArtifactInput("meteorology", "a" * 64),),
        evidence_profile_id="evidence:wrf-sfire:validated-v2",
        metadata={"native_variable": "TIGN_G"},
    )


def _snapshot(tmp_path):
    selected = _record(
        tmp_path / "selected.nc", _descriptor(), content=b"selected")
    wrong_units = _record(
        tmp_path / "wrong-units.nc", _descriptor(units="m"),
        content=b"wrong-units")
    unavailable = _record(
        tmp_path / "unavailable.nc", _descriptor(),
        content=b"unavailable", producer_version="4.5.3")
    snapshot = ArtifactRegistrySnapshot.freeze((
        ArtifactSnapshotEntry(selected, ArtifactAvailability.COMMITTED),
        ArtifactSnapshotEntry(wrong_units, ArtifactAvailability.COMMITTED),
        ArtifactSnapshotEntry(
            unavailable,
            ArtifactAvailability.UNAVAILABLE,
            "LOCATION_UNAVAILABLE:FileNotFoundError",
        ),
    ))
    return snapshot, selected, wrong_units, unavailable


def _full_query(record: ArtifactRecord) -> ArtifactSnapshotQuery:
    descriptor = record.descriptor
    assert descriptor.grid is not None
    assert descriptor.native_resolution is not None
    assert descriptor.vertical_support is not None
    return ArtifactSnapshotQuery(
        concept_id=descriptor.concept_id,
        representation=descriptor.representation,
        schema_version=descriptor.schema_version,
        units=descriptor.units,
        spatial_support=descriptor.spatial_support,
        spatial_crs=descriptor.spatial_support.crs,
        intersects_bounds=("25", "25", "175", "175"),
        grid_id=descriptor.grid.grid_id,
        grid=descriptor.grid,
        grid_crs=descriptor.grid.crs,
        grid_shape=descriptor.grid.shape,
        native_resolution=descriptor.native_resolution,
        temporal_kind=descriptor.temporal_support.kind,
        temporal_support=descriptor.temporal_support,
        intersects_time=(
            "2032-01-01T00:15:00Z", "2032-01-01T00:45:00Z"),
        cadence_s="60",
        vertical_support=descriptor.vertical_support,
        vertical_kind=descriptor.vertical_support.kind,
        origin=descriptor.origin,
        missingness=descriptor.missingness,
        missingness_status=descriptor.missingness.status,
        intrinsic_uncertainty=descriptor.intrinsic_uncertainty,
        uncertainty_status=descriptor.intrinsic_uncertainty.status,
        required_components=("arrival_time",),
        ensemble_member=descriptor.ensemble_member,
        evidence_profile_id=record.evidence_profile_id,
        producer_id=record.producer_id,
        producer_version=record.producer_version,
        output_port_id=record.output_port_id,
        media_type=record.media_type,
        location=record.location,
        record_metadata=record.metadata,
        lineage_inputs=(ArtifactInput("meteorology", "a" * 64),),
        has_lineage=True,
    )


def test_snapshot_query_covers_science_provenance_location_and_lineage(tmp_path):
    snapshot, selected, _wrong, _unavailable = _snapshot(tmp_path)
    query = _full_query(selected)

    result = SnapshotArtifactCatalog(snapshot).search(query)

    assert result.query_id == query.query_id
    assert result.snapshot_id == snapshot.snapshot_id
    assert result.records == (selected,)
    assert result.record_ids == (selected.record_id,)
    assert result.availability_by_record == (
        (selected.record_id, ArtifactAvailability.COMMITTED),)
    assert result.result_id == result.expected_id()
    assert result.to_dict()["matches"] == [{
        "record_id": selected.record_id,
        "artifact_id": selected.artifact_id,
        "content_sha256": selected.content_sha256,
        "availability": "COMMITTED",
        "reason": "",
    }]


def test_same_concept_with_incompatible_units_is_not_returned(tmp_path):
    snapshot, selected, wrong_units, _unavailable = _snapshot(tmp_path)
    assert selected.descriptor.concept_id == wrong_units.descriptor.concept_id
    result = SnapshotArtifactCatalog(snapshot).search(ArtifactSnapshotQuery(
        concept_id=selected.descriptor.concept_id,
        units="s",
    ))
    assert result.records == (selected,)
    assert wrong_units.record_id not in result.record_ids


def test_query_and_result_receipts_replay_strictly(tmp_path):
    snapshot, selected, _wrong, _unavailable = _snapshot(tmp_path)
    query = _full_query(selected)
    result = SnapshotArtifactCatalog(snapshot).search(query)

    decoded_query = ArtifactSnapshotQuery.from_dict(query.to_dict())
    decoded_result = type(result).from_dict(
        result.to_dict(), query=decoded_query, snapshot=snapshot)
    assert decoded_query == query
    assert decoded_query.query_id == query.query_id
    assert decoded_result == result

    tampered = result.to_dict()
    tampered["matches"][0]["content_sha256"] = "b" * 64
    with pytest.raises(ValueError, match="does not replay"):
        type(result).from_dict(tampered, query=query, snapshot=snapshot)

    unknown_query_field = query.to_dict()
    unknown_query_field["guessed_crs"] = "EPSG:4326"
    with pytest.raises(ValueError, match="unexpected"):
        ArtifactSnapshotQuery.from_dict(unknown_query_field)


def test_query_is_bound_to_snapshot_availability_and_never_reopens_payload(
        tmp_path):
    snapshot, selected, _wrong, unavailable = _snapshot(tmp_path)
    for path in tmp_path.iterdir():
        path.unlink()
    catalog = SnapshotArtifactCatalog(snapshot)

    committed = catalog.search(ArtifactSnapshotQuery(
        record_id=selected.record_id))
    missing = catalog.search(ArtifactSnapshotQuery(
        record_id=unavailable.record_id,
        availability=ArtifactAvailability.UNAVAILABLE,
    ))
    all_states = catalog.search(ArtifactSnapshotQuery(availability=None))

    assert committed.records == (selected,)
    assert missing.records == (unavailable,)
    assert len(all_states.records) == 3
    assert {value for _, value in all_states.availability_by_record} == {
        ArtifactAvailability.COMMITTED,
        ArtifactAvailability.UNAVAILABLE,
    }


def test_bbox_query_refuses_to_guess_a_crs():
    with pytest.raises(ValueError, match="exact spatial_crs"):
        ArtifactSnapshotQuery(
            intersects_bounds=("0", "0", "1", "1"))


def test_each_full_query_facet_can_exclude_a_candidate(tmp_path):
    snapshot, selected, _wrong, _unavailable = _snapshot(tmp_path)
    catalog = SnapshotArtifactCatalog(snapshot)
    query = _full_query(selected)
    assert catalog.search(query).records == (selected,)
    other_vertical = VerticalSupport(
        VerticalKind.HEIGHT_AGL, "m", "ground", ("10",))
    exclusions = (
        {"representation": "application/json"},
        {"schema_version": "wrf-sfire-output-v2"},
        {"spatial_support": BBoxSupport(
            "EPSG:32613", ("x", "y"), ("0", "0", "50", "50"))},
        {"spatial_crs": "EPSG:4326",
         "intersects_bounds": ("25", "25", "175", "175")},
        {"intersects_bounds": ("500", "500", "600", "600")},
        {"grid_id": "b" * 64},
        {"grid": dataclasses.replace(query.grid, shape=(3, 3))},
        {"grid_crs": "EPSG:4326"},
        {"grid_shape": (3, 3)},
        {"native_resolution": SpatialScale.isotropic("200", "m")},
        {"temporal_kind": TemporalKind.TIME_INVARIANT},
        {"temporal_support": dataclasses.replace(
            query.temporal_support, max_gap_s="60")},
        {"intersects_time": (
            "2033-01-01T00:00:00Z", "2033-01-01T01:00:00Z")},
        {"cadence_s": "120"},
        {"vertical_support": other_vertical},
        {"vertical_kind": VerticalKind.PRESSURE},
        {"origin": OriginClass.FORECAST},
        {"missingness": Missingness(MissingnessStatus.COMPLETE)},
        {"missingness_status": MissingnessStatus.COMPLETE},
        {"intrinsic_uncertainty": IntrinsicUncertainty.unknown("unknown")},
        {"uncertainty_status": UncertaintyStatus.UNKNOWN},
        {"required_components": ("flame_length",)},
        {"ensemble_member": "member-02"},
        {"evidence_profile_id": "evidence:other"},
        {"producer_id": "other-model"},
        {"producer_version": "0"},
        {"output_port_id": "other"},
        {"media_type": "application/json"},
        {"location": str(tmp_path / "other.nc")},
        {"record_metadata": {"native_variable": "OTHER"}},
        {"lineage_inputs": (ArtifactInput("fuel", "b" * 64),)},
        {"lineage_artifact_ids": ("b" * 64,)},
        {"lineage_port_ids": ("fuel",)},
        {"has_lineage": False},
    )
    for change in exclusions:
        assert catalog.search(dataclasses.replace(query, **change)).records == ()


def test_lineage_artifact_and_port_are_one_exact_edge(tmp_path):
    snapshot, selected, _wrong, _unavailable = _snapshot(tmp_path)
    # The record contains meteorology:A.  Independent port/artifact filters
    # cannot safely express fuel:A and are rejected instead of cross-matching.
    with pytest.raises(ValueError, match="use lineage_inputs"):
        ArtifactSnapshotQuery(
            lineage_artifact_ids=("a" * 64,),
            lineage_port_ids=("fuel",),
        )
    result = SnapshotArtifactCatalog(snapshot).search(ArtifactSnapshotQuery(
        lineage_inputs=(ArtifactInput("fuel", "a" * 64),),
    ))
    assert selected not in result.records
