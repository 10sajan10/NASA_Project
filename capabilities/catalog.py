"""Immutable multi-producer capability catalog and closed finite binding."""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from functools import cached_property
from types import MappingProxyType
from typing import Any, Iterable

from contracts import (
    EvidenceProfile,
    EvidenceSnapshot,
    EvidenceSubject,
    Requirement,
    direct_match,
)
from engine.runtime.identity import require_object_fields, strict_hash

from .deployment import (
    DeploymentCapabilitySnapshot,
    ExecutionProfile,
)
from .implementation import _digest
from .model import (
    BindingCandidate,
    BindingEnumeration,
    BindingRejection,
    BindingRejectionCode,
    BoundInvocation,
    CapabilitySpec,
    invocation_evidence_subject,
)


@dataclass(frozen=True)
class CapabilityCatalog:
    """Frozen alternatives indexed by output concept.

    Unlike the legacy producer registry, multiple capabilities may advertise
    the same concept.  The object has no mutation or callable-registration API.
    """

    catalog_id: str
    capabilities: tuple[CapabilitySpec, ...]
    execution_profiles: tuple[ExecutionProfile, ...]

    def __post_init__(self) -> None:
        _digest(self.catalog_id, "capability catalog_id")
        if (not isinstance(self.capabilities, tuple)
                or not all(isinstance(value, CapabilitySpec)
                           for value in self.capabilities)):
            raise TypeError("capabilities must be an immutable typed tuple")
        if (not isinstance(self.execution_profiles, tuple)
                or not all(isinstance(value, ExecutionProfile)
                           for value in self.execution_profiles)):
            raise TypeError("execution profiles must be an immutable typed tuple")
        if tuple(sorted(self.capabilities, key=lambda value: value.spec_id)) \
                != self.capabilities:
            raise ValueError("capabilities must be sorted by spec_id")
        if tuple(sorted(self.execution_profiles,
                        key=lambda value: value.profile_id)) \
                != self.execution_profiles:
            raise ValueError("execution profiles must be sorted by profile_id")
        if len({value.spec_id for value in self.capabilities}) \
                != len(self.capabilities):
            raise ValueError("capability catalog has duplicate specification IDs")
        if len({value.profile_id for value in self.execution_profiles}) \
                != len(self.execution_profiles):
            raise ValueError("capability catalog has duplicate execution profiles")
        profiles = {value.profile_id: value for value in self.execution_profiles}
        for spec in self.capabilities:
            profile = profiles.get(spec.execution_profile_id)
            if profile is None:
                raise ValueError(
                    f"capability {spec.spec_id} references an unknown execution profile")
            if profile.implementation != spec.implementation:
                raise ValueError(
                    "capability and execution profile result implementations disagree")
        # An invocation key is result identity.  Stage 3 v1 intentionally has
        # one planning record (cost/evidence/profile) per result identity; it
        # cannot silently choose between conflicting planning annotations.
        invocation_records: dict[str, str] = {}
        for spec in self.capabilities:
            for parameterization in spec.parameterizations:
                invocation = BoundInvocation.bind(spec, parameterization)
                previous = invocation_records.setdefault(
                    invocation.invocation_key, invocation.record_id)
                if previous != invocation.record_id:
                    raise ValueError(
                        "capability catalog assigns conflicting planning "
                        "records to one bound invocation identity")
        if self.catalog_id != self.expected_id():
            raise ValueError("capability catalog identity does not verify")

    @classmethod
    def freeze(cls, capabilities: Iterable[CapabilitySpec],
               execution_profiles: Iterable[ExecutionProfile],
               ) -> "CapabilityCatalog":
        specs = tuple(sorted(capabilities, key=lambda value: value.spec_id))
        profiles = tuple(sorted(
            execution_profiles, key=lambda value: value.profile_id))
        payload = _catalog_payload(specs, profiles)
        return cls(strict_hash(payload), specs, profiles)

    def expected_id(self) -> str:
        return strict_hash(_catalog_payload(
            self.capabilities, self.execution_profiles))

    def to_dict(self) -> dict[str, Any]:
        return {
            "catalog_id": self.catalog_id,
            "capabilities": [value.to_dict() for value in self.capabilities],
            "execution_profiles": [
                value.to_dict() for value in self.execution_profiles],
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "CapabilityCatalog":
        raw = require_object_fields(
            value, {field.name for field in dataclasses.fields(cls)},
            "CapabilityCatalog")
        for name, parser in (
            ("capabilities", CapabilitySpec.from_dict),
            ("execution_profiles", ExecutionProfile.from_dict),
        ):
            if not isinstance(raw[name], list):
                raise ValueError(f"CapabilityCatalog.{name} must be an array")
            raw[name] = tuple(parser(item) for item in raw[name])
        return cls(**raw)

    @cached_property
    def _spec_by_id(self):
        return MappingProxyType({
            value.spec_id: value for value in self.capabilities})

    @cached_property
    def _profile_by_id(self):
        return MappingProxyType({
            value.profile_id: value for value in self.execution_profiles})

    @cached_property
    def _specs_by_concept(self):
        index: dict[str, list[CapabilitySpec]] = {}
        for spec in self.capabilities:
            for concept_id in sorted({
                    output.descriptor.concept_id
                    for output in spec.output_ports}):
                index.setdefault(concept_id, []).append(spec)
        return MappingProxyType({
            key: tuple(values) for key, values in index.items()})

    def get(self, spec_id: str) -> CapabilitySpec:
        try:
            return self._spec_by_id[spec_id]
        except KeyError as exc:
            raise KeyError(spec_id) from exc

    def profile(self, profile_id: str) -> ExecutionProfile:
        try:
            return self._profile_by_id[profile_id]
        except KeyError as exc:
            raise KeyError(profile_id) from exc

    def lookup(self, concept_id: str) -> tuple[CapabilitySpec, ...]:
        return self._specs_by_concept.get(concept_id, ())

    def bind_candidates(
            self, spec_id: str, offered_output_port: str,
            requirement: Requirement, *,
            evidence_profile: EvidenceProfile | None = None,
            evidence_snapshot: EvidenceSnapshot | None = None,
            evidence_subject: EvidenceSubject | None = None,
            requested_regimes: tuple[str, ...] = (),
            deployment_snapshot: DeploymentCapabilitySnapshot | None = None,
    ) -> BindingEnumeration:
        """Bind one capability without recursion, ranking, or execution."""
        spec = self.get(spec_id)
        requirement_id = requirement.requirement_id
        output = next((value for value in spec.output_ports
                       if value.port_id == offered_output_port), None)
        if output is None:
            return _rejected(
                spec, requirement_id, offered_output_port,
                BindingRejectionCode.UNKNOWN_OUTPUT_PORT,
                f"capability has no output port {offered_output_port!r}",
                {"declared_ports": [value.port_id for value in spec.output_ports]},
            )

        try:
            spec.binder.verify_current()
        except (KeyError, ValueError) as exc:
            return _rejected(
                spec, requirement_id, offered_output_port,
                BindingRejectionCode.BINDER_STALE, str(exc), {})
        try:
            spec.implementation.verify_current()
        except (KeyError, ValueError) as exc:
            return _rejected(
                spec, requirement_id, offered_output_port,
                BindingRejectionCode.IMPLEMENTATION_STALE, str(exc), {})

        profile = self.profile(spec.execution_profile_id)
        if profile.implementation != spec.implementation:
            return _rejected(
                spec, requirement_id, offered_output_port,
                BindingRejectionCode.PROFILE_MISMATCH,
                "execution profile and capability implementations disagree", {})
        if (evidence_profile is not None
                and evidence_profile.profile_id != spec.evidence_profile_id):
            return _rejected(
                spec, requirement_id, offered_output_port,
                BindingRejectionCode.EVIDENCE_PROFILE_MISMATCH,
                "provided evidence profile is not the capability's frozen profile",
                {"expected": spec.evidence_profile_id,
                 "observed": evidence_profile.profile_id})
        if deployment_snapshot is not None:
            deployment = deployment_snapshot.check(profile)
            if not deployment.feasible:
                return _rejected(
                    spec, requirement_id, offered_output_port,
                    BindingRejectionCode.NO_COMPATIBLE_DEPLOYMENT,
                    "no frozen deployment site class satisfies the execution profile",
                    {"deployment_proof": deployment.to_dict()})

        proof = direct_match(
            output.descriptor, requirement)
        if evidence_profile is None:
            proof = direct_match(
                output.descriptor,
                requirement,
                None,
                requested_regimes=requested_regimes,
            )
            invocations = tuple(
                BoundInvocation.bind(spec, value)
                for value in spec.parameterizations)
            proofs = (proof,) * len(invocations)
        else:
            if evidence_snapshot is None:
                return _rejected(
                    spec, requirement_id, offered_output_port,
                    BindingRejectionCode.EVIDENCE_PROFILE_MISMATCH,
                    "empirical evidence requires a frozen EvidenceSnapshot", {})
            invocations = tuple(
                BoundInvocation.bind(spec, value)
                for value in spec.parameterizations)
            derived_subjects = tuple(
                invocation_evidence_subject(value, offered_output_port)
                for value in invocations)
            if evidence_subject is not None and any(
                    value != evidence_subject for value in derived_subjects):
                return _rejected(
                    spec, requirement_id, offered_output_port,
                    BindingRejectionCode.EVIDENCE_PROFILE_MISMATCH,
                    "caller evidence subject differs from the derived invocation subject",
                    {})
            proofs = tuple(direct_match(
                output.descriptor,
                requirement,
                evidence_profile,
                evidence_snapshot=evidence_snapshot,
                evidence_subject=subject,
                requested_regimes=requested_regimes,
            ) for subject in derived_subjects)
        accepted_values: list[BindingCandidate] = []
        rejected_values: list[BindingRejection] = []
        for invocation, candidate_proof in zip(invocations, proofs):
            if candidate_proof.satisfied:
                accepted_values.append(BindingCandidate(
                    offered_output_port, invocation, candidate_proof))
                continue
            rejected_values.append(BindingRejection(
                BindingRejectionCode.DIRECT_MATCH_REJECTED,
                spec.spec_id,
                offered_output_port,
                "one bound parameterization does not directly satisfy the "
                "requirement",
                {
                    "invocation_key": invocation.invocation_key,
                    "invocation_record_id": invocation.record_id,
                    "parameters": invocation.parameters,
                    "compatibility_proof": candidate_proof.to_dict(),
                    "rejection_codes": [
                        getattr(code, "value", str(code))
                        for code in candidate_proof.rejection_codes],
                },
            ))

        accepted = tuple(sorted(
            accepted_values,
            key=lambda value: value.invocation.invocation_key))
        rejected = tuple(sorted(
            rejected_values,
            key=lambda value: strict_hash(value.to_dict())))
        return BindingEnumeration(
            spec.spec_id, requirement_id, accepted, rejected, True)


def _catalog_payload(
        capabilities: tuple[CapabilitySpec, ...],
        execution_profiles: tuple[ExecutionProfile, ...],
) -> dict[str, Any]:
    return {
        "schema": "stage2-capability-catalog-v1",
        "capabilities": [value.to_dict() for value in capabilities],
        "execution_profiles": [value.to_dict() for value in execution_profiles],
    }


def _rejected(
        spec: CapabilitySpec, requirement_id: str, output_port: str,
        code: BindingRejectionCode, message: str, details: dict[str, Any],
) -> BindingEnumeration:
    return BindingEnumeration(
        spec.spec_id,
        requirement_id,
        (),
        (BindingRejection(code, spec.spec_id, output_port, message, details),),
        True,
    )
