"""Domain-neutral Stage-6 dataset-versus-model fixtures.

The shape under test is the one the roadmap draws:

```text
result
    AND ignition
    AND fuel
    AND terrain
    AND flow
          OR direct fine-grid source
          OR coarse source -> lightweight model
```

Everything is meaningless. ``flow`` is wind-shaped only in the sense that it is
a field with units, a CRS, a time window, and two genuinely different ways to
obtain it — one observed, one modelled — with different declared evidence.

Two things this fixture must make executable rather than aspirational:

* the model producer is admissible **only where its evidence applies**, so the
  same request outside that region must fall back to the direct source; and
* terrain is consumed by both the consequence model and the downscaling model,
  so a correct selector must count it once.  That shared input is the reason
  per-requirement greedy selection is wrong, and it is wired in deliberately.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from capabilities import (
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
    invocation_evidence_subject,
)
from contracts import (
    ArtifactDescriptor,
    BBoxSupport,
    BoundKind,
    EstimateUncertainty,
    EvidenceApplicability,
    EvidenceClaim,
    EvidenceProfile,
    EvidenceRequirement,
    EvidenceSnapshot,
    MetricDefinition,
    MetricEvaluator,
    MissingPolicy,
    Missingness,
    MissingnessStatus,
    OriginClass,
    Requirement,
    RequirementUse,
    SampleSemantics,
    SpatialRequirement,
    TemporalKind,
    TemporalRequirement,
    TemporalSupport,
    ValueConstraint,
)

SCHEMA_VERSION = "example-field-v1"
REPRESENTATION = "application/json"
SCALAR_SCHEMA = "example-scalar-v1"

FLOW_CONCEPT = "example.field.flow"
TERRAIN_CONCEPT = "example.field.support"
FUEL_CONCEPT = "example.scalar.fuel"
IGNITION_CONCEPT = "example.scalar.ignition"
RESULT_CONCEPT = "example.field.consequence"

UNITS = "m.s-1"
SCALAR_UNITS = "1"
CRS = "EPSG:4326"
AXES = ("x", "y")

IN_SCOPE_BOUNDS = ("0", "0", "4", "4")
OUT_OF_SCOPE_BOUNDS = ("20", "20", "24", "24")
WINDOW_START = "2026-01-01T00:00:00Z"
WINDOW_END = "2026-01-01T02:00:00Z"
CADENCE_S = "3600"
MAX_GAP_S = "0"

METRIC_ID = "example.metric.error"
PROTOCOL_ID = "example.protocol.block-bootstrap.v1"
EVALUATOR_ID = "example-evaluator"
REFERENCE_MANIFEST_ID = "example-reference-manifest-v1"
OTHER_REFERENCE_MANIFEST_ID = "example-other-reference-manifest-v1"

# Costs: the modelled path (1 + 3) must beat the direct source (6) so the
# minimum-cost policy has a real contest to resolve.  Terrain is shared by two
# consumers and must be paid for once.
CONSEQUENCE_COST = 4
DIRECT_FLOW_COST = 6
COARSE_FLOW_COST = 1
MODEL_COST = 3
TERRAIN_COST = 1
FUEL_COST = 1
IGNITION_COST = 1

MINIMUM_COST_TOTAL = (CONSEQUENCE_COST + COARSE_FLOW_COST + MODEL_COST
                      + TERRAIN_COST + FUEL_COST + IGNITION_COST)   # 11
DIRECT_PATH_TOTAL = (CONSEQUENCE_COST + DIRECT_FLOW_COST + TERRAIN_COST
                     + FUEL_COST + IGNITION_COST)                    # 13

TIME_STEPS = ("2026-01-01T00:00:00Z", "2026-01-01T01:00:00Z",
              "2026-01-01T02:00:00Z")
X_COORDS = (0.0, 2.0, 4.0)
Y_COORDS = (0.0, 2.0, 4.0)

COARSE_VALUE = 2.0
DIRECT_VALUE = 9.0
TERRAIN_VALUE = 1.0
MODEL_GAIN = 4.0
FUEL_VALUE = 2.0
IGNITION_VALUE = 3.0
THRESHOLD = 1000.0

DIRECT_ERROR = "0.90"
MODEL_ERROR = "0.50"


@dataclass(frozen=True)
class Stage6Fixture:
    catalog: CapabilityCatalog
    deployment_snapshot: DeploymentCapabilitySnapshot
    evidence_snapshot: EvidenceSnapshot
    root_uses: tuple[RequirementUse, ...]
    flow_use: RequirementUse
    expected_cost_units: int
    expected_capability_ids: tuple[str, ...]


# -- primitives ----------------------------------------------------------


def _window() -> TemporalSupport:
    return TemporalSupport(
        TemporalKind.SERIES, start=WINDOW_START, end=WINDOW_END,
        cadence_s=CADENCE_S, max_gap_s=MAX_GAP_S,
        sample_semantics=SampleSemantics.INSTANTANEOUS)


def _field_descriptor(concept_id: str, origin: OriginClass, *,
                      bounds: tuple[str, str, str, str] = IN_SCOPE_BOUNDS,
                      units: str = UNITS) -> ArtifactDescriptor:
    return ArtifactDescriptor(
        concept_id=concept_id, schema_version=SCHEMA_VERSION,
        representation=REPRESENTATION, units=units,
        spatial_support=BBoxSupport(CRS, AXES, bounds),
        temporal_support=_window(), vertical_support=None, grid=None,
        native_resolution=None, origin=origin,
        missingness=Missingness(MissingnessStatus.COMPLETE))


def _scalar_descriptor(concept_id: str) -> ArtifactDescriptor:
    return ArtifactDescriptor(
        concept_id=concept_id, schema_version=SCALAR_SCHEMA,
        representation=REPRESENTATION, units=SCALAR_UNITS,
        spatial_support=BBoxSupport(CRS, AXES, IN_SCOPE_BOUNDS),
        temporal_support=_window(), vertical_support=None, grid=None,
        native_resolution=None, origin=OriginClass.SYNTHETIC,
        missingness=Missingness(MissingnessStatus.COMPLETE))


def _requirement(concept_id: str, *, schema: str, units: str,
                 origins: tuple[OriginClass, ...],
                 bounds: tuple[str, str, str, str] = IN_SCOPE_BOUNDS,
                 evidence: EvidenceRequirement | None = None) -> Requirement:
    return Requirement(
        concept_id=concept_id, accepted_schema_versions=(schema,),
        representation=ValueConstraint.exact(REPRESENTATION),
        units=ValueConstraint.exact(units),
        spatial=SpatialRequirement(BBoxSupport(CRS, AXES, bounds)),
        temporal=TemporalRequirement(
            TemporalKind.SERIES, start=WINDOW_START, end=WINDOW_END,
            cadence_s=CADENCE_S),
        vertical=None, allowed_origins=origins, max_native_resolution=None,
        max_effective_resolution=None, missing_policy=MissingPolicy(),
        minimum_evidence=evidence or EvidenceRequirement(
            allow_unknown_empirical=True))


def flow_requirement(*, bounds: tuple[str, str, str, str] = IN_SCOPE_BOUNDS,
                     require_evidence: bool = True) -> Requirement:
    """The contested input: observed or modelled, and evidence-gated."""
    evidence = (EvidenceRequirement(
        required_metric_ids=(METRIC_ID,), allow_unknown_empirical=False)
        if require_evidence
        else EvidenceRequirement(allow_unknown_empirical=True))
    return _requirement(
        FLOW_CONCEPT, schema=SCHEMA_VERSION, units=UNITS,
        origins=(OriginClass.OBSERVATION, OriginClass.MODEL),
        bounds=bounds, evidence=evidence)


def root_requirement(*, bounds: tuple[str, str, str, str] = IN_SCOPE_BOUNDS
                     ) -> Requirement:
    return _requirement(
        RESULT_CONCEPT, schema=SCHEMA_VERSION, units=UNITS,
        origins=(OriginClass.DERIVED,), bounds=bounds)


def _profile_for(operation_key: str) -> ExecutionProfile:
    return ExecutionProfile.bind(
        ImplementationRef.from_operation_key(operation_key),
        PlacementRequirements(
            architectures=("x86_64",),
            provider_kinds=("stage1-local-subprocess",),
            resources=ResourceEnvelope(
                min_cpu_cores=1, min_memory_mb=64,
                max_cpu_cores=1, max_memory_mb=256)))


def _constant_spec(capability_id: str, descriptor: ArtifactDescriptor,
                   value: Any, cost: int, *,
                   evidence_profile_id: str = "evidence:unknown"
                   ) -> CapabilitySpec:
    profile = _profile_for("synthetic.constant.v1")
    return CapabilitySpec.bind(
        capability_id=capability_id, capability_version="1.0.0",
        implementation=profile.implementation,
        binder=BinderRef.from_key("synthetic.constant.bind.v1"),
        input_ports=(), output_ports=(DescriptorTemplate("result", descriptor),),
        parameter_schema=ParameterSchema((
            ParameterField("value", ParameterKind.JSON),)),
        parameterizations=(BindingParameterization(
            {"value": value}, {"cost_units": cost}),),
        execution_profile_id=profile.profile_id,
        evidence_profile_id=evidence_profile_id)


def _field_payload(value: float, component: str = "flow") -> dict[str, Any]:
    return {
        "schema": "field-json-v1", "crs": CRS,
        "x": list(X_COORDS), "y": list(Y_COORDS), "time": list(TIME_STEPS),
        "components": {component: [[[value for _ in X_COORDS]
                                    for _ in Y_COORDS] for _ in TIME_STEPS]},
    }


def expected_result_field() -> dict[str, Any]:
    """What the minimum-cost (modelled) path must actually commit."""
    flow = COARSE_VALUE * MODEL_GAIN + TERRAIN_VALUE
    value = min(flow * FUEL_VALUE + TERRAIN_VALUE * IGNITION_VALUE, THRESHOLD)
    return {
        "schema": "field-json-v1", "crs": CRS,
        "x": list(X_COORDS), "y": list(Y_COORDS), "time": list(TIME_STEPS),
        "components": {"flow": [[[value for _ in X_COORDS]
                                 for _ in Y_COORDS] for _ in TIME_STEPS]},
    }


# -- evidence ------------------------------------------------------------


def metric_definition() -> MetricDefinition:
    return MetricDefinition(METRIC_ID, "1.0.0", UNITS, PROTOCOL_ID)


def evaluator() -> MetricEvaluator:
    return MetricEvaluator(EVALUATOR_ID, "1.0.0", (metric_definition(),))


def _applicability(bounds: tuple[str, str, str, str]) -> EvidenceApplicability:
    return EvidenceApplicability(
        spatial=BBoxSupport(CRS, AXES, bounds),
        start=WINDOW_START, end=WINDOW_END)


def _claim(value: str, bounds: tuple[str, str, str, str], *,
           reference_manifest_id: str = REFERENCE_MANIFEST_ID,
           interval: tuple[str, str] | None = None) -> EvidenceClaim:
    uncertainty = (
        EstimateUncertainty(
            __import__("contracts").UncertaintyStatus.KNOWN,
            lower_bound=interval[0], upper_bound=interval[1], unit=UNITS,
            confidence_level="0.95",
            method_id="example.block-bootstrap.v1")
        if interval is not None else None)
    return EvidenceClaim.known(
        METRIC_ID, value, UNITS, bound_kind=BoundKind.POINT_ESTIMATE,
        reference_manifest_id=reference_manifest_id, protocol_id=PROTOCOL_ID,
        evaluator_id=EVALUATOR_ID, applicability=_applicability(bounds),
        estimate_uncertainty=uncertainty)


# -- catalog assembly ----------------------------------------------------


def _subject_for(spec: CapabilitySpec, requirement: Requirement,
                 deployment: DeploymentCapabilitySnapshot):
    """Derive the only evidence subject valid for this capability's output.

    Evidence identity is never chosen by a fixture author: it is derived from
    the exact implementation and bound parameters, which is why this has to
    round-trip through a real binding rather than being written by hand.
    """
    catalog = CapabilityCatalog.freeze((spec,), (_execution_profile(spec),))
    enumeration = catalog.bind_candidates(
        spec.spec_id, "result", requirement, deployment_snapshot=deployment)
    if not enumeration.accepted:
        raise RuntimeError(
            f"fixture capability {spec.capability_id} did not bind: "
            f"{[item.code.value for item in enumeration.rejected]}")
    return invocation_evidence_subject(enumeration.accepted[0].invocation,
                                       "result")


def _execution_profile(spec: CapabilitySpec) -> ExecutionProfile:
    return _profile_for(spec.implementation.operation_key)


def make_stage6_fixture(
    *,
    model_applicability_bounds: tuple[str, str, str, str] = IN_SCOPE_BOUNDS,
    model_reference_manifest_id: str = REFERENCE_MANIFEST_ID,
    direct_interval: tuple[str, str] | None = ("0.80", "1.00"),
    model_interval: tuple[str, str] | None = ("0.40", "0.60"),
) -> Stage6Fixture:
    """Build the dataset-versus-model contest and its frozen evidence.

    Parameters exist so tests can move the model's evidence out of scope, or
    evaluate it against a different reference, without rewriting the graph.
    """
    deployment = deployment_snapshot()
    flow_req = flow_requirement()
    # Deriving an evidence subject must not depend on the evidence gate it will
    # later satisfy, so the probe binding uses the ungated requirement.
    subject_probe_req = flow_requirement(require_evidence=False)

    coarse_descriptor = _field_descriptor(FLOW_CONCEPT, OriginClass.OBSERVATION)
    # The coarse source is a different concept so it cannot itself satisfy the
    # contested flow requirement; only the model can turn it into one.
    coarse_descriptor = ArtifactDescriptor(
        concept_id=f"{FLOW_CONCEPT}.coarse", schema_version=SCHEMA_VERSION,
        representation=REPRESENTATION, units=UNITS,
        spatial_support=BBoxSupport(CRS, AXES, IN_SCOPE_BOUNDS),
        temporal_support=_window(), vertical_support=None, grid=None,
        native_resolution=None, origin=OriginClass.OBSERVATION,
        missingness=Missingness(MissingnessStatus.COMPLETE))
    terrain_descriptor = _field_descriptor(
        TERRAIN_CONCEPT, OriginClass.OBSERVATION)
    direct_descriptor = _field_descriptor(FLOW_CONCEPT, OriginClass.OBSERVATION)
    model_descriptor = _field_descriptor(FLOW_CONCEPT, OriginClass.MODEL)
    result_descriptor = _field_descriptor(RESULT_CONCEPT, OriginClass.DERIVED)

    coarse = _constant_spec("example-flow-coarse", coarse_descriptor,
                            _field_payload(COARSE_VALUE), COARSE_FLOW_COST)
    terrain = _constant_spec("example-terrain", terrain_descriptor,
                             _field_payload(TERRAIN_VALUE, "support"),
                             TERRAIN_COST)
    fuel = _constant_spec("example-fuel", _scalar_descriptor(FUEL_CONCEPT),
                          FUEL_VALUE, FUEL_COST)
    ignition = _constant_spec(
        "example-ignition", _scalar_descriptor(IGNITION_CONCEPT),
        IGNITION_VALUE, IGNITION_COST)

    direct_stub = _constant_spec("example-flow-direct", direct_descriptor,
                                 _field_payload(DIRECT_VALUE), DIRECT_FLOW_COST)
    model_stub = _model_spec(coarse_descriptor, terrain_descriptor,
                             model_descriptor, "evidence:unknown")

    direct_profile = EvidenceProfile(
        "ExampleEvidence-v1",
        _subject_for(direct_stub, subject_probe_req, deployment),
        (_claim(DIRECT_ERROR, IN_SCOPE_BOUNDS, interval=direct_interval),))
    model_profile = EvidenceProfile(
        "ExampleEvidence-v1",
        _subject_for(model_stub, subject_probe_req, deployment),
        (_claim(MODEL_ERROR, model_applicability_bounds,
                reference_manifest_id=model_reference_manifest_id,
                interval=model_interval),))

    direct = _constant_spec(
        "example-flow-direct", direct_descriptor,
        _field_payload(DIRECT_VALUE), DIRECT_FLOW_COST,
        evidence_profile_id=direct_profile.profile_id)
    model = _model_spec(coarse_descriptor, terrain_descriptor,
                        model_descriptor, model_profile.profile_id)
    consequence = _consequence_spec(
        model_descriptor, terrain_descriptor, result_descriptor, flow_req)

    catalog = CapabilityCatalog.freeze(
        (coarse, terrain, fuel, ignition, direct, model, consequence),
        (_profile_for("synthetic.constant.v1"),
         _profile_for("reduced.downscale.v1"),
         _profile_for("reduced.consequence.v1")))
    snapshot = EvidenceSnapshot(
        "2026-08-15T00:00:00Z", evaluator(), (direct_profile, model_profile))
    return Stage6Fixture(
        catalog=catalog,
        deployment_snapshot=deployment,
        evidence_snapshot=snapshot,
        root_uses=(RequirementUse("stage6-root", "result", root_requirement()),),
        flow_use=RequirementUse("stage6-flow", "flow", flow_req),
        expected_cost_units=MINIMUM_COST_TOTAL,
        expected_capability_ids=(
            "example-flow-coarse", "example-flow-model", "example-fuel",
            "example-ignition", "example-reduced-consequence",
            "example-terrain"),
    )


def _model_spec(coarse: ArtifactDescriptor, terrain: ArtifactDescriptor,
                result: ArtifactDescriptor,
                evidence_profile_id: str) -> CapabilitySpec:
    profile = _profile_for("reduced.downscale.v1")
    return CapabilitySpec.bind(
        capability_id="example-flow-model", capability_version="1.0.0",
        implementation=profile.implementation,
        binder=BinderRef.from_key("reduced.downscale.bind.v1"),
        input_ports=(
            InputPortTemplate("coarse", _exact_requirement(coarse)),
            InputPortTemplate("terrain", _exact_requirement(terrain)),
        ),
        output_ports=(DescriptorTemplate("result", result),),
        parameter_schema=ParameterSchema((
            ParameterField("gain", ParameterKind.NUMBER),)),
        parameterizations=(BindingParameterization(
            {"gain": MODEL_GAIN}, {"cost_units": MODEL_COST}),),
        execution_profile_id=profile.profile_id,
        evidence_profile_id=evidence_profile_id)


def _consequence_spec(flow: ArtifactDescriptor, terrain: ArtifactDescriptor,
                      result: ArtifactDescriptor,
                      flow_req: Requirement) -> CapabilitySpec:
    profile = _profile_for("reduced.consequence.v1")
    return CapabilitySpec.bind(
        capability_id="example-reduced-consequence", capability_version="1.0.0",
        implementation=profile.implementation,
        binder=BinderRef.from_key("reduced.consequence.bind.v1"),
        input_ports=(
            InputPortTemplate("flow", flow_req),
            InputPortTemplate("fuel", _requirement(
                FUEL_CONCEPT, schema=SCALAR_SCHEMA, units=SCALAR_UNITS,
                origins=(OriginClass.SYNTHETIC,))),
            InputPortTemplate("ignition", _requirement(
                IGNITION_CONCEPT, schema=SCALAR_SCHEMA, units=SCALAR_UNITS,
                origins=(OriginClass.SYNTHETIC,))),
            InputPortTemplate("terrain", _exact_requirement(terrain)),
        ),
        output_ports=(DescriptorTemplate("result", result),),
        parameter_schema=ParameterSchema((
            ParameterField("threshold", ParameterKind.NUMBER),)),
        parameterizations=(BindingParameterization(
            {"threshold": THRESHOLD}, {"cost_units": CONSEQUENCE_COST}),),
        execution_profile_id=profile.profile_id)


def _exact_requirement(value: ArtifactDescriptor) -> Requirement:
    return Requirement(
        concept_id=value.concept_id,
        accepted_schema_versions=(value.schema_version,),
        representation=ValueConstraint.exact(value.representation),
        units=ValueConstraint.exact(value.units),
        spatial=SpatialRequirement(value.spatial_support),
        temporal=TemporalRequirement(
            TemporalKind.SERIES, start=value.temporal_support.start,
            end=value.temporal_support.end, cadence_s=CADENCE_S),
        vertical=None, allowed_origins=(value.origin,),
        max_native_resolution=None, max_effective_resolution=None,
        missing_policy=MissingPolicy(),
        minimum_evidence=EvidenceRequirement(allow_unknown_empirical=True))


def deployment_snapshot() -> DeploymentCapabilitySnapshot:
    profiles = (_profile_for("synthetic.constant.v1"),
                _profile_for("reduced.downscale.v1"),
                _profile_for("reduced.consequence.v1"))
    digests = tuple(sorted({
        item.implementation.implementation_sha256 for item in profiles}))
    return DeploymentCapabilitySnapshot.freeze(
        "2026-08-15T00:00:00Z",
        (SiteClassCapability(
            site_class_id="private-node-example-cpu", architecture="x86_64",
            provider_kinds=("stage1-local-subprocess",),
            implementation_digests=digests, environment_classes=(),
            network_classes=("none",), credential_classes=(), mount_classes=(),
            policy_classes=(), max_cpu_cores=1, max_memory_mb=256,
            max_gpus=0),))


__all__ = [
    "DIRECT_ERROR",
    "DIRECT_PATH_TOTAL",
    "FLOW_CONCEPT",
    "IN_SCOPE_BOUNDS",
    "METRIC_ID",
    "MINIMUM_COST_TOTAL",
    "MODEL_ERROR",
    "OTHER_REFERENCE_MANIFEST_ID",
    "OUT_OF_SCOPE_BOUNDS",
    "REFERENCE_MANIFEST_ID",
    "RESULT_CONCEPT",
    "Stage6Fixture",
    "deployment_snapshot",
    "evaluator",
    "expected_result_field",
    "flow_requirement",
    "make_stage6_fixture",
    "metric_definition",
    "root_requirement",
]
