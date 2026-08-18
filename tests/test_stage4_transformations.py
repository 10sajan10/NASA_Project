"""Stage-4 transformation contract and finite-closure tests.

These cover the *declaration* and *search* layers.  Execution semantics of the
closed operations are covered separately by ``test_stage4_runtime_ops.py``.
"""
from __future__ import annotations

import dataclasses

import pytest

from capabilities import (
    BindingParameterization,
    BoundInvocation,
    CapabilityCatalog,
    CapabilitySpec,
    TransformationAuthority,
)
from contracts import IntrinsicUncertainty, OriginClass
from stage4.fixtures import (
    CONCEPT,
    make_unit_bridge_fixture,
    make_vector_decomposition_spec,
)
from engine.runtime.identity import strict_copy
from transformations import (
    TransformationKind,
    TransformationLimitCode,
    TransformationLossPolicy,
    TransformationPort,
    TransformationSearchLimits,
    TransformationSpec,
    UncertaintyPropagationPolicy,
    ValueSemantics,
    expand_transform_catalog,
    unit_affine_rule,
    unit_affine_rules,
)


def _fixture_descriptors():
    """Return the ``m`` and derived ``km`` descriptors from the fixture."""
    fixture = make_unit_bridge_fixture()
    bridge = fixture.transformations[0]
    return bridge.input_ports[0].descriptor, bridge.output_ports[0].descriptor


# --------------------------------------------------------------------------
# Declaration: a transformation is an explicit, closed, versioned contract.
# --------------------------------------------------------------------------

def test_unit_conversion_coefficients_come_only_from_the_closed_registry():
    metres, kilometres = _fixture_descriptors()
    rule = unit_affine_rule("m", "km")

    spec = TransformationSpec.bind_unit_affine(
        transformation_id="registry-check",
        transformation_version="1.0.0",
        execution_profile=make_unit_bridge_fixture()
        .transformations[0].execution_profile,
        source=metres,
        result=kilometres,
        cost_units=1,
    )

    assert spec.parameters == rule.parameters
    assert spec.kind is TransformationKind.UNIT_AFFINE
    # The coefficient is authority-bearing, so it must be the registry value
    # rather than anything a caller happened to pass.
    assert spec.parameters["factor"] == 0.001
    assert spec.semantic_rule_id == "semantic:unit-affine-registry-v1"


def test_unregistered_unit_pair_cannot_become_a_transformation():
    metres, _ = _fixture_descriptors()
    with pytest.raises(KeyError):
        unit_affine_rule("m", "furlong")


def test_registry_rules_are_unique_and_canonical():
    rules = unit_affine_rules()
    pairs = [(value.source_units, value.target_units) for value in rules]
    assert len(pairs) == len(set(pairs))
    assert all(value.factor != 0 for value in rules)


def test_transformation_output_must_declare_derived_origin():
    metres, kilometres = _fixture_descriptors()
    profile = make_unit_bridge_fixture().transformations[0].execution_profile
    # Re-labelling a converted product as an original observation would be a
    # provenance forgery, so the contract layer refuses it.
    synthetic_result = dataclasses.replace(
        kilometres, origin=OriginClass.SYNTHETIC)
    with pytest.raises(ValueError, match="DERIVED"):
        TransformationSpec.bind_unit_affine(
            transformation_id="origin-forgery",
            transformation_version="1.0.0",
            execution_profile=profile,
            source=metres,
            result=synthetic_result,
            cost_units=1,
        )


def test_unit_conversion_may_not_change_non_unit_metadata():
    metres, kilometres = _fixture_descriptors()
    profile = make_unit_bridge_fixture().transformations[0].execution_profile
    # A unit conversion that also silently renamed the concept would hide a
    # scientific claim inside a formatting step.
    relabelled = dataclasses.replace(
        kilometres, concept_id=f"{CONCEPT}.other")
    with pytest.raises(ValueError, match="only units"):
        TransformationSpec.bind_unit_affine(
            transformation_id="concept-drift",
            transformation_version="1.0.0",
            execution_profile=profile,
            source=metres,
            result=relabelled,
            cost_units=1,
        )


def test_vector_decomposition_declares_distinct_typed_outputs():
    spec = make_vector_decomposition_spec()

    assert spec.kind is TransformationKind.VECTOR_UV_TO_SPEED_DIRECTION
    ports = {value.port_id: value for value in spec.output_ports}
    assert set(ports) == {"speed", "direction"}
    assert ports["speed"].value_semantics \
        is ValueSemantics.SCALAR_CONTINUOUS_INTENSIVE
    assert ports["direction"].value_semantics is ValueSemantics.CIRCULAR_DIRECTION
    assert ports["direction"].descriptor.units == "degree"
    # The zero-vector and direction conventions are recorded as explicit,
    # versioned scientific assumptions rather than left to platform atan2.
    assert "vector:calm-direction-zero-v1" in spec.scientific_assumption_ids
    assert all(value.descriptor.origin is OriginClass.DERIVED
               for value in spec.output_ports)


def test_vector_decomposition_rejects_untyped_vector_input():
    spec = make_vector_decomposition_spec()
    source = spec.input_ports[0]
    with pytest.raises(ValueError, match="canonical U/V"):
        TransformationSpec.bind(
            transformation_id="untyped-vector",
            transformation_version="1.0.0",
            kind=TransformationKind.VECTOR_UV_TO_SPEED_DIRECTION,
            execution_profile=spec.execution_profile,
            input_ports=(TransformationPort(
                "source", source.descriptor, ValueSemantics.UNSPECIFIED),),
            output_ports=spec.output_ports,
            parameters={},
            cost_units=1,
        )


def test_transformation_identity_is_content_addressed_and_deterministic():
    first = make_vector_decomposition_spec()
    second = make_vector_decomposition_spec()

    assert first.spec_id == second.spec_id == first.expected_id()
    assert TransformationSpec.from_dict(first.to_dict()) == first
    assert first.to_capability_spec().spec_id \
        == second.to_capability_spec().spec_id


def test_unit_mapping_requires_known_uncertainty_to_be_rebound_or_unknown():
    bridge = make_unit_bridge_fixture().transformations[0]
    manufactured = dataclasses.replace(
        bridge.output_ports[0].descriptor,
        intrinsic_uncertainty=IntrinsicUncertainty.known(
            "uncertainty:invented-kilometres-v1", "c" * 64))
    with pytest.raises(ValueError, match="UNKNOWN source uncertainty"):
        TransformationSpec.bind_unit_affine(
            transformation_id="manufactured-known-uncertainty",
            transformation_version="1.0.0",
            execution_profile=bridge.execution_profile,
            source=bridge.input_ports[0].descriptor,
            result=manufactured,
            cost_units=1,
        )

    source_uncertainty = IntrinsicUncertainty.known(
        "uncertainty:absolute-metres-v1", "a" * 64)
    source = dataclasses.replace(
        bridge.input_ports[0].descriptor,
        intrinsic_uncertainty=source_uncertainty)

    unchanged = dataclasses.replace(
        bridge.output_ports[0].descriptor,
        intrinsic_uncertainty=source_uncertainty)
    with pytest.raises(ValueError, match="cannot preserve a KNOWN"):
        TransformationSpec.bind_unit_affine(
            transformation_id="unchanged-known-uncertainty",
            transformation_version="1.0.0",
            execution_profile=bridge.execution_profile,
            source=source,
            result=unchanged,
            cost_units=1,
        )

    propagated = dataclasses.replace(
        bridge.output_ports[0].descriptor,
        intrinsic_uncertainty=IntrinsicUncertainty.known(
            "uncertainty:absolute-kilometres-v1", "b" * 64))
    rebound = TransformationSpec.bind_unit_affine(
        transformation_id="rebound-known-uncertainty",
        transformation_version="1.0.0",
        execution_profile=bridge.execution_profile,
        source=source,
        result=propagated,
        cost_units=1,
    )
    assert rebound.loss_policy \
        is TransformationLossPolicy.NUMERIC_AFFINE_MAPPING
    assert rebound.uncertainty_propagation_policy \
        is UncertaintyPropagationPolicy.REBIND_OR_MARK_UNKNOWN
    assert TransformationSpec.from_dict(rebound.to_dict()) == rebound


def test_transformation_lowers_to_an_ordinary_capability():
    bridge = make_unit_bridge_fixture().transformations[0]
    capability = bridge.to_capability_spec()

    # Stage 3 needs no transform-specific code path: the edge becomes a normal
    # multi-port capability whose declared cost competes with direct data.
    assert capability.capability_id == "transform:example-metres-to-kilometres"
    assert len(capability.input_ports) == 1
    assert len(capability.output_ports) == 1
    assert capability.parameterizations[0].metric_estimates["cost_units"] == 1


def test_lowering_retains_content_addressed_semantic_authority_roundtrip():
    bridge = make_unit_bridge_fixture().transformations[0]
    capability = bridge.to_capability_spec()
    authority = capability.transformation_authority

    assert authority is not None
    assert authority.transformation_spec_id == bridge.spec_id
    assert authority.kind == TransformationKind.UNIT_AFFINE.value
    assert authority.semantic_rule_id == bridge.semantic_rule_id
    assert authority.loss_policy == bridge.loss_policy.value
    assert authority.uncertainty_propagation_policy \
        == bridge.uncertainty_propagation_policy.value
    assert authority.transformation_spec["input_ports"][0][
        "value_semantics"] == ValueSemantics.SCALAR_CONTINUOUS_INTENSIVE.value
    assert strict_copy(
        authority.transformation_spec["input_ports"][0]["descriptor"]) \
        == bridge.input_ports[0].descriptor.to_dict()
    with pytest.raises(TypeError, match="immutable"):
        authority.transformation_spec["parameters"]["factor"] = 666
    assert CapabilitySpec.from_dict(capability.to_dict()) == capability

    invocation = BoundInvocation.bind(
        capability, capability.parameterizations[0])
    assert invocation.transformation_authority == authority
    assert BoundInvocation.from_dict(invocation.to_dict()) == invocation


def _rebind_transform_capability(
        capability, *, parameters, authority):
    return CapabilitySpec.bind(
        capability_id=capability.capability_id,
        capability_version=capability.capability_version,
        implementation=capability.implementation,
        binder=capability.binder,
        input_ports=capability.input_ports,
        output_ports=capability.output_ports,
        parameter_schema=capability.parameter_schema,
        parameterizations=(BindingParameterization(
            parameters, {"cost_units": 0}),),
        execution_profile_id=capability.execution_profile_id,
        transformation_authority=authority,
    )


def test_raw_reserved_transform_capability_cannot_forge_factor_666():
    capability = make_unit_bridge_fixture().transformations[0].to_capability_spec()

    with pytest.raises(ValueError, match="require authenticated"):
        _rebind_transform_capability(
            capability,
            parameters={"factor": 666, "offset": 0},
            authority=None,
        )

    # Stealing a genuine certificate is also insufficient: the exact closed
    # parameters and cost are part of the authority replay.
    with pytest.raises(ValueError, match="capability lowering"):
        _rebind_transform_capability(
            capability,
            parameters={"factor": 666, "offset": 0},
            authority=capability.transformation_authority,
        )


def test_readdressed_certificate_tampering_still_fails_semantic_replay():
    bridge = make_unit_bridge_fixture().transformations[0]
    capability = bridge.to_capability_spec()
    authority = capability.transformation_authority
    assert authority is not None

    forged_rule = TransformationAuthority.bind(
        transformation_spec=bridge.to_dict(),
        semantic_rule_implementation_sha256="0" * 64,
        parameter_rule_sha256=authority.parameter_rule_sha256,
    )
    # The outer certificate has a correct content hash.  It still cannot
    # substitute a caller-chosen semantic implementation digest.
    assert forged_rule.authority_id == forged_rule.expected_id()
    with pytest.raises(ValueError, match="semantic-rule implementation"):
        _rebind_transform_capability(
            capability,
            parameters=dict(bridge.parameters),
            authority=forged_rule,
        )

    changed_spec = bridge.to_dict()
    changed_spec["parameters"]["factor"] = 666
    readdressed = TransformationAuthority.bind(
        transformation_spec=changed_spec,
        semantic_rule_implementation_sha256=
            authority.semantic_rule_implementation_sha256,
        parameter_rule_sha256=authority.parameter_rule_sha256,
    )
    assert readdressed.authority_id == readdressed.expected_id()
    with pytest.raises(ValueError, match="unit coefficients|specification identity"):
        _rebind_transform_capability(
            capability,
            parameters={"factor": 666, "offset": 0},
            authority=readdressed,
        )

    changed_policy = bridge.to_dict()
    changed_policy["loss_policy"] = \
        TransformationLossPolicy.EXACT_SELECTION.value
    readdressed_policy = TransformationAuthority.bind(
        transformation_spec=changed_policy,
        semantic_rule_implementation_sha256=
            authority.semantic_rule_implementation_sha256,
        parameter_rule_sha256=authority.parameter_rule_sha256,
    )
    assert readdressed_policy.authority_id != authority.authority_id
    with pytest.raises(ValueError, match="loss policy"):
        _rebind_transform_capability(
            capability,
            parameters=dict(bridge.parameters),
            authority=readdressed_policy,
        )

    changed_uncertainty_policy = bridge.to_dict()
    changed_uncertainty_policy["uncertainty_propagation_policy"] = \
        UncertaintyPropagationPolicy.PRESERVE.value
    readdressed_uncertainty_policy = TransformationAuthority.bind(
        transformation_spec=changed_uncertainty_policy,
        semantic_rule_implementation_sha256=
            authority.semantic_rule_implementation_sha256,
        parameter_rule_sha256=authority.parameter_rule_sha256,
    )
    assert readdressed_uncertainty_policy.authority_id \
        != authority.authority_id
    with pytest.raises(ValueError, match="uncertainty policy"):
        _rebind_transform_capability(
            capability,
            parameters=dict(bridge.parameters),
            authority=readdressed_uncertainty_policy,
        )


# --------------------------------------------------------------------------
# Search: finite closure, honest truncation.
# --------------------------------------------------------------------------

def test_closure_reaches_the_transformed_state_and_reports_complete():
    fixture = make_unit_bridge_fixture()
    expansion = expand_transform_catalog(
        fixture.base_catalog, fixture.transformations)

    assert expansion.complete
    assert expansion.limit_reasons == ()
    assert len(expansion.transitions) == 1
    assert isinstance(expansion.augmented_catalog, CapabilityCatalog)
    # Base capabilities survive and the transform joins them.
    augmented_ids = {value.spec_id
                     for value in expansion.augmented_catalog.capabilities}
    base_ids = {value.spec_id for value in fixture.base_catalog.capabilities}
    assert base_ids < augmented_ids
    assert len(augmented_ids) == len(base_ids) + 1


def test_closure_is_deterministic_for_identical_inputs():
    fixture = make_unit_bridge_fixture()
    first = expand_transform_catalog(
        fixture.base_catalog, fixture.transformations)
    second = expand_transform_catalog(
        fixture.base_catalog, fixture.transformations)

    assert first.expansion_id == second.expansion_id
    assert first.augmented_catalog.catalog_id == second.augmented_catalog.catalog_id


def test_transform_with_unavailable_input_is_a_frontier_not_a_failure():
    fixture = make_unit_bridge_fixture()
    # A catalog with no producer for the transform's input descriptor: the
    # edge is simply unreachable, which is ordinary and must not truncate.
    empty = CapabilityCatalog.freeze((), ())
    expansion = expand_transform_catalog(empty, fixture.transformations)

    assert expansion.transitions == ()
    assert len(expansion.frontier) == 1
    assert expansion.frontier[0].code.value == "MISSING_INPUT_DESCRIPTORS"
    # Unreachable is not truncated: completeness is preserved.
    assert expansion.complete


@pytest.mark.parametrize(
    "limits,expected_code",
    [
        (TransformationSearchLimits(max_depth=0),
         TransformationLimitCode.MAX_DEPTH),
        (TransformationSearchLimits(max_transformations=0),
         TransformationLimitCode.MAX_TRANSFORMATIONS),
        (TransformationSearchLimits(max_descriptor_states=1),
         TransformationLimitCode.MAX_DESCRIPTOR_STATES),
    ],
)
def test_every_activated_bound_marks_the_closure_incomplete(
        limits, expected_code):
    fixture = make_unit_bridge_fixture()
    expansion = expand_transform_catalog(
        fixture.base_catalog, fixture.transformations, limits=limits)

    assert not expansion.complete
    assert expected_code in {value.code for value in expansion.limit_reasons}
    # A truncation must name what it dropped rather than terminate silently.
    assert all(value.subject_ids for value in expansion.limit_reasons)
