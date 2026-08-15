"""Stage-2 scientific contract and direct matching acceptance matrix."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime

import pytest

from contracts import (
    ArtifactDescriptor,
    BBoxSupport,
    BoundKind,
    CadencePolicy,
    Cardinality,
    CompatibilityProof,
    ConstraintMode,
    DistinctnessPolicy,
    EvidenceApplicability,
    EvidenceBound,
    EvidenceClaim,
    EstimateUncertainty,
    EvidenceProfile,
    EvidenceRequirement,
    EvidenceSnapshot,
    EvidenceSubject,
    GridDescriptor,
    IntrinsicUncertainty,
    MatchCode,
    MetricDefinition,
    MetricEvaluator,
    MissingPolicy,
    Missingness,
    MissingnessStatus,
    OriginClass,
    Requirement,
    RequirementUse,
    SampleSemantics,
    ScaleBasis,
    SpatialRequirement,
    SpatialScale,
    TemporalKind,
    TemporalRequirement,
    TemporalSupport,
    UnknownPolicy,
    UncertaintyStatus,
    ValueConstraint,
    VerticalKind,
    VerticalRequirement,
    VerticalSupport,
    canonical_decimal,
    canonical_timestamp,
    direct_match,
)


def bbox(bounds=(0, 0, 10, 10), crs="EPSG:4326", complete=True):
    return BBoxSupport(crs, ("longitude", "latitude"), bounds, complete)


def scale(value, *, basis=ScaleBasis.LINEAR, unit="m"):
    return SpatialScale.isotropic(value, unit, basis)


def grid(spacing=900, *, crs="EPSG:4326", affine=(900, 0, 0, 0, -900, 10)):
    return GridDescriptor(
        crs, ("longitude", "latitude"), (100, 100), affine, scale(spacing))


def descriptor(**changes):
    base = ArtifactDescriptor(
        concept_id="wind.vector.horizontal",
        schema_version="1",
        representation="uv-components",
        units="m/s",
        spatial_support=bbox(),
        temporal_support=TemporalSupport(
            TemporalKind.SERIES,
            "2019-09-04T00:00:00Z", "2019-09-06T00:00:00Z",
            "3600", "2019-09-04T00:00:00Z", "0",
            SampleSemantics.INSTANTANEOUS),
        vertical_support=VerticalSupport(
            VerticalKind.HEIGHT_AGL, "m", "local-ground", ("10",)),
        grid=grid(),
        native_resolution=scale(31_000),
        origin=OriginClass.REANALYSIS,
        missingness=Missingness(MissingnessStatus.COMPLETE),
    )
    return replace(base, **changes)


def requirement(**changes):
    base = Requirement(
        concept_id="wind.vector.horizontal",
        accepted_schema_versions=("1",),
        representation=ValueConstraint.exact("uv-components"),
        units=ValueConstraint.exact("m.s-1"),
        spatial=SpatialRequirement(bbox((2, 2, 8, 8))),
        temporal=TemporalRequirement(
            TemporalKind.SERIES,
            "2019-09-04T06:00:00Z", "2019-09-05T18:00:00Z",
            "3600", CadencePolicy.AT_MOST,
            "2019-09-04T00:00:00Z", "0",
            SampleSemantics.INSTANTANEOUS),
        vertical=VerticalRequirement(
            VerticalKind.HEIGHT_AGL, "m", "local-ground", ("10",)),
        allowed_origins=(OriginClass.REANALYSIS, OriginClass.ANALYSIS),
        max_native_resolution=None,
        max_effective_resolution=None,
        missing_policy=MissingPolicy(),
        minimum_evidence=EvidenceRequirement(allow_unknown_empirical=True),
    )
    return replace(base, **changes)


def subject(**changes):
    values = {
        "component_id": "era5-wind",
        "component_version": "2026.1",
        "configuration_id": "config-sha256",
        "output_port_id": "wind",
    }
    values.update(changes)
    return EvidenceSubject(**values)


def evidence(value="2500", *, applicability=None, bound_kind=BoundKind.CONSERVATIVE_UPPER,
             claim_subject=None):
    applicability = applicability or EvidenceApplicability(
        bbox(), "2019-09-01T00:00:00Z", "2019-09-10T00:00:00Z",
        VerticalKind.HEIGHT_AGL, "m", "local-ground", ("10",),
        ("warm-season",))
    profile = EvidenceProfile(
        "WindEvidencePack-v1", claim_subject or subject(),
        (EvidenceClaim.known(
            "effective-resolution-v1", value, "m", bound_kind=bound_kind,
            reference_manifest_id="obs-manifest-sha256",
            protocol_id="spectral-method-v1", evaluator_id="wind-evaluator-v1",
            applicability=applicability),))
    evaluator = MetricEvaluator(
        "wind-evaluator-v1", "1",
        (MetricDefinition("effective-resolution-v1", "1", "m",
                          "spectral-method-v1"),))
    snapshot = EvidenceSnapshot("2026-08-13T00:00:00Z", evaluator, (profile,))
    return profile, snapshot


def codes(proof):
    return set(proof.rejection_codes)


def test_exact_match_is_stable_and_round_trips() -> None:
    offered = descriptor()
    needed = requirement()
    first = direct_match(offered, needed)
    second = direct_match(
        ArtifactDescriptor.from_dict(offered.to_dict()),
        Requirement.from_dict(needed.to_dict()))
    assert first.satisfied and second.satisfied
    assert first.proof_id == second.proof_id
    assert first.to_dict() == second.to_dict()
    assert CompatibilityProof.from_dict(first.to_dict()) == first
    assert CompatibilityProof.identity_schema == "compatibility-proof-v1"
    assert Requirement.identity_schema == "requirement-v3"


def test_exact_descriptor_requirement_is_a_closed_transform_input_boundary() -> None:
    offered = descriptor()
    exact = replace(requirement(), exact_descriptor_id=offered.descriptor_id)

    assert direct_match(offered, exact).satisfied

    scientifically_compatible_but_distinct = replace(
        offered,
        intrinsic_uncertainty=IntrinsicUncertainty.unknown(
            "DIFFERENT_DECLARED_UNCERTAINTY"),
    )
    proof = direct_match(scientifically_compatible_but_distinct, exact)
    assert not proof.satisfied
    assert MatchCode.DESCRIPTOR_IDENTITY_MISMATCH in codes(proof)
    assert Requirement.from_dict(exact.to_dict()) == exact

    malformed = exact.to_dict()
    malformed["exact_descriptor_id"] = "not-a-digest"
    with pytest.raises(ValueError, match="lowercase SHA-256"):
        Requirement.from_dict(malformed)


def test_intrinsic_and_estimate_uncertainty_are_distinct_stable_contracts() -> None:
    offered = descriptor()
    claim = evidence()[0].claims[0]

    assert offered.intrinsic_uncertainty.status is UncertaintyStatus.UNKNOWN
    assert offered.intrinsic_uncertainty.reason == "NOT_REPORTED_BY_PRODUCER"
    assert claim.estimate_uncertainty.status is UncertaintyStatus.UNKNOWN
    assert claim.estimate_uncertainty.reason == (
        "NOT_REPORTED_BY_EVIDENCE_PROVIDER")
    assert (offered.intrinsic_uncertainty.identity
            != claim.estimate_uncertainty.identity)
    assert offered.to_dict()["intrinsic_uncertainty"]["status"] == "UNKNOWN"
    assert claim.to_dict()["estimate_uncertainty"]["status"] == "UNKNOWN"
    assert ArtifactDescriptor.from_dict(offered.to_dict()) == offered
    assert EvidenceClaim.from_dict(claim.to_dict()) == claim

    intrinsic = IntrinsicUncertainty.known(
        "wind-error-model-v1", "sha256:intrinsic-parameter-manifest")
    estimate = EstimateUncertainty.known(
        "2400", "2600", "m", "0.95", "blocked-bootstrap-v1")
    with_intrinsic = replace(offered, intrinsic_uncertainty=intrinsic)
    with_estimate = replace(claim, estimate_uncertainty=estimate)

    assert with_intrinsic.descriptor_id != offered.descriptor_id
    assert with_estimate.identity != claim.identity
    assert with_intrinsic.intrinsic_uncertainty.identity_schema == (
        "intrinsic-uncertainty-v1")
    assert with_estimate.estimate_uncertainty.identity_schema == (
        "estimate-uncertainty-v1")
    assert ArtifactDescriptor.from_dict(with_intrinsic.to_dict()) == with_intrinsic
    assert EvidenceClaim.from_dict(with_estimate.to_dict()) == with_estimate


def test_uncertainty_unknown_and_not_applicable_never_imply_values() -> None:
    unknown = EstimateUncertainty.unknown("INTERVAL_NOT_REPORTED")
    not_applicable = EstimateUncertainty.not_applicable(
        "DETERMINISTIC_REFERENCE_VALUE")

    assert unknown.identity != not_applicable.identity
    for uncertainty in (unknown, not_applicable):
        encoded = uncertainty.to_dict()
        assert encoded["lower_bound"] is None
        assert encoded["upper_bound"] is None
        assert encoded["confidence_level"] is None
        assert EstimateUncertainty.from_dict(encoded) == uncertainty

    unknown_claim = EvidenceClaim.unknown(
        "effective-resolution-v1", "m", "NO_HELD_OUT_REFERENCE")
    assert unknown_claim.estimate_uncertainty.status is (
        UncertaintyStatus.NOT_APPLICABLE)
    assert unknown_claim.estimate_uncertainty.reason == "NO_NUMERIC_ESTIMATE"

    with pytest.raises(ValueError, match="cannot exceed"):
        EstimateUncertainty.known(
            "2", "1", "m", "0.95", "invalid-interval-method")


def test_uncertainty_fields_are_mandatory_at_closed_world_decode_boundary() -> None:
    encoded_descriptor = descriptor().to_dict()
    encoded_descriptor.pop("intrinsic_uncertainty")
    with pytest.raises(ValueError, match="missing"):
        ArtifactDescriptor.from_dict(encoded_descriptor)

    encoded_claim = evidence()[0].claims[0].to_dict()
    encoded_claim.pop("estimate_uncertainty")
    with pytest.raises(ValueError, match="missing"):
        EvidenceClaim.from_dict(encoded_claim)


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"concept_id": "wind.speed"}, MatchCode.CONCEPT_MISMATCH),
        ({"schema_version": "2"}, MatchCode.SCHEMA_VERSION_MISMATCH),
        ({"representation": "speed-direction"}, MatchCode.REPRESENTATION_MISMATCH),
        ({"units": "km/h"}, MatchCode.UNITS_MISMATCH),
    ],
)
def test_semantic_identity_mismatches_do_not_convert(change, code) -> None:
    proof = direct_match(descriptor(**change), requirement())
    assert not proof.satisfied
    assert code in codes(proof)


def test_unit_alias_normalizes_but_not_unit_conversion() -> None:
    assert descriptor(units="m s^-1").units == "m.s-1"
    assert direct_match(descriptor(units="m s^-1"), requirement()).satisfied


def test_finer_cadence_must_lie_on_requested_lattice_without_interpolation() -> None:
    ninety_minutes = replace(
        requirement().temporal,
        cadence_s="5400",
        cadence_policy=CadencePolicy.AT_MOST,
    )
    proof = direct_match(
        descriptor(),
        replace(requirement(), temporal=ninety_minutes),
    )

    assert not proof.satisfied
    assert MatchCode.CADENCE_TOO_COARSE in codes(proof)


def test_empty_compatibility_proof_is_rejected() -> None:
    with pytest.raises(ValueError, match="non-empty typed check set"):
        CompatibilityProof(
            requirement_id=requirement().requirement_id,
            descriptor_id=descriptor().descriptor_id,
            evidence_profile_id=None,
            evidence_snapshot_id=None,
            evidence_subject_id=None,
            checks=(),
        )


def test_spatial_crs_partial_coverage_and_grid_identity_fail() -> None:
    different_crs = descriptor(spatial_support=bbox(crs="EPSG:3857"))
    assert MatchCode.SPATIAL_CRS_MISMATCH in codes(
        direct_match(different_crs, requirement()))
    partial = descriptor(spatial_support=bbox((0, 0, 4, 4)))
    assert MatchCode.SPATIAL_COVERAGE_GAP in codes(
        direct_match(partial, requirement()))
    exact = replace(requirement(), spatial=SpatialRequirement(grid().spacing and bbox(
        (2, 2, 8, 8)), exact_grid=grid(900)))
    shifted = descriptor(grid=grid(900, affine=(900, 0, 1, 0, -900, 10)))
    assert MatchCode.GRID_IDENTITY_MISMATCH in codes(direct_match(shifted, exact))


def test_grid_spacing_is_not_native_or_effective_resolution() -> None:
    needed = replace(
        requirement(),
        spatial=SpatialRequirement(bbox((2, 2, 8, 8)), max_grid_spacing=scale(1000)),
        max_native_resolution=scale(3000))
    proof = direct_match(descriptor(grid=grid(900), native_resolution=scale(31_000)), needed)
    assert MatchCode.GRID_SPACING_TOO_COARSE not in codes(proof)
    assert MatchCode.NATIVE_RESOLUTION_TOO_COARSE in codes(proof)


def test_unknown_and_incomparable_native_resolution_fail_closed() -> None:
    needed = replace(requirement(), max_native_resolution=scale(3000))
    assert MatchCode.NATIVE_RESOLUTION_UNKNOWN in codes(
        direct_match(descriptor(native_resolution=None), needed))
    angular = scale("0.25", basis=ScaleBasis.ANGULAR, unit="degree")
    assert MatchCode.NATIVE_RESOLUTION_INCOMPARABLE in codes(
        direct_match(descriptor(native_resolution=angular), needed))


def test_linear_scale_metadata_normalizes_without_angular_approximation() -> None:
    assert SpatialScale.isotropic("1", "km") == SpatialScale.isotropic("1000", "m")


def test_temporal_coverage_cadence_alignment_semantics_and_gaps() -> None:
    coarse = replace(descriptor().temporal_support, cadence_s="10800")
    assert MatchCode.CADENCE_TOO_COARSE in codes(
        direct_match(descriptor(temporal_support=coarse), requirement()))
    gapped = replace(descriptor().temporal_support, max_gap_s="7200")
    assert MatchCode.TEMPORAL_GAPS_EXCEED_POLICY in codes(
        direct_match(descriptor(temporal_support=gapped), requirement()))
    mean = replace(descriptor().temporal_support, sample_semantics=SampleSemantics.MEAN)
    assert MatchCode.TIME_SEMANTICS_MISMATCH in codes(
        direct_match(descriptor(temporal_support=mean), requirement()))
    shifted = replace(descriptor().temporal_support,
                      anchor="2019-09-04T00:30:00Z")
    assert MatchCode.TIME_ALIGNMENT_MISMATCH in codes(
        direct_match(descriptor(temporal_support=shifted), requirement()))
    short = replace(descriptor().temporal_support, end="2019-09-05T00:00:00Z")
    assert MatchCode.TEMPORAL_COVERAGE_GAP in codes(
        direct_match(descriptor(temporal_support=short), requirement()))


def test_finer_cadence_passes_at_most_but_fails_exact() -> None:
    half_hourly = replace(descriptor().temporal_support, cadence_s="1800")
    assert direct_match(descriptor(temporal_support=half_hourly), requirement()).satisfied
    exact = replace(requirement(), temporal=replace(
        requirement().temporal, cadence_policy=CadencePolicy.EXACT))
    assert MatchCode.CADENCE_NOT_EXACT in codes(
        direct_match(descriptor(temporal_support=half_hourly), exact))


def test_static_does_not_satisfy_time_varying_wind() -> None:
    static = TemporalSupport(TemporalKind.TIME_INVARIANT)
    assert MatchCode.TIME_KIND_MISMATCH in codes(
        direct_match(descriptor(temporal_support=static), requirement()))


@pytest.mark.parametrize(
    ("vertical", "code"),
    [
        (None, MatchCode.VERTICAL_REQUIRED_MISSING),
        (VerticalSupport(VerticalKind.HEIGHT_MSL, "m", "sea-level", ("10",)),
         MatchCode.VERTICAL_KIND_MISMATCH),
        (VerticalSupport(VerticalKind.HEIGHT_AGL, "m", "local-ground", ("2", "20")),
         MatchCode.VERTICAL_LEVEL_MISSING),
    ],
)
def test_vertical_is_exact_without_hidden_interpolation(vertical, code) -> None:
    assert code in codes(direct_match(descriptor(vertical_support=vertical), requirement()))


def test_origin_and_missingness_policies() -> None:
    forecast = descriptor(origin=OriginClass.FORECAST)
    assert MatchCode.ORIGIN_DISALLOWED in codes(direct_match(forecast, requirement()))
    bounded = descriptor(missingness=Missingness(
        MissingnessStatus.BOUNDED, "0.2", ("nan",)))
    assert MatchCode.MISSINGNESS_EXCEEDS_POLICY in codes(
        direct_match(bounded, requirement()))
    unknown = descriptor(missingness=Missingness(MissingnessStatus.UNKNOWN))
    assert MatchCode.MISSINGNESS_UNKNOWN in codes(direct_match(unknown, requirement()))
    allow = replace(requirement(), missing_policy=MissingPolicy(
        "0", UnknownPolicy.ALLOW_WITH_CAVEAT))
    proof = direct_match(unknown, allow)
    assert proof.satisfied and MatchCode.QUALITY_UNKNOWN_ALLOWED in proof.caveat_codes


def quality_requirement(**changes):
    base = replace(
        requirement(),
        max_effective_resolution=EvidenceBound(
            "effective-resolution-v1", "3000", "m"),
        minimum_evidence=EvidenceRequirement(
            "WindEvidencePack-v1", False, ("effective-resolution-v1",)),
        required_regimes=("warm-season",))
    return replace(base, **changes)


def test_effective_resolution_requires_applicable_frozen_evidence() -> None:
    profile, snapshot = evidence()
    proof = direct_match(
        descriptor(), quality_requirement(), profile,
        evidence_snapshot=snapshot, evidence_subject=subject(),
        requested_regimes=("warm-season",))
    assert proof.satisfied

    outside = replace(profile.claims[0].applicability,
                      spatial=bbox((0, 0, 3, 3)))
    outside_profile, outside_snapshot = evidence(applicability=outside)
    outside_proof = direct_match(
        descriptor(), quality_requirement(), outside_profile,
        evidence_snapshot=outside_snapshot, evidence_subject=subject(),
        requested_regimes=("warm-season",))
    assert MatchCode.EVIDENCE_NOT_APPLICABLE in codes(outside_proof)


def test_evidence_regimes_are_requirement_identity_not_match_side_channel() -> None:
    profile, snapshot = evidence()
    warm = quality_requirement()
    cold = replace(warm, required_regimes=("cold-season",))

    assert warm.requirement_id != cold.requirement_id
    implicit = direct_match(
        descriptor(), warm, profile,
        evidence_snapshot=snapshot, evidence_subject=subject())
    explicit = direct_match(
        descriptor(), warm, profile,
        evidence_snapshot=snapshot, evidence_subject=subject(),
        requested_regimes=("warm-season",))
    assert implicit.satisfied
    assert implicit.proof_id == explicit.proof_id
    assert MatchCode.EVIDENCE_NOT_APPLICABLE in codes(direct_match(
        descriptor(), cold, profile,
        evidence_snapshot=snapshot, evidence_subject=subject()))

    with pytest.raises(ValueError, match="exactly match"):
        direct_match(
            descriptor(), warm, profile,
            evidence_snapshot=snapshot, evidence_subject=subject(),
            requested_regimes=("cold-season",))


@pytest.mark.parametrize(
    "decoder_and_value",
    [
        lambda: (ArtifactDescriptor.from_dict, descriptor().to_dict()),
        lambda: (Requirement.from_dict, requirement().to_dict()),
        lambda: (EvidenceProfile.from_dict, evidence()[0].to_dict()),
        lambda: (
            CompatibilityProof.from_dict,
            direct_match(descriptor(), requirement()).to_dict(),
        ),
    ],
)
def test_contract_decoders_reject_unknown_and_missing_fields(
        decoder_and_value) -> None:
    decoder, encoded = decoder_and_value()
    encoded["unexpected"] = "silently-dangerous"
    with pytest.raises(ValueError, match="unknown"):
        decoder(encoded)

    decoder, encoded = decoder_and_value()
    encoded.pop(next(iter(encoded)))
    with pytest.raises(ValueError, match="missing"):
        decoder(encoded)


@pytest.mark.parametrize(
    "decoder_and_value",
    [
        lambda: (
            ArtifactDescriptor.from_dict,
            {**descriptor().to_dict(), "spatial_support": []},
        ),
        lambda: (
            Requirement.from_dict,
            {**requirement().to_dict(), "allowed_origins": "REANALYSIS"},
        ),
        lambda: (
            EvidenceProfile.from_dict,
            {**evidence()[0].to_dict(), "claims": ["not-an-object"]},
        ),
        lambda: (
            CompatibilityProof.from_dict,
            {
                **direct_match(descriptor(), requirement()).to_dict(),
                "checks": {"not": "an-array"},
            },
        ),
    ],
)
def test_contract_decoders_reject_wrong_nested_types(decoder_and_value) -> None:
    decoder, encoded = decoder_and_value()
    with pytest.raises(TypeError):
        decoder(encoded)


def test_contract_constructors_reject_untyped_nested_values() -> None:
    with pytest.raises(TypeError, match="typed support"):
        SpatialRequirement("not-a-bbox")
    with pytest.raises(TypeError, match="typed subject"):
        EvidenceProfile("WindEvidencePack-v1", {}, ())
    with pytest.raises(TypeError, match="typed SpatialScale"):
        GridDescriptor(
            "EPSG:4326", ("x", "y"), (1, 1), (1, 0, 0, 0, 1, 0),
            "not-a-scale",
        )


def test_effective_resolution_rejects_unknown_wrong_subject_and_weak_bound() -> None:
    unknown = EvidenceProfile(
        "WindEvidencePack-v1", subject(),
        (EvidenceClaim.unknown(
            "effective-resolution-v1", "m", "NO_HELD_OUT_REFERENCE"),))
    assert MatchCode.EVIDENCE_CLAIM_UNKNOWN in codes(
        direct_match(descriptor(), quality_requirement(), unknown))
    incomplete_binding = direct_match(descriptor(), quality_requirement(), unknown)
    assert MatchCode.EVIDENCE_SNAPSHOT_MISSING in codes(incomplete_binding)
    assert MatchCode.EVIDENCE_SUBJECT_MISSING in codes(incomplete_binding)

    profile, snapshot = evidence(claim_subject=subject(configuration_id="other"))
    assert MatchCode.EVIDENCE_SUBJECT_MISMATCH in codes(direct_match(
        descriptor(), quality_requirement(), profile,
        evidence_snapshot=snapshot, evidence_subject=subject()))

    point, point_snapshot = evidence(bound_kind=BoundKind.POINT_ESTIMATE)
    assert MatchCode.EVIDENCE_BOUND_KIND_MISMATCH in codes(direct_match(
        descriptor(), quality_requirement(), point, evidence_snapshot=point_snapshot))


def test_unknown_quality_only_passes_when_no_hard_bound_and_explicitly_allowed() -> None:
    no_evidence = direct_match(descriptor(), requirement())
    assert no_evidence.satisfied
    assert MatchCode.QUALITY_UNKNOWN_ALLOWED in no_evidence.caveat_codes
    hard = replace(quality_requirement(), minimum_evidence=EvidenceRequirement(
        "WindEvidencePack-v1", True, ("effective-resolution-v1",)))
    proof = direct_match(descriptor(), hard)
    assert not proof.satisfied
    assert MatchCode.EVIDENCE_PROFILE_MISSING in codes(proof)


def test_not_applicable_evidence_is_never_downgraded_to_unknown_caveat() -> None:
    metric_id = "representativeness-v1"
    profile = EvidenceProfile(
        "WindEvidencePack-v1",
        subject(),
        (EvidenceClaim.not_applicable(
            metric_id, "1", "METRIC_OUTSIDE_DECLARED_DOMAIN"),),
    )
    evaluator = MetricEvaluator(
        "wind-evaluator-v1", "1",
        (MetricDefinition(metric_id, "1", "1", "domain-check-v1"),),
    )
    snapshot = EvidenceSnapshot(
        "2026-08-13T00:00:00Z", evaluator, (profile,))
    soft_requirement = replace(
        requirement(),
        minimum_evidence=EvidenceRequirement(
            "WindEvidencePack-v1", True, (metric_id,)),
    )

    proof = direct_match(
        descriptor(), soft_requirement, profile,
        evidence_snapshot=snapshot, evidence_subject=subject())

    assert not proof.satisfied
    assert MatchCode.EVIDENCE_NOT_APPLICABLE in proof.rejection_codes
    assert MatchCode.QUALITY_UNKNOWN_ALLOWED not in proof.caveat_codes


def test_requirement_uses_preserve_equal_requirements_as_distinct_ports() -> None:
    needed = requirement()
    first = RequirementUse("member-0", "left", needed)
    second = RequirementUse("member-1", "right", needed)
    assert first.requirement.requirement_id == second.requirement.requirement_id
    assert first.requirement_use_id != second.requirement_use_id
    ensemble = RequirementUse(
        "ensemble", "wind", needed, cardinality=Cardinality.exactly(2),
        distinctness=DistinctnessPolicy.DISTINCT_ARTIFACT)
    assert RequirementUse.from_dict(ensemble.to_dict()) == ensemble


def test_canonical_scientific_normalization_rejects_ambiguous_values() -> None:
    assert canonical_decimal("-0.000") == "0"
    assert canonical_timestamp("2019-09-04T01:00:00+01:00") == "2019-09-04T00:00:00Z"
    with pytest.raises(ValueError):
        canonical_decimal(float("nan"))
    with pytest.raises(ValueError):
        canonical_timestamp(datetime(2019, 9, 4))
    with pytest.raises(ValueError):
        BBoxSupport("EPSG:4326", ("x", "y"), (0, 0, 0, 1))


def test_all_rejections_are_recorded_and_match_is_pure() -> None:
    offered = descriptor(concept_id="wrong", units="km/h",
                         origin=OriginClass.FORECAST)
    before = offered.to_dict()
    proof = direct_match(offered, requirement())
    assert {MatchCode.CONCEPT_MISMATCH, MatchCode.UNITS_MISMATCH,
            MatchCode.ORIGIN_DISALLOWED}.issubset(codes(proof))
    assert offered.to_dict() == before
    assert all(check.transformation_hint is None
               or isinstance(check.transformation_hint, str)
               for check in proof.checks)
