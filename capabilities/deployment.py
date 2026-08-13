"""Frozen execution requirements and static deployment feasibility.

The snapshot records durable site *classes*, not live queue state or guesses
about currently free resources.  Exact placement remains a runtime decision.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Iterable

from engine.runtime.identity import require_object_fields, strict_hash

from .implementation import ImplementationRef, _digest, _required_text


def _canonical_strings(values: Iterable[str], label: str, *,
                       allow_empty: bool = False) -> tuple[str, ...]:
    if not isinstance(values, tuple):
        raise TypeError(f"{label} must be an immutable tuple")
    result = tuple(values)
    if any(not isinstance(value, str) or not value.strip() for value in result):
        raise ValueError(f"{label} must contain non-empty strings")
    if len(result) != len(set(result)):
        raise ValueError(f"{label} cannot contain duplicates")
    if result != tuple(sorted(result)):
        raise ValueError(f"{label} must be canonically sorted")
    if not result and not allow_empty:
        raise ValueError(f"{label} cannot be empty")
    return result


def _positive_int(value: int, label: str, *, zero_ok: bool = False) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{label} must be an integer")
    if value < (0 if zero_ok else 1):
        qualifier = "non-negative" if zero_ok else "positive"
        raise ValueError(f"{label} must be {qualifier}")


@dataclass(frozen=True)
class ResourceEnvelope:
    min_cpu_cores: int = 1
    min_memory_mb: int = 64
    min_gpus: int = 0
    max_cpu_cores: int = 1
    max_memory_mb: int = 1024
    max_gpus: int = 0

    def __post_init__(self) -> None:
        _positive_int(self.min_cpu_cores, "min_cpu_cores")
        _positive_int(self.min_memory_mb, "min_memory_mb")
        _positive_int(self.min_gpus, "min_gpus", zero_ok=True)
        _positive_int(self.max_cpu_cores, "max_cpu_cores")
        _positive_int(self.max_memory_mb, "max_memory_mb")
        _positive_int(self.max_gpus, "max_gpus", zero_ok=True)
        if (self.min_cpu_cores > self.max_cpu_cores
                or self.min_memory_mb > self.max_memory_mb
                or self.min_gpus > self.max_gpus):
            raise ValueError("resource minima cannot exceed maxima")

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ResourceEnvelope":
        raw = require_object_fields(
            value, {field.name for field in dataclasses.fields(cls)},
            "ResourceEnvelope")
        return cls(**raw)


@dataclass(frozen=True)
class PlacementRequirements:
    architectures: tuple[str, ...]
    provider_kinds: tuple[str, ...]
    environment_classes: tuple[str, ...] = ()
    network_classes: tuple[str, ...] = ("none",)
    credential_classes: tuple[str, ...] = ()
    mount_classes: tuple[str, ...] = ()
    policy_classes: tuple[str, ...] = ()
    allowed_site_classes: tuple[str, ...] = ()
    resources: ResourceEnvelope = ResourceEnvelope()

    def __post_init__(self) -> None:
        for field_name, allow_empty in (
            ("architectures", False), ("provider_kinds", False),
            ("environment_classes", True),
            ("network_classes", False), ("credential_classes", True),
            ("mount_classes", True), ("policy_classes", True),
            ("allowed_site_classes", True),
        ):
            _canonical_strings(
                getattr(self, field_name), field_name, allow_empty=allow_empty)
        if not isinstance(self.resources, ResourceEnvelope):
            raise TypeError("resources must be a ResourceEnvelope")

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "PlacementRequirements":
        raw = require_object_fields(
            value, {field.name for field in dataclasses.fields(cls)},
            "PlacementRequirements")
        for name in (
            "architectures", "provider_kinds", "environment_classes",
            "network_classes", "credential_classes", "mount_classes",
            "policy_classes", "allowed_site_classes",
        ):
            if not isinstance(raw[name], list):
                raise ValueError(f"PlacementRequirements.{name} must be an array")
            raw[name] = tuple(raw[name])
        raw["resources"] = ResourceEnvelope.from_dict(raw["resources"])
        return cls(**raw)


@dataclass(frozen=True)
class ExecutionProfile:
    """Result implementation plus hard placement compatibility requirements."""

    profile_id: str
    implementation: ImplementationRef
    placement: PlacementRequirements

    def __post_init__(self) -> None:
        _digest(self.profile_id, "profile_id")
        if not isinstance(self.implementation, ImplementationRef):
            raise TypeError("implementation must be an ImplementationRef")
        if not isinstance(self.placement, PlacementRequirements):
            raise TypeError("placement must be PlacementRequirements")
        if self.profile_id != self.expected_id():
            raise ValueError("execution profile identity does not verify")

    @classmethod
    def bind(cls, implementation: ImplementationRef,
             placement: PlacementRequirements) -> "ExecutionProfile":
        payload = {
            "schema": "stage2-execution-profile-v1",
            "implementation": implementation.to_dict(),
            "placement": placement.to_dict(),
        }
        return cls(strict_hash(payload), implementation, placement)

    def expected_id(self) -> str:
        return strict_hash({
            "schema": "stage2-execution-profile-v1",
            "implementation": self.implementation.to_dict(),
            "placement": self.placement.to_dict(),
        })

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ExecutionProfile":
        raw = require_object_fields(
            value, {field.name for field in dataclasses.fields(cls)},
            "ExecutionProfile")
        raw["implementation"] = ImplementationRef.from_dict(
            raw["implementation"])
        raw["placement"] = PlacementRequirements.from_dict(raw["placement"])
        return cls(**raw)


@dataclass(frozen=True)
class SiteClassCapability:
    site_class_id: str
    architecture: str
    provider_kinds: tuple[str, ...]
    implementation_digests: tuple[str, ...]
    environment_classes: tuple[str, ...]
    network_classes: tuple[str, ...]
    credential_classes: tuple[str, ...]
    mount_classes: tuple[str, ...]
    policy_classes: tuple[str, ...]
    max_cpu_cores: int
    max_memory_mb: int
    max_gpus: int = 0

    def __post_init__(self) -> None:
        _required_text(self.site_class_id, "site_class_id")
        _required_text(self.architecture, "architecture")
        for field_name, allow_empty in (
            ("provider_kinds", False),
            ("implementation_digests", False),
            ("environment_classes", True),
            ("network_classes", False),
            ("credential_classes", True),
            ("mount_classes", True),
            ("policy_classes", True),
        ):
            values = _canonical_strings(
                getattr(self, field_name), field_name, allow_empty=allow_empty)
            if field_name == "implementation_digests":
                for value in values:
                    _digest(value, "implementation digest")
        _positive_int(self.max_cpu_cores, "max_cpu_cores")
        _positive_int(self.max_memory_mb, "max_memory_mb")
        _positive_int(self.max_gpus, "max_gpus", zero_ok=True)

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "SiteClassCapability":
        raw = require_object_fields(
            value, {field.name for field in dataclasses.fields(cls)},
            "SiteClassCapability")
        for name in (
            "provider_kinds", "implementation_digests", "environment_classes",
            "network_classes", "credential_classes", "mount_classes",
            "policy_classes",
        ):
            if not isinstance(raw[name], list):
                raise ValueError(f"SiteClassCapability.{name} must be an array")
            raw[name] = tuple(raw[name])
        return cls(**raw)


class DeploymentRejectionCode(str, Enum):
    SITE_CLASS_DISALLOWED = "SITE_CLASS_DISALLOWED"
    ARCHITECTURE_UNSUPPORTED = "ARCHITECTURE_UNSUPPORTED"
    PROVIDER_UNSUPPORTED = "PROVIDER_UNSUPPORTED"
    IMPLEMENTATION_UNAVAILABLE = "IMPLEMENTATION_UNAVAILABLE"
    ENVIRONMENT_UNAVAILABLE = "ENVIRONMENT_UNAVAILABLE"
    NETWORK_UNAVAILABLE = "NETWORK_UNAVAILABLE"
    CREDENTIAL_UNAVAILABLE = "CREDENTIAL_UNAVAILABLE"
    MOUNT_UNAVAILABLE = "MOUNT_UNAVAILABLE"
    POLICY_DISALLOWED = "POLICY_DISALLOWED"
    CPU_ENVELOPE_UNAVAILABLE = "CPU_ENVELOPE_UNAVAILABLE"
    MEMORY_ENVELOPE_UNAVAILABLE = "MEMORY_ENVELOPE_UNAVAILABLE"
    GPU_ENVELOPE_UNAVAILABLE = "GPU_ENVELOPE_UNAVAILABLE"


@dataclass(frozen=True)
class DeploymentRejection:
    code: DeploymentRejectionCode
    field: str
    expected: tuple[str, ...]
    observed: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.code, DeploymentRejectionCode):
            raise TypeError("deployment rejection code is invalid")
        _required_text(self.field, "deployment rejection field")
        if not isinstance(self.expected, tuple) or not isinstance(
                self.observed, tuple):
            raise TypeError("deployment rejection values must be tuples")
        if any(not isinstance(value, str) for value in
               (*self.expected, *self.observed)):
            raise TypeError("deployment rejection values must be strings")

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code.value,
            "field": self.field,
            "expected": list(self.expected),
            "observed": list(self.observed),
        }


@dataclass(frozen=True)
class SiteFeasibility:
    site_class_id: str
    feasible: bool
    rejections: tuple[DeploymentRejection, ...]

    def __post_init__(self) -> None:
        _required_text(self.site_class_id, "site_class_id")
        if type(self.feasible) is not bool:
            raise TypeError("feasible must be bool")
        if (not isinstance(self.rejections, tuple)
                or not all(isinstance(value, DeploymentRejection)
                           for value in self.rejections)):
            raise TypeError("site rejections must be an immutable typed tuple")
        if self.feasible != (not self.rejections):
            raise ValueError("site feasibility and rejection list disagree")

    def to_dict(self) -> dict[str, Any]:
        return {
            "site_class_id": self.site_class_id,
            "feasible": self.feasible,
            "rejections": [value.to_dict() for value in self.rejections],
        }


@dataclass(frozen=True)
class DeploymentFeasibilityProof:
    profile_id: str
    snapshot_id: str
    sites: tuple[SiteFeasibility, ...]

    def __post_init__(self) -> None:
        _digest(self.profile_id, "feasibility profile_id")
        _digest(self.snapshot_id, "feasibility snapshot_id")
        if (not isinstance(self.sites, tuple)
                or not all(isinstance(value, SiteFeasibility)
                           for value in self.sites)):
            raise TypeError("feasibility sites must be an immutable typed tuple")
        site_ids = [value.site_class_id for value in self.sites]
        if site_ids != sorted(site_ids) or len(site_ids) != len(set(site_ids)):
            raise ValueError("feasibility sites must be unique and sorted")

    @property
    def feasible_site_class_ids(self) -> tuple[str, ...]:
        return tuple(site.site_class_id for site in self.sites if site.feasible)

    @property
    def feasible(self) -> bool:
        return bool(self.feasible_site_class_ids)

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "snapshot_id": self.snapshot_id,
            "sites": [site.to_dict() for site in self.sites],
        }


@dataclass(frozen=True)
class DeploymentCapabilitySnapshot:
    snapshot_id: str
    captured_at: str
    site_classes: tuple[SiteClassCapability, ...]

    def __post_init__(self) -> None:
        _digest(self.snapshot_id, "snapshot_id")
        try:
            captured = datetime.fromisoformat(self.captured_at.replace("Z", "+00:00"))
        except (TypeError, ValueError) as exc:
            raise ValueError("captured_at must be an ISO-8601 UTC timestamp") from exc
        if (captured.tzinfo is None
                or captured.utcoffset() != timezone.utc.utcoffset(captured)):
            raise ValueError("captured_at must include the UTC offset")
        if not self.captured_at.endswith("Z"):
            raise ValueError("captured_at must use canonical UTC Z notation")
        if not self.site_classes:
            raise ValueError("deployment snapshot must contain a site class")
        if tuple(sorted(self.site_classes, key=lambda value: value.site_class_id)) \
                != self.site_classes:
            raise ValueError("site classes must be sorted by site_class_id")
        if len({value.site_class_id for value in self.site_classes}) \
                != len(self.site_classes):
            raise ValueError("deployment snapshot has duplicate site classes")
        if self.snapshot_id != self.expected_id():
            raise ValueError("deployment snapshot identity does not verify")

    @classmethod
    def freeze(cls, captured_at: str,
               site_classes: Iterable[SiteClassCapability],
               ) -> "DeploymentCapabilitySnapshot":
        sites = tuple(sorted(site_classes, key=lambda value: value.site_class_id))
        payload = {
            "schema": "stage2-deployment-capability-snapshot-v1",
            "captured_at": captured_at,
            "site_classes": [value.to_dict() for value in sites],
        }
        return cls(strict_hash(payload), captured_at, sites)

    def expected_id(self) -> str:
        return strict_hash({
            "schema": "stage2-deployment-capability-snapshot-v1",
            "captured_at": self.captured_at,
            "site_classes": [value.to_dict() for value in self.site_classes],
        })

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "captured_at": self.captured_at,
            "site_classes": [value.to_dict() for value in self.site_classes],
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]
                  ) -> "DeploymentCapabilitySnapshot":
        raw = require_object_fields(
            value, {field.name for field in dataclasses.fields(cls)},
            "DeploymentCapabilitySnapshot")
        if not isinstance(raw["site_classes"], list):
            raise ValueError("site_classes must be an array")
        raw["site_classes"] = tuple(
            SiteClassCapability.from_dict(item) for item in raw["site_classes"])
        return cls(**raw)

    def check(self, profile: ExecutionProfile) -> DeploymentFeasibilityProof:
        sites = tuple(_check_site(profile, site) for site in self.site_classes)
        return DeploymentFeasibilityProof(profile.profile_id, self.snapshot_id, sites)


def _check_site(profile: ExecutionProfile,
                site: SiteClassCapability) -> SiteFeasibility:
    req = profile.placement
    rejected: list[DeploymentRejection] = []

    def reject(code: DeploymentRejectionCode, field: str,
               expected: Iterable[str], observed: Iterable[str]) -> None:
        rejected.append(DeploymentRejection(
            code, field, tuple(expected), tuple(observed)))

    if req.allowed_site_classes and site.site_class_id not in req.allowed_site_classes:
        reject(DeploymentRejectionCode.SITE_CLASS_DISALLOWED, "site_class_id",
               req.allowed_site_classes, (site.site_class_id,))
    if site.architecture not in req.architectures:
        reject(DeploymentRejectionCode.ARCHITECTURE_UNSUPPORTED, "architecture",
               req.architectures, (site.architecture,))
    if not set(req.provider_kinds).intersection(site.provider_kinds):
        reject(DeploymentRejectionCode.PROVIDER_UNSUPPORTED, "provider_kinds",
               req.provider_kinds, site.provider_kinds)
    digest = profile.implementation.implementation_sha256
    if digest not in site.implementation_digests:
        reject(DeploymentRejectionCode.IMPLEMENTATION_UNAVAILABLE,
               "implementation_sha256", (digest,), site.implementation_digests)
    if not set(req.environment_classes).issubset(site.environment_classes):
        reject(DeploymentRejectionCode.ENVIRONMENT_UNAVAILABLE,
               "environment_classes", req.environment_classes,
               site.environment_classes)
    if not set(req.network_classes).issubset(site.network_classes):
        reject(DeploymentRejectionCode.NETWORK_UNAVAILABLE, "network_classes",
               req.network_classes, site.network_classes)
    if not set(req.credential_classes).issubset(site.credential_classes):
        reject(DeploymentRejectionCode.CREDENTIAL_UNAVAILABLE,
               "credential_classes", req.credential_classes,
               site.credential_classes)
    if not set(req.mount_classes).issubset(site.mount_classes):
        reject(DeploymentRejectionCode.MOUNT_UNAVAILABLE, "mount_classes",
               req.mount_classes, site.mount_classes)
    if not set(req.policy_classes).issubset(site.policy_classes):
        reject(DeploymentRejectionCode.POLICY_DISALLOWED, "policy_classes",
               req.policy_classes, site.policy_classes)
    if site.max_cpu_cores < req.resources.min_cpu_cores:
        reject(DeploymentRejectionCode.CPU_ENVELOPE_UNAVAILABLE,
               "min_cpu_cores", (str(req.resources.min_cpu_cores),),
               (str(site.max_cpu_cores),))
    if site.max_memory_mb < req.resources.min_memory_mb:
        reject(DeploymentRejectionCode.MEMORY_ENVELOPE_UNAVAILABLE,
               "min_memory_mb", (str(req.resources.min_memory_mb),),
               (str(site.max_memory_mb),))
    if site.max_gpus < req.resources.min_gpus:
        reject(DeploymentRejectionCode.GPU_ENVELOPE_UNAVAILABLE,
               "min_gpus", (str(req.resources.min_gpus),),
               (str(site.max_gpus),))
    return SiteFeasibility(site.site_class_id, not rejected, tuple(rejected))
