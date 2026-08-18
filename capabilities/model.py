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
    EvidenceSubject,
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


def _is_reserved_transformation_operation(operation_key: str) -> bool:
    """Return whether an operation belongs to the semantic-transform namespace.

    ``transform.*`` is deliberately reserved.  Those operations make a
    scientific claim (units, grids, value semantics, and assumptions), so a
    generic capability declaration is not sufficient authority to use one.
    """
    return operation_key.startswith("transform.")


_ACQUISITION_OPERATION_KEY = "acquisition.materialize.v1"
_ACQUISITION_BINDER_KEY = "acquisition.materialize.bind.v1"
_ACQUISITION_AUTHORITY_MINT = object()


def _is_reserved_acquisition(
        operation_key: str, binder_key: str,
) -> bool:
    """Return whether either executable identity names acquisition lowering.

    Both sides are checked so a future binder-registry mistake cannot turn the
    reserved materializer into an ordinary caller-authored producer.
    """
    return (operation_key == _ACQUISITION_OPERATION_KEY
            or binder_key == _ACQUISITION_BINDER_KEY)


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
class TransformationAuthority:
    """Content-addressed authority carried by every lowered transform.

    The complete immutable :class:`transformations.TransformationSpec` record
    is retained instead of projecting only its executable parameters.  The
    two additional digests bind that record to the closed semantic validator
    version and to the exact parameter/descriptors rule.  Reconstruction and
    scientific replay live in :mod:`transformations.model`; this low-level
    carrier intentionally has no dependency on the Stage-4 package.
    """

    authority_id: str
    transformation_spec: dict[str, Any]
    semantic_rule_implementation_sha256: str
    parameter_rule_sha256: str

    def __post_init__(self) -> None:
        _digest(self.authority_id, "transformation authority_id")
        _digest(
            self.semantic_rule_implementation_sha256,
            "semantic rule implementation digest",
        )
        _digest(self.parameter_rule_sha256, "transformation parameter rule digest")
        object.__setattr__(
            self, "transformation_spec", freeze_json(self.transformation_spec))
        if not isinstance(self.transformation_spec, dict):
            raise ValueError("transformation authority spec must be a JSON object")
        _digest(
            self.transformation_spec.get("spec_id"),
            "authority transformation spec_id",
        )
        _required_text(
            self.transformation_spec.get("kind"),
            "authority transformation kind",
        )
        _required_text(
            self.transformation_spec.get("semantic_rule_id"),
            "authority semantic_rule_id",
        )
        _required_text(
            self.transformation_spec.get("loss_policy"),
            "authority transformation loss_policy",
        )
        _required_text(
            self.transformation_spec.get("uncertainty_propagation_policy"),
            "authority transformation uncertainty_propagation_policy",
        )
        if self.authority_id != self.expected_id():
            raise ValueError("transformation authority identity does not verify")

    @classmethod
    def bind(
            cls, *, transformation_spec: dict[str, Any],
            semantic_rule_implementation_sha256: str,
            parameter_rule_sha256: str,
    ) -> "TransformationAuthority":
        values = {
            "transformation_spec": strict_copy(transformation_spec),
            "semantic_rule_implementation_sha256":
                semantic_rule_implementation_sha256,
            "parameter_rule_sha256": parameter_rule_sha256,
        }
        return cls(strict_hash(cls._identity_payload(values)), **values)

    @staticmethod
    def _identity_payload(values: dict[str, Any]) -> dict[str, Any]:
        return {
            "schema": "stage8r-transformation-authority-v1",
            **values,
        }

    @property
    def transformation_spec_id(self) -> str:
        return self.transformation_spec["spec_id"]

    @property
    def kind(self) -> str:
        return self.transformation_spec["kind"]

    @property
    def semantic_rule_id(self) -> str:
        return self.transformation_spec["semantic_rule_id"]

    @property
    def loss_policy(self) -> str:
        return self.transformation_spec["loss_policy"]

    @property
    def uncertainty_propagation_policy(self) -> str:
        return self.transformation_spec["uncertainty_propagation_policy"]

    def expected_id(self) -> str:
        return strict_hash(self._identity_payload({
            "transformation_spec": strict_copy(self.transformation_spec),
            "semantic_rule_implementation_sha256":
                self.semantic_rule_implementation_sha256,
            "parameter_rule_sha256": self.parameter_rule_sha256,
        }))

    def to_dict(self) -> dict[str, Any]:
        return {
            "authority_id": self.authority_id,
            "transformation_spec": strict_copy(self.transformation_spec),
            "semantic_rule_implementation_sha256":
                self.semantic_rule_implementation_sha256,
            "parameter_rule_sha256": self.parameter_rule_sha256,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "TransformationAuthority":
        raw = require_object_fields(
            value, {field.name for field in dataclasses.fields(cls)},
            "TransformationAuthority")
        return cls(**raw)


@dataclass(frozen=True, init=False)
class AcquisitionAuthority:
    """Content-addressed certificate for one verified acquisition lowering.

    The carrier deliberately lives below :mod:`acquisition` so generic
    capability records can retain it without introducing a module cycle.  A
    fresh instance is minted only from a live ``FetchedContentBinding`` by the
    acquisition lowerer.  Construction and later deserialization are both
    replayed by :mod:`acquisition.lowering`; this record is not a replacement
    for receipt/blob verification at the storage boundary.
    """

    authority_id: str
    content_binding: dict[str, Any]
    source_schema_id: str
    descriptor_id: str
    source_schema: dict[str, Any]
    bound_manifest: dict[str, Any]
    manifest_assets: tuple[dict[str, Any], ...]
    coverage_contract_id: str
    execution_profile: dict[str, Any]
    capability_version: str
    cost_units: int
    evidence_profile_id: str
    lowering_rule_implementation_sha256: str

    def __init__(
            self, mint: object, *, authority_id: str,
            content_binding: dict[str, Any], source_schema_id: str,
            descriptor_id: str, source_schema: dict[str, Any],
            bound_manifest: dict[str, Any],
            manifest_assets: tuple[dict[str, Any], ...] | list[dict[str, Any]],
            coverage_contract_id: str,
            execution_profile: dict[str, Any],
            capability_version: str, cost_units: int,
            evidence_profile_id: str,
            lowering_rule_implementation_sha256: str,
    ) -> None:
        if mint is not _ACQUISITION_AUTHORITY_MINT:
            raise PermissionError(
                "AcquisitionAuthority is minted by verified acquisition "
                "lowering or reconstructed by strict deserialization")
        _digest(authority_id, "acquisition authority_id")
        _digest(source_schema_id, "acquisition source_schema_id")
        _digest(descriptor_id, "acquisition descriptor_id")
        _digest(
            coverage_contract_id, "acquisition coverage_contract_id")
        _digest(
            lowering_rule_implementation_sha256,
            "acquisition lowering-rule implementation digest",
        )
        _required_text(capability_version, "acquisition capability_version")
        _required_text(evidence_profile_id, "acquisition evidence_profile_id")
        if (isinstance(cost_units, bool) or not isinstance(cost_units, int)
                or cost_units < 0):
            raise ValueError(
                "acquisition authority cost_units must be a non-negative integer")
        frozen_binding = freeze_json(content_binding)
        frozen_schema = freeze_json(source_schema)
        frozen_manifest = freeze_json(bound_manifest)
        frozen_assets = freeze_json(manifest_assets)
        frozen_profile = freeze_json(execution_profile)
        if not isinstance(frozen_binding, dict):
            raise TypeError("acquisition authority content binding must be an object")
        if not isinstance(frozen_profile, dict):
            raise TypeError("acquisition authority execution profile must be an object")
        if not isinstance(frozen_schema, dict):
            raise TypeError("acquisition authority source schema must be an object")
        if not isinstance(frozen_manifest, dict):
            raise TypeError("acquisition authority bound manifest must be an object")
        if (not isinstance(frozen_assets, tuple) or not frozen_assets
                or not all(isinstance(item, dict) for item in frozen_assets)):
            raise TypeError(
                "acquisition authority manifest assets must be a non-empty array")
        values = {
            "authority_id": authority_id,
            "content_binding": frozen_binding,
            "source_schema_id": source_schema_id,
            "descriptor_id": descriptor_id,
            "source_schema": frozen_schema,
            "bound_manifest": frozen_manifest,
            "manifest_assets": frozen_assets,
            "coverage_contract_id": coverage_contract_id,
            "execution_profile": frozen_profile,
            "capability_version": capability_version,
            "cost_units": cost_units,
            "evidence_profile_id": evidence_profile_id,
            "lowering_rule_implementation_sha256":
                lowering_rule_implementation_sha256,
        }
        for name, value in values.items():
            object.__setattr__(self, name, value)
        if self.authority_id != self.expected_id():
            raise ValueError("acquisition authority identity does not verify")

    @classmethod
    def _bind_verified_content(
            cls, content: Any, execution_profile: Any, *,
            capability_version: str, cost_units: int,
            evidence_profile_id: str,
            lowering_rule_implementation_sha256: str,
    ) -> "AcquisitionAuthority":
        """Internal mint used only after Stage-5 receipt/blob verification."""
        # Local imports avoid making the generic Stage-2 model load Stage 5.
        from acquisition.content import FetchedContentBinding
        from .deployment import ExecutionProfile

        if not isinstance(content, FetchedContentBinding):
            raise TypeError(
                "acquisition authority requires a verified FetchedContentBinding")
        if not isinstance(execution_profile, ExecutionProfile):
            raise TypeError("acquisition authority requires an ExecutionProfile")
        values = {
            "content_binding": content.to_dict(),
            "source_schema_id": content.source_schema_id,
            "descriptor_id": content.descriptor.descriptor_id,
            "source_schema": content.bound_manifest.source_schema.to_dict(),
            "bound_manifest": content.bound_manifest.to_dict(),
            "manifest_assets": tuple(
                item.to_dict() for item in content.manifest_assets),
            "coverage_contract_id":
                content.bound_manifest.coverage_contract_id,
            "execution_profile": execution_profile.to_dict(),
            "capability_version": capability_version,
            "cost_units": cost_units,
            "evidence_profile_id": evidence_profile_id,
            "lowering_rule_implementation_sha256":
                lowering_rule_implementation_sha256,
        }
        authority_id = strict_hash(cls._identity_payload(values))
        return cls(
            _ACQUISITION_AUTHORITY_MINT,
            authority_id=authority_id,
            **values,
        )

    @staticmethod
    def _identity_payload(values: dict[str, Any]) -> dict[str, Any]:
        return {"schema": "stage8r-acquisition-authority-v1", **values}

    def expected_id(self) -> str:
        return strict_hash(self._identity_payload({
            "content_binding": strict_copy(self.content_binding),
            "source_schema_id": self.source_schema_id,
            "descriptor_id": self.descriptor_id,
            "source_schema": strict_copy(self.source_schema),
            "bound_manifest": strict_copy(self.bound_manifest),
            "manifest_assets": strict_copy(self.manifest_assets),
            "coverage_contract_id": self.coverage_contract_id,
            "execution_profile": strict_copy(self.execution_profile),
            "capability_version": self.capability_version,
            "cost_units": self.cost_units,
            "evidence_profile_id": self.evidence_profile_id,
            "lowering_rule_implementation_sha256":
                self.lowering_rule_implementation_sha256,
        }))

    def to_dict(self) -> dict[str, Any]:
        return {
            "authority_id": self.authority_id,
            "content_binding": strict_copy(self.content_binding),
            "source_schema_id": self.source_schema_id,
            "descriptor_id": self.descriptor_id,
            "source_schema": strict_copy(self.source_schema),
            "bound_manifest": strict_copy(self.bound_manifest),
            "manifest_assets": strict_copy(self.manifest_assets),
            "coverage_contract_id": self.coverage_contract_id,
            "execution_profile": strict_copy(self.execution_profile),
            "capability_version": self.capability_version,
            "cost_units": self.cost_units,
            "evidence_profile_id": self.evidence_profile_id,
            "lowering_rule_implementation_sha256":
                self.lowering_rule_implementation_sha256,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "AcquisitionAuthority":
        raw = require_object_fields(
            value, {field.name for field in dataclasses.fields(cls)},
            "AcquisitionAuthority")
        return cls(_ACQUISITION_AUTHORITY_MINT, **raw)


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
    transformation_authority: TransformationAuthority | None = None
    acquisition_authority: AcquisitionAuthority | None = None

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
        if (self.transformation_authority is not None
                and not isinstance(
                    self.transformation_authority, TransformationAuthority)):
            raise TypeError("capability transformation authority is invalid")
        if (self.acquisition_authority is not None
                and not isinstance(
                    self.acquisition_authority, AcquisitionAuthority)):
            raise TypeError("capability acquisition authority is invalid")
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
        reserved_transform = _is_reserved_transformation_operation(
            self.implementation.operation_key)
        if reserved_transform and self.transformation_authority is None:
            raise ValueError(
                "reserved transform.* operations require authenticated "
                "TransformationAuthority")
        if not reserved_transform and self.transformation_authority is not None:
            raise ValueError(
                "transformation authority cannot authorize a non-transform operation")
        if reserved_transform:
            # Local import avoids making the generic Stage-2 carrier import
            # Stage 4 at module-load time.  Construction nevertheless fails
            # closed unless the complete scientific contract replays.
            from transformations.model import (
                verify_capability_transformation_authority,
            )
            verify_capability_transformation_authority(self)
        reserved_acquisition = _is_reserved_acquisition(
            self.implementation.operation_key, self.binder.binder_key)
        if reserved_acquisition and self.acquisition_authority is None:
            raise ValueError(
                "reserved acquisition materializer requires authenticated "
                "AcquisitionAuthority")
        if not reserved_acquisition and self.acquisition_authority is not None:
            raise ValueError(
                "acquisition authority cannot authorize a non-acquisition operation")
        if reserved_acquisition:
            from acquisition.lowering import (
                verify_capability_acquisition_authority,
            )
            verify_capability_acquisition_authority(self)
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
            transformation_authority: TransformationAuthority | None = None,
            acquisition_authority: AcquisitionAuthority | None = None,
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
            evidence_profile_id, cost_model_id, execution_profile_id,
            transformation_authority, acquisition_authority)
        return cls(
            strict_hash(payload), capability_id, capability_version,
            implementation, binder, inputs, outputs, parameter_schema, params,
            applicability_key, evidence_profile_id, cost_model_id,
            execution_profile_id, transformation_authority,
            acquisition_authority,
        )

    def expected_id(self) -> str:
        return strict_hash(_capability_payload(
            self.capability_id, self.capability_version,
            self.implementation, self.binder, self.input_ports,
            self.output_ports, self.parameter_schema, self.parameterizations,
            self.applicability_key, self.evidence_profile_id,
            self.cost_model_id, self.execution_profile_id,
            self.transformation_authority, self.acquisition_authority))

    def to_dict(self) -> dict[str, Any]:
        result = {
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
        if self.transformation_authority is not None:
            result["transformation_authority"] = (
                self.transformation_authority.to_dict())
        if self.acquisition_authority is not None:
            result["acquisition_authority"] = (
                self.acquisition_authority.to_dict())
        return result

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "CapabilitySpec":
        expected = {field.name for field in dataclasses.fields(cls)}
        for optional in (
                "transformation_authority", "acquisition_authority"):
            if optional not in value:
                expected.remove(optional)
        raw = require_object_fields(value, expected, "CapabilitySpec")
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
        if "transformation_authority" in raw:
            raw["transformation_authority"] = TransformationAuthority.from_dict(
                raw["transformation_authority"])
        if "acquisition_authority" in raw:
            raw["acquisition_authority"] = AcquisitionAuthority.from_dict(
                raw["acquisition_authority"])
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
        transformation_authority: TransformationAuthority | None = None,
        acquisition_authority: AcquisitionAuthority | None = None,
) -> dict[str, Any]:
    result = {
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
    if transformation_authority is not None:
        result["transformation_authority"] = transformation_authority.to_dict()
    if acquisition_authority is not None:
        result["acquisition_authority"] = acquisition_authority.to_dict()
    return result


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
    transformation_authority: TransformationAuthority | None = None
    acquisition_authority: AcquisitionAuthority | None = None

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
        if (self.transformation_authority is not None
                and not isinstance(
                    self.transformation_authority, TransformationAuthority)):
            raise TypeError("bound transformation authority is invalid")
        if (self.acquisition_authority is not None
                and not isinstance(
                    self.acquisition_authority, AcquisitionAuthority)):
            raise TypeError("bound acquisition authority is invalid")
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
        reserved_transform = _is_reserved_transformation_operation(
            self.implementation.operation_key)
        if reserved_transform and self.transformation_authority is None:
            raise ValueError(
                "reserved transform.* invocation lacks authenticated "
                "TransformationAuthority")
        if not reserved_transform and self.transformation_authority is not None:
            raise ValueError(
                "transformation authority cannot authorize a non-transform invocation")
        if reserved_transform:
            from transformations.model import (
                verify_bound_transformation_authority,
            )
            verify_bound_transformation_authority(self)
        reserved_acquisition = _is_reserved_acquisition(
            self.implementation.operation_key, self.binder.binder_key)
        if reserved_acquisition and self.acquisition_authority is None:
            raise ValueError(
                "reserved acquisition invocation lacks authenticated "
                "AcquisitionAuthority")
        if not reserved_acquisition and self.acquisition_authority is not None:
            raise ValueError(
                "acquisition authority cannot authorize a non-acquisition invocation")
        if reserved_acquisition:
            from acquisition.lowering import verify_bound_acquisition_authority
            verify_bound_acquisition_authority(self)
        if self.invocation_key != self.expected_key():
            raise ValueError("bound invocation identity does not verify")

    @classmethod
    def bind(cls, spec: CapabilitySpec,
             parameterization: BindingParameterization) -> "BoundInvocation":
        spec.implementation.verify_current()
        spec.binder.verify_current()
        parameters = spec.parameter_schema.validate(parameterization.parameters)
        binding_seed = strict_hash({
            "schema": "stage2-invocation-binding-seed-v2",
            "capability_id": spec.capability_id,
            "capability_version": spec.capability_version,
            "binder": spec.binder.to_dict(),
            "implementation": spec.implementation.to_dict(),
            "parameters": strict_copy(parameters),
            # RequirementUse identities must be local to the complete bound
            # invocation contract.  Omitting sibling input templates allowed
            # two distinct invocations to mint the same unchanged-port use ID.
            "inputs": [value.to_dict() for value in spec.input_ports],
            "outputs": [value.to_dict() for value in spec.output_ports],
            **({"transformation_authority":
                spec.transformation_authority.to_dict()}
               if spec.transformation_authority is not None else {}),
            **({"acquisition_authority":
                spec.acquisition_authority.to_dict()}
               if spec.acquisition_authority is not None else {}),
        })
        inputs = tuple(value.bind(binding_seed) for value in spec.input_ports)
        outputs = tuple(BoundOutputPort(value.port_id, value.descriptor)
                        for value in spec.output_ports)
        payload = _invocation_payload(
            spec.capability_id, spec.capability_version, spec.implementation,
            spec.binder, parameters, inputs, outputs,
            spec.transformation_authority, spec.acquisition_authority)
        return cls(
            strict_hash(payload), spec.capability_id, spec.capability_version,
            spec.implementation, spec.binder, parameters, inputs, outputs,
            parameterization.metric_estimates, spec.evidence_profile_id,
            spec.cost_model_id, spec.execution_profile_id,
            spec.transformation_authority, spec.acquisition_authority,
        )

    def expected_key(self) -> str:
        # Evidence assessments, cost estimates, and placement compatibility do
        # not change result bytes and therefore are deliberately excluded.
        return strict_hash(_invocation_payload(
            self.capability_id, self.capability_version,
            self.implementation, self.binder, self.parameters,
            self.input_uses, self.outputs, self.transformation_authority,
            self.acquisition_authority))

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
        result = {
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
        if self.transformation_authority is not None:
            result["transformation_authority"] = (
                self.transformation_authority.to_dict())
        if self.acquisition_authority is not None:
            result["acquisition_authority"] = (
                self.acquisition_authority.to_dict())
        return result

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "BoundInvocation":
        expected = {field.name for field in dataclasses.fields(cls)}
        for optional in (
                "transformation_authority", "acquisition_authority"):
            if optional not in value:
                expected.remove(optional)
        raw = require_object_fields(value, expected, "BoundInvocation")
        raw["implementation"] = ImplementationRef.from_dict(raw["implementation"])
        raw["binder"] = BinderRef.from_dict(raw["binder"])
        for name, parser in (("input_uses", RequirementUse.from_dict),
                             ("outputs", BoundOutputPort.from_dict)):
            if not isinstance(raw[name], list):
                raise ValueError(f"BoundInvocation.{name} must be an array")
            raw[name] = tuple(parser(item) for item in raw[name])
        if "transformation_authority" in raw:
            raw["transformation_authority"] = TransformationAuthority.from_dict(
                raw["transformation_authority"])
        if "acquisition_authority" in raw:
            raw["acquisition_authority"] = AcquisitionAuthority.from_dict(
                raw["acquisition_authority"])
        return cls(**raw)


def _invocation_payload(
        capability_id: str, capability_version: str,
        implementation: ImplementationRef, binder: BinderRef,
        parameters: dict[str, Any], input_uses: tuple[RequirementUse, ...],
        outputs: tuple[BoundOutputPort, ...],
        transformation_authority: TransformationAuthority | None = None,
        acquisition_authority: AcquisitionAuthority | None = None,
) -> dict[str, Any]:
    result = {
        "schema": "stage2-bound-invocation-v1",
        "capability_id": capability_id,
        "capability_version": capability_version,
        "implementation": implementation.to_dict(),
        "binder": binder.to_dict(),
        "parameters": strict_copy(parameters),
        "input_uses": [value.to_dict() for value in input_uses],
        "outputs": [value.to_dict() for value in outputs],
    }
    if transformation_authority is not None:
        result["transformation_authority"] = transformation_authority.to_dict()
    if acquisition_authority is not None:
        result["acquisition_authority"] = acquisition_authority.to_dict()
    return result


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


def invocation_evidence_subject(
        invocation: BoundInvocation, output_port_id: str,
) -> EvidenceSubject:
    """Derive the only evidence subject valid for an invocation output.

    Evidence identity is never accepted from a catalog caller.  It is derived
    from the result-affecting implementation/configuration and the exact bound
    parameters, so evidence for one parameterization cannot authorize another.
    """
    if not isinstance(invocation, BoundInvocation):
        raise TypeError("invocation must be BoundInvocation")
    invocation.output(output_port_id)
    return EvidenceSubject(
        component_id=invocation.implementation.component_id,
        component_version=invocation.implementation.component_version,
        configuration_id=strict_hash({
            "implementation_configuration":
                invocation.implementation.configuration_sha256,
            "parameters": invocation.parameters,
        }),
        output_port_id=output_port_id,
    )


def artifact_evidence_subject(
        leaf: ArtifactLeaf, output_port_id: str = "artifact",
) -> EvidenceSubject:
    """Bind evidence to one exact immutable artifact realization.

    An artifact leaf does not expose its historical producer implementation.
    Its admissible evidence subject is therefore the exact manifest-backed
    realization (artifact, manifest root, and descriptor), not an arbitrary
    subject supplied by an evidence profile.
    """
    if not isinstance(leaf, ArtifactLeaf):
        raise TypeError("leaf must be ArtifactLeaf")
    _port(output_port_id, "artifact output_port_id")
    return EvidenceSubject(
        component_id="artifact-manifest",
        component_version="stage2-artifact-leaf-v1",
        configuration_id=strict_hash({
            "schema": "stage2-artifact-evidence-subject-v1",
            "artifact_id": leaf.artifact_id,
            "manifest_root_sha256": leaf.manifest_root_sha256,
            "descriptor_id": leaf.descriptor.descriptor_id,
        }),
        output_port_id=output_port_id,
    )


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
