"""Strict canonical identity helpers for the durable runtime.

Persisted runtime identities deliberately accept only the JSON data model.  In
particular, they never fall back to ``str(value)``: doing so would make an
identity depend on a Python object's representation and could conceal a
configuration error.  The helpers in this module are also used at every
deserialization boundary so an old or malformed record cannot be interpreted
as a different, newer runtime object.
"""
from __future__ import annotations

import hashlib
import json
import math
from typing import Any


class FrozenDict(dict[str, Any]):
    """A JSON-object-compatible dictionary that cannot change after binding."""

    @staticmethod
    def _immutable(*_args: Any, **_kwargs: Any) -> None:
        raise TypeError("bound JSON identity data is immutable")

    __setitem__ = _immutable
    __delitem__ = _immutable
    clear = _immutable
    pop = _immutable
    popitem = _immutable
    setdefault = _immutable
    update = _immutable
    __ior__ = _immutable


def strict_canonical_json(value: Any) -> str:
    """Canonical JSON without coercion, NaN, or implementation-defined values."""
    _validate_json_value(value)
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )


def strict_hash(value: Any) -> str:
    return hashlib.sha256(strict_canonical_json(value).encode("utf-8")).hexdigest()


def strict_copy(value: Any) -> Any:
    """Validate and detach JSON identity data."""
    return strict_json_loads(strict_canonical_json(value))


def freeze_json(value: Any) -> Any:
    """Return an immutable, detached representation of strict JSON data."""
    detached = strict_copy(value)

    def freeze(item: Any) -> Any:
        if isinstance(item, dict):
            return FrozenDict((key, freeze(child))
                              for key, child in item.items())
        if isinstance(item, list):
            return tuple(freeze(child) for child in item)
        return item

    return freeze(detached)


def strict_json_loads(encoded: str | bytes | bytearray) -> Any:
    """Parse strict JSON, rejecting duplicate keys and non-finite numbers."""

    def reject_constant(value: str) -> None:
        raise ValueError(f"non-finite JSON number {value!r} is forbidden")

    def reject_duplicate_keys(
            pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON object key {key!r}")
            result[key] = value
        return result

    try:
        value = json.loads(
            encoded,
            parse_constant=reject_constant,
            object_pairs_hook=reject_duplicate_keys,
        )
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON: {exc.msg}") from exc
    _validate_json_value(value)
    return value


def require_object_fields(value: Any, expected: set[str] | frozenset[str],
                          context: str) -> dict[str, Any]:
    """Return a detached object only when its field set is exactly expected."""
    if not isinstance(value, dict):
        raise ValueError(f"{context} must be a JSON object")
    actual = set(value)
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    if missing or extra:
        details = []
        if missing:
            details.append(f"missing={missing}")
        if extra:
            details.append(f"unexpected={extra}")
        raise ValueError(f"invalid {context} fields: " + ", ".join(details))
    return strict_copy(value)


def _validate_json_value(value: Any, path: str = "$") -> None:
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"non-finite number at {path} is forbidden")
        return
    if isinstance(value, list) or isinstance(value, tuple):
        for index, item in enumerate(value):
            _validate_json_value(item, f"{path}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(
                    f"JSON object key at {path} must be str, got "
                    f"{type(key).__name__}")
            _validate_json_value(item, f"{path}.{key}")
        return
    raise TypeError(
        f"identity value at {path} is not strict JSON: "
        f"{type(value).__module__}.{type(value).__qualname__}")
