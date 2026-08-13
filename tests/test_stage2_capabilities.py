"""Strict Stage-2 capability, binding, and deployment acceptance tests.

Only closed scalar/JSON synthetic operations are referenced.  This module does
not execute WRF, MPI, SLURM, a remote connector, or a generic callable.
"""
from __future__ import annotations

import dataclasses

import pytest

from capabilities import (
    ArtifactLeaf,
    BinderRef,
    BindingParameterization,
    BindingRejectionCode,
    CapabilityCatalog,
    CapabilitySpec,
    DescriptorTemplate,
    InputPortTemplate,
    ParameterField,
    ParameterKind,
    ParameterSchema,
)
from capabilities.binders import binder_keys
from capabilities.deployment import (
    DeploymentCapabilitySnapshot,
    DeploymentRejectionCode,
    ExecutionProfile,
    PlacementRequirements,
    ResourceEnvelope,
    SiteClassCapability,
)
from capabilities.implementation import ImplementationRef
from contracts import (
    ArtifactDescriptor,
    BBoxSupport,
    DistinctnessPolicy,
    EvidenceRequirement,
    MissingPolicy,
    Missingness,
    MissingnessStatus,
    OriginClass,
    Requirement,
    SpatialRequirement,
    TemporalKind,
    TemporalRequirement,
    TemporalSupport,
    ValueConstraint,
)


def _local_profile(operation_key: str = "synthetic.constant.v1"
                   ) -> ExecutionProfile:
    implementation = ImplementationRef.from_operation_key(operation_key)
    return ExecutionProfile.bind(
        implementation,
        PlacementRequirements(
            architectures=("x86_64",),
            provider_kinds=("stage1-local-subprocess",),
            resources=ResourceEnvelope(
                min_cpu_cores=1,
                min_memory_mb=64,
                min_gpus=0,
                max_cpu_cores=2,
                max_memory_mb=512,
                max_gpus=0,
            ),
        ),
    )


def _site(profile: ExecutionProfile, *, memory_mb: int = 1024,
          digest: str | None = None) -> SiteClassCapability:
    return SiteClassCapability(
        site_class_id="private-local-cpu",
        architecture="x86_64",
        provider_kinds=("stage1-local-subprocess",),
        implementation_digests=(
            digest or profile.implementation.implementation_sha256,),
        environment_classes=(),
        network_classes=("none",),
        credential_classes=(),
        mount_classes=(),
        policy_classes=(),
        max_cpu_cores=4,
        max_memory_mb=memory_mb,
        max_gpus=0,
    )


def _descriptor(concept: str, *, units: str = "1") -> ArtifactDescriptor:
    return ArtifactDescriptor(
        concept_id=concept,
        schema_version="synthetic-scalar-v1",
        representation="application/json",
        units=units,
        spatial_support=BBoxSupport(
            "EPSG:4326", ("x", "y"), ("-1", "-1", "1", "1")),
        temporal_support=TemporalSupport(TemporalKind.TIME_INVARIANT),
        vertical_support=None,
        grid=None,
        native_resolution=None,
        origin=OriginClass.SYNTHETIC,
        missingness=Missingness(MissingnessStatus.COMPLETE),
    )


def _requirement(concept: str, *, units: str = "1") -> Requirement:
    return Requirement(
        concept_id=concept,
        accepted_schema_versions=("synthetic-scalar-v1",),
        representation=ValueConstraint.exact("application/json"),
        units=ValueConstraint.exact(units),
        spatial=SpatialRequirement(BBoxSupport(
            "EPSG:4326", ("x", "y"), ("-1", "-1", "1", "1"))),
        temporal=TemporalRequirement(TemporalKind.TIME_INVARIANT),
        vertical=None,
        allowed_origins=(OriginClass.SYNTHETIC,),
        max_native_resolution=None,
        max_effective_resolution=None,
        missing_policy=MissingPolicy(),
        minimum_evidence=EvidenceRequirement(allow_unknown_empirical=True),
    )


def _pair_spec() -> tuple[CapabilitySpec, ExecutionProfile]:
    profile = _local_profile("synthetic.pair.v1")
    spec = CapabilitySpec.bind(
        capability_id="synthetic-pair",
        capability_version="1.0.0",
        implementation=profile.implementation,
        binder=BinderRef.from_key("synthetic.pair.bind.v1"),
        input_ports=(),
        output_ports=(
            DescriptorTemplate("left", _descriptor("scalar.left")),
            DescriptorTemplate("right", _descriptor("scalar.right")),
        ),
        parameter_schema=ParameterSchema((
            ParameterField("left", ParameterKind.NUMBER),
            ParameterField("right", ParameterKind.NUMBER),
        )),
        parameterizations=(BindingParameterization(
            {"left": 2, "right": 3}, {"cost_units": 1}),),
        execution_profile_id=profile.profile_id,
    )
    return spec, profile


def test_implementation_is_minted_and_rechecked_from_closed_registry():
    reference = ImplementationRef.from_operation_key("synthetic.constant.v1")
    assert reference.verify_current() == reference.executable_component()

    forged = dataclasses.replace(
        reference, implementation_sha256="0" * 64)
    with pytest.raises(ValueError, match="stale"):
        forged.verify_current()
    with pytest.raises(KeyError, match="closed Stage-1 registry"):
        ImplementationRef.from_operation_key("python:arbitrary.callable")


def test_binder_registry_is_closed_and_stale_refs_are_rejected():
    assert binder_keys() == (
        "synthetic.add.bind.v1",
        "synthetic.constant.bind.v1",
        "synthetic.pair.bind.v1",
    )
    reference = BinderRef.from_key("synthetic.pair.bind.v1")
    assert reference.verify_current().output_ports == ("left", "right")
    with pytest.raises(KeyError, match="unknown closed Stage-2 binder"):
        BinderRef.from_key("module:function")
    forged = dataclasses.replace(reference, implementation_sha256="f" * 64)
    with pytest.raises(ValueError, match="stale"):
        forged.verify_current()


def test_result_implementation_is_separate_from_placement_profile():
    first = _local_profile()
    second = ExecutionProfile.bind(
        first.implementation,
        PlacementRequirements(
            architectures=("x86_64",),
            provider_kinds=("another-compatible-local-provider",),
        ),
    )
    assert first.profile_id != second.profile_id
    assert first.implementation.identity_id == second.implementation.identity_id


def test_frozen_deployment_snapshot_proves_static_compatibility_only():
    profile = _local_profile()
    snapshot = DeploymentCapabilitySnapshot.freeze(
        "2026-08-13T12:00:00Z", (_site(profile),))
    proof = snapshot.check(profile)

    assert proof.feasible
    assert proof.feasible_site_class_ids == ("private-local-cpu",)
    encoded = snapshot.to_dict()
    assert "queue" not in str(encoded).lower()
    assert "free" not in str(encoded).lower()
    assert DeploymentCapabilitySnapshot.from_dict(encoded) == snapshot


def test_deployment_feasibility_returns_structured_rejections():
    profile = _local_profile()
    site = _site(profile, memory_mb=32, digest="1" * 64)
    snapshot = DeploymentCapabilitySnapshot.freeze(
        "2026-08-13T12:00:00Z", (site,))
    proof = snapshot.check(profile)

    assert not proof.feasible
    codes = {rejection.code for rejection in proof.sites[0].rejections}
    assert codes == {
        DeploymentRejectionCode.IMPLEMENTATION_UNAVAILABLE,
        DeploymentRejectionCode.MEMORY_ENVELOPE_UNAVAILABLE,
    }


def test_deployment_snapshot_identity_rejects_tampering():
    profile = _local_profile()
    snapshot = DeploymentCapabilitySnapshot.freeze(
        "2026-08-13T12:00:00Z", (_site(profile),))
    with pytest.raises(ValueError, match="identity does not verify"):
        dataclasses.replace(snapshot, captured_at="2026-08-14T12:00:00Z")


def test_catalog_allows_alternative_producers_for_one_concept():
    first, first_profile = _pair_spec()
    second = CapabilitySpec.bind(
        capability_id="synthetic-pair-alternative",
        capability_version="1.0.0",
        implementation=first_profile.implementation,
        binder=BinderRef.from_key("synthetic.pair.bind.v1"),
        input_ports=(),
        output_ports=first.output_ports,
        parameter_schema=first.parameter_schema,
        parameterizations=(BindingParameterization(
            {"left": 4, "right": 5}, {"cost": 2}),),
        execution_profile_id=first_profile.profile_id,
    )
    catalog = CapabilityCatalog.freeze(
        (first, second), (first_profile,))

    assert len(catalog.lookup("scalar.left")) == 2
    assert CapabilityCatalog.from_dict(catalog.to_dict()) == catalog


def test_duplicate_scientific_binding_with_conflicting_metrics_is_rejected():
    spec, profile = _pair_spec()
    with pytest.raises(ValueError, match="duplicate one bound invocation"):
        CapabilitySpec.bind(
            capability_id="ambiguous-metrics",
            capability_version="1.0.0",
            implementation=profile.implementation,
            binder=spec.binder,
            input_ports=spec.input_ports,
            output_ports=spec.output_ports,
            parameter_schema=spec.parameter_schema,
            parameterizations=(
                BindingParameterization(
                    {"left": 2, "right": 3}, {"cost": 1}),
                BindingParameterization(
                    {"left": 2, "right": 3}, {"cost": 9}),
            ),
            execution_profile_id=profile.profile_id,
        )


def test_pair_binding_preserves_all_outputs_and_is_deterministic():
    spec, profile = _pair_spec()
    catalog = CapabilityCatalog.freeze((spec,), (profile,))
    requirement = _requirement("scalar.left")
    deployment = DeploymentCapabilitySnapshot.freeze(
        "2026-08-13T12:00:00Z", (_site(profile),))

    first = catalog.bind_candidates(
        spec.spec_id, "left", requirement,
        deployment_snapshot=deployment)
    second = catalog.bind_candidates(
        spec.spec_id, "left", requirement,
        deployment_snapshot=deployment)

    assert first.complete and not first.rejected
    assert first.to_dict() == second.to_dict()
    invocation = first.accepted[0].invocation
    assert [output.port_id for output in invocation.outputs] == ["left", "right"]
    assert invocation.output("right").descriptor.concept_id == "scalar.right"
    assert invocation.implementation.verify_current()
    assert type(invocation).from_dict(invocation.to_dict()) == invocation


def test_direct_mismatch_is_structured_and_does_not_insert_conversion():
    spec, profile = _pair_spec()
    catalog = CapabilityCatalog.freeze((spec,), (profile,))
    result = catalog.bind_candidates(
        spec.spec_id, "left", _requirement("scalar.left", units="m"))

    assert not result.accepted
    assert result.complete
    rejection = result.rejected[0]
    assert rejection.code is BindingRejectionCode.DIRECT_MATCH_REJECTED
    assert "UNITS_MISMATCH" in rejection.details["rejection_codes"]


def test_add_binding_retains_distinct_equal_requirement_uses():
    result_descriptor = _descriptor("scalar.sum")
    operand_requirement = _requirement("scalar.operand")
    profile = _local_profile("synthetic.add.v1")
    spec = CapabilitySpec.bind(
        capability_id="synthetic-add",
        capability_version="1.0.0",
        implementation=profile.implementation,
        binder=BinderRef.from_key("synthetic.add.bind.v1"),
        input_ports=(
            InputPortTemplate(
                "left", operand_requirement,
                distinctness=DistinctnessPolicy.DISTINCT_ARTIFACT,
                shareable=False),
            InputPortTemplate(
                "right", operand_requirement,
                distinctness=DistinctnessPolicy.DISTINCT_ARTIFACT,
                shareable=False),
        ),
        output_ports=(DescriptorTemplate("result", result_descriptor),),
        parameter_schema=ParameterSchema(),
        parameterizations=(BindingParameterization({}, {"cost": 1}),),
        execution_profile_id=profile.profile_id,
    )
    catalog = CapabilityCatalog.freeze((spec,), (profile,))
    invocation = catalog.bind_candidates(
        spec.spec_id, "result", _requirement("scalar.sum")
    ).accepted[0].invocation

    assert (invocation.input_uses[0].requirement.requirement_id
            == invocation.input_uses[1].requirement.requirement_id)
    assert (invocation.input_uses[0].requirement_use_id
            != invocation.input_uses[1].requirement_use_id)
    assert {use.port_id for use in invocation.input_uses} == {"left", "right"}


def test_artifact_leaf_is_a_distinct_committed_realization_identity():
    descriptor = _descriptor("scalar.left")
    leaf = ArtifactLeaf.bind(
        artifact_id="a" * 64,
        manifest_root_sha256="b" * 64,
        descriptor=descriptor,
    )

    assert ArtifactLeaf.from_dict(leaf.to_dict()) == leaf
    assert leaf.leaf_id != leaf.artifact_id
    reassessed = dataclasses.replace(leaf, evidence_profile_id="evidence:new")
    assert reassessed.leaf_id == leaf.leaf_id
    assert reassessed.record_id != leaf.record_id
    with pytest.raises(ValueError, match="identity does not verify"):
        dataclasses.replace(leaf, artifact_id="c" * 64)
