"""Canonical scientific identity primitives for Stage 2.

The Stage-1 identity helpers already provide strict JSON and SHA-256.  This
module adds the scientific normalization which must happen *before* those
helpers are used: UTC timestamps, decimal numbers, CRS authorities, and unit
tokens.  It deliberately does not perform scientific conversions.
"""
from __future__ import annotations

import dataclasses
import math
import re
from dataclasses import fields
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, ClassVar, Mapping

from engine.runtime.identity import strict_canonical_json, strict_hash


_EPSG = re.compile(r"^EPSG\s*:\s*0*([1-9][0-9]*)$", re.IGNORECASE)


def require_exact_fields(
    value: object,
    expected: tuple[str, ...],
    label: str,
) -> dict[str, Any]:
    """Return a JSON object only when its field set is exactly ``expected``.

    Contract decoding is deliberately closed-world.  Silently ignoring a new
    or misspelled field would let two parties assign different semantics to
    the same parsed value; accepting a missing default would make identity
    depend on decoder version.  Every persisted contract therefore carries
    every field, including fields whose value is ``None`` or an empty list.
    """
    if not isinstance(value, dict):
        raise TypeError(f"{label} must be an object")
    if not all(isinstance(key, str) for key in value):
        raise TypeError(f"{label} field names must be strings")
    expected_set = set(expected)
    actual_set = set(value)
    missing = sorted(expected_set - actual_set)
    unknown = sorted(actual_set - expected_set)
    if missing or unknown:
        details: list[str] = []
        if missing:
            details.append(f"missing={missing!r}")
        if unknown:
            details.append(f"unknown={unknown!r}")
        raise ValueError(f"{label} has invalid fields ({', '.join(details)})")
    return value


def require_sequence(value: object, label: str) -> tuple[Any, ...]:
    """Decode a JSON-array-like value without treating text as a sequence."""
    if not isinstance(value, (list, tuple)):
        raise TypeError(f"{label} must be an array")
    return tuple(value)


def required_text(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value.strip()


def canonical_decimal(value: object, label: str = "number") -> str:
    """Return a finite, non-exponential decimal token.

    Floats are accepted at API boundaries for usability but are interpreted
    through their shortest round-trippable decimal spelling.  Persisted values
    are always strings, so a round trip is independent of Python's float
    representation.  Negative zero is canonicalized to zero.
    """
    if isinstance(value, bool):
        raise TypeError(f"{label} must be numeric, not bool")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"{label} must be finite")
    if not isinstance(value, (str, int, float, Decimal)):
        raise TypeError(f"{label} must be a decimal string or number")
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{label} is not a valid decimal") from exc
    if not number.is_finite():
        raise ValueError(f"{label} must be finite")
    if number == 0:
        return "0"
    text = format(number.normalize(), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def decimal_value(value: object) -> Decimal:
    return Decimal(canonical_decimal(value))


def canonical_timestamp(value: str | datetime, label: str = "timestamp") -> str:
    """Normalize an aware timestamp to an ISO-8601 UTC ``Z`` spelling."""
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        text = required_text(value, label)
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{label} must be an ISO-8601 timestamp") from exc
    else:
        raise TypeError(f"{label} must be a string or datetime")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{label} must include a UTC offset")
    utc = parsed.astimezone(timezone.utc)
    text = utc.isoformat(timespec="microseconds")
    text = text.removesuffix("+00:00") + "Z"
    return text.replace(".000000Z", "Z")


def timestamp_value(value: str | datetime) -> datetime:
    return datetime.fromisoformat(canonical_timestamp(value).replace("Z", "+00:00"))


def canonical_crs(value: str) -> str:
    """Normalize authority CRS identifiers without guessing WKT equivalence."""
    text = required_text(value, "CRS")
    match = _EPSG.fullmatch(text)
    if match:
        return f"EPSG:{int(match.group(1))}"
    if any(char.isspace() for char in text):
        raise ValueError(
            "non-authority CRS values must already be canonical and whitespace-free")
    return text


_UNIT_ALIASES = {
    "1": "1",
    "%": "%",
    "m": "m",
    "meter": "m",
    "metre": "m",
    "km": "km",
    "s": "s",
    "second": "s",
    "pa": "Pa",
    "pascal": "Pa",
    "hpa": "hPa",
    "k": "K",
    "kelvin": "K",
    "degree": "degree",
    "degrees": "degree",
    "deg": "degree",
    "m/s": "m.s-1",
    "m s-1": "m.s-1",
    "m s^-1": "m.s-1",
    "m.s-1": "m.s-1",
    "km/h": "km.h-1",
    "km h-1": "km.h-1",
    "km.h-1": "km.h-1",
}


def canonical_unit(value: str) -> str:
    """Normalize spellings only; never change a value's physical magnitude."""
    text = required_text(value, "unit")
    return _UNIT_ALIASES.get(text.lower(), text)


def sorted_unique_text(values: object, label: str) -> tuple[str, ...]:
    if isinstance(values, str) or not isinstance(values, (list, tuple, set, frozenset)):
        raise TypeError(f"{label} must be a sequence of strings")
    normalized = tuple(required_text(value, label) for value in values)
    if len(set(normalized)) != len(normalized):
        raise ValueError(f"{label} cannot contain duplicates")
    return tuple(sorted(normalized))


def to_primitive(value: Any) -> Any:
    """Convert contract values to the strict JSON data model."""
    if isinstance(value, Enum):
        return value.value
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: to_primitive(getattr(value, field.name))
            for field in fields(value)
            if not field.name.startswith("_")
        }
    if isinstance(value, Mapping):
        return {str(key): to_primitive(item) for key, item in sorted(value.items())}
    if isinstance(value, (tuple, list)):
        return [to_primitive(item) for item in value]
    if isinstance(value, (set, frozenset)):
        items = [to_primitive(item) for item in value]
        return sorted(items, key=strict_canonical_json)
    if isinstance(value, Decimal):
        return canonical_decimal(value)
    if isinstance(value, datetime):
        return canonical_timestamp(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        # Validation and non-finite rejection happen in strict_canonical_json.
        return value
    raise TypeError(f"cannot serialize contract value {type(value).__name__}")


class ScientificIdentity:
    """Mixin for immutable, content-addressed scientific contract values."""

    identity_schema: ClassVar[str] = "scientific-contract-value-v1"

    def to_dict(self) -> dict[str, Any]:
        primitive = to_primitive(self)
        if not isinstance(primitive, dict):  # pragma: no cover - mixin invariant
            raise TypeError("identity object must serialize as a JSON object")
        return primitive

    def canonical_json(self) -> str:
        return strict_canonical_json(
            {"schema": self.identity_schema, "value": self.to_dict()})

    @property
    def identity(self) -> str:
        return strict_hash(
            {"schema": self.identity_schema, "value": self.to_dict()})
