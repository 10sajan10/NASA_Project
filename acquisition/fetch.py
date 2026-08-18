"""Payload transfer — the first place in Stage 5 that moves a byte.

Everything this module does is gated on a :class:`FetchAuthorization`, which
only a bound manifest can mint.  There is no code path from a metadata search
to a payload read.

Two failure modes are handled very differently on purpose:

* a **transient** outage retries *the same binding*.  The manifest root does
  not change, no new search runs, and nothing is replanned — the provider was
  briefly unavailable, not wrong; and
* a **mutation or disappearance** is not retried at all.  It ends the transfer
  with ``BINDING_STALE`` so the caller can record an exclusion and replan
  deliberately, rather than committing bytes that no longer match the plan.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from capabilities.implementation import _digest, _required_text
from engine.runtime.identity import (
    require_object_fields,
    strict_canonical_json,
    strict_hash,
    strict_json_loads,
)

from .binding import (
    BindingStatus,
    BindingVerification,
    BoundAssetManifest,
    StaleReason,
    StaleReasonCode,
)
from .connector import (
    AssetMissingError,
    AssetMutatedError,
    SourceConnector,
    TransientSourceError,
)
from .manifest import ManifestShardStore
from .session import PlanningSessionStore, ProviderQuota

RECEIPT_DIRECTORY = "receipts"
FETCH_LOCK_DIRECTORY = "fetch-locks"


class BindingStaleError(RuntimeError):
    """A bound asset changed or vanished; the plan may not proceed."""

    def __init__(self, verification: BindingVerification) -> None:
        super().__init__(
            "binding is stale: "
            + ", ".join(f"{item.asset_id}:{item.code.value}"
                        for item in verification.reasons))
        self.verification = verification


class FetchLeaseBusyError(RuntimeError):
    """Another same-node process owns this manifest transfer."""


class PayloadStore:
    """Content-addressed local home for transferred bytes."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        if not self.root.is_absolute():
            raise ValueError("payload store root must be absolute")
        (self.root / RECEIPT_DIRECTORY).mkdir(parents=True, exist_ok=True)
        (self.root / FETCH_LOCK_DIRECTORY).mkdir(parents=True, exist_ok=True)

    def path_for(self, digest: str) -> Path:
        _digest(digest, "payload digest")
        return self.root / digest[:2] / digest

    def put(self, payload: bytes) -> str:
        if not isinstance(payload, (bytes, bytearray)):
            raise TypeError("payload must be bytes")
        digest = hashlib.sha256(payload).hexdigest()
        path = self.path_for(digest)
        if path.exists():
            self.get(digest)
            return digest
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(f".{os.getpid()}.{secrets.token_hex(8)}.tmp")
        descriptor = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            directory = os.open(
                path.parent,
                os.O_RDONLY | int(getattr(os, "O_DIRECTORY", 0)))
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                temporary.unlink()
            raise
        return digest

    def get(self, digest: str) -> bytes:
        path = self.path_for(digest)
        if not path.exists():
            raise KeyError(f"payload {digest} is not in this store")
        payload = path.read_bytes()
        observed = hashlib.sha256(payload).hexdigest()
        if observed != digest:
            raise ValueError(f"payload {digest} failed its content check")
        return payload

    def receipt_path(self, manifest_root: str) -> Path:
        _digest(manifest_root, "receipt manifest_root")
        return self.root / RECEIPT_DIRECTORY / f"{manifest_root}.json"

    def write_receipt(self, receipt: "FetchReceipt") -> Path:
        path = self.receipt_path(receipt.manifest_root)
        temporary = path.with_suffix(f".{os.getpid()}.{secrets.token_hex(8)}.tmp")
        descriptor = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(strict_canonical_json(receipt.to_dict()))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            directory = os.open(
                path.parent,
                os.O_RDONLY | int(getattr(os, "O_DIRECTORY", 0)))
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                temporary.unlink()
            raise
        return path

    def read_receipt(self, manifest_root: str) -> "FetchReceipt":
        path = self.receipt_path(manifest_root)
        if not path.exists():
            raise KeyError(
                f"no fetch receipt for manifest {manifest_root}; payload "
                "transfer has not completed for this binding")
        return FetchReceipt.from_dict(
            strict_json_loads(path.read_text(encoding="utf-8")))

    @contextlib.contextmanager
    def fetch_lock(self, manifest_root: str):
        """Own one manifest transfer until process death or context exit.

        This is intentionally a same-node OS lock, not an expiring timestamp.
        A restarted process can take over only after the kernel has released a
        dead owner's descriptor, so a slow live transfer is never duplicated.
        """
        _digest(manifest_root, "fetch lock manifest root")
        path = self.root / FETCH_LOCK_DIRECTORY / f"{manifest_root}.lock"
        flags = os.O_RDWR | os.O_CREAT | int(getattr(os, "O_NOFOLLOW", 0))
        descriptor = os.open(path, flags, 0o600)
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise FetchLeaseBusyError(
                    f"manifest {manifest_root} already has an active fetch "
                    "owner") from exc
            yield
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)


@dataclass(frozen=True)
class FetchedAsset:
    """One exact payload blob in manifest assembly order."""

    asset_id: str
    blob_sha256: str
    byte_size: int

    def __post_init__(self) -> None:
        _required_text(self.asset_id, "fetched asset_id")
        _digest(self.blob_sha256, "fetched asset blob sha256")
        if (isinstance(self.byte_size, bool)
                or not isinstance(self.byte_size, int) or self.byte_size < 0):
            raise ValueError("fetched asset byte_size must be non-negative")

    def to_dict(self) -> dict[str, Any]:
        return {
            "asset_id": self.asset_id,
            "blob_sha256": self.blob_sha256,
            "byte_size": self.byte_size,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "FetchedAsset":
        raw = require_object_fields(
            value, {"asset_id", "blob_sha256", "byte_size"}, "FetchedAsset")
        return cls(**raw)


@dataclass(frozen=True)
class FetchReceipt:
    """What was actually transferred for one bound manifest.

    The receipt is the bridge to execution: it pins each asset's *content*
    digest, so a downstream task's result is fixed by content even when the
    provider only offered a weak ETag at binding time.
    """

    manifest_root: str
    source_id: str
    assets: tuple[FetchedAsset, ...]
    content_root: str
    total_bytes: int
    transient_retries: int

    def __post_init__(self) -> None:
        _digest(self.manifest_root, "receipt manifest_root")
        _required_text(self.source_id, "receipt source_id")
        if (not isinstance(self.assets, tuple) or not self.assets
                or not all(isinstance(item, FetchedAsset)
                           for item in self.assets)):
            raise ValueError("a receipt needs a non-empty fetched asset tuple")
        ids = tuple(item.asset_id for item in self.assets)
        if len(set(ids)) != len(ids):
            raise ValueError("a receipt cannot repeat an asset")
        if ids != tuple(sorted(ids)):
            raise ValueError("receipt assets must use canonical manifest order")
        _digest(self.content_root, "receipt content_root")
        if self.content_root != self.expected_content_root(self.assets):
            raise ValueError("fetch receipt content root does not verify")
        for name in ("total_bytes", "transient_retries"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"receipt {name} must be a non-negative integer")
        if self.total_bytes != sum(item.byte_size for item in self.assets):
            raise ValueError("fetch receipt total_bytes does not match its assets")

    @classmethod
    def bind(cls, manifest_root: str, source_id: str,
             assets: tuple[FetchedAsset, ...], transient_retries: int
             ) -> "FetchReceipt":
        return cls(
            manifest_root=manifest_root,
            source_id=source_id,
            assets=assets,
            content_root=cls.expected_content_root(assets),
            total_bytes=sum(item.byte_size for item in assets),
            transient_retries=transient_retries,
        )

    @staticmethod
    def expected_content_root(assets: tuple[FetchedAsset, ...]) -> str:
        return strict_hash({
            "schema": "stage8r-fetched-content-root-v1",
            "assets": [item.to_dict() for item in assets],
        })

    @property
    def receipt_id(self) -> str:
        # Retry count is operational attribution, not scientific content.
        return strict_hash({
            "schema": "stage8r-fetch-receipt-identity-v1",
            "manifest_root": self.manifest_root,
            "source_id": self.source_id,
            "content_root": self.content_root,
            "assets": [item.to_dict() for item in self.assets],
            "total_bytes": self.total_bytes,
        })

    @property
    def asset_ids(self) -> tuple[str, ...]:
        return tuple(item.asset_id for item in self.assets)

    @property
    def asset_digests(self) -> tuple[tuple[str, str], ...]:
        """Compatibility view; new identities use full asset records."""
        return tuple((item.asset_id, item.blob_sha256) for item in self.assets)

    def digest_for(self, asset_id: str) -> str:
        for item in self.assets:
            if item.asset_id == asset_id:
                return item.blob_sha256
        raise KeyError(f"asset {asset_id!r} is not in this receipt")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "stage8r-fetch-receipt-v2",
            "receipt_id": self.receipt_id,
            "manifest_root": self.manifest_root,
            "source_id": self.source_id,
            "assets": [item.to_dict() for item in self.assets],
            "content_root": self.content_root,
            "total_bytes": self.total_bytes,
            "transient_retries": self.transient_retries,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "FetchReceipt":
        raw = require_object_fields(
            value,
            {"schema", "receipt_id", "manifest_root", "source_id", "assets",
             "content_root", "total_bytes", "transient_retries"},
            "FetchReceipt")
        if raw.pop("schema") != "stage8r-fetch-receipt-v2":
            raise ValueError("FetchReceipt schema is not stage8r-fetch-receipt-v2")
        receipt_id = raw.pop("receipt_id")
        _digest(receipt_id, "serialized fetch receipt_id")
        if not isinstance(raw["assets"], list):
            raise ValueError("FetchReceipt.assets must be an array")
        raw["assets"] = tuple(FetchedAsset.from_dict(item)
                              for item in raw["assets"])
        receipt = cls(**raw)
        if receipt.receipt_id != receipt_id:
            raise ValueError("fetch receipt identity does not verify")
        return receipt


def verify_fetch_receipt(
    bound: BoundAssetManifest,
    receipt: FetchReceipt,
    payload_store: PayloadStore,
    shard_store: ManifestShardStore,
) -> FetchReceipt:
    """Verify a cached receipt against binding, manifest, and every blob."""
    if receipt.manifest_root != bound.manifest_root:
        raise ValueError("cached receipt manifest root does not match binding")
    if receipt.source_id != bound.source_id:
        raise ValueError("cached receipt source does not match binding")
    if receipt.asset_ids != bound.asset_ids:
        raise ValueError("cached receipt asset order/set does not match binding")
    manifest_assets = tuple(
        item for item in bound.manifest.iter_assets(shard_store)
        if item.asset_id in set(bound.asset_ids))
    if tuple(item.asset_id for item in manifest_assets) != bound.asset_ids:
        raise ValueError("manifest asset order does not match bound coverage")
    for expected, fetched in zip(manifest_assets, receipt.assets):
        if fetched.byte_size != expected.byte_size:
            raise ValueError(
                f"cached receipt size for {fetched.asset_id!r} disagrees "
                "with the manifest")
        payload = payload_store.get(fetched.blob_sha256)
        if len(payload) != fetched.byte_size:
            raise ValueError(
                f"cached blob size for {fetched.asset_id!r} does not verify")
        if (expected.conditional_identity.content_addressed
                and expected.conditional_identity.value
                != fetched.blob_sha256):
            raise ValueError(
                f"cached blob for {fetched.asset_id!r} violates its "
                "content-addressed manifest identity")
    return receipt


class PayloadFetcher:
    """Transfers payload for an already-bound manifest, under shared quota."""

    def __init__(
        self,
        session_store: PlanningSessionStore,
        payload_store: PayloadStore,
        *,
        quota: ProviderQuota = ProviderQuota(),
        max_transient_retries: int = 3,
        sleep: Callable[[float], None] = time.sleep,
        retry_backoff_s: float = 0.0,
        failpoint: Callable[[str], None] | None = None,
    ) -> None:
        if not isinstance(session_store, PlanningSessionStore):
            raise TypeError("fetcher requires a PlanningSessionStore")
        if not isinstance(payload_store, PayloadStore):
            raise TypeError("fetcher requires a PayloadStore")
        if (isinstance(max_transient_retries, bool)
                or not isinstance(max_transient_retries, int)
                or max_transient_retries < 0):
            raise ValueError("max_transient_retries must be a non-negative int")
        self.session_store = session_store
        self.payload_store = payload_store
        self.quota = quota
        self.max_transient_retries = max_transient_retries
        self._sleep = sleep
        self._backoff = float(retry_backoff_s)
        if failpoint is not None and not callable(failpoint):
            raise TypeError("fetch failpoint must be callable")
        self._failpoint = failpoint

    def _hit(self, event: str) -> None:
        if self._failpoint is not None:
            self._failpoint(event)

    def fetch(self, bound: BoundAssetManifest, connector: SourceConnector,
              shard_store: ManifestShardStore) -> FetchReceipt:
        """Transfer every selected asset for one binding and receipt it.

        Re-running this for a manifest that already has a receipt is a no-op:
        the binding is immutable, so the transfer is idempotent.
        """
        if not isinstance(bound, BoundAssetManifest):
            raise TypeError("fetch requires a BoundAssetManifest")
        if connector.source_id != bound.source_id:
            raise ValueError(
                f"connector {connector.source_id!r} does not own manifest "
                f"source {bound.source_id!r}")
        with self.payload_store.fetch_lock(bound.manifest_root):
            existing = self.payload_store.receipt_path(bound.manifest_root)
            if existing.exists():
                receipt = verify_fetch_receipt(
                    bound, self.payload_store.read_receipt(bound.manifest_root),
                    self.payload_store, shard_store)
                self.session_store.reconcile_fetch_receipt(bound.manifest_root)
                return receipt

            manifest_assets = tuple(bound.manifest.iter_assets(shard_store))
            if tuple(item.asset_id for item in manifest_assets) != bound.asset_ids:
                raise ValueError(
                    "manifest asset order does not match the bound transfer")
            owner_token = secrets.token_hex(32)
            self.session_store.initialize_fetch_transfer(
                bound.manifest_root, bound.source_id, owner_token, self.quota,
                tuple((item.asset_id, item.byte_size)
                      for item in manifest_assets),
            )
            self._hit("after_transfer_initialized")

            authorization = bound.authorization()
            fetched_assets: list[FetchedAsset] = []
            stale: list[StaleReason] = []

            for asset in manifest_assets:
                checkpoint = self.session_store.fetch_checkpoint(
                    bound.manifest_root, asset.asset_id)
                if checkpoint.complete:
                    payload = self.payload_store.get(checkpoint.blob_sha256)
                    if len(payload) != checkpoint.byte_size \
                            or checkpoint.byte_size != asset.byte_size:
                        raise ValueError(
                            f"fetch checkpoint for {asset.asset_id!r} is corrupt")
                    if (asset.conditional_identity.content_addressed
                            and checkpoint.blob_sha256
                            != asset.conditional_identity.value):
                        raise ValueError(
                            f"fetch checkpoint for {asset.asset_id!r} violates "
                            "its manifest content identity")
                    fetched_assets.append(FetchedAsset(
                        asset.asset_id, checkpoint.blob_sha256,
                        checkpoint.byte_size))
                    continue

                payload: bytes | None = None
                retry = 0
                while True:
                    self.session_store.begin_fetch_asset_attempt(
                        bound.manifest_root, owner_token, asset.asset_id)
                    self._hit(f"after_asset_attempt_started:{asset.asset_id}")
                    try:
                        payload = connector.open_payload(
                            authorization, asset.asset_id, asset.locator,
                            asset.conditional_identity)
                        break
                    except TransientSourceError:
                        # Every retry is a separate pre-charged attempt. The
                        # same immutable binding is retained; no discovery runs.
                        if retry >= self.max_transient_retries:
                            raise
                        retry += 1
                        if self._backoff:
                            self._sleep(self._backoff * retry)
                    except AssetMutatedError as exc:
                        stale.append(StaleReason(
                            StaleReasonCode.ASSET_MUTATED, asset.asset_id,
                            asset.conditional_identity.value, exc.observed))
                        break
                    except AssetMissingError:
                        stale.append(StaleReason(
                            StaleReasonCode.ASSET_MISSING, asset.asset_id,
                            asset.conditional_identity.value, ""))
                        break
                if payload is None:
                    continue
                if len(payload) != asset.byte_size:
                    stale.append(StaleReason(
                        StaleReasonCode.ASSET_SIZE_MISMATCH,
                        asset.asset_id, str(asset.byte_size), str(len(payload))))
                    continue
                digest = self.payload_store.put(payload)
                self._hit(f"after_blob_persisted:{asset.asset_id}")
                if (asset.conditional_identity.content_addressed
                        and digest != asset.conditional_identity.value):
                    stale.append(StaleReason(
                        StaleReasonCode.ASSET_MUTATED, asset.asset_id,
                        asset.conditional_identity.value, digest))
                    continue
                checkpoint = self.session_store.complete_fetch_asset(
                    bound.manifest_root, owner_token, asset.asset_id,
                    digest, len(payload))
                self._hit(f"after_asset_checkpoint:{asset.asset_id}")
                fetched_assets.append(FetchedAsset(
                    checkpoint.asset_id, checkpoint.blob_sha256,
                    checkpoint.byte_size))

            if stale:
                raise BindingStaleError(BindingVerification(
                    BindingStatus.BINDING_STALE,
                    tuple(sorted(stale, key=lambda item: item.asset_id))))
            receipt = FetchReceipt.bind(
                bound.manifest_root, bound.source_id, tuple(fetched_assets),
                self.session_store.fetch_retry_count(bound.manifest_root))
            verify_fetch_receipt(bound, receipt, self.payload_store, shard_store)
            self.payload_store.write_receipt(receipt)
            self._hit("after_receipt_persisted")
            self.session_store.complete_fetch_transfer(
                bound.manifest_root, owner_token)
            return receipt


__all__ = [
    "BindingStaleError",
    "FetchLeaseBusyError",
    "FetchedAsset",
    "FetchReceipt",
    "PayloadFetcher",
    "PayloadStore",
    "RECEIPT_DIRECTORY",
    "FETCH_LOCK_DIRECTORY",
    "verify_fetch_receipt",
]
