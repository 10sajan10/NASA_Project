"""Small strict parameter schema used by the finite Stage-2 binders."""
from __future__ import annotations

import dataclasses
import math
from dataclasses import dataclass
from enum import Enum
from typing import Any

from engine.runtime.identity import freeze_json, require_object_fields, strict_copy

from .implementation import _required_text


class ParameterKind(str, Enum):
    NUMBER = "number"
    INTEGER = "integer"
    STRING = "string"
    BOOLEAN = "boolean"
    JSON = "json"


@dataclass(frozen=True)
class ParameterField:
    name: str
    kind: ParameterKind
    required: bool = True

    def __post_init__(self) -> None:
        _required_text(self.name, "parameter field name")
        if not isinstance(self.kind, ParameterKind):
            raise TypeError("parameter field kind is invalid")
        if type(self.required) is not bool:
            raise TypeError("parameter required must be bool")

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "kind": self.kind.value,
                "required": self.required}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ParameterField":
        raw = require_object_fields(
            value, {field.name for field in dataclasses.fields(cls)},
            "ParameterField")
        raw["kind"] = ParameterKind(raw["kind"])
        return cls(**raw)


@dataclass(frozen=True)
class ParameterSchema:
    fields: tuple[ParameterField, ...] = ()
    allow_additional: bool = False

    def __post_init__(self) -> None:
        if (not isinstance(self.fields, tuple)
                or not all(isinstance(field, ParameterField)
                           for field in self.fields)):
            raise TypeError("parameter fields must be an immutable typed tuple")
        if len({field.name for field in self.fields}) != len(self.fields):
            raise ValueError("parameter schema contains duplicate fields")
        if tuple(sorted(self.fields, key=lambda field: field.name)) != self.fields:
            raise ValueError("parameter fields must be sorted by name")
        if type(self.allow_additional) is not bool:
            raise TypeError("allow_additional must be bool")

    def validate(self, values: dict[str, Any]) -> dict[str, Any]:
        detached = strict_copy(values)
        if not isinstance(detached, dict):
            raise ValueError("parameters must be a strict JSON object")
        expected = {field.name: field for field in self.fields}
        missing = sorted(
            name for name, field in expected.items()
            if field.required and name not in detached)
        extra = sorted(set(detached) - set(expected))
        if missing:
            raise ValueError(f"missing required parameters: {missing}")
        if extra and not self.allow_additional:
            raise ValueError(f"undeclared parameters are forbidden: {extra}")
        for name, value in detached.items():
            field = expected.get(name)
            if field is not None:
                _validate_kind(name, value, field.kind)
        frozen = freeze_json(detached)
        assert isinstance(frozen, dict)
        return frozen

    def to_dict(self) -> dict[str, Any]:
        return {
            "fields": [field.to_dict() for field in self.fields],
            "allow_additional": self.allow_additional,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ParameterSchema":
        raw = require_object_fields(
            value, {field.name for field in dataclasses.fields(cls)},
            "ParameterSchema")
        if not isinstance(raw["fields"], list):
            raise ValueError("ParameterSchema.fields must be an array")
        raw["fields"] = tuple(
            ParameterField.from_dict(item) for item in raw["fields"])
        return cls(**raw)


def _validate_kind(name: str, value: Any, kind: ParameterKind) -> None:
    valid = False
    if kind is ParameterKind.NUMBER:
        valid = (not isinstance(value, bool)
                 and isinstance(value, (int, float))
                 and (not isinstance(value, float) or math.isfinite(value)))
    elif kind is ParameterKind.INTEGER:
        valid = not isinstance(value, bool) and isinstance(value, int)
    elif kind is ParameterKind.STRING:
        valid = isinstance(value, str)
    elif kind is ParameterKind.BOOLEAN:
        valid = type(value) is bool
    elif kind is ParameterKind.JSON:
        strict_copy(value)
        valid = True
    if not valid:
        raise ValueError(
            f"parameter {name!r} must have kind {kind.value!r}")
