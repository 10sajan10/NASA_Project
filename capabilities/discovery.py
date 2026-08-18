"""Typed discovery provenance embedded in capability-catalog identity.

These records live below ``resolution`` so a catalog can carry its own
replayable origin without creating a dependency cycle.  A resolver certificate
is derived from these records; callers cannot substitute a different discovery
story for the same catalog identity.
"""
from __future__ import annotations

import dataclasses
import math
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from engine.runtime.identity import require_object_fields

from .implementation import _digest, _required_text


def _finite_scalar(value: Any, label: str) -> int | float | str:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise TypeError(f"{label} must be finite JSON number or text")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"{label} must be finite")
    if isinstance(value, str) and not value.strip():
        raise ValueError(f"{label} text must be non-empty")
    return value


@dataclass(frozen=True, order=True)
class DiscoveryLimitSetting:
    """One configured bound in an upstream discovery layer."""

    name: str
    value: int | float | str

    def __post_init__(self) -> None:
        _required_text(self.name, "discovery limit name")
        _finite_scalar(self.value, "discovery limit value")

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "DiscoveryLimitSetting":
        raw = require_object_fields(
            value, {field.name for field in dataclasses.fields(cls)},
            "DiscoveryLimitSetting")
        return cls(**raw)


@dataclass(frozen=True, order=True)
class DiscoveryLimitActivation:
    """A typed bound that actually cut an upstream search short."""

    code: str
    limit: int | float
    subject_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _required_text(self.code, "discovery limit code")
        value = _finite_scalar(self.limit, "activated discovery limit")
        if not isinstance(value, (int, float)) or value < 0:
            raise ValueError(
                "activated discovery limit must be a non-negative number")
        if (not isinstance(self.subject_ids, tuple) or not self.subject_ids
                or any(not isinstance(item, str) or not item.strip()
                       for item in self.subject_ids)):
            raise TypeError(
                "activated discovery limit subjects must be non-empty text")
        if self.subject_ids != tuple(sorted(set(self.subject_ids))):
            raise ValueError(
                "activated discovery limit subjects must be unique and sorted")

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "limit": self.limit,
            "subject_ids": list(self.subject_ids),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]
                  ) -> "DiscoveryLimitActivation":
        raw = require_object_fields(
            value, {field.name for field in dataclasses.fields(cls)},
            "DiscoveryLimitActivation")
        if not isinstance(raw["subject_ids"], list):
            raise ValueError(
                "DiscoveryLimitActivation.subject_ids must be an array")
        raw["subject_ids"] = tuple(raw["subject_ids"])
        return cls(**raw)


@dataclass(frozen=True)
class DiscoveryLayerCertificate:
    """The replay identity and completeness verdict of one discovery layer."""

    layer_kind: str
    expansion_id: str
    source_ids: tuple[str, ...]
    limits: tuple[DiscoveryLimitSetting, ...]
    limit_reasons: tuple[DiscoveryLimitActivation, ...]
    complete: bool

    def __post_init__(self) -> None:
        _required_text(self.layer_kind, "discovery layer kind")
        _digest(self.expansion_id, "discovery expansion_id")
        if (not isinstance(self.source_ids, tuple)
                or any(not isinstance(item, str) or not item.strip()
                       for item in self.source_ids)):
            raise TypeError("discovery source IDs must be text")
        if self.source_ids != tuple(sorted(set(self.source_ids))):
            raise ValueError("discovery source IDs must be unique and sorted")
        if (not isinstance(self.limits, tuple)
                or not all(isinstance(item, DiscoveryLimitSetting)
                           for item in self.limits)):
            raise TypeError("discovery limits must be typed and immutable")
        if self.limits != tuple(sorted(self.limits)) \
                or len({item.name for item in self.limits}) != len(self.limits):
            raise ValueError("discovery limits must be unique and sorted")
        if (not isinstance(self.limit_reasons, tuple)
                or not all(isinstance(item, DiscoveryLimitActivation)
                           for item in self.limit_reasons)):
            raise TypeError(
                "discovery limit reasons must be typed and immutable")
        if self.limit_reasons != tuple(sorted(self.limit_reasons)):
            raise ValueError("discovery limit reasons must be sorted")
        if type(self.complete) is not bool:
            raise TypeError("discovery layer completeness must be bool")
        if self.complete != (not self.limit_reasons):
            raise ValueError(
                "discovery layer completeness must agree with limit reasons")

    @classmethod
    def bind(
        cls,
        layer_kind: str,
        expansion_id: str,
        *,
        source_ids: Iterable[str] = (),
        limits: Mapping[str, int | float | str] | None = None,
        limit_reasons: Iterable[Any] = (),
        complete: bool,
    ) -> "DiscoveryLayerCertificate":
        limit_values = {} if limits is None else limits
        if not isinstance(limit_values, Mapping):
            raise TypeError("discovery limits must be a mapping")
        settings = tuple(sorted(
            DiscoveryLimitSetting(name, value)
            for name, value in limit_values.items()))
        reasons: list[DiscoveryLimitActivation] = []
        for item in limit_reasons:
            payload = item.to_dict() if hasattr(item, "to_dict") else item
            if not isinstance(payload, dict):
                raise TypeError(
                    "discovery limit reasons must serialize to objects")
            reasons.append(DiscoveryLimitActivation.from_dict(payload))
        return cls(
            layer_kind=layer_kind,
            expansion_id=expansion_id,
            source_ids=tuple(sorted(set(source_ids))),
            limits=settings,
            limit_reasons=tuple(sorted(reasons)),
            complete=complete,
        )

    @property
    def limit_codes(self) -> tuple[str, ...]:
        return tuple(sorted({item.code for item in self.limit_reasons}))

    def to_dict(self) -> dict[str, Any]:
        return {
            "layer_kind": self.layer_kind,
            "expansion_id": self.expansion_id,
            "source_ids": list(self.source_ids),
            "limits": [item.to_dict() for item in self.limits],
            "limit_reasons": [item.to_dict() for item in self.limit_reasons],
            "complete": self.complete,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]
                  ) -> "DiscoveryLayerCertificate":
        raw = require_object_fields(
            value, {field.name for field in dataclasses.fields(cls)},
            "DiscoveryLayerCertificate")
        for name, parser in (
                ("limits", DiscoveryLimitSetting.from_dict),
                ("limit_reasons", DiscoveryLimitActivation.from_dict)):
            if not isinstance(raw[name], list):
                raise ValueError(
                    f"DiscoveryLayerCertificate.{name} must be an array")
            raw[name] = tuple(parser(item) for item in raw[name])
        if not isinstance(raw["source_ids"], list):
            raise ValueError(
                "DiscoveryLayerCertificate.source_ids must be an array")
        raw["source_ids"] = tuple(raw["source_ids"])
        return cls(**raw)


@dataclass(frozen=True)
class CatalogDiscoveryProvenance:
    """Discovery origin included in a ``CapabilityCatalog`` hash.

    Authored catalogs have neither a base ID nor layers. Discovered catalogs
    have both. This makes stripping discovery history a catalog-identity
    change rather than an alternate interpretation of the same object.
    """

    base_catalog_id: str | None
    layers: tuple[DiscoveryLayerCertificate, ...]

    def __post_init__(self) -> None:
        if (not isinstance(self.layers, tuple)
                or not all(isinstance(item, DiscoveryLayerCertificate)
                           for item in self.layers)):
            raise TypeError("catalog discovery layers must be immutable")
        canonical = tuple(sorted(
            self.layers,
            key=lambda item: (item.layer_kind, item.expansion_id)))
        if self.layers != canonical:
            raise ValueError("catalog discovery layers must be sorted")
        if len({(item.layer_kind, item.expansion_id) for item in self.layers}) \
                != len(self.layers):
            raise ValueError("catalog discovery layers cannot repeat")
        if self.layers:
            if self.base_catalog_id is None:
                raise ValueError(
                    "discovered catalog provenance needs a base catalog ID")
            _digest(self.base_catalog_id, "discovery base catalog_id")
        elif self.base_catalog_id is not None:
            raise ValueError(
                "authored catalog provenance cannot name a discovery base")

    @classmethod
    def authored(cls) -> "CatalogDiscoveryProvenance":
        return cls(None, ())

    @classmethod
    def discovered(
        cls,
        base_catalog_id: str,
        layers: Iterable[DiscoveryLayerCertificate],
    ) -> "CatalogDiscoveryProvenance":
        values = tuple(sorted(
            layers, key=lambda item: (item.layer_kind, item.expansion_id)))
        if not values:
            raise ValueError("discovered provenance needs at least one layer")
        return cls(base_catalog_id, values)

    @property
    def authored_directly(self) -> bool:
        return not self.layers

    @property
    def complete(self) -> bool:
        return all(item.complete for item in self.layers)

    def to_dict(self) -> dict[str, Any]:
        return {
            "base_catalog_id": self.base_catalog_id,
            "layers": [item.to_dict() for item in self.layers],
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]
                  ) -> "CatalogDiscoveryProvenance":
        raw = require_object_fields(
            value, {field.name for field in dataclasses.fields(cls)},
            "CatalogDiscoveryProvenance")
        if not isinstance(raw["layers"], list):
            raise ValueError("CatalogDiscoveryProvenance.layers must be an array")
        raw["layers"] = tuple(
            DiscoveryLayerCertificate.from_dict(item)
            for item in raw["layers"])
        return cls(**raw)


__all__ = [
    "CatalogDiscoveryProvenance",
    "DiscoveryLayerCertificate",
    "DiscoveryLimitActivation",
    "DiscoveryLimitSetting",
]
