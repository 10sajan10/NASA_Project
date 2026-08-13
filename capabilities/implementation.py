"""Closed, result-affecting implementation identities for Stage 2.

Stage 2 never accepts a Python callable, import path, command, or caller supplied
digest as executable authority.  An implementation reference is minted from the
small Stage-1 operation registry and is rechecked both during capability binding
and deployment compilation.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Any

from engine.runtime.identity import require_object_fields, strict_hash
from engine.runtime.operations import operation_component
from engine.runtime.types import ExecutableComponent


_DIGEST_LENGTH = 64


def _required_text(value: str, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")


def _digest(value: str, label: str) -> None:
    _required_text(value, label)
    if (len(value) != _DIGEST_LENGTH
            or any(char not in "0123456789abcdef" for char in value)):
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")


@dataclass(frozen=True)
class ImplementationRef:
    """Exact result-affecting component selected for an invocation.

    ``configuration_sha256`` covers public, non-secret component configuration
    outside invocation parameters.  Placement fields such as node, provider,
    memory request, credentials, and retry policy deliberately do not appear.
    """

    component_id: str
    component_version: str
    operation_key: str
    implementation_sha256: str
    configuration_sha256: str
    deterministic: bool
    idempotent: bool
    retry_safe: bool

    def __post_init__(self) -> None:
        for value, label in (
            (self.component_id, "component_id"),
            (self.component_version, "component_version"),
            (self.operation_key, "operation_key"),
        ):
            _required_text(value, label)
        _digest(self.implementation_sha256, "implementation_sha256")
        _digest(self.configuration_sha256, "configuration_sha256")
        for field_name in ("deterministic", "idempotent", "retry_safe"):
            if type(getattr(self, field_name)) is not bool:
                raise TypeError(f"{field_name} must be bool")

    @classmethod
    def from_operation_key(
            cls, operation_key: str, *,
            public_configuration: dict[str, Any] | None = None,
    ) -> "ImplementationRef":
        """Mint a reference from the closed local operation registry."""
        component = operation_component(operation_key)
        return cls(
            component_id=component.component_id,
            component_version=component.version,
            operation_key=component.operation_key,
            implementation_sha256=component.implementation_digest,
            configuration_sha256=strict_hash(public_configuration or {}),
            deterministic=component.deterministic,
            idempotent=component.idempotent,
            retry_safe=component.retry_safe,
        )

    @property
    def identity_id(self) -> str:
        return strict_hash({
            "schema": "stage2-result-implementation-v1",
            **self.to_dict(),
        })

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ImplementationRef":
        raw = require_object_fields(
            value,
            {field.name for field in dataclasses.fields(cls)},
            "ImplementationRef",
        )
        return cls(**raw)

    def executable_component(self) -> ExecutableComponent:
        return ExecutableComponent(
            component_id=self.component_id,
            version=self.component_version,
            implementation_digest=self.implementation_sha256,
            operation_key=self.operation_key,
            deterministic=self.deterministic,
            idempotent=self.idempotent,
            retry_safe=self.retry_safe,
        )

    def verify_current(self) -> ExecutableComponent:
        """Return the current component or reject a stale/forged reference."""
        current = operation_component(self.operation_key)
        if current != self.executable_component():
            raise ValueError(
                f"implementation is stale for closed operation "
                f"{self.operation_key!r}")
        return current
