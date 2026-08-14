"""Small domain-neutral capability graphs used to verify Stage 3.

The concepts are deliberately meaningless scalar labels.  Wind, terrain, fire,
and every other scientific domain must enter through the same contracts and
catalog interface; none is special-cased by the resolver.
"""
from __future__ import annotations

from dataclasses import dataclass

from capabilities import (
    ArtifactLeaf,
    BinderRef,
    BindingParameterization,
    CapabilityCatalog,
    CapabilitySpec,
    DeploymentCapabilitySnapshot,
    DescriptorTemplate,
    ExecutionProfile,
    ImplementationRef,
    InputPortTemplate,
    ParameterField,
    ParameterKind,
    ParameterSchema,
    PlacementRequirements,
    ResourceEnvelope,
    SiteClassCapability,
)
from contracts import (
    ArtifactDescriptor,
    BBoxSupport,
    EvidenceRequirement,
    MissingPolicy,
    Missingness,
    MissingnessStatus,
    OriginClass,
    Requirement,
    RequirementUse,
    SpatialRequirement,
    TemporalKind,
    TemporalRequirement,
    TemporalSupport,
    ValueConstraint,
)


@dataclass(frozen=True)
class CompositionFixture:
    """Frozen catalog and request for the pair-plus-add counterexample."""

    catalog: CapabilityCatalog
    deployment_snapshot: DeploymentCapabilitySnapshot
    root_uses: tuple[RequirementUse, ...]
    offered_artifact_leaves: tuple[ArtifactLeaf, ...]
    expected_result: int


def make_composition_fixture() -> CompositionFixture:
    """Return a graph whose optimum requires recognizing co-production.

    A two-output invocation produces ``left=20`` and ``right=22`` for cost 2.
    Independent producers cost 4 each, and the final add costs 1.  Recursive
    discovery must intern the pair invocation reached through both inputs, and
    global selection must count it once, yielding a total cost of 3.
    """
    left_descriptor = _descriptor("example.scalar.left")
    right_descriptor = _descriptor("example.scalar.right")
    sum_descriptor = _descriptor("example.scalar.sum")
    left_requirement = _requirement("example.scalar.left")
    right_requirement = _requirement("example.scalar.right")
    sum_requirement = _requirement("example.scalar.sum")

    pair_profile = _profile("synthetic.pair.v1")
    left_profile = _profile("synthetic.constant.v1")
    right_profile = left_profile
    add_profile = _profile("synthetic.add.v1")

    pair_spec = CapabilitySpec.bind(
        capability_id="example-pair",
        capability_version="1.0.0",
        implementation=pair_profile.implementation,
        binder=BinderRef.from_key("synthetic.pair.bind.v1"),
        input_ports=(),
        output_ports=(
            DescriptorTemplate("left", left_descriptor),
            DescriptorTemplate("right", right_descriptor),
        ),
        parameter_schema=ParameterSchema((
            ParameterField("left", ParameterKind.NUMBER),
            ParameterField("right", ParameterKind.NUMBER),
        )),
        parameterizations=(BindingParameterization(
            {"left": 20, "right": 22}, {"cost_units": 2}),),
        execution_profile_id=pair_profile.profile_id,
    )
    left_spec = _constant_spec(
        "example-left-constant", left_descriptor, 20, left_profile, cost=4)
    right_spec = _constant_spec(
        "example-right-constant", right_descriptor, 22, right_profile, cost=4)
    add_spec = CapabilitySpec.bind(
        capability_id="example-add",
        capability_version="1.0.0",
        implementation=add_profile.implementation,
        binder=BinderRef.from_key("synthetic.add.bind.v1"),
        input_ports=(
            InputPortTemplate("left", left_requirement),
            InputPortTemplate("right", right_requirement),
        ),
        output_ports=(DescriptorTemplate("result", sum_descriptor),),
        parameter_schema=ParameterSchema(),
        parameterizations=(BindingParameterization(
            {}, {"cost_units": 1}),),
        execution_profile_id=add_profile.profile_id,
    )
    profiles = tuple({value.profile_id: value for value in (
        pair_profile, left_profile, right_profile, add_profile)}.values())
    catalog = CapabilityCatalog.freeze(
        (pair_spec, left_spec, right_spec, add_spec), profiles)
    deployment = _deployment_snapshot(catalog)
    leaf = ArtifactLeaf.bind(
        artifact_id="a" * 64,
        manifest_root_sha256="b" * 64,
        descriptor=left_descriptor,
    )
    return CompositionFixture(
        catalog=catalog,
        deployment_snapshot=deployment,
        root_uses=(RequirementUse("stage3-root", "result", sum_requirement),),
        offered_artifact_leaves=(leaf,),
        expected_result=42,
    )


def _constant_spec(
    capability_id: str,
    descriptor: ArtifactDescriptor,
    value: int,
    profile: ExecutionProfile,
    *,
    cost: int,
) -> CapabilitySpec:
    return CapabilitySpec.bind(
        capability_id=capability_id,
        capability_version="1.0.0",
        implementation=profile.implementation,
        binder=BinderRef.from_key("synthetic.constant.bind.v1"),
        input_ports=(),
        output_ports=(DescriptorTemplate("result", descriptor),),
        parameter_schema=ParameterSchema((
            ParameterField("value", ParameterKind.NUMBER),)),
        parameterizations=(BindingParameterization(
            {"value": value}, {"cost_units": cost}),),
        execution_profile_id=profile.profile_id,
    )


def _descriptor(concept_id: str) -> ArtifactDescriptor:
    return ArtifactDescriptor(
        concept_id=concept_id,
        schema_version="example-scalar-v1",
        representation="application/json",
        units="1",
        spatial_support=BBoxSupport(
            "EPSG:4326", ("x", "y"), ("-1", "-1", "1", "1")),
        temporal_support=TemporalSupport(TemporalKind.TIME_INVARIANT),
        vertical_support=None,
        grid=None,
        native_resolution=None,
        origin=OriginClass.SYNTHETIC,
        missingness=Missingness(MissingnessStatus.COMPLETE),
    )


def _requirement(concept_id: str) -> Requirement:
    return Requirement(
        concept_id=concept_id,
        accepted_schema_versions=("example-scalar-v1",),
        representation=ValueConstraint.exact("application/json"),
        units=ValueConstraint.exact("1"),
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


def _profile(operation_key: str) -> ExecutionProfile:
    implementation = ImplementationRef.from_operation_key(operation_key)
    return ExecutionProfile.bind(
        implementation,
        PlacementRequirements(
            architectures=("x86_64",),
            provider_kinds=("stage1-local-subprocess",),
            resources=ResourceEnvelope(
                min_cpu_cores=1,
                min_memory_mb=64,
                max_cpu_cores=1,
                max_memory_mb=128,
            ),
        ),
    )


def _deployment_snapshot(
    catalog: CapabilityCatalog,
) -> DeploymentCapabilitySnapshot:
    implementation_digests = tuple(sorted({
        value.implementation.implementation_sha256
        for value in catalog.execution_profiles
    }))
    return DeploymentCapabilitySnapshot.freeze(
        "2026-08-13T00:00:00Z",
        (SiteClassCapability(
            site_class_id="private-node-example-cpu",
            architecture="x86_64",
            provider_kinds=("stage1-local-subprocess",),
            implementation_digests=implementation_digests,
            environment_classes=(),
            network_classes=("none",),
            credential_classes=(),
            mount_classes=(),
            policy_classes=(),
            max_cpu_cores=1,
            max_memory_mb=128,
            max_gpus=0,
        ),),
    )
