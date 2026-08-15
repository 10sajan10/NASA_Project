"""Stage-5 source connector boundary: metadata in, never payload bytes.

The split enforced here is the load-bearing one for the whole stage.
:meth:`SourceConnector.search_metadata` returns *descriptions* of assets and
has no way to return bytes.  :meth:`SourceConnector.open_payload` returns bytes
and cannot be called without a :class:`FetchAuthorization`, which only exists
once a manifest has been bound.  The ordering invariant is therefore a type
property rather than a convention someone has to remember.
"""
from __future__ import annotations

import dataclasses
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Any

from capabilities.implementation import _digest, _required_text
from contracts import BBoxSupport, TemporalSupport
from engine.runtime.identity import require_object_fields, strict_hash

from .manifest import AssetConditionalIdentity, AssetExtent


class SourceSearchError(RuntimeError):
    """Base class for connector search and transfer failures."""


class TransientSourceError(SourceSearchError):
    """A retryable outage.

    A transient failure is retried *against the same binding*.  It never
    invalidates a manifest and never causes replanning: the assets a plan
    selected are still the assets it wants once the provider recovers.
    """


class PermanentSourceError(SourceSearchError):
    """A failure that retrying cannot fix (bad request, gone, forbidden)."""


@dataclass(frozen=True)
class CredentialRef:
    """A *reference* to a secret. The secret value never lives in this object.

    Plans, manifests, cursors, and logs serialize this reference only.
    Resolution happens at transfer time through a resolver the planner never
    holds, so no serialized Stage-5 record can leak a credential.
    """

    scheme: str
    name: str

    def __post_init__(self) -> None:
        _required_text(self.scheme, "credential scheme")
        _required_text(self.name, "credential name")
        if self.scheme not in ("env", "file", "none"):
            raise ValueError("credential scheme must be env, file, or none")

    @property
    def reference(self) -> str:
        return f"{self.scheme}:{self.name}"

    def to_dict(self) -> dict[str, Any]:
        return {"scheme": self.scheme, "name": self.name}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "CredentialRef":
        raw = require_object_fields(
            value, {field.name for field in dataclasses.fields(cls)},
            "CredentialRef")
        return cls(**raw)


@dataclass(frozen=True)
class SourceDescriptor:
    """Static, declared facts about one provider."""

    source_id: str
    provider_kind: str
    network_class: str
    credential_ref: CredentialRef
    supports_conditional_fetch: bool
    page_size: int

    def __post_init__(self) -> None:
        for value, label in (
                (self.source_id, "source_id"),
                (self.provider_kind, "provider_kind"),
                (self.network_class, "network_class")):
            _required_text(value, label)
        if not isinstance(self.credential_ref, CredentialRef):
            raise TypeError("source credential_ref must be a CredentialRef")
        if type(self.supports_conditional_fetch) is not bool:
            raise TypeError("supports_conditional_fetch must be bool")
        if (isinstance(self.page_size, bool)
                or not isinstance(self.page_size, int) or self.page_size < 1):
            raise ValueError("source page_size must be a positive integer")

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "provider_kind": self.provider_kind,
            "network_class": self.network_class,
            "credential_ref": self.credential_ref.to_dict(),
            "supports_conditional_fetch": self.supports_conditional_fetch,
            "page_size": self.page_size,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "SourceDescriptor":
        raw = require_object_fields(
            value, {field.name for field in dataclasses.fields(cls)},
            "SourceDescriptor")
        raw["credential_ref"] = CredentialRef.from_dict(raw["credential_ref"])
        return cls(**raw)


@dataclass(frozen=True)
class MetadataQuery:
    """A bounded, content-identified metadata request.

    Its ``query_id`` is what the durable planning session keys cursors on, so
    the same logical question resumes on the same page after a restart.
    """

    source_id: str
    concept_id: str
    schema_version: str
    units: str
    representation: str
    spatial: BBoxSupport
    temporal: TemporalSupport

    def __post_init__(self) -> None:
        for value, label in (
                (self.source_id, "query source_id"),
                (self.concept_id, "query concept_id"),
                (self.schema_version, "query schema_version"),
                (self.units, "query units"),
                (self.representation, "query representation")):
            _required_text(value, label)
        if not isinstance(self.spatial, BBoxSupport):
            raise TypeError("query spatial must be BBoxSupport")
        if not isinstance(self.temporal, TemporalSupport):
            raise TypeError("query temporal must be TemporalSupport")

    @property
    def query_id(self) -> str:
        return strict_hash(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "stage5-metadata-query-v1",
            "source_id": self.source_id,
            "concept_id": self.concept_id,
            "schema_version": self.schema_version,
            "units": self.units,
            "representation": self.representation,
            "spatial": self.spatial.to_dict(),
            "temporal": self.temporal.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "MetadataQuery":
        raw = require_object_fields(
            value,
            {"schema", "source_id", "concept_id", "schema_version", "units",
             "representation", "spatial", "temporal"},
            "MetadataQuery")
        if raw.pop("schema") != "stage5-metadata-query-v1":
            raise ValueError("MetadataQuery schema is not stage5-metadata-query-v1")
        raw["spatial"] = BBoxSupport.from_dict(raw["spatial"])
        raw["temporal"] = TemporalSupport.from_dict(raw["temporal"])
        return cls(**raw)


@dataclass(frozen=True)
class AssetCandidate:
    """One asset as *described* by a provider, before any binding decision.

    ``conditional_identity`` is optional here precisely so that a provider
    which cannot version its assets is representable.  Binding is where that
    becomes ``UNBINDABLE``; search does not silently drop it, because a
    scientist is entitled to know the data exists but cannot be pinned.
    """

    asset_id: str
    locator: str
    extent: AssetExtent
    byte_size: int
    conditional_identity: AssetConditionalIdentity | None = None

    def __post_init__(self) -> None:
        _required_text(self.asset_id, "candidate asset_id")
        _required_text(self.locator, "candidate locator")
        if not isinstance(self.extent, AssetExtent):
            raise TypeError("candidate extent must be AssetExtent")
        if (isinstance(self.byte_size, bool)
                or not isinstance(self.byte_size, int) or self.byte_size < 0):
            raise ValueError("candidate byte_size must be a non-negative integer")
        if self.conditional_identity is not None and not isinstance(
                self.conditional_identity, AssetConditionalIdentity):
            raise TypeError("candidate conditional identity is invalid")

    def to_dict(self) -> dict[str, Any]:
        return {
            "asset_id": self.asset_id,
            "locator": self.locator,
            "extent": self.extent.to_dict(),
            "byte_size": self.byte_size,
            "conditional_identity": (
                self.conditional_identity.to_dict()
                if self.conditional_identity is not None else None),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "AssetCandidate":
        raw = require_object_fields(
            value, {field.name for field in dataclasses.fields(cls)},
            "AssetCandidate")
        raw["extent"] = AssetExtent.from_dict(raw["extent"])
        if raw["conditional_identity"] is not None:
            raw["conditional_identity"] = AssetConditionalIdentity.from_dict(
                raw["conditional_identity"])
        return cls(**raw)


@dataclass(frozen=True)
class MetadataPage:
    """One page of metadata results plus an explicit continuation cursor."""

    candidates: tuple[AssetCandidate, ...]
    next_cursor: str | None

    def __post_init__(self) -> None:
        if (not isinstance(self.candidates, tuple)
                or not all(isinstance(item, AssetCandidate)
                           for item in self.candidates)):
            raise TypeError("metadata page candidates are invalid")
        if self.next_cursor is not None:
            _required_text(self.next_cursor, "metadata page cursor")

    @property
    def exhausted(self) -> bool:
        return self.next_cursor is None


class FetchAuthorization:
    """Proof that a manifest was bound before any byte was requested.

    Instances cannot be constructed directly.  ``acquisition.binding`` mints
    them from a bound manifest, which is the only path by which a connector
    will release payload bytes.
    """

    __slots__ = ("manifest_root", "source_id", "asset_ids")

    def __init__(self, mint: object, manifest_root: str, source_id: str,
                 asset_ids: frozenset[str]) -> None:
        if mint is not _MINT:
            raise PermissionError(
                "FetchAuthorization is minted from a bound manifest only")
        _digest(manifest_root, "authorized manifest_root")
        self.manifest_root = manifest_root
        self.source_id = source_id
        self.asset_ids = asset_ids

    def authorizes(self, source_id: str, asset_id: str) -> bool:
        return source_id == self.source_id and asset_id in self.asset_ids


_MINT = object()


def _mint_authorization(manifest_root: str, source_id: str,
                        asset_ids: frozenset[str]) -> FetchAuthorization:
    """Internal: used by :mod:`acquisition.binding` after binding succeeds."""
    return FetchAuthorization(_MINT, manifest_root, source_id, asset_ids)


class SourceConnector(ABC):
    """The two-method provider boundary.

    Implementations must never perform payload transfer inside
    ``search_metadata``.  ``bytes_transferred`` exists so tests can assert that
    property against a real implementation rather than trusting a docstring.
    """

    def __init__(self, descriptor: SourceDescriptor) -> None:
        if not isinstance(descriptor, SourceDescriptor):
            raise TypeError("connector requires a SourceDescriptor")
        self.descriptor = descriptor
        self.bytes_transferred = 0
        self.search_calls = 0

    @property
    def source_id(self) -> str:
        return self.descriptor.source_id

    @abstractmethod
    def search_metadata(self, query: MetadataQuery, cursor: str | None,
                        limit: int) -> MetadataPage:
        """Return one bounded page of asset descriptions. No payload bytes."""

    @abstractmethod
    def open_payload(self, authorization: FetchAuthorization, asset_id: str,
                     locator: str,
                     expected: AssetConditionalIdentity) -> bytes:
        """Return payload bytes for one already-bound asset.

        Implementations must verify ``expected`` against the provider's current
        conditional identity and raise :class:`AssetMutatedError` on a
        mismatch, so mutation is caught before the bytes are ever committed.
        """


class AssetMutatedError(SourceSearchError):
    """The provider's conditional identity changed after binding."""

    def __init__(self, asset_id: str, expected: str, observed: str) -> None:
        super().__init__(
            f"asset {asset_id!r} changed after binding: "
            f"expected {expected!r}, observed {observed!r}")
        self.asset_id = asset_id
        self.expected = expected
        self.observed = observed


class AssetMissingError(SourceSearchError):
    """A bound asset is no longer offered by the provider."""

    def __init__(self, asset_id: str) -> None:
        super().__init__(f"bound asset {asset_id!r} is no longer available")
        self.asset_id = asset_id


class SecretResolver(ABC):
    """Resolves a :class:`CredentialRef` at transfer time only."""

    @abstractmethod
    def resolve(self, reference: CredentialRef) -> str:
        """Return the secret value for one reference."""


class EnvironmentSecretResolver(SecretResolver):
    """Reads secrets from the process environment, never from a plan."""

    def resolve(self, reference: CredentialRef) -> str:
        import os
        if not isinstance(reference, CredentialRef):
            raise TypeError("resolve requires a CredentialRef")
        if reference.scheme == "none":
            return ""
        if reference.scheme != "env":
            raise ValueError(
                f"this resolver handles env references, not {reference.scheme!r}")
        try:
            return os.environ[reference.name]
        except KeyError as exc:
            raise PermissionError(
                f"credential {reference.reference!r} is not present") from exc


class ProviderClass(str, Enum):
    """Coarse provider families the MVP admits."""

    LOCAL_FILE = "LOCAL_FILE"
    REMOTE_HTTP = "REMOTE_HTTP"


__all__ = [
    "AssetCandidate",
    "AssetMissingError",
    "AssetMutatedError",
    "CredentialRef",
    "EnvironmentSecretResolver",
    "FetchAuthorization",
    "MetadataPage",
    "MetadataQuery",
    "PermanentSourceError",
    "ProviderClass",
    "SecretResolver",
    "SourceConnector",
    "SourceDescriptor",
    "SourceSearchError",
    "TransientSourceError",
]
