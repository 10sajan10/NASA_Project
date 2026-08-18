"""Post-fetch scientific identity for acquisition producers.

A manifest identifies provider promises.  A :class:`FetchedContentBinding`
identifies the bytes actually present in the local content-addressed store and
is therefore the earliest object from which an executable producer may be
lowered.  Construction is gated on receipt and blob verification; callers
cannot instantiate an authority from metadata alone.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from capabilities.implementation import _digest, _required_text
from contracts import ArtifactDescriptor
from engine.runtime.identity import require_object_fields, strict_hash

from .binding import BoundAssetManifest
from .fetch import (
    FetchReceipt,
    FetchedAsset,
    PayloadStore,
    verify_fetch_receipt,
)
from .manifest import AssetRef, ManifestShardStore
from .schema import AssemblyMode

_MINT = object()


@dataclass(frozen=True, init=False)
class FetchedContentBinding:
    """Verified exact bytes plus their derived scientific descriptor."""

    binding_id: str
    manifest_root: str
    source_id: str
    receipt_id: str
    content_root: str
    assets: tuple[FetchedAsset, ...]
    source_schema_id: str
    coverage_contract_id: str
    assembly_mode: AssemblyMode
    descriptor: ArtifactDescriptor
    bound_manifest: BoundAssetManifest
    manifest_assets: tuple[AssetRef, ...]

    def __init__(
        self,
        mint: object,
        *,
        binding_id: str,
        manifest_root: str,
        source_id: str,
        receipt_id: str,
        content_root: str,
        assets: tuple[FetchedAsset, ...],
        source_schema_id: str,
        coverage_contract_id: str,
        assembly_mode: AssemblyMode,
        descriptor: ArtifactDescriptor,
        bound_manifest: BoundAssetManifest,
        manifest_assets: tuple[AssetRef, ...],
    ) -> None:
        if mint is not _MINT:
            raise PermissionError(
                "FetchedContentBinding must be minted by verified payload "
                "receipt and blob checks")
        for name, value in (
                ("binding_id", binding_id), ("manifest_root", manifest_root),
                ("receipt_id", receipt_id), ("content_root", content_root),
                ("source_schema_id", source_schema_id),
                ("coverage_contract_id", coverage_contract_id)):
            _digest(value, f"fetched content {name}")
        _required_text(source_id, "fetched content source_id")
        if (not isinstance(assets, tuple) or not assets
                or not all(isinstance(item, FetchedAsset) for item in assets)):
            raise TypeError("fetched content assets must be a non-empty tuple")
        if not isinstance(assembly_mode, AssemblyMode):
            raise TypeError("fetched content assembly_mode must be typed")
        if not isinstance(descriptor, ArtifactDescriptor):
            raise TypeError("fetched content descriptor must be typed")
        if not isinstance(bound_manifest, BoundAssetManifest):
            raise TypeError("fetched content must retain its bound manifest proof")
        if (not isinstance(manifest_assets, tuple) or not manifest_assets
                or not all(isinstance(item, AssetRef)
                           for item in manifest_assets)):
            raise TypeError(
                "fetched content manifest assets must be a non-empty typed tuple")
        if bound_manifest.manifest_root != manifest_root:
            raise ValueError("fetched content bound manifest root disagrees")
        if bound_manifest.source_id != source_id:
            raise ValueError("fetched content bound manifest source disagrees")
        if bound_manifest.source_schema.schema_id != source_schema_id:
            raise ValueError("fetched content bound source schema disagrees")
        if bound_manifest.coverage_contract_id != coverage_contract_id:
            raise ValueError("fetched content coverage contract disagrees")
        if bound_manifest.source_schema.assembly_mode is not assembly_mode:
            raise ValueError("fetched content assembly mode disagrees")
        if bound_manifest.source_schema.derive_descriptor(
                bound_manifest.target_spatial,
                bound_manifest.target_temporal) != descriptor:
            raise ValueError(
                "fetched content descriptor is not derived from its source schema")
        manifest_ids = tuple(item.asset_id for item in manifest_assets)
        if manifest_ids != bound_manifest.asset_ids:
            raise ValueError(
                "fetched content manifest rows disagree with bound coverage")
        if manifest_ids != tuple(item.asset_id for item in assets):
            raise ValueError(
                "fetched content manifest rows disagree with fetched assets")
        values = {
            "binding_id": binding_id,
            "manifest_root": manifest_root,
            "source_id": source_id,
            "receipt_id": receipt_id,
            "content_root": content_root,
            "assets": assets,
            "source_schema_id": source_schema_id,
            "coverage_contract_id": coverage_contract_id,
            "assembly_mode": assembly_mode,
            "descriptor": descriptor,
            "bound_manifest": bound_manifest,
            "manifest_assets": manifest_assets,
        }
        for name, value in values.items():
            object.__setattr__(self, name, value)
        if binding_id != self.expected_id():
            raise ValueError("fetched content binding identity does not verify")

    @classmethod
    def bind(
        cls,
        bound: BoundAssetManifest,
        receipt: FetchReceipt,
        payload_store: PayloadStore,
        shard_store: ManifestShardStore,
    ) -> "FetchedContentBinding":
        if not isinstance(bound, BoundAssetManifest):
            raise TypeError("fetched content binding needs a BoundAssetManifest")
        verified = verify_fetch_receipt(
            bound, receipt, payload_store, shard_store)
        descriptor = bound.source_schema.derive_descriptor(
            bound.target_spatial, bound.target_temporal)
        manifest_assets = tuple(bound.manifest.iter_assets(shard_store))
        payload = cls._identity_payload(
            bound.manifest_root,
            bound.source_id,
            verified.receipt_id,
            verified.content_root,
            verified.assets,
            bound.source_schema.schema_id,
            bound.coverage_contract_id,
            bound.source_schema.assembly_mode,
            descriptor,
        )
        return cls(
            _MINT,
            binding_id=strict_hash(payload),
            manifest_root=bound.manifest_root,
            source_id=bound.source_id,
            receipt_id=verified.receipt_id,
            content_root=verified.content_root,
            assets=verified.assets,
            source_schema_id=bound.source_schema.schema_id,
            coverage_contract_id=bound.coverage_contract_id,
            assembly_mode=bound.source_schema.assembly_mode,
            descriptor=descriptor,
            bound_manifest=bound,
            manifest_assets=manifest_assets,
        )

    @staticmethod
    def _identity_payload(
        manifest_root: str,
        source_id: str,
        receipt_id: str,
        content_root: str,
        assets: tuple[FetchedAsset, ...],
        source_schema_id: str,
        coverage_contract_id: str,
        assembly_mode: AssemblyMode,
        descriptor: ArtifactDescriptor,
    ) -> dict[str, Any]:
        return {
            "schema": "stage8r-fetched-content-binding-v1",
            "manifest_root": manifest_root,
            "source_id": source_id,
            "receipt_id": receipt_id,
            "content_root": content_root,
            "assets": [item.to_dict() for item in assets],
            "source_schema_id": source_schema_id,
            "coverage_contract_id": coverage_contract_id,
            "assembly_mode": assembly_mode.value,
            "descriptor": descriptor.to_dict(),
        }

    def expected_id(self) -> str:
        return strict_hash(self._identity_payload(
            self.manifest_root,
            self.source_id,
            self.receipt_id,
            self.content_root,
            self.assets,
            self.source_schema_id,
            self.coverage_contract_id,
            self.assembly_mode,
            self.descriptor,
        ))

    def to_dict(self) -> dict[str, Any]:
        payload = self._identity_payload(
            self.manifest_root,
            self.source_id,
            self.receipt_id,
            self.content_root,
            self.assets,
            self.source_schema_id,
            self.coverage_contract_id,
            self.assembly_mode,
            self.descriptor,
        )
        payload["binding_id"] = self.binding_id
        return payload

    @classmethod
    def load_verified(
        cls,
        value: dict[str, Any],
        *,
        bound: BoundAssetManifest,
        receipt: FetchReceipt,
        payload_store: PayloadStore,
        shard_store: ManifestShardStore,
    ) -> "FetchedContentBinding":
        """Reload serialized identity only by replaying local verification."""
        raw = require_object_fields(
            value,
            {"schema", "binding_id", "manifest_root", "source_id",
             "receipt_id", "content_root", "assets", "source_schema_id",
             "coverage_contract_id", "assembly_mode", "descriptor"},
            "FetchedContentBinding")
        if raw["schema"] != "stage8r-fetched-content-binding-v1":
            raise ValueError("unknown fetched content binding schema")
        expected = cls.bind(bound, receipt, payload_store, shard_store)
        if expected.to_dict() != value:
            raise ValueError(
                "serialized fetched content binding does not match verified "
                "receipt, blobs, schema, and coverage")
        return expected


__all__ = ["FetchedContentBinding"]
