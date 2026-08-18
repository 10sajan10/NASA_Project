"""Adversarial gates for reserved acquisition capability authority."""
from __future__ import annotations

from pathlib import Path

import pytest

from acquisition import BoundAssetManifest
from capabilities import (
    BindingParameterization,
    BoundInvocation,
    CapabilitySpec,
    DescriptorTemplate,
)
from contracts import OriginClass
from engine.runtime.identity import strict_copy, strict_hash
from stage5 import fixtures as fx
from stage5.demo import build_demo_plan


@pytest.fixture()
def lowered_acquisition(tmp_path: Path):
    demo = build_demo_plan(tmp_path / "authority-plan")
    capability = next(
        item for item in demo.catalog.capabilities
        if (item.acquisition_authority is not None
            and item.acquisition_authority.content_binding["binding_id"]
            == demo.coarse_content.binding_id))
    return demo, capability


def _rebind(
        capability: CapabilitySpec, *, descriptor=None,
        parameterization=None, authority="original",
) -> CapabilitySpec:
    return CapabilitySpec.bind(
        capability_id=capability.capability_id,
        capability_version=capability.capability_version,
        implementation=capability.implementation,
        binder=capability.binder,
        input_ports=capability.input_ports,
        output_ports=(DescriptorTemplate(
            "result",
            capability.output_ports[0].descriptor
            if descriptor is None else descriptor,
        ),),
        parameter_schema=capability.parameter_schema,
        parameterizations=(
            capability.parameterizations[0]
            if parameterization is None else parameterization,
        ),
        execution_profile_id=capability.execution_profile_id,
        applicability_key=capability.applicability_key,
        evidence_profile_id=capability.evidence_profile_id,
        cost_model_id=capability.cost_model_id,
        acquisition_authority=(
            capability.acquisition_authority
            if authority == "original" else authority),
    )


def test_safe_acquisition_authority_round_trips_to_a_bound_invocation(
        lowered_acquisition):
    _demo, capability = lowered_acquisition
    authority = capability.acquisition_authority
    assert authority is not None
    assert authority.source_schema_id == \
        capability.parameterizations[0].parameters[
            "content_binding"]["source_schema_id"]
    assert authority.descriptor_id == \
        capability.output_ports[0].descriptor.descriptor_id
    assert CapabilitySpec.from_dict(capability.to_dict()) == capability

    invocation = BoundInvocation.bind(
        capability, capability.parameterizations[0])
    assert invocation.acquisition_authority == authority
    assert BoundInvocation.from_dict(invocation.to_dict()) == invocation


def test_reserved_materializer_rejects_forged_output_even_with_real_content(
        lowered_acquisition):
    demo, capability = lowered_acquisition
    forged = fx.descriptor(
        "example.forged.temperature",
        "K",
        demo.coarse_content.descriptor.spatial_support.bounds,
        OriginClass.OBSERVATION,
    )

    with pytest.raises(ValueError, match="requires authenticated"):
        _rebind(capability, descriptor=forged, authority=None)

    # Stealing the genuine content certificate is insufficient: the result
    # port's exact descriptor is replayed from the fetched-content binding.
    with pytest.raises(
            ValueError, match="acquisition authority disagrees.*output_ports"):
        _rebind(capability, descriptor=forged)


def test_reserved_materializer_rejects_readdressed_source_schema(
        lowered_acquisition):
    _demo, capability = lowered_acquisition
    binding = strict_copy(
        capability.parameterizations[0].parameters["content_binding"])
    binding["source_schema_id"] = "0" * 64
    identity_payload = strict_copy(binding)
    identity_payload.pop("binding_id")
    binding["binding_id"] = strict_hash(identity_payload)
    forged_parameterization = BindingParameterization(
        {"content_binding": binding},
        strict_copy(capability.parameterizations[0].metric_estimates),
    )

    with pytest.raises(
            ValueError, match="acquisition authority disagrees.*parameterizations"):
        _rebind(capability, parameterization=forged_parameterization)


def test_bound_invocation_replays_the_authoritative_output_descriptor(
        lowered_acquisition):
    demo, capability = lowered_acquisition
    invocation = BoundInvocation.bind(
        capability, capability.parameterizations[0])
    forged = fx.descriptor(
        "example.forged.temperature",
        "K",
        demo.coarse_content.descriptor.spatial_support.bounds,
        OriginClass.OBSERVATION,
    )
    serialized = invocation.to_dict()
    serialized["outputs"][0]["descriptor"] = forged.to_dict()

    with pytest.raises(
            ValueError, match="acquisition authority disagrees.*outputs"):
        BoundInvocation.from_dict(serialized)


def test_fully_readdressed_content_descriptor_cannot_override_source_schema(
        lowered_acquisition):
    """Self-consistent hashes do not turn a relabelled receipt into authority."""
    demo, capability = lowered_acquisition
    forged = fx.descriptor(
        "example.forged.temperature",
        "K",
        demo.coarse_content.descriptor.spatial_support.bounds,
        OriginClass.OBSERVATION,
    )
    serialized = capability.to_dict()

    content_binding = strict_copy(
        serialized["parameterizations"][0]["parameters"]["content_binding"])
    content_binding["descriptor"] = forged.to_dict()
    content_identity = strict_copy(content_binding)
    content_identity.pop("binding_id")
    content_binding["binding_id"] = strict_hash(content_identity)

    authority = serialized["acquisition_authority"]
    authority["content_binding"] = strict_copy(content_binding)
    authority["descriptor_id"] = forged.descriptor_id
    authority_identity = strict_copy(authority)
    authority_identity.pop("authority_id")
    authority["authority_id"] = strict_hash({
        "schema": "stage8r-acquisition-authority-v1",
        **authority_identity,
    })

    serialized["capability_id"] = (
        f"acquire:{content_binding['source_id']}:"
        f"{content_binding['binding_id'][:16]}")
    serialized["output_ports"][0]["descriptor"] = forged.to_dict()
    serialized["parameterizations"][0]["parameters"][
        "content_binding"] = strict_copy(content_binding)
    capability_identity = strict_copy(serialized)
    capability_identity.pop("spec_id")
    serialized["spec_id"] = strict_hash({
        "schema": "stage2-capability-spec-v1",
        **capability_identity,
    })

    with pytest.raises(
            ValueError,
            match="not derived from authoritative source schema and coverage"):
        CapabilitySpec.from_dict(serialized)


def test_fully_readdressed_coverage_claim_is_independently_replayed(
        lowered_acquisition):
    _demo, capability = lowered_acquisition
    serialized = capability.to_dict()
    authority = serialized["acquisition_authority"]
    bound_record = strict_copy(authority["bound_manifest"])
    # COMPLETE permits explanatory text as data, so all record identities can
    # be readdressed.  The authoritative replay must still reproduce the exact
    # assessment from manifest rows rather than trusting that claim.
    bound_record["coverage"]["detail"] = "caller-authored coverage claim"
    rebound = BoundAssetManifest.from_dict(bound_record)
    authority["bound_manifest"] = strict_copy(bound_record)
    authority["coverage_contract_id"] = rebound.coverage_contract_id

    content_binding = strict_copy(authority["content_binding"])
    content_binding["coverage_contract_id"] = rebound.coverage_contract_id
    content_identity = strict_copy(content_binding)
    content_identity.pop("binding_id")
    content_binding["binding_id"] = strict_hash(content_identity)
    authority["content_binding"] = strict_copy(content_binding)

    authority_identity = strict_copy(authority)
    authority_identity.pop("authority_id")
    authority["authority_id"] = strict_hash({
        "schema": "stage8r-acquisition-authority-v1",
        **authority_identity,
    })
    serialized["capability_id"] = (
        f"acquire:{content_binding['source_id']}:"
        f"{content_binding['binding_id'][:16]}")
    serialized["parameterizations"][0]["parameters"][
        "content_binding"] = strict_copy(content_binding)
    capability_identity = strict_copy(serialized)
    capability_identity.pop("spec_id")
    serialized["spec_id"] = strict_hash({
        "schema": "stage2-capability-spec-v1",
        **capability_identity,
    })

    with pytest.raises(ValueError, match="coverage proof does not replay"):
        CapabilitySpec.from_dict(serialized)
