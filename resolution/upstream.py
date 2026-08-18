"""Content-addressed hand-off from catalog discovery into resolution.

Discovery layers are embedded in the capability catalog's own identity.  A
``DiscoveryCertificate`` is therefore a projection of catalog provenance, not
an independent caller assertion.  The resolver reconstructs the projection
and accepts only an exact match.

That certificate is an integrity record, not a declaration of which discovery
layers planning was supposed to run.  ``DiscoveryUniverseContract`` supplies
that second, independently provided statement.  Resolution accepts a catalog
only when its certificate covers the exact predeclared base catalog, layer
kinds, source snapshots, and configured limits.

Neither object is a signature or an authorization token. They make an
inconsistent or accidentally stripped declaration detectable inside a trusted
catalog-construction boundary; they cannot prove that a caller disclosed every
source in the open world.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from capabilities import (
    CapabilityCatalog,
    DiscoveryLayerCertificate,
    DiscoveryLimitActivation,
    DiscoveryLimitSetting,
)
from capabilities.implementation import _digest
from engine.runtime.identity import require_object_fields, strict_hash


@dataclass(frozen=True)
class DiscoveryCertificate:
    """Content-addressed projection of one catalog's embedded provenance."""

    certificate_id: str
    subject_catalog_id: str
    base_catalog_id: str
    layers: tuple[DiscoveryLayerCertificate, ...]
    complete: bool

    def __post_init__(self) -> None:
        for value, label in (
                (self.certificate_id, "discovery certificate_id"),
                (self.subject_catalog_id, "discovery subject catalog_id"),
                (self.base_catalog_id, "discovery base catalog_id")):
            _digest(value, label)
        if (not isinstance(self.layers, tuple)
                or not all(isinstance(item, DiscoveryLayerCertificate)
                           for item in self.layers)):
            raise TypeError("discovery certificate layers must be immutable")
        if self.layers != tuple(sorted(
                self.layers,
                key=lambda item: (item.layer_kind, item.expansion_id))):
            raise ValueError("discovery certificate layers must be sorted")
        if len({(item.layer_kind, item.expansion_id) for item in self.layers}) \
                != len(self.layers):
            raise ValueError("discovery certificate layers cannot repeat")
        if type(self.complete) is not bool:
            raise TypeError("discovery certificate completeness must be bool")
        if self.complete != all(item.complete for item in self.layers):
            raise ValueError(
                "certificate completeness must equal its layer conjunction")
        if self.certificate_id != self.expected_id():
            raise ValueError("discovery certificate identity does not verify")

    @classmethod
    def bind(
        cls,
        subject_catalog_id: str,
        base_catalog_id: str,
        layers: Iterable[DiscoveryLayerCertificate] = (),
    ) -> "DiscoveryCertificate":
        """Bind a value; resolver still verifies it against catalog origin."""
        values = tuple(sorted(
            layers, key=lambda item: (item.layer_kind, item.expansion_id)))
        complete = all(item.complete for item in values)
        payload = cls._payload(
            subject_catalog_id, base_catalog_id, values, complete)
        return cls(
            strict_hash(payload), subject_catalog_id, base_catalog_id,
            values, complete)

    @classmethod
    def for_catalog(cls, catalog: CapabilityCatalog) -> "DiscoveryCertificate":
        """Derive the only certificate valid for ``catalog``."""
        if not isinstance(catalog, CapabilityCatalog):
            raise TypeError("catalog certificate requires CapabilityCatalog")
        provenance = catalog.discovery_provenance
        base_id = (catalog.catalog_id if provenance.authored_directly
                   else provenance.base_catalog_id)
        assert base_id is not None
        return cls.bind(catalog.catalog_id, base_id, provenance.layers)

    @classmethod
    def for_base_catalog(
            cls, catalog: CapabilityCatalog) -> "DiscoveryCertificate":
        """Certify a directly authored catalog and reject generated catalogs."""
        if not isinstance(catalog, CapabilityCatalog):
            raise TypeError("base catalog certificate requires CapabilityCatalog")
        if not catalog.discovery_provenance.authored_directly:
            raise ValueError(
                "generated catalog cannot be relabelled as a base catalog")
        return cls.for_catalog(catalog)

    @staticmethod
    def _payload(
        subject_catalog_id: str,
        base_catalog_id: str,
        layers: tuple[DiscoveryLayerCertificate, ...],
        complete: bool,
    ) -> dict[str, Any]:
        return {
            "schema": "stage8r-discovery-certificate-v2",
            "subject_catalog_id": subject_catalog_id,
            "base_catalog_id": base_catalog_id,
            "layers": [item.to_dict() for item in layers],
            "complete": complete,
        }

    def expected_id(self) -> str:
        return strict_hash(self._payload(
            self.subject_catalog_id, self.base_catalog_id,
            self.layers, self.complete))

    @property
    def limit_codes(self) -> tuple[str, ...]:
        return tuple(sorted({
            code for layer in self.layers for code in layer.limit_codes}))

    @property
    def expansion_ids(self) -> tuple[str, ...]:
        return tuple(layer.expansion_id for layer in self.layers)

    def to_dict(self) -> dict[str, Any]:
        payload = self._payload(
            self.subject_catalog_id, self.base_catalog_id,
            self.layers, self.complete)
        payload["certificate_id"] = self.certificate_id
        return payload

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "DiscoveryCertificate":
        raw = require_object_fields(value, {
            "schema", "certificate_id", "subject_catalog_id",
            "base_catalog_id", "layers", "complete",
        }, "DiscoveryCertificate")
        if raw.pop("schema") != "stage8r-discovery-certificate-v2":
            raise ValueError("unsupported discovery certificate schema")
        if not isinstance(raw["layers"], list):
            raise ValueError("DiscoveryCertificate.layers must be an array")
        raw["layers"] = tuple(
            DiscoveryLayerCertificate.from_dict(item)
            for item in raw["layers"])
        return cls(**raw)


@dataclass(frozen=True)
class DiscoveryLayerScope:
    """One discovery layer required by a predeclared planning universe.

    Expansion IDs and limit activations are deliberately absent: neither is
    knowable before discovery runs.  Source identities and configured limits
    are knowable, and must match the resulting certificate exactly.
    """

    layer_kind: str
    source_ids: tuple[str, ...]
    limits: tuple[DiscoveryLimitSetting, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.layer_kind, str) or not self.layer_kind.strip():
            raise ValueError("discovery scope layer_kind must be non-empty text")
        if (not isinstance(self.source_ids, tuple)
                or any(not isinstance(item, str) or not item.strip()
                       for item in self.source_ids)):
            raise TypeError("discovery scope source IDs must be text")
        if self.source_ids != tuple(sorted(set(self.source_ids))):
            raise ValueError(
                "discovery scope source IDs must be unique and sorted")
        if (not isinstance(self.limits, tuple)
                or not all(isinstance(item, DiscoveryLimitSetting)
                           for item in self.limits)):
            raise TypeError("discovery scope limits must be typed and immutable")
        if (self.limits != tuple(sorted(self.limits))
                or len({item.name for item in self.limits}) != len(self.limits)):
            raise ValueError("discovery scope limits must be unique and sorted")

    @classmethod
    def bind(
        cls,
        layer_kind: str,
        *,
        source_ids: Iterable[str] = (),
        limits: Mapping[str, int | float | str] | None = None,
    ) -> "DiscoveryLayerScope":
        limit_values = {} if limits is None else limits
        if not isinstance(limit_values, Mapping):
            raise TypeError("discovery scope limits must be a mapping")
        sources = tuple(source_ids)
        if any(not isinstance(item, str) or not item.strip()
               for item in sources):
            raise TypeError("discovery scope source IDs must be text")
        return cls(
            layer_kind,
            tuple(sorted(set(sources))),
            tuple(sorted(DiscoveryLimitSetting(name, value)
                         for name, value in limit_values.items())),
        )

    @classmethod
    def from_certificate_layer(
            cls, layer: DiscoveryLayerCertificate) -> "DiscoveryLayerScope":
        """Project coverage facts; do not use this to author the contract."""
        if not isinstance(layer, DiscoveryLayerCertificate):
            raise TypeError("scope projection requires a discovery layer")
        return cls(layer.layer_kind, layer.source_ids, layer.limits)

    def to_dict(self) -> dict[str, Any]:
        return {
            "layer_kind": self.layer_kind,
            "source_ids": list(self.source_ids),
            "limits": [item.to_dict() for item in self.limits],
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "DiscoveryLayerScope":
        raw = require_object_fields(
            value, {"layer_kind", "source_ids", "limits"},
            "DiscoveryLayerScope")
        if not isinstance(raw["source_ids"], list):
            raise ValueError("DiscoveryLayerScope.source_ids must be an array")
        if not isinstance(raw["limits"], list):
            raise ValueError("DiscoveryLayerScope.limits must be an array")
        raw["source_ids"] = tuple(raw["source_ids"])
        raw["limits"] = tuple(
            DiscoveryLimitSetting.from_dict(item) for item in raw["limits"])
        return cls(**raw)


def _scope_sort_key(scope: DiscoveryLayerScope) -> tuple[str, str]:
    # Hashing the strict payload avoids Python comparisons between unlike
    # configured scalar types (for example one integer and one text value).
    return (scope.layer_kind, strict_hash(scope.to_dict()))


@dataclass(frozen=True)
class DiscoveryUniverseContract:
    """Content-addressed declaration of the finite discovery universe.

    This contract is mandatory and comes from planning intent, independently
    of the output catalog and certificate.  It establishes integrity over a
    declared universe; it is not an authority proof that the caller declared
    every source that exists outside the system.
    """

    universe_id: str
    base_catalog_id: str
    required_layers: tuple[DiscoveryLayerScope, ...]

    def __post_init__(self) -> None:
        _digest(self.universe_id, "discovery universe_id")
        _digest(self.base_catalog_id, "discovery universe base_catalog_id")
        if (not isinstance(self.required_layers, tuple)
                or not all(isinstance(item, DiscoveryLayerScope)
                           for item in self.required_layers)):
            raise TypeError(
                "discovery universe layers must be typed and immutable")
        if self.required_layers != tuple(sorted(
                self.required_layers, key=_scope_sort_key)):
            raise ValueError("discovery universe layers must be sorted")
        if self.universe_id != self.expected_id():
            raise ValueError("discovery universe identity does not verify")

    @classmethod
    def declare(
        cls,
        base_catalog_id: str,
        required_layers: Iterable[DiscoveryLayerScope] = (),
    ) -> "DiscoveryUniverseContract":
        """Explicitly declare scope; there is intentionally no resolver default."""
        supplied = tuple(required_layers)
        if not all(isinstance(item, DiscoveryLayerScope)
                   for item in supplied):
            raise TypeError("declared discovery layers must be typed scopes")
        layers = tuple(sorted(supplied, key=_scope_sort_key))
        payload = cls._payload(base_catalog_id, layers)
        return cls(strict_hash(payload), base_catalog_id, layers)

    @staticmethod
    def _payload(
        base_catalog_id: str,
        required_layers: tuple[DiscoveryLayerScope, ...],
    ) -> dict[str, Any]:
        return {
            "schema": "stage8r-discovery-universe-v1",
            "base_catalog_id": base_catalog_id,
            "required_layers": [item.to_dict() for item in required_layers],
        }

    def expected_id(self) -> str:
        return strict_hash(self._payload(
            self.base_catalog_id, self.required_layers))

    def verify_coverage(self, certificate: DiscoveryCertificate) -> None:
        """Reject missing, additional, or differently configured layers."""
        if not isinstance(certificate, DiscoveryCertificate):
            raise TypeError("universe coverage needs DiscoveryCertificate")
        if certificate.base_catalog_id != self.base_catalog_id:
            raise ValueError(
                "discovery certificate covers another declared base catalog")
        observed = tuple(sorted((
            DiscoveryLayerScope.from_certificate_layer(item)
            for item in certificate.layers), key=_scope_sort_key))
        if observed != self.required_layers:
            required_counts = Counter(self.required_layers)
            observed_counts = Counter(observed)
            missing = sum((required_counts - observed_counts).values())
            unexpected = sum((observed_counts - required_counts).values())
            raise ValueError(
                "discovery certificate does not cover the declared universe "
                f"(missing={missing}, unexpected={unexpected})")

    def to_dict(self) -> dict[str, Any]:
        payload = self._payload(self.base_catalog_id, self.required_layers)
        payload["universe_id"] = self.universe_id
        return payload

    @classmethod
    def from_dict(cls, value: dict[str, Any]
                  ) -> "DiscoveryUniverseContract":
        raw = require_object_fields(value, {
            "schema", "universe_id", "base_catalog_id", "required_layers",
        }, "DiscoveryUniverseContract")
        if raw.pop("schema") != "stage8r-discovery-universe-v1":
            raise ValueError("unsupported discovery universe schema")
        if not isinstance(raw["required_layers"], list):
            raise ValueError(
                "DiscoveryUniverseContract.required_layers must be an array")
        raw["required_layers"] = tuple(
            DiscoveryLayerScope.from_dict(item)
            for item in raw["required_layers"])
        return cls(**raw)


@dataclass(frozen=True)
class UpstreamCompleteness:
    """Legacy local conjunction value with no resolver adapter."""

    complete: bool
    limit_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if type(self.complete) is not bool:
            raise TypeError("upstream completeness must be bool")
        if (not isinstance(self.limit_codes, tuple)
                or any(not isinstance(value, str) or not value.strip()
                       for value in self.limit_codes)):
            raise TypeError("upstream limit codes must be non-empty text")
        if self.limit_codes != tuple(sorted(set(self.limit_codes))):
            raise ValueError("upstream limit codes must be unique and sorted")
        if self.complete and self.limit_codes:
            raise ValueError(
                "complete upstream discovery cannot report limit codes")
        if not self.complete and not self.limit_codes:
            raise ValueError(
                "incomplete upstream discovery must name its limit codes")

    @classmethod
    def whole(cls) -> "UpstreamCompleteness":
        return cls(True, ())

    @classmethod
    def from_layer(cls, complete: bool,
                   limit_codes: Iterable[str] = ()) -> "UpstreamCompleteness":
        return cls(bool(complete), tuple(sorted(set(limit_codes))))

    def merge(self, other: "UpstreamCompleteness") -> "UpstreamCompleteness":
        if not isinstance(other, UpstreamCompleteness):
            raise TypeError("merge requires an UpstreamCompleteness")
        return UpstreamCompleteness(
            self.complete and other.complete,
            tuple(sorted(set(self.limit_codes) | set(other.limit_codes))),
        )

    @classmethod
    def merge_all(cls, layers: Iterable["UpstreamCompleteness"]
                  ) -> "UpstreamCompleteness":
        result = cls.whole()
        for layer in layers:
            result = result.merge(layer)
        return result

    def to_dict(self) -> dict[str, Any]:
        return {
            "complete": self.complete,
            "limit_codes": list(self.limit_codes),
        }


__all__ = [
    "DiscoveryCertificate",
    "DiscoveryLayerCertificate",
    "DiscoveryLayerScope",
    "DiscoveryLimitActivation",
    "DiscoveryLimitSetting",
    "DiscoveryUniverseContract",
    "UpstreamCompleteness",
]
