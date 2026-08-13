"""Closed Stage-2 binder identities and synthetic binding rules.

There is deliberately no public ``register(callable)`` escape hatch.  Adding a
binder is a source-controlled code change that changes its digest and therefore
invalidates stale serialized capability specifications.
"""
from __future__ import annotations

import dataclasses
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from engine.runtime.identity import require_object_fields

from .implementation import _digest, _required_text


@dataclass(frozen=True)
class BinderRule:
    operation_key: str
    input_ports: tuple[str, ...]
    output_ports: tuple[str, ...]
    parameter_names: tuple[str, ...]


_BINDER_RULES: dict[str, BinderRule] = {
    "synthetic.constant.bind.v1": BinderRule(
        operation_key="synthetic.constant.v1",
        input_ports=(),
        output_ports=("result",),
        parameter_names=("value",),
    ),
    "synthetic.pair.bind.v1": BinderRule(
        operation_key="synthetic.pair.v1",
        input_ports=(),
        output_ports=("left", "right"),
        parameter_names=("left", "right"),
    ),
    "synthetic.add.bind.v1": BinderRule(
        operation_key="synthetic.add.v1",
        input_ports=("left", "right"),
        output_ports=("result",),
        parameter_names=(),
    ),
}


def binder_keys() -> tuple[str, ...]:
    return tuple(sorted(_BINDER_RULES))


def binder_rule(key: str) -> BinderRule:
    try:
        return _BINDER_RULES[key]
    except KeyError as exc:
        raise KeyError(f"unknown closed Stage-2 binder {key!r}") from exc


def _binder_digest(key: str) -> str:
    # Conservative by design: changing any rule or verification behavior makes
    # every binder serialized from this module stale rather than ambiguous.
    source = Path(__file__).read_bytes()
    return hashlib.sha256(key.encode("utf-8") + b"\0" + source).hexdigest()


@dataclass(frozen=True)
class BinderRef:
    binder_key: str
    binder_version: str
    implementation_sha256: str

    def __post_init__(self) -> None:
        _required_text(self.binder_key, "binder_key")
        _required_text(self.binder_version, "binder_version")
        _digest(self.implementation_sha256, "binder implementation_sha256")

    @classmethod
    def from_key(cls, key: str) -> "BinderRef":
        binder_rule(key)  # closed-registry check
        version = key.rsplit(".", 1)[-1]
        return cls(key, version, _binder_digest(key))

    def verify_current(self) -> BinderRule:
        rule = binder_rule(self.binder_key)
        if self != BinderRef.from_key(self.binder_key):
            raise ValueError(f"binder is stale for key {self.binder_key!r}")
        return rule

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "BinderRef":
        raw = require_object_fields(
            value, {field.name for field in dataclasses.fields(cls)},
            "BinderRef")
        return cls(**raw)
