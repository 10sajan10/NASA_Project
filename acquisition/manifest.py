"""Immutable Stage-5 asset manifests with bounded, streaming shard access.

A manifest is the exact, frozen answer to one metadata query: *these* asset
identities, at *these* conditional versions, covering *this* extent.  It is
bound before any payload byte moves and it never mutates afterwards.

Memory boundedness is structural rather than advisory.  ``AssetManifest``
retains only shard digests and summary counts; the asset rows themselves live
in a content-addressed :class:`ManifestShardStore` and are read one shard at a
time.  A million-asset manifest therefore costs the controller the same
resident memory as a hundred-asset one.
"""
from __future__ import annotations

import dataclasses
import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Iterator

from capabilities.implementation import _digest, _required_text
from contracts import BBoxSupport, TemporalKind, TemporalSupport
from contracts.identity import decimal_value, timestamp_value
from engine.runtime.identity import (
    require_object_fields,
    strict_canonical_json,
    strict_hash,
    strict_json_loads,
)

DEFAULT_SHARD_SIZE = 256


class ConditionalIdentityKind(str, Enum):
    """How a provider lets a client detect that an asset changed.

    All three kinds are admissible for binding.  Their *absence* is what makes
    a source unbindable: without one of these there is no way to notice that
    the bytes behind a locator were replaced between planning and transfer.
    """

    CHECKSUM_SHA256 = "CHECKSUM_SHA256"
    ETAG = "ETAG"
    VERSION_ID = "VERSION_ID"


@dataclass(frozen=True)
class AssetConditionalIdentity:
    """One provider-supplied change-detection token."""

    kind: ConditionalIdentityKind
    value: str

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ConditionalIdentityKind):
            raise TypeError("conditional identity kind must be typed")
        _required_text(self.value, "conditional identity value")
        if self.kind is ConditionalIdentityKind.CHECKSUM_SHA256:
            _digest(self.value, "conditional identity checksum")

    @property
    def content_addressed(self) -> bool:
        """True when the token also proves the bytes, not merely a change."""
        return self.kind is ConditionalIdentityKind.CHECKSUM_SHA256

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind.value, "value": self.value}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "AssetConditionalIdentity":
        raw = require_object_fields(
            value, {field.name for field in dataclasses.fields(cls)},
            "AssetConditionalIdentity")
        raw["kind"] = ConditionalIdentityKind(raw["kind"])
        return cls(**raw)


@dataclass(frozen=True)
class AssetExtent:
    """The exact region and window one asset actually covers."""

    spatial: BBoxSupport
    temporal: TemporalSupport

    def __post_init__(self) -> None:
        if not isinstance(self.spatial, BBoxSupport):
            raise TypeError("asset spatial extent must be BBoxSupport")
        if not isinstance(self.temporal, TemporalSupport):
            raise TypeError("asset temporal extent must be TemporalSupport")

    def to_dict(self) -> dict[str, Any]:
        return {
            "spatial": self.spatial.to_dict(),
            "temporal": self.temporal.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "AssetExtent":
        raw = require_object_fields(
            value, {field.name for field in dataclasses.fields(cls)},
            "AssetExtent")
        return cls(
            BBoxSupport.from_dict(raw["spatial"]),
            TemporalSupport.from_dict(raw["temporal"]),
        )


@dataclass(frozen=True)
class AssetRef:
    """One immutable asset row: identity, address, version, and extent.

    ``locator`` is an opaque provider address.  It carries no credential and is
    never interpreted here; only the owning connector understands it.
    """

    asset_id: str
    source_id: str
    locator: str
    conditional_identity: AssetConditionalIdentity
    extent: AssetExtent
    byte_size: int

    def __post_init__(self) -> None:
        for value, label in (
                (self.asset_id, "asset_id"),
                (self.source_id, "asset source_id"),
                (self.locator, "asset locator")):
            _required_text(value, label)
        if not isinstance(self.conditional_identity, AssetConditionalIdentity):
            raise TypeError("asset conditional identity is invalid")
        if not isinstance(self.extent, AssetExtent):
            raise TypeError("asset extent is invalid")
        if (isinstance(self.byte_size, bool)
                or not isinstance(self.byte_size, int) or self.byte_size < 0):
            raise ValueError("asset byte_size must be a non-negative integer")

    @property
    def row_id(self) -> str:
        """Content identity of this exact asset row at this exact version."""
        return strict_hash(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "stage5-asset-ref-v1",
            "asset_id": self.asset_id,
            "source_id": self.source_id,
            "locator": self.locator,
            "conditional_identity": self.conditional_identity.to_dict(),
            "extent": self.extent.to_dict(),
            "byte_size": self.byte_size,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "AssetRef":
        raw = require_object_fields(
            value,
            {"schema", "asset_id", "source_id", "locator",
             "conditional_identity", "extent", "byte_size"},
            "AssetRef")
        if raw.pop("schema") != "stage5-asset-ref-v1":
            raise ValueError("AssetRef schema is not stage5-asset-ref-v1")
        raw["conditional_identity"] = AssetConditionalIdentity.from_dict(
            raw["conditional_identity"])
        raw["extent"] = AssetExtent.from_dict(raw["extent"])
        return cls(**raw)


@dataclass(frozen=True)
class ManifestShard:
    """A bounded, independently addressed slice of one manifest."""

    shard_index: int
    assets: tuple[AssetRef, ...]

    def __post_init__(self) -> None:
        if (isinstance(self.shard_index, bool)
                or not isinstance(self.shard_index, int)
                or self.shard_index < 0):
            raise ValueError("shard_index must be a non-negative integer")
        if (not isinstance(self.assets, tuple) or not self.assets
                or not all(isinstance(item, AssetRef) for item in self.assets)):
            raise TypeError("a manifest shard needs a non-empty AssetRef tuple")
        ids = tuple(item.asset_id for item in self.assets)
        if len(set(ids)) != len(ids):
            raise ValueError("a manifest shard cannot repeat an asset ID")

    @property
    def shard_digest(self) -> str:
        return strict_hash(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "stage5-manifest-shard-v1",
            "shard_index": self.shard_index,
            "assets": [item.to_dict() for item in self.assets],
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ManifestShard":
        raw = require_object_fields(
            value, {"schema", "shard_index", "assets"}, "ManifestShard")
        if raw.pop("schema") != "stage5-manifest-shard-v1":
            raise ValueError("ManifestShard schema is not stage5-manifest-shard-v1")
        if not isinstance(raw["assets"], list):
            raise ValueError("ManifestShard.assets must be an array")
        raw["assets"] = tuple(AssetRef.from_dict(item) for item in raw["assets"])
        return cls(**raw)


class ManifestShardStore:
    """Content-addressed on-disk home for manifest shards.

    Shards are immutable and addressed by their own digest, so writing the
    same shard twice is idempotent and a corrupted shard cannot be read back
    silently.
    """

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        if not self.root.is_absolute():
            raise ValueError("manifest shard store root must be absolute")
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, digest: str) -> Path:
        _digest(digest, "manifest shard digest")
        return self.root / digest[:2] / f"{digest}.json"

    def put(self, shard: ManifestShard) -> str:
        if not isinstance(shard, ManifestShard):
            raise TypeError("put requires a ManifestShard")
        digest = shard.shard_digest
        path = self._path(digest)
        if path.exists():
            return digest
        path.parent.mkdir(parents=True, exist_ok=True)
        encoded = strict_canonical_json(shard.to_dict())
        temporary = path.with_suffix(f".{os.getpid()}.tmp")
        temporary.write_text(encoded, encoding="utf-8")
        os.replace(temporary, path)
        return digest

    def get(self, digest: str) -> ManifestShard:
        path = self._path(digest)
        if not path.exists():
            raise KeyError(f"manifest shard {digest} is not in this store")
        shard = ManifestShard.from_dict(
            strict_json_loads(path.read_text(encoding="utf-8")))
        if shard.shard_digest != digest:
            raise ValueError(f"manifest shard {digest} failed its digest check")
        return shard

    def has(self, digest: str) -> bool:
        return self._path(digest).exists()


@dataclass(frozen=True)
class AssetManifest:
    """The frozen, exact asset set bound to one query before any transfer.

    This object is deliberately *summary only*.  Callers reach the asset rows
    through :meth:`iter_assets`, which streams one shard at a time from the
    shard store, so controller memory does not scale with asset count.
    """

    manifest_root: str
    source_id: str
    query_id: str
    shard_digests: tuple[str, ...]
    asset_count: int
    total_bytes: int
    shard_size: int

    def __post_init__(self) -> None:
        _digest(self.manifest_root, "manifest_root")
        _required_text(self.source_id, "manifest source_id")
        _digest(self.query_id, "manifest query_id")
        if (not isinstance(self.shard_digests, tuple)
                or not self.shard_digests):
            raise TypeError("a manifest needs at least one shard digest")
        for value in self.shard_digests:
            _digest(value, "manifest shard digest")
        for name in ("asset_count", "total_bytes", "shard_size"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"manifest {name} must be a non-negative integer")
        if self.shard_size < 1:
            raise ValueError("manifest shard_size must be positive")
        if self.asset_count < 1:
            raise ValueError("a manifest cannot be empty")
        if self.manifest_root != self.expected_root():
            raise ValueError("manifest root does not verify")

    def expected_root(self) -> str:
        return strict_hash(self._root_payload(
            self.source_id, self.query_id, self.shard_digests,
            self.asset_count, self.total_bytes, self.shard_size))

    @staticmethod
    def _root_payload(source_id: str, query_id: str,
                      shard_digests: tuple[str, ...], asset_count: int,
                      total_bytes: int, shard_size: int) -> dict[str, Any]:
        # The root commits to shard *order* as well as content: reordering the
        # asset sequence changes the derivation, because assembly order is
        # scientifically meaningful for a tiled artifact.
        return {
            "schema": "stage5-asset-manifest-v1",
            "source_id": source_id,
            "query_id": query_id,
            "shard_digests": list(shard_digests),
            "asset_count": asset_count,
            "total_bytes": total_bytes,
            "shard_size": shard_size,
        }

    @classmethod
    def freeze(cls, *, source_id: str, query_id: str,
               assets: Iterable[AssetRef], store: ManifestShardStore,
               shard_size: int = DEFAULT_SHARD_SIZE) -> "AssetManifest":
        """Shard, persist, and seal an asset sequence into a manifest.

        The input is consumed as a stream and only one shard is held at a
        time, so freezing a very large manifest is also bounded.
        """
        if not isinstance(store, ManifestShardStore):
            raise TypeError("freeze requires a ManifestShardStore")
        if isinstance(shard_size, bool) or not isinstance(shard_size, int) \
                or shard_size < 1:
            raise ValueError("shard_size must be a positive integer")
        digests: list[str] = []
        asset_count = 0
        total_bytes = 0
        seen: set[str] = set()
        buffer: list[AssetRef] = []
        for asset in assets:
            if not isinstance(asset, AssetRef):
                raise TypeError("freeze requires AssetRef values")
            if asset.source_id != source_id:
                raise ValueError(
                    "a manifest binds exactly one source; "
                    f"{asset.asset_id!r} belongs to {asset.source_id!r}")
            if asset.asset_id in seen:
                raise ValueError(f"duplicate asset {asset.asset_id!r} in manifest")
            seen.add(asset.asset_id)
            buffer.append(asset)
            asset_count += 1
            total_bytes += asset.byte_size
            if len(buffer) == shard_size:
                digests.append(store.put(
                    ManifestShard(len(digests), tuple(buffer))))
                buffer.clear()
        if buffer:
            digests.append(store.put(ManifestShard(len(digests), tuple(buffer))))
        if not digests:
            raise ValueError("cannot freeze an empty manifest")
        root = strict_hash(cls._root_payload(
            source_id, query_id, tuple(digests), asset_count, total_bytes,
            shard_size))
        return cls(root, source_id, query_id, tuple(digests), asset_count,
                   total_bytes, shard_size)

    def iter_shards(self, store: ManifestShardStore) -> Iterator[ManifestShard]:
        """Stream shards in bound order, holding at most one at a time."""
        if not isinstance(store, ManifestShardStore):
            raise TypeError("iter_shards requires a ManifestShardStore")
        for index, digest in enumerate(self.shard_digests):
            shard = store.get(digest)
            if shard.shard_index != index:
                raise ValueError(
                    f"manifest shard {digest} is out of bound order")
            yield shard

    def iter_assets(self, store: ManifestShardStore) -> Iterator[AssetRef]:
        for shard in self.iter_shards(store):
            yield from shard.assets

    def to_dict(self) -> dict[str, Any]:
        return {
            "manifest_root": self.manifest_root,
            "source_id": self.source_id,
            "query_id": self.query_id,
            "shard_digests": list(self.shard_digests),
            "asset_count": self.asset_count,
            "total_bytes": self.total_bytes,
            "shard_size": self.shard_size,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "AssetManifest":
        raw = require_object_fields(
            value, {field.name for field in dataclasses.fields(cls)},
            "AssetManifest")
        if not isinstance(raw["shard_digests"], list):
            raise ValueError("AssetManifest.shard_digests must be an array")
        raw["shard_digests"] = tuple(raw["shard_digests"])
        return cls(**raw)


def bbox_union_covers(assets: Iterable[AssetRef],
                      target: BBoxSupport) -> bool:
    """Report whether an axis-aligned tiling of ``assets`` contains ``target``.

    This is deliberately *not* a general polygon union.  It admits the single
    honest case Stage 5 claims: tiles that share a CRS and axis order and that
    partition the target along one axis without leaving a hole.  Anything else
    is reported as a gap rather than approximated.
    """
    tiles = [item for item in assets
             if item.extent.spatial.crs == target.crs
             and item.extent.spatial.axis_order == target.axis_order
             and item.extent.spatial.coverage_complete]
    if not tiles:
        return False
    if any(tile.extent.spatial.contains(target) for tile in tiles):
        return True
    target_bounds = tuple(decimal_value(item) for item in target.bounds)
    for axis in (0, 1):
        other = 1 - axis
        spans = []
        for tile in tiles:
            bounds = tuple(decimal_value(item)
                           for item in tile.extent.spatial.bounds)
            if (bounds[other] > target_bounds[other]
                    or bounds[other + 2] < target_bounds[other + 2]):
                continue  # does not span the full width of the other axis
            spans.append((bounds[axis], bounds[axis + 2]))
        if not spans:
            continue
        spans.sort()
        reach = target_bounds[axis]
        for low, high in spans:
            if low > reach:
                break  # a hole: nothing continues the sweep
            reach = max(reach, high)
        if reach >= target_bounds[axis + 2]:
            return True
    return False


def temporal_union_covers(assets: Iterable[AssetRef],
                          target: TemporalSupport) -> bool:
    """Report whether the asset windows jointly contain ``target``."""
    if target.kind is TemporalKind.TIME_INVARIANT:
        return any(item.extent.temporal.kind is TemporalKind.TIME_INVARIANT
                   for item in assets)
    windows = []
    for item in assets:
        support = item.extent.temporal
        if support.kind is TemporalKind.TIME_INVARIANT:
            continue
        windows.append((timestamp_value(support.start),
                        timestamp_value(support.end)))
    if not windows:
        return False
    windows.sort()
    reach = timestamp_value(target.start)
    end = timestamp_value(target.end)
    for start, stop in windows:
        if start > reach:
            break
        reach = max(reach, stop)
    return reach >= end


__all__ = [
    "DEFAULT_SHARD_SIZE",
    "AssetConditionalIdentity",
    "AssetExtent",
    "AssetManifest",
    "AssetRef",
    "ConditionalIdentityKind",
    "ManifestShard",
    "ManifestShardStore",
    "bbox_union_covers",
    "temporal_union_covers",
]
