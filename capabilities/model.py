"""Immutable Stage-2 capability and bound-invocation records."""
from __future__ import annotations

import dataclasses
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable

from contracts import (
    ArtifactDescriptor,
    Cardinality,
    CompatibilityProof,
    DistinctnessPolicy,
    Requirement,
    RequirementUse,
)
from engine.runtime.identity import (
    freeze_json,
    require_object_fields,
    strict_copy,
    strict_hash,
)

from .binders import BinderRef, binder_rule
from .implementation import ImplementationRef, _digest, _required_text
from .parameters import ParameterSchema


_PORT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


def _port(value: str, label: str = "port_id") -> None:
    if not isinstance(value, str) or not _PORT.fullmatch(value):
        raise ValueError(f"{label} is not a safe, non-empty port identifier")


@dataclass(frozen=True)
class InputPortTemplate:
    """A reusable consumer port before its invocation-local use ID exists."""

    port_id: str
    requirement: Requirement
    optional: bool = False
    default_id: str | None = None
    cardinality: Cardinality = Cardinality()
    distinctness: DistinctnessPolicy = DistinctnessPolicy.ALLOW_SAME
    shareable: bool = True

    def __post_init__(self) -> None:
        _port(self.port_id)
        if not isinstance(self.requirement, Requirement):
            raise TypeError("input template requirement is invalid")
        if type(self.optional) is not bool or type(self.shareable) is not bool:
            raise TypeError("input optional/shareable flags must be bool")
        if self.default_id is not None:
            _required_text(self.default_id, "default_id")
        if not isinstance(self.cardinality, Cardinality):
            raise TypeError("input cardinality is invalid")
        if not isinstance(self.distinctness, DistinctnessPolicy):
            raise TypeError("input distinctness policy is invalid")

    def bind(self, binding_seed: str) -> RequirementUse:
        _digest(binding_seed, "binding_seed")
        use_id = "use:" + strict_hash({
            "schema": "stage2-bound-input-use-v1",
            "binding_seed": binding_seed,
            "port_id": self.port_id,
        })
        return RequirementUse(
            use_id=use_id,
            port_id=self.port_id,
            requirement=self.requirement,
            optional=self.optional,
            default_id=self.default_id,
            cardinality=self.cardinality,
            distinctness=self.distinctness,
            shareable=self.shareable,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "port_id": self.port_id,
            "requirement": self.requirement.to_dict(),
            "optional": self.optional,
            "default_id": self.default_id,
            "cardinality": self.cardinality.to_dict(),
            "distinctness": self.distinctness.value,
            "shareable": self.shareable,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "InputPortTemplate":
        raw = require_object_fields(
            value, {field.name for field in dataclasses.fields(cls)},
            "InputPortTemplate")
        raw["requirement"] = Requirement.from_dict(raw["requirement"])
        raw["cardinality"] = Cardinality.from_dict(raw["cardinality"])
        raw["distinctness"] = DistinctnessPolicy(raw["distinctness"])
        return cls(**raw)


@dataclass(frozen=True)
class DescriptorTemplate:
    """A fixed descriptor template for the bounded Stage-2 synthetic slice."""

    port_id: str
    descriptor: ArtifactDescriptor

    def __post_init__(self) -> None:
        _port(self.port_id)
        if not isinstance(self.descriptor, ArtifactDescriptor):
            raise TypeError("output descriptor template is invalid")

    def to_dict(self) -> dict[str, Any]:
        return {"port_id": self.port_id,
                "descriptor": self.descriptor.to_dict()}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "DescriptorTemplate":
        raw = require_object_fields(
            value, {field.name for field in dataclasses.fields(cls)},
            "DescriptorTemplate")
        raw["descriptor"] = ArtifactDescriptor.from_dict(raw["descriptor"])
        return cls(**raw)


@dataclass(frozen=True)
class BindingParameterization:
    """One member of a finite, exhaustively enumerated binding domain."""

    parameters: dict[str, Any] = field(default_factory=dict)
    metric_estimates: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "parameters", freeze_json(self.parameters))
        object.__setattr__(self, "metric_estimates",
                           freeze_json(self.metric_estimates))
        if (not isinstance(self.parameters, dict)
                or not isinstance(self.metric_estimates, dict)):
            raise ValueError("binding parameters and metrics must be JSON objects")

    def to_dict(self) -> dict[str, Any]:
        return {
            "parameters": strict_copy(self.parameters),
            "metric_estimates": strict_copy(self.metric_estimates),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "BindingParameterization":
        raw = require_object_fields(
            value, {field.name for field in dataclasses.fields(cls)},
            "BindingParameterization")
        return cls(**raw)


@dataclass(frozen=True)
class CapabilitySpec:
    """A finite relation from input requirements to prospective outputs."""

    spec_id: str
    capability_id: str
    capability_version: str
    implementation: ImplementationRef
    binder: BinderRef
    input_ports: tuple[InputPortTemplate, ...]
    output_ports: tuple[DescriptorTemplate, ...]
    parameter_schema: ParameterSchema
    parameterizations: tuple[BindingParameterization, ...]
    applicability_key: str
    evidence_profile_id: str
    cost_model_id: str
    execution_profile_id: str

    def __post_init__(self) -> None:
        _digest(self.spec_id, "capability spec_id")
        _digest(self.execution_profile_id, "execution_profile_id")
        for name in (
            "capability_id", "capability_version", "applicability_key",
            "evidence_profile_id", "cost_model_id",
        ):
            _required_text(getattr(self, name), name)
        if self.applicability_key != "always.v1":
            raise ValueError("only the closed Stage-2 applicability key 'always.v1' is supported")
        if not isinstance(self.implementation, ImplementationRef):
            raise TypeError("capability implementation is invalid")
        if not isinstance(self.binder, BinderRef):
            raise TypeError("capability binder is invalid")
        if not isinstance(self.parameter_schema, ParameterSchema):
            raise TypeError("capability parameter schema is invalid")
        for values, expected, label in (
            (self.input_ports, InputPortTemplate, "input ports"),
            (self.output_ports, DescriptorTemplate, "output ports"),
            (self.parameterizations, BindingParameterization,
             "parameterizations"),
        ):
            if (not isinstance(values, tuple)
                    or not all(isinstance(value, expected) for value in values)):
                raise TypeError(f"{label} must be an immutable typed tuple")
        if not self.output_ports or not self.parameterizations:
            raise ValueError("capability needs outputs and a finite parameterization")
        for values, label in ((self.input_ports, "input"),
                              (self.output_ports, "output")):
            port_ids = [value.port_id for value in values]
            if len(port_ids) != len(set(port_ids)):
                raise ValueError(f"capability has duplicate {label} ports")
        serialized = [strict_hash(value.to_dict())
                      for value in self.parameterizations]
        if serialized != sorted(serialized) or len(serialized) != len(set(serialized)):
            raise ValueError(
                "parameterizations must be unique and sorted by canonical identity")
        binding_keys = [strict_hash({"parameters": value.parameters})
                        for value in self.parameterizations]
        if len(binding_keys) != len(set(binding_keys)):
            raise ValueError(
                "distinct metric estimates cannot duplicate one bound invocation")
        rule = binder_rule(self.binder.binder_key)
        if rule.operation_key != self.implementation.operation_key:
            raise ValueError("binder and executable operation disagree")
        if tuple(value.port_id for value in self.input_ports) != rule.input_ports:
            raise ValueError("capability input ports disagree with its closed binder")
        if tuple(value.port_id for value in self.output_ports) != rule.output_ports:
            raise ValueError("capability output ports disagree with its closed binder")
        schema_names = tuple(field.name for field in self.parameter_schema.fields)
        if schema_names != tuple(sorted(rule.parameter_names)):
            raise ValueError("parameter schema disagrees with its closed binder")
        for parameterization in self.parameterizations:
            self.parameter_schema.validate(parameterization.parameters)
        if self.spec_id != self.expected_id():
            raise ValueError("capability specification identity does not verify")

    @classmethod
    def bind(
            cls, *, capability_id: str, capability_version: str,
            implementation: ImplementationRef, binder: BinderRef,
            input_ports: Iterable[InputPortTemplate],
            output_ports: Iterable[DescriptorTemplate],
            parameter_schema: ParameterSchema,
            parameterizations: Iterable[BindingParameterization],
            execution_profile_id: str,
            applicability_key: str = "always.v1",
            evidence_profile_id: str = "evidence:unknown",
            cost_model_id: str = "cost:declared-v1",
    ) -> "CapabilitySpec":
        implementation.verify_current()
        binder.verify_current()
        inputs = tuple(input_ports)
        outputs = tuple(output_ports)
        params = tuple(sorted(
            parameterizations, key=lambda value: strict_hash(value.to_dict())))
        payload = _capability_payload(
            capability_id, capability_version, implementation, binder,
            inputs, outputs, parameter_schema, params, applicability_key,
            evidence_profile_id, cost_model_id, execution_profile_id)
        return cls(
            strict_hash(payload), capability_id, capability_version,
            implementation, binder, inputs, outputs, parameter_schema, params,
            applicability_key, evidence_profile_id, cost_model_id,
            execution_profile_id,
        )

    def expected_id(self) -> str:
        return strict_hash(_capability_payload(
            self.capability_id, self.capability_version,
            self.implementation, self.binder, self.input_ports,
            self.output_ports, self.parameter_schema, self.parameterizations,
            self.applicability_key, self.evidence_profile_id,
            self.cost_model_id, self.execution_profile_id))

    def to_dict(self) -> dict[str, Any]:
        return {
            "spec_id": self.spec_id,
            "capability_id": self.capability_id,
            "capability_version": self.capability_version,
            "implementation": self.implementation.to_dict(),
            "binder": self.binder.to_dict(),
            "input_ports": [value.to_dict() for value in self.input_ports],
            "output_ports": [value.to_dict() for value in self.output_ports],
            "parameter_schema": self.parameter_schema.to_dict(),
            "parameterizations": [
                value.to_dict() for value in self.parameterizations],
            "applicability_key": self.applicability_key,
            "evidence_profile_id": self.evidence_profile_id,
            "cost_model_id": self.cost_model_id,
            "execution_profile_id": self.execution_profile_id,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "CapabilitySpec":
        raw = require_object_fields(
            value, {field.name for field in dataclasses.fields(cls)},
            "CapabilitySpec")
        raw["implementation"] = ImplementationRef.from_dict(raw["implementation"])
        raw["binder"] = BinderRef.from_dict(raw["binder"])
        for name, parser in (
            ("input_ports", InputPortTemplate.from_dict),
            ("output_ports", DescriptorTemplate.from_dict),
            ("parameterizations", BindingParameterization.from_dict),
        ):
            if not isinstance(raw[name], list):
                raise ValueError(f"CapabilitySpec.{name} must be an array")
            raw[name] = tuple(parser(item) for item in raw[name])
        raw["parameter_schema"] = ParameterSchema.from_dict(
            raw["parameter_schema"])
        return cls(**raw)


def _capability_payload(
        capability_id: str, capability_version: str,
        implementation: ImplementationRef, binder: BinderRef,
        input_ports: tuple[InputPortTemplate, ...],
        output_ports: tuple[DescriptorTemplate, ...],
        parameter_schema: ParameterSchema,
        parameterizations: tuple[BindingParameterization, ...],
        applicability_key: str, evidence_profile_id: str,
        cost_model_id: str, execution_profile_id: str,
) -> dict[str, Any]:
    return {
        "schema": "stage2-capability-spec-v1",
        "capability_id": capability_id,
        "capability_version": capability_version,
        "implementation": implementation.to_dict(),
        "binder": binder.to_dict(),
        "input_ports": [value.to_dict() for value in input_ports],
        "output_ports": [value.to_dict() for value in output_ports],
        "parameter_schema": parameter_schema.to_dict(),
        "parameterizations": [value.to_dict() for value in parameterizations],
        "applicability_key": applicability_key,
        "evidence_profile_id": evidence_profile_id,
        "cost_model_id": cost_model_id,
        "execution_profile_id": execution_profile_id,
    }


@dataclass(frozen=True)
class BoundOutputPort:
    port_id: str
    descriptor: ArtifactDescriptor

    def __post_init__(self) -> None:
        _port(self.port_id)
        if not isinstance(self.descriptor, ArtifactDescriptor):
            raise TypeError("bound output descriptor is invalid")

    def to_dict(self) -> dict[str, Any]:
        return {"port_id": self.port_id,
                "descriptor": self.descriptor.to_dict()}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "BoundOutputPort":
        raw = require_object_fields(
            value, {field.name for field in dataclasses.fields(cls)},
            "BoundOutputPort")
        raw["descriptor"] = ArtifactDescriptor.from_dict(raw["descriptor"])
        return cls(**raw)


@dataclass(frozen=True)
class BoundInvocation:
    invocation_key: str
    capability_id: str
    capability_version: str
    implementation: ImplementationRef
    binder: BinderRef
    parameters: dict[str, Any]
    input_uses: tuple[RequirementUse, ...]
    outputs: tuple[BoundOutputPort, ...]
    metric_estimates: dict[str, Any]
    evidence_profile_id: str
    cost_model_id: str
    execution_profile_id: str

    def __post_init__(self) -> None:
        _digest(self.invocation_key, "invocation_key")
        _digest(self.execution_profile_id, "bound execution_profile_id")
        for name in (
            "capability_id", "capability_version", "evidence_profile_id",
            "cost_model_id",
        ):
            _required_text(getattr(self, name), name)
        if not isinstance(self.implementation, ImplementationRef):
            raise TypeError("bound implementation is invalid")
        if not isinstance(self.binder, BinderRef):
            raise TypeError("bound binder is invalid")
        object.__setattr__(self, "parameters", freeze_json(self.parameters))
        object.__setattr__(self, "metric_estimates",
                           freeze_json(self.metric_estimates))
        if (not isinstance(self.parameters, dict)
                or not isinstance(self.metric_estimates, dict)):
            raise ValueError("bound parameters and metrics must be JSON objects")
        if (not isinstance(self.input_uses, tuple)
                or not all(isinstance(value, RequirementUse)
                           for value in self.input_uses)):
            raise TypeError("bound input uses must be an immutable typed tuple")
        if (not isinstance(self.outputs, tuple) or not self.outputs
                or not all(isinstance(value, BoundOutputPort)
                           for value in self.outputs)):
            raise TypeError("bound outputs must be a non-empty typed tuple")
        if len({value.port_id for value in self.input_uses}) != len(self.input_uses):
            raise ValueError("bound invocation has duplicate input ports")
        if len({value.port_id for value in self.outputs}) != len(self.outputs):
            raise ValueError("bound invocation has duplicate output ports")
        if self.invocation_key != self.expected_key():
            raise ValueError("bound invocation identity does not verify")

    @classmethod
    def bind(cls, spec: CapabilitySpec,
             parameterization: BindingParameterization) -> "BoundInvocation":
        spec.implementation.verify_current()
        spec.binder.verify_current()
        parameters = spec.parameter_schema.validate(parameterization.parameters)
        binding_seed = strict_hash({
            "schema": "stage2-invocation-binding-seed-v1",
            "capability_id": spec.capability_id,
            "capability_version": spec.capability_version,
            "binder": spec.binder.to_dict(),
            "implementation": spec.implementation.to_dict(),
            "parameters": strict_copy(parameters),
            "outputs": [value.to_dict() for value in spec.output_ports],
        })
        inputs = tuple(value.bind(binding_seed) for value in spec.input_ports)
        outputs = tuple(BoundOutputPort(value.port_id, value.descriptor)
                        for value in spec.output_ports)
        payload = _invocation_payload(
            spec.capability_id, spec.capability_version, spec.implementation,
            spec.binder, parameters, inputs, outputs)
        return cls(
            strict_hash(payload), spec.capability_id, spec.capability_version,
            spec.implementation, spec.binder, parameters, inputs, outputs,
            parameterization.metric_estimates, spec.evidence_profile_id,
            spec.cost_model_id, spec.execution_profile_id,
        )

    def expected_key(self) -> str:
        # Evidence assessments, cost estimates, and placement compatibility do
        # not change result bytes and therefore are deliberately excluded.
        return strict_hash(_invocation_payload(
            self.capability_id, self.capability_version,
            self.implementation, self.binder, self.parameters,
            self.input_uses, self.outputs))

    @property
    def record_id(self) -> str:
        """Identity of the full planning record, including metrics/profile."""
        return strict_hash({
            "schema": "stage2-bound-invocation-record-v1",
            **self.to_dict(),
        })

    def output(self, port_id: str) -> BoundOutputPort:
        try:
            return next(value for value in self.outputs
                        if value.port_id == port_id)
        except StopIteration as exc:
            raise KeyError(port_id) from exc

    def to_dict(self) -> dict[str, Any]:
        return {
            "invocation_key": self.invocation_key,
            "capability_id": self.capability_id,
            "capability_version": self.capability_version,
            "implementation": self.implementation.to_dict(),
            "binder": self.binder.to_dict(),
            "parameters": strict_copy(self.parameters),
            "input_uses": [value.to_dict() for value in self.input_uses],
            "outputs": [value.to_dict() for value in self.outputs],
            "metric_estimates": strict_copy(self.metric_estimates),
            "evidence_profile_id": self.evidence_profile_id,
            "cost_model_id": self.cost_model_id,
            "execution_profile_id": self.execution_profile_id,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "BoundInvocation":
        raw = require_object_fields(
            value, {field.name for field in dataclasses.fields(cls)},
            "BoundInvocation")
        raw["implementation"] = ImplementationRef.from_dict(raw["implementation"])
        raw["binder"] = BinderRef.from_dict(raw["binder"])
        for name, parser in (("input_uses", RequirementUse.from_dict),
                             ("outputs", BoundOutputPort.from_dict)):
            if not isinstance(raw[name], list):
                raise ValueError(f"BoundInvocation.{name} must be an array")
            raw[name] = tuple(parser(item) for item in raw[name])
        return cls(**raw)


def _invocation_payload(
        capability_id: str, capability_version: str,
        implementation: ImplementationRef, binder: BinderRef,
        parameters: dict[str, Any], input_uses: tuple[RequirementUse, ...],
        outputs: tuple[BoundOutputPort, ...],
) -> dict[str, Any]:
    return {
        "schema": "stage2-bound-invocation-v1",
        "capability_id": capability_id,
        "capability_version": capability_version,
        "implementation": implementation.to_dict(),
        "binder": binder.to_dict(),
        "parameters": strict_copy(parameters),
        "input_uses": [value.to_dict() for value in input_uses],
        "outputs": [value.to_dict() for value in outputs],
    }


@dataclass(frozen=True)
class ArtifactLeaf:
    """An immutable artifact declaration, not a storage commit attestation."""

    leaf_id: str
    artifact_id: str
    manifest_root_sha256: str
    descriptor: ArtifactDescriptor
    evidence_profile_id: str

    def __post_init__(self) -> None:
        _digest(self.leaf_id, "leaf_id")
        _digest(self.artifact_id, "artifact_id")
        _digest(self.manifest_root_sha256, "manifest_root_sha256")
        if not isinstance(self.descriptor, ArtifactDescriptor):
            raise TypeError("artifact leaf descriptor is invalid")
        _required_text(self.evidence_profile_id, "leaf evidence_profile_id")
        if self.leaf_id != self.expected_id():
            raise ValueError("artifact leaf identity does not verify")

    @classmethod
    def bind(cls, *, artifact_id: str, manifest_root_sha256: str,
             descriptor: ArtifactDescriptor,
             evidence_profile_id: str = "evidence:unknown") -> "ArtifactLeaf":
        payload = {
            "schema": "stage2-artifact-leaf-v1",
            "artifact_id": artifact_id,
            "manifest_root_sha256": manifest_root_sha256,
            "descriptor": descriptor.to_dict(),
        }
        return cls(strict_hash(payload), artifact_id, manifest_root_sha256,
                   descriptor, evidence_profile_id)

    def expected_id(self) -> str:
        return strict_hash({
            "schema": "stage2-artifact-leaf-v1",
            "artifact_id": self.artifact_id,
            "manifest_root_sha256": self.manifest_root_sha256,
            "descriptor": self.descriptor.to_dict(),
        })

    @property
    def record_id(self) -> str:
        """Planning record identity, including the frozen evidence assessment."""
        return strict_hash({
            "schema": "stage2-artifact-leaf-record-v1",
            "leaf_id": self.leaf_id,
            "evidence_profile_id": self.evidence_profile_id,
        })

    def to_dict(self) -> dict[str, Any]:
        return {
            "leaf_id": self.leaf_id,
            "artifact_id": self.artifact_id,
            "manifest_root_sha256": self.manifest_root_sha256,
            "descriptor": self.descriptor.to_dict(),
            "evidence_profile_id": self.evidence_profile_id,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ArtifactLeaf":
        raw = require_object_fields(
            value, {field.name for field in dataclasses.fields(cls)},
            "ArtifactLeaf")
        raw["descriptor"] = ArtifactDescriptor.from_dict(raw["descriptor"])
        return cls(**raw)


class BindingRejectionCode(str, Enum):
    UNKNOWN_OUTPUT_PORT = "UNKNOWN_OUTPUT_PORT"
    BINDER_STALE = "BINDER_STALE"
    IMPLEMENTATION_STALE = "IMPLEMENTATION_STALE"
    PROFILE_MISMATCH = "PROFILE_MISMATCH"
    EVIDENCE_PROFILE_MISMATCH = "EVIDENCE_PROFILE_MISMATCH"
    DIRECT_MATCH_REJECTED = "DIRECT_MATCH_REJECTED"
    NO_COMPATIBLE_DEPLOYMENT = "NO_COMPATIBLE_DEPLOYMENT"


@dataclass(frozen=True)
class BindingRejection:
    code: BindingRejectionCode
    capability_spec_id: str
    offered_output_port: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.code, BindingRejectionCode):
            raise TypeError("binding rejection code is invalid")
        _digest(self.capability_spec_id, "rejection capability_spec_id")
        _required_text(self.offered_output_port, "offered_output_port")
        _required_text(self.message, "binding rejection message")
        object.__setattr__(self, "details", freeze_json(self.details))
        if not isinstance(self.details, dict):
            raise ValueError("binding rejection details must be an object")

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code.value,
            "capability_spec_id": self.capability_spec_id,
            "offered_output_port": self.offered_output_port,
            "message": self.message,
            "details": strict_copy(self.details),
        }


@dataclass(frozen=True)
class BindingCandidate:
    offered_output_port: str
    invocation: BoundInvocation
    compatibility: CompatibilityProof

    def __post_init__(self) -> None:
        _port(self.offered_output_port, "offered_output_port")
        if not isinstance(self.invocation, BoundInvocation):
            raise TypeError("binding candidate invocation is invalid")
        if not isinstance(self.compatibility, CompatibilityProof):
            raise TypeError("binding compatibility proof is invalid")
        if not self.compatibility.satisfied:
            raise ValueError("an accepted binding needs a satisfied proof")
        output = self.invocation.output(self.offered_output_port)
        if self.compatibility.descriptor_id != output.descriptor.descriptor_id:
            raise ValueError("binding proof covers a different output descriptor")

    def to_dict(self) -> dict[str, Any]:
        return {
            "offered_output_port": self.offered_output_port,
            "invocation": self.invocation.to_dict(),
            "compatibility": self.compatibility.to_dict(),
        }


@dataclass(frozen=True)
class BindingEnumeration:
    capability_spec_id: str
    requirement_id: str
    accepted: tuple[BindingCandidate, ...]
    rejected: tuple[BindingRejection, ...]
    complete: bool = True

    def __post_init__(self) -> None:
        _digest(self.capability_spec_id, "enumeration capability_spec_id")
        _digest(self.requirement_id, "enumeration requirement_id")
        if (not isinstance(self.accepted, tuple)
                or not all(isinstance(value, BindingCandidate)
                           for value in self.accepted)):
            raise TypeError("accepted bindings must be an immutable typed tuple")
        if (not isinstance(self.rejected, tuple)
                or not all(isinstance(value, BindingRejection)
                           for value in self.rejected)):
            raise TypeError("binding rejections must be an immutable typed tuple")
        if type(self.complete) is not bool:
            raise TypeError("binding completeness must be bool")
        if tuple(sorted(self.accepted,
                        key=lambda value: value.invocation.invocation_key)) \
                != self.accepted:
            raise ValueError("accepted bindings must be canonically sorted")
        keys = [value.invocation.invocation_key for value in self.accepted]
        if len(keys) != len(set(keys)):
            raise ValueError("binding enumeration contains duplicate invocations")
        if any(value.compatibility.requirement_id != self.requirement_id
               for value in self.accepted):
            raise ValueError("binding proof covers another requirement")

    def to_dict(self) -> dict[str, Any]:
        return {
            "capability_spec_id": self.capability_spec_id,
            "requirement_id": self.requirement_id,
            "accepted": [value.to_dict() for value in self.accepted],
            "rejected": [value.to_dict() for value in self.rejected],
            "complete": self.complete,
        }
