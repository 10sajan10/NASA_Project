"""Finite, content-addressed semantic transformations for Stage 4.

A transformation is an explicit hyperedge between *exact* artifact descriptor
states.  It is not a callable adapter and it cannot generate more transforms
while the resolver is searching.  Every executable is selected from the same
closed operation/binder registries used by ordinary capabilities.
"""
from __future__ import annotations

import dataclasses
import hashlib
import math
from dataclasses import dataclass
from datetime import timedelta
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterable

from capabilities import (
    BinderRef,
    BindingParameterization,
    BoundInvocation,
    BoundOutputPort,
    CapabilitySpec,
    DescriptorTemplate,
    ExecutionProfile,
    InputPortTemplate,
    ParameterField,
    ParameterKind,
    ParameterSchema,
    TransformationAuthority,
)
from capabilities.implementation import _digest, _required_text
from contracts import (
    ArtifactDescriptor,
    CadencePolicy,
    EvidenceRequirement,
    MissingPolicy,
    MissingnessStatus,
    OriginClass,
    Requirement,
    SpatialRequirement,
    TemporalKind,
    TemporalRequirement,
    UncertaintyStatus,
    UnknownPolicy,
    ValueConstraint,
    VerticalRequirement,
)
from contracts.identity import (
    canonical_decimal,
    canonical_unit,
    decimal_value,
    timestamp_value,
)
from .resampling import AggregationKind
from engine.runtime.identity import (
    freeze_json,
    require_object_fields,
    strict_copy,
    strict_hash,
)


class TransformationKind(str, Enum):
    UNIT_AFFINE = "UNIT_AFFINE"
    SPATIAL_SUBSET = "SPATIAL_SUBSET"
    TEMPORAL_SUBSET = "TEMPORAL_SUBSET"
    TEMPORAL_ALIGN = "TEMPORAL_ALIGN"
    REGRID_BILINEAR = "REGRID_BILINEAR"
    REPROJECT_BILINEAR = "REPROJECT_BILINEAR"
    SPATIAL_BLOCK_AGGREGATE = "SPATIAL_BLOCK_AGGREGATE"
    VECTOR_ROTATE = "VECTOR_ROTATE"
    VECTOR_UV_TO_SPEED_DIRECTION = "VECTOR_UV_TO_SPEED_DIRECTION"


class ValueSemantics(str, Enum):
    """Payload value class asserted for one exact descriptor state.

    Stage 4 cannot infer this from a concept name.  Regridding is therefore
    admitted only when the transform contract explicitly types the exact input
    and output as continuous/intensive.  Categorical and extensive fields are
    outside the bilinear MVP instead of being silently interpolated.
    """

    UNSPECIFIED = "UNSPECIFIED"
    SCALAR_CONTINUOUS_INTENSIVE = "SCALAR_CONTINUOUS_INTENSIVE"
    CANONICAL_UV_VECTOR = "CANONICAL_UV_VECTOR"
    CIRCULAR_DIRECTION = "CIRCULAR_DIRECTION"
    # The four classes the paragraph above excludes from bilinear.  They are
    # not interpolatable, but they *are* exactly aggregatable over a block
    # partition, which is what SPATIAL_BLOCK_AGGREGATE admits them for.
    FIRST_OCCURRENCE_TIME = "FIRST_OCCURRENCE_TIME"
    SCALAR_EXTREMUM = "SCALAR_EXTREMUM"
    AREAL_FRACTION = "AREAL_FRACTION"
    CATEGORICAL_LABEL = "CATEGORICAL_LABEL"


class TransformationLossPolicy(str, Enum):
    """Closed classification of the scientific information change.

    This is deliberately a policy identifier rather than an inferred property
    of an operation name.  It travels in the content-addressed transformation
    contract and therefore cannot be silently relabelled after selection.
    """

    EXACT_SELECTION = "EXACT_SELECTION"
    NUMERIC_AFFINE_MAPPING = "NUMERIC_AFFINE_MAPPING"
    BILINEAR_INTERPOLATION = "BILINEAR_INTERPOLATION"
    BLOCK_AGGREGATION = "BLOCK_AGGREGATION"
    NUMERIC_VECTOR_MAPPING = "NUMERIC_VECTOR_MAPPING"
    DERIVED_OUTPUTS = "DERIVED_OUTPUTS"


class UncertaintyPropagationPolicy(str, Enum):
    """How intrinsic uncertainty must cross a transformation edge."""

    PRESERVE = "PRESERVE"
    REBIND_OR_MARK_UNKNOWN = "REBIND_OR_MARK_UNKNOWN"


#: The one aggregation each value class may be reduced with over a block
#: partition.  A bijection on purpose: if a field's semantics are declared,
#: the aggregation follows, and if they are not, no aggregation is admitted.
_BLOCK_AGGREGATIONS = MappingProxyType({
    ValueSemantics.FIRST_OCCURRENCE_TIME: AggregationKind.FIRST_OCCURRENCE_MIN,
    ValueSemantics.SCALAR_EXTREMUM: AggregationKind.EXTREMUM_MAX,
    ValueSemantics.AREAL_FRACTION: AggregationKind.AREAL_FRACTION_MEAN,
    ValueSemantics.SCALAR_CONTINUOUS_INTENSIVE:
        AggregationKind.INTENSIVE_AREA_WEIGHTED_MEAN,
    ValueSemantics.CATEGORICAL_LABEL: AggregationKind.CATEGORICAL_MAJORITY,
})


def block_aggregation_for(semantics: ValueSemantics) -> AggregationKind:
    """The admissible aggregation for a value class, or a refusal."""
    try:
        return _BLOCK_AGGREGATIONS[semantics]
    except KeyError:
        raise ValueError(
            f"{semantics.value} has no admissible block aggregation; a field "
            "whose value class is undeclared cannot be reduced") from None


@dataclass(frozen=True)
class _KindRule:
    operation_key: str
    binder_key: str
    input_ports: tuple[str, ...]
    output_ports: tuple[str, ...]
    parameter_fields: tuple[tuple[str, ParameterKind], ...]
    semantic_rule_id: str
    scientific_assumption_ids: tuple[str, ...] = ()


_KIND_RULES = MappingProxyType({
    TransformationKind.UNIT_AFFINE: _KindRule(
        "transform.unit_affine.v1", "transform.unit_affine.bind.v1",
        ("source",), ("result",),
        (("factor", ParameterKind.NUMBER), ("offset", ParameterKind.NUMBER)),
        "semantic:unit-affine-registry-v1",
    ),
    TransformationKind.SPATIAL_SUBSET: _KindRule(
        "transform.spatial_subset.v1", "transform.spatial_subset.bind.v1",
        ("source",), ("result",),
        tuple((name, ParameterKind.INTEGER) for name in (
            "x_start", "x_stop", "y_start", "y_stop")),
        "semantic:spatial-index-subset-v1",
        ("grid:sample-centres-axis-aligned-v1",),
    ),
    TransformationKind.TEMPORAL_SUBSET: _KindRule(
        "transform.temporal_subset.v1", "transform.temporal_subset.bind.v1",
        ("source",), ("result",),
        (("start", ParameterKind.INTEGER), ("stop", ParameterKind.INTEGER)),
        "semantic:temporal-index-subset-v1",
    ),
    TransformationKind.TEMPORAL_ALIGN: _KindRule(
        "transform.temporal_align.v1", "transform.temporal_align.bind.v1",
        ("source",), ("result",),
        tuple((name, ParameterKind.INTEGER) for name in (
            "count", "start_index", "step")),
        "semantic:temporal-index-alignment-v1",
    ),
    TransformationKind.REGRID_BILINEAR: _KindRule(
        "transform.regrid_bilinear.v1", "transform.regrid_bilinear.bind.v1",
        ("source",), ("result",),
        (("target_x", ParameterKind.JSON), ("target_y", ParameterKind.JSON)),
        "semantic:rectilinear-bilinear-regrid-v1",
        ("grid:sample-centres-axis-aligned-v1",
         "quantity:continuous-intensive-v1"),
    ),
    TransformationKind.REPROJECT_BILINEAR: _KindRule(
        "transform.reproject_bilinear.v1",
        "transform.reproject_bilinear.bind.v1",
        ("source",), ("result",),
        (
            ("source_crs", ParameterKind.STRING),
            ("source_axis_order", ParameterKind.JSON),
            ("target_crs", ParameterKind.STRING),
            ("target_axis_order", ParameterKind.JSON),
            ("target_x", ParameterKind.JSON),
            ("target_y", ParameterKind.JSON),
            ("pipeline_projjson", ParameterKind.JSON),
        ),
        "semantic:rectilinear-bilinear-reprojection-v1",
        ("grid:sample-centres-axis-aligned-v1",
         "quantity:continuous-intensive-v1",
         "reprojection:projjson-local-best-available-v1"),
    ),
    TransformationKind.SPATIAL_BLOCK_AGGREGATE: _KindRule(
        "transform.spatial_block_aggregate.v1",
        "transform.spatial_block_aggregate.bind.v1",
        ("source",), ("result",),
        (
            ("block_x", ParameterKind.INTEGER),
            ("block_y", ParameterKind.INTEGER),
            ("aggregation", ParameterKind.STRING),
        ),
        "semantic:block-aggregation-registry-v1",
        ("grid:sample-centres-axis-aligned-v1",
         "partition:exact-integer-block-cover-v1"),
    ),
    TransformationKind.VECTOR_ROTATE: _KindRule(
        "transform.vector_rotate.v1", "transform.vector_rotate.bind.v1",
        ("source",), ("result",),
        (("angle_degrees", ParameterKind.NUMBER),),
        "semantic:canonical-uv-vector-rotation-v1",
        (
            "payload:canonical-uv-vector-v1",
            "vector:rotation-counterclockwise-v1",
        ),
    ),
    TransformationKind.VECTOR_UV_TO_SPEED_DIRECTION: _KindRule(
        "transform.vector_uv_to_speed_direction.v1",
        "transform.vector_uv_to_speed_direction.bind.v1",
        ("source",), ("speed", "direction"), (),
        "semantic:canonical-uv-to-speed-direction-v1",
        (
            "payload:canonical-uv-vector-v1",
            "vector:calm-direction-zero-v1",
            "vector:direction-mathematical-ccw-from-positive-x-v1",
        ),
    ),
})


_LOSS_POLICY_BY_KIND = MappingProxyType({
    TransformationKind.UNIT_AFFINE:
        TransformationLossPolicy.NUMERIC_AFFINE_MAPPING,
    TransformationKind.SPATIAL_SUBSET:
        TransformationLossPolicy.EXACT_SELECTION,
    TransformationKind.TEMPORAL_SUBSET:
        TransformationLossPolicy.EXACT_SELECTION,
    TransformationKind.TEMPORAL_ALIGN:
        TransformationLossPolicy.EXACT_SELECTION,
    TransformationKind.REGRID_BILINEAR:
        TransformationLossPolicy.BILINEAR_INTERPOLATION,
    TransformationKind.REPROJECT_BILINEAR:
        TransformationLossPolicy.BILINEAR_INTERPOLATION,
    TransformationKind.SPATIAL_BLOCK_AGGREGATE:
        TransformationLossPolicy.BLOCK_AGGREGATION,
    TransformationKind.VECTOR_ROTATE:
        TransformationLossPolicy.NUMERIC_VECTOR_MAPPING,
    TransformationKind.VECTOR_UV_TO_SPEED_DIRECTION:
        TransformationLossPolicy.DERIVED_OUTPUTS,
})

_UNCERTAINTY_POLICY_BY_KIND = MappingProxyType({
    kind: (UncertaintyPropagationPolicy.PRESERVE
           if _LOSS_POLICY_BY_KIND[kind]
           is TransformationLossPolicy.EXACT_SELECTION
           else UncertaintyPropagationPolicy.REBIND_OR_MARK_UNKNOWN)
    for kind in TransformationKind
})

if (set(_LOSS_POLICY_BY_KIND) != set(TransformationKind)
        or set(_UNCERTAINTY_POLICY_BY_KIND) != set(TransformationKind)):
    raise AssertionError("every transformation kind needs closed loss policies")


@dataclass(frozen=True)
class UnitAffineRule:
    """One member of the closed, versioned affine unit registry."""

    conversion_id: str
    source_units: str
    target_units: str
    factor: int | float
    offset: int | float

    def __post_init__(self) -> None:
        _required_text(self.conversion_id, "conversion_id")
        object.__setattr__(self, "source_units", canonical_unit(self.source_units))
        object.__setattr__(self, "target_units", canonical_unit(self.target_units))
        for name in ("factor", "offset"):
            value = getattr(self, name)
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or (isinstance(value, float) and not math.isfinite(value))):
                raise ValueError(f"unit conversion {name} must be finite numeric")

    @property
    def parameters(self) -> dict[str, int | float]:
        return {"factor": self.factor, "offset": self.offset}


_UNIT_RULES = tuple(sorted((
    UnitAffineRule("unit:m-to-km:v1", "m", "km", 0.001, 0),
    UnitAffineRule("unit:km-to-m:v1", "km", "m", 1000, 0),
    UnitAffineRule("unit:pa-to-hpa:v1", "Pa", "hPa", 0.01, 0),
    UnitAffineRule("unit:hpa-to-pa:v1", "hPa", "Pa", 100, 0),
    UnitAffineRule("unit:mps-to-kmph:v1", "m.s-1", "km.h-1", 3.6, 0),
    UnitAffineRule(
        "unit:kmph-to-mps:v1", "km.h-1", "m.s-1",
        0.2777777777777778, 0),
), key=lambda item: (item.source_units, item.target_units)))
_UNIT_RULE_BY_PAIR = MappingProxyType({
    (item.source_units, item.target_units): item for item in _UNIT_RULES})


def unit_affine_rules() -> tuple[UnitAffineRule, ...]:
    return _UNIT_RULES


def unit_affine_rule(source_units: str, target_units: str) -> UnitAffineRule:
    """Resolve a unit pair without accepting caller-supplied coefficients."""
    pair = (canonical_unit(source_units), canonical_unit(target_units))
    try:
        return _UNIT_RULE_BY_PAIR[pair]
    except KeyError as exc:
        raise KeyError(f"no closed Stage-4 affine unit conversion for {pair!r}") \
            from exc


@dataclass(frozen=True)
class TransformationPort:
    port_id: str
    descriptor: ArtifactDescriptor
    value_semantics: ValueSemantics = ValueSemantics.UNSPECIFIED

    def __post_init__(self) -> None:
        _required_text(self.port_id, "transformation port_id")
        if not isinstance(self.descriptor, ArtifactDescriptor):
            raise TypeError("transformation port descriptor is invalid")
        if not isinstance(self.value_semantics, ValueSemantics):
            raise TypeError("transformation port value semantics are invalid")

    def to_dict(self) -> dict[str, Any]:
        return {
            "port_id": self.port_id,
            "descriptor": self.descriptor.to_dict(),
            "value_semantics": self.value_semantics.value,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "TransformationPort":
        raw = require_object_fields(
            value, {field.name for field in dataclasses.fields(cls)},
            "TransformationPort")
        raw["descriptor"] = ArtifactDescriptor.from_dict(raw["descriptor"])
        raw["value_semantics"] = ValueSemantics(raw["value_semantics"])
        return cls(**raw)


def _parameter_schema(kind: TransformationKind) -> ParameterSchema:
    fields = tuple(sorted(
        (ParameterField(name, field_kind)
         for name, field_kind in _KIND_RULES[kind].parameter_fields),
        key=lambda item: item.name,
    ))
    return ParameterSchema(fields)


@dataclass(frozen=True)
class TransformationSpec:
    """One immutable, executable hyperedge over exact descriptor states."""

    spec_id: str
    transformation_id: str
    transformation_version: str
    kind: TransformationKind
    execution_profile: ExecutionProfile
    binder: BinderRef
    input_ports: tuple[TransformationPort, ...]
    output_ports: tuple[TransformationPort, ...]
    parameters: dict[str, Any]
    cost_units: int
    semantic_rule_id: str
    loss_policy: TransformationLossPolicy
    uncertainty_propagation_policy: UncertaintyPropagationPolicy
    scientific_assumption_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _digest(self.spec_id, "transformation spec_id")
        _required_text(self.transformation_id, "transformation_id")
        _required_text(self.transformation_version, "transformation_version")
        if not isinstance(self.kind, TransformationKind):
            raise TypeError("transformation kind is invalid")
        if not isinstance(self.execution_profile, ExecutionProfile):
            raise TypeError("transformation execution profile is invalid")
        if not isinstance(self.binder, BinderRef):
            raise TypeError("transformation binder is invalid")
        if not isinstance(self.loss_policy, TransformationLossPolicy):
            raise TypeError("transformation loss policy is invalid")
        if not isinstance(
                self.uncertainty_propagation_policy,
                UncertaintyPropagationPolicy):
            raise TypeError("transformation uncertainty policy is invalid")
        for values, label in (
                (self.input_ports, "input_ports"),
                (self.output_ports, "output_ports")):
            if (not isinstance(values, tuple) or not values
                    or not all(isinstance(item, TransformationPort)
                               for item in values)):
                raise TypeError(f"{label} must be a non-empty immutable port tuple")
            port_ids = tuple(item.port_id for item in values)
            descriptor_ids = tuple(item.descriptor.descriptor_id for item in values)
            if len(port_ids) != len(set(port_ids)):
                raise ValueError(f"transformation has duplicate {label} port IDs")
            if len(descriptor_ids) != len(set(descriptor_ids)):
                raise ValueError(
                    f"transformation has duplicate exact descriptor states in {label}")
        if isinstance(self.cost_units, bool) or not isinstance(self.cost_units, int) \
                or self.cost_units < 0:
            raise ValueError("transformation cost_units must be a non-negative integer")
        object.__setattr__(self, "parameters", freeze_json(self.parameters))
        if not isinstance(self.parameters, dict):
            raise ValueError("transformation parameters must be a JSON object")
        if (not isinstance(self.scientific_assumption_ids, tuple)
                or any(not isinstance(item, str) or not item.strip()
                       for item in self.scientific_assumption_ids)):
            raise TypeError("scientific assumption IDs must be an immutable text tuple")
        if self.scientific_assumption_ids != tuple(sorted(
                set(self.scientific_assumption_ids))):
            raise ValueError("scientific assumption IDs must be unique and sorted")

        rule = _KIND_RULES[self.kind]
        if self.execution_profile.implementation.operation_key != rule.operation_key:
            raise ValueError("transformation kind and closed operation disagree")
        self.execution_profile.implementation.verify_current()
        binder_rule = self.binder.verify_current()
        if self.binder.binder_key != rule.binder_key:
            raise ValueError("transformation kind and closed binder disagree")
        if binder_rule.operation_key != rule.operation_key:
            raise ValueError("transformation binder and operation disagree")
        if tuple(item.port_id for item in self.input_ports) != rule.input_ports:
            raise ValueError("transformation input ports disagree with its kind")
        if tuple(item.port_id for item in self.output_ports) != rule.output_ports:
            raise ValueError("transformation output ports disagree with its kind")
        if self.semantic_rule_id != rule.semantic_rule_id:
            raise ValueError("transformation semantic rule is not the closed version")
        if self.loss_policy is not _LOSS_POLICY_BY_KIND[self.kind]:
            raise ValueError(
                "transformation loss policy is not valid for its kind")
        if (self.uncertainty_propagation_policy
                is not _UNCERTAINTY_POLICY_BY_KIND[self.kind]):
            raise ValueError(
                "transformation uncertainty policy is not valid for its kind")
        if self.scientific_assumption_ids != rule.scientific_assumption_ids:
            raise ValueError("transformation scientific assumptions are not explicit")
        _parameter_schema(self.kind).validate(self.parameters)
        _validate_semantics(self)
        # Fail at construction if an input descriptor cannot be represented as
        # an exact ordinary CapabilitySpec requirement.
        for port in self.input_ports:
            exact_requirement(port.descriptor)
        if self.spec_id != self.expected_id():
            raise ValueError("transformation specification identity does not verify")

    @classmethod
    def bind(
            cls, *, transformation_id: str,
            transformation_version: str,
            kind: TransformationKind,
            execution_profile: ExecutionProfile,
            input_ports: Iterable[TransformationPort],
            output_ports: Iterable[TransformationPort],
            parameters: dict[str, Any],
            cost_units: int,
    ) -> "TransformationSpec":
        if not isinstance(kind, TransformationKind):
            raise TypeError("transformation kind is invalid")
        rule = _KIND_RULES[kind]
        inputs = tuple(input_ports)
        outputs = tuple(output_ports)
        params = strict_copy(parameters)
        if kind is TransformationKind.REPROJECT_BILINEAR:
            # Callers declare the CRS pair and target lattice, never an
            # authority-bearing pipeline.  Planning selects the locally best
            # fully available operation and freezes its exact PROJJSON.
            from engine.runtime.operations import bind_reprojection_parameters
            try:
                source_grid = inputs[0].descriptor.grid
                target_grid = outputs[0].descriptor.grid
            except IndexError as exc:
                raise ValueError(
                    "reprojection requires source/result descriptor ports") from exc
            if source_grid is None or target_grid is None:
                raise ValueError("reprojection requires declared source/result grids")
            params = bind_reprojection_parameters(
                params,
                source_axis_order=source_grid.axis_order,
                target_axis_order=target_grid.axis_order,
            )
        payload = _transformation_payload(
            transformation_id, transformation_version, kind,
            execution_profile, BinderRef.from_key(rule.binder_key), inputs,
            outputs, params, cost_units, rule.semantic_rule_id,
            _LOSS_POLICY_BY_KIND[kind], _UNCERTAINTY_POLICY_BY_KIND[kind],
            rule.scientific_assumption_ids,
        )
        return cls(
            strict_hash(payload), transformation_id, transformation_version,
            kind, execution_profile, BinderRef.from_key(rule.binder_key),
            inputs, outputs, params, cost_units, rule.semantic_rule_id,
            _LOSS_POLICY_BY_KIND[kind], _UNCERTAINTY_POLICY_BY_KIND[kind],
            rule.scientific_assumption_ids,
        )

    @classmethod
    def bind_unit_affine(
            cls, *, transformation_id: str,
            transformation_version: str,
            execution_profile: ExecutionProfile,
            source: ArtifactDescriptor,
            result: ArtifactDescriptor,
            cost_units: int,
    ) -> "TransformationSpec":
        conversion = unit_affine_rule(source.units, result.units)
        return cls.bind(
            transformation_id=transformation_id,
            transformation_version=transformation_version,
            kind=TransformationKind.UNIT_AFFINE,
            execution_profile=execution_profile,
            input_ports=(TransformationPort(
                "source", source,
                ValueSemantics.SCALAR_CONTINUOUS_INTENSIVE),),
            output_ports=(TransformationPort(
                "result", result,
                ValueSemantics.SCALAR_CONTINUOUS_INTENSIVE),),
            parameters=conversion.parameters,
            cost_units=cost_units,
        )

    @property
    def implementation(self):
        return self.execution_profile.implementation

    @property
    def input_descriptor_ids(self) -> tuple[str, ...]:
        return tuple(item.descriptor.descriptor_id for item in self.input_ports)

    @property
    def output_descriptor_ids(self) -> tuple[str, ...]:
        return tuple(item.descriptor.descriptor_id for item in self.output_ports)

    def expected_id(self) -> str:
        return strict_hash(_transformation_payload(
            self.transformation_id, self.transformation_version, self.kind,
            self.execution_profile, self.binder, self.input_ports,
            self.output_ports, self.parameters, self.cost_units,
            self.semantic_rule_id, self.loss_policy,
            self.uncertainty_propagation_policy,
            self.scientific_assumption_ids,
        ))

    def to_capability_spec(self) -> CapabilitySpec:
        """Lower this edge without discarding its scientific authority."""
        return CapabilitySpec.bind(
            capability_id=f"transform:{self.transformation_id}",
            capability_version=self.transformation_version,
            implementation=self.implementation,
            binder=self.binder,
            input_ports=tuple(InputPortTemplate(
                item.port_id, exact_requirement(item.descriptor))
                for item in self.input_ports),
            output_ports=tuple(DescriptorTemplate(
                item.port_id, item.descriptor) for item in self.output_ports),
            parameter_schema=_parameter_schema(self.kind),
            parameterizations=(BindingParameterization(
                strict_copy(self.parameters), {"cost_units": self.cost_units}),),
            execution_profile_id=self.execution_profile.profile_id,
            evidence_profile_id="evidence:unknown",
            cost_model_id="cost:declared-v1",
            transformation_authority=self.to_authority(),
        )

    def to_authority(self) -> TransformationAuthority:
        """Freeze this specification and its closed semantic-rule identities."""
        return TransformationAuthority.bind(
            transformation_spec=self.to_dict(),
            semantic_rule_implementation_sha256=
                _semantic_rule_implementation_sha256(self),
            parameter_rule_sha256=_parameter_rule_sha256(self),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "spec_id": self.spec_id,
            "transformation_id": self.transformation_id,
            "transformation_version": self.transformation_version,
            "kind": self.kind.value,
            "execution_profile": self.execution_profile.to_dict(),
            "binder": self.binder.to_dict(),
            "input_ports": [item.to_dict() for item in self.input_ports],
            "output_ports": [item.to_dict() for item in self.output_ports],
            "parameters": strict_copy(self.parameters),
            "cost_units": self.cost_units,
            "semantic_rule_id": self.semantic_rule_id,
            "loss_policy": self.loss_policy.value,
            "uncertainty_propagation_policy":
                self.uncertainty_propagation_policy.value,
            "scientific_assumption_ids": list(self.scientific_assumption_ids),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "TransformationSpec":
        raw = require_object_fields(
            value, {field.name for field in dataclasses.fields(cls)},
            "TransformationSpec")
        raw["kind"] = TransformationKind(raw["kind"])
        raw["execution_profile"] = ExecutionProfile.from_dict(
            raw["execution_profile"])
        raw["binder"] = BinderRef.from_dict(raw["binder"])
        raw["loss_policy"] = TransformationLossPolicy(raw["loss_policy"])
        raw["uncertainty_propagation_policy"] = (
            UncertaintyPropagationPolicy(
                raw["uncertainty_propagation_policy"]))
        for name in ("input_ports", "output_ports"):
            if not isinstance(raw[name], list):
                raise ValueError(f"TransformationSpec.{name} must be an array")
            raw[name] = tuple(TransformationPort.from_dict(item)
                              for item in raw[name])
        if not isinstance(raw["scientific_assumption_ids"], list):
            raise ValueError(
                "TransformationSpec.scientific_assumption_ids must be an array")
        raw["scientific_assumption_ids"] = tuple(
            raw["scientific_assumption_ids"])
        return cls(**raw)


def _transformation_payload(
        transformation_id: str, transformation_version: str,
        kind: TransformationKind, execution_profile: ExecutionProfile,
        binder: BinderRef, input_ports: tuple[TransformationPort, ...],
        output_ports: tuple[TransformationPort, ...], parameters: dict[str, Any],
        cost_units: int, semantic_rule_id: str,
        loss_policy: TransformationLossPolicy,
        uncertainty_propagation_policy: UncertaintyPropagationPolicy,
        scientific_assumption_ids: tuple[str, ...],
) -> dict[str, Any]:
    return {
        "schema": "stage8r-transformation-spec-v2",
        "transformation_id": transformation_id,
        "transformation_version": transformation_version,
        "kind": kind.value,
        "execution_profile": execution_profile.to_dict(),
        "binder": binder.to_dict(),
        "input_ports": [item.to_dict() for item in input_ports],
        "output_ports": [item.to_dict() for item in output_ports],
        "parameters": strict_copy(parameters),
        "cost_units": cost_units,
        "semantic_rule_id": semantic_rule_id,
        "loss_policy": loss_policy.value,
        "uncertainty_propagation_policy":
            uncertainty_propagation_policy.value,
        "scientific_assumption_ids": list(scientific_assumption_ids),
    }


def _semantic_rule_implementation_sha256(spec: TransformationSpec) -> str:
    """Digest the closed semantic validator implementation and rule version.

    This is intentionally conservative, like the Stage-2 binder digest: any
    source change to the module that owns ``_validate_semantics`` invalidates
    old authority rather than silently reinterpreting it under new code.
    """
    rule = _KIND_RULES[spec.kind]
    identity = strict_hash({
        "schema": "stage8r-semantic-rule-implementation-v1",
        "kind": spec.kind.value,
        "semantic_rule_id": rule.semantic_rule_id,
        "operation": spec.implementation.to_dict(),
        "binder": spec.binder.to_dict(),
        "input_ports": list(rule.input_ports),
        "output_ports": list(rule.output_ports),
        "parameter_schema": _parameter_schema(spec.kind).to_dict(),
        "loss_policy": spec.loss_policy.value,
        "uncertainty_propagation_policy":
            spec.uncertainty_propagation_policy.value,
        "scientific_assumption_ids": list(rule.scientific_assumption_ids),
    })
    source = Path(__file__).read_bytes()
    return hashlib.sha256(
        identity.encode("ascii") + b"\0" + source).hexdigest()


def _parameter_rule_sha256(spec: TransformationSpec) -> str:
    """Bind parameters to exact descriptors, semantics, and the closed rule."""
    return strict_hash({
        "schema": "stage8r-transformation-parameter-rule-v1",
        "transformation_spec_id": spec.spec_id,
        "kind": spec.kind.value,
        "semantic_rule_id": spec.semantic_rule_id,
        "input_ports": [item.to_dict() for item in spec.input_ports],
        "output_ports": [item.to_dict() for item in spec.output_ports],
        "parameters": strict_copy(spec.parameters),
        "parameter_schema": _parameter_schema(spec.kind).to_dict(),
        "loss_policy": spec.loss_policy.value,
        "uncertainty_propagation_policy":
            spec.uncertainty_propagation_policy.value,
        "scientific_assumption_ids": list(spec.scientific_assumption_ids),
    })


def _verified_authority_spec(
        authority: TransformationAuthority,
) -> TransformationSpec:
    """Reconstruct and replay the exact authority under current closed code."""
    if not isinstance(authority, TransformationAuthority):
        raise TypeError("transformation authority is invalid")
    if authority.authority_id != authority.expected_id():
        raise ValueError("transformation authority identity does not verify")
    spec = TransformationSpec.from_dict(
        strict_copy(authority.transformation_spec))
    if authority.transformation_spec_id != spec.spec_id:
        raise ValueError("transformation authority names another specification")
    semantic_digest = _semantic_rule_implementation_sha256(spec)
    if authority.semantic_rule_implementation_sha256 != semantic_digest:
        raise ValueError(
            "transformation semantic-rule implementation is stale or forged")
    parameter_digest = _parameter_rule_sha256(spec)
    if authority.parameter_rule_sha256 != parameter_digest:
        raise ValueError(
            "transformation parameter/descriptors rule is stale or forged")
    if authority != spec.to_authority():
        raise ValueError("transformation authority is not the canonical certificate")
    return spec


def verify_capability_transformation_authority(
        capability: CapabilitySpec,
) -> TransformationSpec:
    """Independently replay a transform and its exact capability lowering."""
    if not isinstance(capability, CapabilitySpec):
        raise TypeError("capability must be CapabilitySpec")
    authority = capability.transformation_authority
    if authority is None:
        raise ValueError("reserved transform capability lacks authority")
    spec = _verified_authority_spec(authority)
    expected_inputs = tuple(InputPortTemplate(
        item.port_id, exact_requirement(item.descriptor))
        for item in spec.input_ports)
    expected_outputs = tuple(DescriptorTemplate(
        item.port_id, item.descriptor) for item in spec.output_ports)
    expected_parameterizations = (BindingParameterization(
        strict_copy(spec.parameters), {"cost_units": spec.cost_units}),)
    comparisons = {
        "capability_id": (
            f"transform:{spec.transformation_id}", capability.capability_id),
        "capability_version": (
            spec.transformation_version, capability.capability_version),
        "implementation": (spec.implementation, capability.implementation),
        "binder": (spec.binder, capability.binder),
        "input_ports": (expected_inputs, capability.input_ports),
        "output_ports": (expected_outputs, capability.output_ports),
        "parameter_schema": (
            _parameter_schema(spec.kind), capability.parameter_schema),
        "parameterizations": (
            expected_parameterizations, capability.parameterizations),
        "applicability_key": ("always.v1", capability.applicability_key),
        "evidence_profile_id": (
            "evidence:unknown", capability.evidence_profile_id),
        "cost_model_id": ("cost:declared-v1", capability.cost_model_id),
        "execution_profile_id": (
            spec.execution_profile.profile_id,
            capability.execution_profile_id),
        "transformation_authority": (spec.to_authority(), authority),
    }
    mismatches = sorted(
        name for name, (expected, observed) in comparisons.items()
        if expected != observed)
    if mismatches:
        raise ValueError(
            "transformation authority disagrees with capability lowering: "
            f"{mismatches}")
    return spec


def verify_bound_transformation_authority(
        invocation: BoundInvocation,
) -> TransformationSpec:
    """Independently replay authority retained by a bound invocation."""
    if not isinstance(invocation, BoundInvocation):
        raise TypeError("invocation must be BoundInvocation")
    authority = invocation.transformation_authority
    if authority is None:
        raise ValueError("reserved transform invocation lacks authority")
    spec = _verified_authority_spec(authority)
    input_templates = tuple(InputPortTemplate(
        item.port_id, exact_requirement(item.descriptor))
        for item in spec.input_ports)
    output_templates = tuple(DescriptorTemplate(
        item.port_id, item.descriptor) for item in spec.output_ports)
    binding_seed = strict_hash({
        "schema": "stage2-invocation-binding-seed-v2",
        "capability_id": f"transform:{spec.transformation_id}",
        "capability_version": spec.transformation_version,
        "binder": spec.binder.to_dict(),
        "implementation": spec.implementation.to_dict(),
        "parameters": strict_copy(spec.parameters),
        "inputs": [item.to_dict() for item in input_templates],
        "outputs": [item.to_dict() for item in output_templates],
        "transformation_authority": authority.to_dict(),
    })
    expected_inputs = tuple(
        item.bind(binding_seed) for item in input_templates)
    expected_outputs = tuple(BoundOutputPort(
        item.port_id, item.descriptor) for item in output_templates)
    comparisons = {
        "capability_id": (
            f"transform:{spec.transformation_id}", invocation.capability_id),
        "capability_version": (
            spec.transformation_version, invocation.capability_version),
        "implementation": (spec.implementation, invocation.implementation),
        "binder": (spec.binder, invocation.binder),
        "parameters": (spec.parameters, invocation.parameters),
        "input_uses": (expected_inputs, invocation.input_uses),
        "outputs": (expected_outputs, invocation.outputs),
        "metric_estimates": (
            {"cost_units": spec.cost_units}, invocation.metric_estimates),
        "evidence_profile_id": (
            "evidence:unknown", invocation.evidence_profile_id),
        "cost_model_id": ("cost:declared-v1", invocation.cost_model_id),
        "execution_profile_id": (
            spec.execution_profile.profile_id,
            invocation.execution_profile_id),
        "transformation_authority": (spec.to_authority(), authority),
    }
    mismatches = sorted(
        name for name, (expected, observed) in comparisons.items()
        if expected != observed)
    if mismatches:
        raise ValueError(
            "transformation authority disagrees with bound invocation: "
            f"{mismatches}")
    return spec


def exact_requirement(descriptor: ArtifactDescriptor) -> Requirement:
    """Represent one descriptor identity as an ordinary closed requirement."""
    temporal = descriptor.temporal_support
    if temporal.kind is TemporalKind.TIME_INVARIANT:
        temporal_requirement = TemporalRequirement(TemporalKind.TIME_INVARIANT)
    else:
        if temporal.cadence_s is None or temporal.max_gap_s is None:
            raise ValueError(
                "exact transform inputs with series data require declared "
                "cadence and maximum gap")
        temporal_requirement = TemporalRequirement(
            kind=TemporalKind.SERIES,
            start=temporal.start,
            end=temporal.end,
            cadence_s=temporal.cadence_s,
            cadence_policy=CadencePolicy.EXACT,
            anchor=temporal.anchor,
            maximum_gap_s=temporal.max_gap_s,
            sample_semantics=temporal.sample_semantics,
            reference_time=temporal.reference_time,
        )
    vertical = descriptor.vertical_support
    vertical_requirement = (VerticalRequirement(
        vertical.kind, vertical.unit, vertical.datum, vertical.levels)
        if vertical is not None else None)
    missing = descriptor.missingness
    missing_policy = MissingPolicy(
        max_fraction=(missing.max_fraction if missing.max_fraction is not None else "1"),
        unknown_policy=(UnknownPolicy.ALLOW_WITH_CAVEAT
                        if missing.status is MissingnessStatus.UNKNOWN
                        else UnknownPolicy.FORBID),
        accepted_encodings=missing.encodings,
    )
    return Requirement(
        concept_id=descriptor.concept_id,
        accepted_schema_versions=(descriptor.schema_version,),
        representation=ValueConstraint.exact(descriptor.representation),
        units=ValueConstraint.exact(descriptor.units),
        spatial=SpatialRequirement(
            descriptor.spatial_support, exact_grid=descriptor.grid),
        temporal=temporal_requirement,
        vertical=vertical_requirement,
        allowed_origins=(descriptor.origin,),
        max_native_resolution=None,
        max_effective_resolution=None,
        missing_policy=missing_policy,
        minimum_evidence=EvidenceRequirement(allow_unknown_empirical=True),
        exact_descriptor_id=descriptor.descriptor_id,
    )


def _descriptor_fields_equal(
        source: ArtifactDescriptor, result: ArtifactDescriptor,
        names: tuple[str, ...]) -> bool:
    return all(getattr(source, name) == getattr(result, name) for name in names)


_VALUE_FIELDS = (
    "concept_id", "schema_version", "representation", "spatial_support",
    "temporal_support", "vertical_support", "grid", "native_resolution",
    "missingness", "ensemble_member", "component_names",
)
_SPATIAL_PRESERVED_FIELDS = (
    "concept_id", "schema_version", "representation", "units",
    "temporal_support", "vertical_support", "native_resolution",
    "missingness", "ensemble_member", "component_names",
)
_TEMPORAL_PRESERVED_FIELDS = (
    "concept_id", "schema_version", "representation", "units",
    "spatial_support", "vertical_support", "grid", "native_resolution",
    "missingness", "ensemble_member", "component_names",
)


def _ports(spec: TransformationSpec) -> tuple[dict[str, ArtifactDescriptor],
                                                dict[str, ArtifactDescriptor]]:
    return (
        {item.port_id: item.descriptor for item in spec.input_ports},
        {item.port_id: item.descriptor for item in spec.output_ports},
    )


def _validate_block_cell_sizes(source, result, block_x: int,
                               block_y: int) -> None:
    """Each result cell must be exactly `block_x` x `block_y` source cells.

    Shapes agreeing is not enough: two grids can have the right cell counts and
    still describe different ground, so the cell sizes and the origin are
    checked too.
    """
    source_x, source_y = (decimal_value(source.affine[0]),
                          decimal_value(source.affine[4]))
    result_x, result_y = (decimal_value(result.affine[0]),
                          decimal_value(result.affine[4]))
    if (source_x * block_x != result_x) or (source_y * block_y != result_y):
        raise ValueError(
            "block factors disagree with the declared cell sizes")
    # Affines name sample centres.  A coarse output centre is the mean of the
    # contributing fine centres, not the fine grid's first centre.
    expected_x0 = (decimal_value(source.affine[2])
                   + source_x * (block_x - 1) / 2)
    expected_y0 = (decimal_value(source.affine[5])
                   + source_y * (block_y - 1) / 2)
    if (expected_x0 != decimal_value(result.affine[2])
            or expected_y0 != decimal_value(result.affine[5])):
        raise ValueError(
            "block aggregation output origin must be the exact block centre; "
            "an offset lattice is a resampling, not a reduction")


def _validate_uncertainty_propagation(
        spec: TransformationSpec,
        source: ArtifactDescriptor,
        outputs: dict[str, ArtifactDescriptor],
) -> None:
    """Refuse uncertainty claims that contradict the closed edge policy.

    Exact index selection does not alter values, so it must preserve the
    complete uncertainty record.  Every numerical mapping, interpolation,
    aggregation, and derivation must instead bind a new known model/parameter
    manifest or explicitly report that the propagated uncertainty is unknown.
    Reusing the source's known record would falsely claim that a model in the
    old units/value space also describes the transformed values.

    UNKNOWN is intentionally allowed to remain UNKNOWN.  The transformation
    contract cannot manufacture knowledge that the producer never supplied.
    """
    source_uncertainty = source.intrinsic_uncertainty
    policy = spec.uncertainty_propagation_policy
    if policy is UncertaintyPropagationPolicy.PRESERVE:
        if any(result.intrinsic_uncertainty != source_uncertainty
               for result in outputs.values()):
            raise ValueError(
                "exact-selection uncertainty policy requires preservation")
        return
    if policy is not UncertaintyPropagationPolicy.REBIND_OR_MARK_UNKNOWN:
        raise AssertionError(f"unvalidated uncertainty policy {policy!r}")
    if source_uncertainty.status is UncertaintyStatus.UNKNOWN:
        if any(result.intrinsic_uncertainty.status
               is not UncertaintyStatus.UNKNOWN
               for result in outputs.values()):
            raise ValueError(
                "a transformation cannot turn UNKNOWN source uncertainty "
                "into a known or not-applicable output uncertainty")
        return
    if source_uncertainty.status is not UncertaintyStatus.KNOWN:
        return
    for port_id, result in sorted(outputs.items()):
        result_uncertainty = result.intrinsic_uncertainty
        if result_uncertainty == source_uncertainty:
            raise ValueError(
                "value-changing transformation cannot preserve a KNOWN "
                f"intrinsic uncertainty unchanged on output {port_id!r}; "
                "bind a propagated model/manifest or mark it UNKNOWN")
        if result_uncertainty.status is UncertaintyStatus.NOT_APPLICABLE:
            raise ValueError(
                "value-changing transformation cannot discard a KNOWN "
                f"intrinsic uncertainty as NOT_APPLICABLE on output {port_id!r}")


def _validate_semantics(spec: TransformationSpec) -> None:
    inputs, outputs = _ports(spec)
    input_semantics = {
        item.port_id: item.value_semantics for item in spec.input_ports}
    output_semantics = {
        item.port_id: item.value_semantics for item in spec.output_ports}
    if any(item.origin is not OriginClass.DERIVED for item in outputs.values()):
        raise ValueError("every transformation output must declare DERIVED origin")
    source = inputs["source"]
    _validate_uncertainty_propagation(spec, source, outputs)

    if spec.kind is TransformationKind.UNIT_AFFINE:
        result = outputs["result"]
        if (input_semantics["source"] is ValueSemantics.UNSPECIFIED
                or input_semantics["source"] is not output_semantics["result"]):
            raise ValueError(
                "unit conversion must preserve an explicit value semantics")
        conversion = unit_affine_rule(source.units, result.units)
        if strict_hash(spec.parameters) != strict_hash(conversion.parameters):
            raise ValueError(
                "unit coefficients do not match the closed versioned registry")
        if not _descriptor_fields_equal(source, result, _VALUE_FIELDS):
            raise ValueError("unit conversion may change only units and derivation metadata")
        return

    if spec.kind is TransformationKind.SPATIAL_SUBSET:
        result = outputs["result"]
        if input_semantics["source"] is not output_semantics["result"]:
            raise ValueError("spatial subset must preserve value semantics")
        if not _descriptor_fields_equal(
                source, result, _SPATIAL_PRESERVED_FIELDS):
            raise ValueError("spatial subset falsely changes non-spatial metadata")
        if not source.spatial_support.contains(result.spatial_support):
            raise ValueError("spatial subset result is outside source support")
        if source.grid is None or result.grid is None:
            raise ValueError("spatial index subset requires declared source/result grids")
        _validate_grid_support(source)
        x0, x1 = spec.parameters["x_start"], spec.parameters["x_stop"]
        y0, y1 = spec.parameters["y_start"], spec.parameters["y_stop"]
        if min(x0, y0) < 0 or x1 <= x0 or y1 <= y0:
            raise ValueError("spatial subset indices are invalid")
        if result.grid.shape != (y1 - y0, x1 - x0):
            raise ValueError("spatial subset indices disagree with result grid shape")
        if y1 > source.grid.shape[0] or x1 > source.grid.shape[1]:
            raise ValueError("spatial subset indices exceed the source grid")
        _validate_subset_grid(source, result, x0, y0)
        return

    if spec.kind in (
            TransformationKind.TEMPORAL_SUBSET,
            TransformationKind.TEMPORAL_ALIGN):
        result = outputs["result"]
        if input_semantics["source"] is not output_semantics["result"]:
            raise ValueError("temporal transform must preserve value semantics")
        if not _descriptor_fields_equal(
                source, result, _TEMPORAL_PRESERVED_FIELDS):
            raise ValueError("temporal transform falsely changes non-temporal metadata")
        _validate_temporal_indices(spec, source, result)
        return

    if spec.kind in (
            TransformationKind.REGRID_BILINEAR,
            TransformationKind.REPROJECT_BILINEAR):
        result = outputs["result"]
        required_semantics = ValueSemantics.SCALAR_CONTINUOUS_INTENSIVE
        if (input_semantics["source"] is not required_semantics
                or output_semantics["result"] is not required_semantics):
            raise ValueError(
                "bilinear transforms admit only explicitly typed "
                "continuous/intensive scalar fields")
        if not _descriptor_fields_equal(
                source, result, _SPATIAL_PRESERVED_FIELDS):
            raise ValueError("grid transform falsely changes non-spatial metadata")
        if source.grid is None or result.grid is None:
            raise ValueError("bilinear grid transforms require declared grids")
        _validate_grid_support(source)
        target_x = spec.parameters["target_x"]
        target_y = spec.parameters["target_y"]
        _validate_axis(target_x, "target_x")
        _validate_axis(target_y, "target_y")
        if result.grid.shape != (len(target_y), len(target_x)):
            raise ValueError("target coordinates disagree with result grid shape")
        _validate_target_grid(result, target_x, target_y)
        if spec.kind is TransformationKind.REGRID_BILINEAR:
            if (source.spatial_support.crs != result.spatial_support.crs
                    or source.grid.crs != result.grid.crs):
                raise ValueError("regrid cannot change CRS; use explicit reprojection")
            if not source.spatial_support.contains(result.spatial_support):
                raise ValueError("regrid target is outside source support")
            _require_samples_inside_source_grid(
                source.grid,
                [[(x, y) for x in target_x] for y in target_y],
                "regrid target")
        else:
            if (spec.parameters["source_crs"] != source.grid.crs
                    or spec.parameters["target_crs"] != result.grid.crs
                    or tuple(spec.parameters["source_axis_order"])
                    != source.grid.axis_order
                    or tuple(spec.parameters["target_axis_order"])
                    != result.grid.axis_order
                    or source.spatial_support.crs != source.grid.crs
                    or result.spatial_support.crs != result.grid.crs):
                raise ValueError("reprojection CRS parameters disagree with descriptors")
            if source.grid.crs == result.grid.crs:
                raise ValueError("same-CRS interpolation is regrid, not reprojection")
            from engine.runtime.operations import reprojection_sample_points
            sample_points = reprojection_sample_points(
                spec.parameters, verify_local_selection=True)
            _require_samples_inside_source_grid(
                source.grid, sample_points, "reprojected target")
        return

    if spec.kind is TransformationKind.SPATIAL_BLOCK_AGGREGATE:
        result = outputs["result"]
        semantics = input_semantics["source"]
        if semantics is not output_semantics["result"]:
            raise ValueError(
                "block aggregation must preserve the declared value class")
        # The declared value class picks the aggregation; the parameter may
        # only restate it.  This is what stops a mean being applied to an
        # arrival time by writing a different string in the parameters.
        expected = block_aggregation_for(semantics)
        if spec.parameters["aggregation"] != expected.value:
            raise ValueError(
                f"{semantics.value} admits only {expected.value}, not "
                f"{spec.parameters['aggregation']!r}")
        if not _descriptor_fields_equal(
                source, result, _SPATIAL_PRESERVED_FIELDS):
            raise ValueError(
                "block aggregation falsely changes non-spatial metadata")
        if source.grid is None or result.grid is None:
            raise ValueError("block aggregation requires declared grids")
        _validate_grid_support(source)
        _validate_grid_support(result)
        if source.grid.crs != result.grid.crs:
            raise ValueError(
                "block aggregation cannot change CRS; it is an index "
                "operation, not a reprojection")
        block_x = spec.parameters["block_x"]
        block_y = spec.parameters["block_y"]
        if block_x < 1 or block_y < 1:
            raise ValueError("block factors must be positive")
        # Exact cover: no partial trailing block, in either axis.
        if source.grid.shape != (result.grid.shape[0] * block_y,
                                 result.grid.shape[1] * block_x):
            raise ValueError(
                "block factors do not exactly cover the source grid; a "
                "partial trailing block is not an aggregation")
        _validate_block_cell_sizes(source.grid, result.grid, block_x, block_y)
        return

    if spec.kind is TransformationKind.VECTOR_ROTATE:
        result = outputs["result"]
        if (input_semantics["source"] is not ValueSemantics.CANONICAL_UV_VECTOR
                or output_semantics["result"]
                is not ValueSemantics.CANONICAL_UV_VECTOR):
            raise ValueError(
                "vector rotation requires one canonical U/V vector artifact")
        if not _descriptor_fields_equal(source, result, (
                "concept_id", "schema_version", "representation", "units",
                "spatial_support", "temporal_support", "vertical_support",
                "grid", "native_resolution", "missingness", "ensemble_member",
                "component_names",
                )):
            raise ValueError("vector rotation falsely changes vector descriptor metadata")
        if source.component_names != ("u", "v"):
            raise ValueError(
                "vector rotation requires exact ('u', 'v') components")
        return

    if spec.kind is TransformationKind.VECTOR_UV_TO_SPEED_DIRECTION:
        speed, direction = outputs["speed"], outputs["direction"]
        if (input_semantics["source"] is not ValueSemantics.CANONICAL_UV_VECTOR
                or output_semantics["speed"]
                is not ValueSemantics.SCALAR_CONTINUOUS_INTENSIVE
                or output_semantics["direction"]
                is not ValueSemantics.CIRCULAR_DIRECTION):
            raise ValueError(
                "vector decomposition requires one canonical U/V input and "
                "typed scalar outputs")
        common = (
            "schema_version", "representation", "spatial_support",
            "temporal_support", "vertical_support", "grid",
            "native_resolution", "missingness", "ensemble_member",
        )
        if (not _descriptor_fields_equal(source, speed, common)
                or not _descriptor_fields_equal(source, direction, common)):
            raise ValueError("vector decomposition falsely changes support metadata")
        if speed.units != source.units or direction.units != "degree":
            raise ValueError("vector decomposition has invalid output units")
        if len({source.concept_id, speed.concept_id, direction.concept_id}) != 3:
            raise ValueError("vector and scalar output concepts must be distinct")
        if (source.component_names != ("u", "v")
                or speed.component_names != ("speed",)
                or direction.component_names != ("direction",)):
            raise ValueError(
                "vector decomposition component contracts are invalid")
        return

    raise AssertionError(f"unvalidated transformation kind {spec.kind!r}")


def _validate_axis(value: Any, label: str) -> None:
    if not isinstance(value, (tuple, list)) or len(value) < 2:
        raise ValueError(f"{label} must have at least two coordinates")
    numbers: list[float] = []
    for item in value:
        if (isinstance(item, bool) or not isinstance(item, (int, float))
                or (isinstance(item, float) and not math.isfinite(item))):
            raise ValueError(f"{label} coordinates must be finite numeric")
        numbers.append(float(item))
    differences = [right - left
                   for left, right in zip(numbers, numbers[1:])]
    if (any(item == 0 for item in differences)
            or min(differences) < 0 < max(differences)):
        raise ValueError(f"{label} coordinates must be strictly monotonic")


def _validate_subset_grid(
        source: ArtifactDescriptor, result: ArtifactDescriptor,
        x_start: int, y_start: int) -> None:
    assert source.grid is not None and result.grid is not None
    offered = source.grid
    produced = result.grid
    if (offered.crs != produced.crs
            or offered.axis_order != produced.axis_order
            or offered.spacing != produced.spacing):
        raise ValueError("spatial subset must preserve grid CRS/axes/spacing")
    a, b, c, d, e, f = (decimal_value(item) for item in offered.affine)
    expected_affine = tuple(canonical_decimal(item) for item in (
        a, b, c + a * x_start + b * y_start,
        d, e, f + d * x_start + e * y_start,
    ))
    if produced.affine != expected_affine:
        raise ValueError("spatial subset result affine does not follow its indices")
    _validate_grid_support(result)


def _validate_grid_support(descriptor: ArtifactDescriptor) -> None:
    assert descriptor.grid is not None
    descriptor.grid.require_support(descriptor.spatial_support)


def _require_samples_inside_source_grid(
        source_grid, sample_points: list[list[tuple[float, float]]],
        label: str) -> None:
    """Bilinear support is bounded by centres, not outer cell edges."""
    source_x = tuple(decimal_value(item)
                     for item in source_grid.centre_axis("x"))
    source_y = tuple(decimal_value(item)
                     for item in source_grid.centre_axis("y"))
    xmin, xmax = min(source_x), max(source_x)
    ymin, ymax = min(source_y), max(source_y)
    for row_index, row in enumerate(sample_points):
        for column_index, (raw_x, raw_y) in enumerate(row):
            x, y = decimal_value(raw_x), decimal_value(raw_y)
            if x < xmin or x > xmax or y < ymin or y > ymax:
                raise ValueError(
                    f"{label} sample [{row_index},{column_index}] is outside "
                    "the source sample-centre interpolation support")


def _validate_target_grid(
        result: ArtifactDescriptor, target_x: Any, target_y: Any) -> None:
    assert result.grid is not None
    x = tuple(decimal_value(item) for item in target_x)
    y = tuple(decimal_value(item) for item in target_y)
    dx = x[1] - x[0]
    dy = y[1] - y[0]
    if any(x[index] - x[index - 1] != dx for index in range(2, len(x))):
        raise ValueError("target_x must be uniformly spaced for an affine grid")
    if any(y[index] - y[index - 1] != dy for index in range(2, len(y))):
        raise ValueError("target_y must be uniformly spaced for an affine grid")
    expected_affine = tuple(canonical_decimal(item) for item in (
        dx, 0, x[0], 0, dy, y[0]))
    if result.grid.affine != expected_affine:
        raise ValueError("target coordinates disagree with result grid affine")
    if (decimal_value(result.grid.spacing.x) != abs(dx)
            or decimal_value(result.grid.spacing.y) != abs(dy)):
        raise ValueError("target coordinates disagree with result grid spacing")
    _validate_grid_support(result)


def _validate_temporal_indices(
        spec: TransformationSpec, source: ArtifactDescriptor,
        result: ArtifactDescriptor) -> None:
    offered = source.temporal_support
    produced = result.temporal_support
    if (offered.kind is not TemporalKind.SERIES
            or produced.kind is not TemporalKind.SERIES
            or offered.start is None or offered.end is None
            or produced.start is None or produced.end is None
            or offered.cadence_s is None or produced.cadence_s is None):
        raise ValueError("temporal index transforms require regular series metadata")
    cadence_decimal = decimal_value(offered.cadence_s)
    cadence = float(cadence_decimal)
    source_span_s = decimal_value(str(
        (timestamp_value(offered.end) - timestamp_value(offered.start))
        .total_seconds()))
    if source_span_s % cadence_decimal != 0:
        raise ValueError("source temporal support is not a regular half-open lattice")
    source_count = int(source_span_s / cadence_decimal)
    if spec.kind is TransformationKind.TEMPORAL_SUBSET:
        start = spec.parameters["start"]
        stop = spec.parameters["stop"]
        if start < 0 or stop <= start or stop > source_count:
            raise ValueError("temporal subset indices exceed the source lattice")
        expected_start = timestamp_value(offered.start) + timedelta(
            seconds=start * cadence)
        expected_end = timestamp_value(offered.start) + timedelta(
            seconds=stop * cadence)
        expected_cadence = offered.cadence_s
        expected_anchor = offered.anchor
    else:
        start = spec.parameters["start_index"]
        step = spec.parameters["step"]
        count = spec.parameters["count"]
        if (start < 0 or step < 1 or count < 1
                or start + (count - 1) * step >= source_count):
            raise ValueError("temporal alignment indices are invalid")
        expected_start = timestamp_value(offered.start) + timedelta(
            seconds=start * cadence)
        expected_end = expected_start + timedelta(
            seconds=count * step * cadence)
        expected_cadence = str(decimal_value(offered.cadence_s) * step)
        expected_anchor = expected_start
    if (timestamp_value(produced.start) != expected_start
            or timestamp_value(produced.end) != expected_end
            or decimal_value(produced.cadence_s) != decimal_value(expected_cadence)
            or produced.sample_semantics is not offered.sample_semantics
            or produced.reference_time != offered.reference_time
            or produced.max_gap_s != offered.max_gap_s
            or (timestamp_value(produced.anchor) if produced.anchor is not None
                else None) != (timestamp_value(expected_anchor)
                               if expected_anchor is not None else None)):
        raise ValueError("temporal indices disagree with result temporal descriptor")


@dataclass(frozen=True)
class TransformationCatalog:
    catalog_id: str
    transformations: tuple[TransformationSpec, ...]

    def __post_init__(self) -> None:
        _digest(self.catalog_id, "transformation catalog_id")
        if (not isinstance(self.transformations, tuple)
                or not all(isinstance(item, TransformationSpec)
                           for item in self.transformations)):
            raise TypeError("transformations must be an immutable typed tuple")
        if self.transformations != tuple(sorted(
                self.transformations, key=lambda item: item.spec_id)):
            raise ValueError("transformations must be sorted by spec_id")
        if len({item.spec_id for item in self.transformations}) \
                != len(self.transformations):
            raise ValueError("transformation catalog has duplicate specifications")
        if self.catalog_id != self.expected_id():
            raise ValueError("transformation catalog identity does not verify")

    @classmethod
    def freeze(cls, transformations: Iterable[TransformationSpec]
               ) -> "TransformationCatalog":
        items = tuple(sorted(transformations, key=lambda item: item.spec_id))
        payload = {
            "schema": "stage4-transformation-catalog-v1",
            "transformations": [item.to_dict() for item in items],
        }
        return cls(strict_hash(payload), items)

    def expected_id(self) -> str:
        return strict_hash({
            "schema": "stage4-transformation-catalog-v1",
            "transformations": [item.to_dict() for item in self.transformations],
        })

    def to_dict(self) -> dict[str, Any]:
        return {
            "catalog_id": self.catalog_id,
            "transformations": [item.to_dict() for item in self.transformations],
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "TransformationCatalog":
        raw = require_object_fields(
            value, {field.name for field in dataclasses.fields(cls)},
            "TransformationCatalog")
        if not isinstance(raw["transformations"], list):
            raise ValueError("TransformationCatalog.transformations must be an array")
        raw["transformations"] = tuple(
            TransformationSpec.from_dict(item) for item in raw["transformations"])
        return cls(**raw)
