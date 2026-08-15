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

import hashlib
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from capabilities.implementation import _digest, _required_text
from engine.runtime.identity import (
    require_object_fields,
    strict_canonical_json,
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


class BindingStaleError(RuntimeError):
    """A bound asset changed or vanished; the plan may not proceed."""

    def __init__(self, verification: BindingVerification) -> None:
        super().__init__(
            "binding is stale: "
            + ", ".join(f"{item.asset_id}:{item.code.value}"
                        for item in verification.reasons))
        self.verification = verification


class PayloadStore:
    """Content-addressed local home for transferred bytes."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        if not self.root.is_absolute():
            raise ValueError("payload store root must be absolute")
        (self.root / RECEIPT_DIRECTORY).mkdir(parents=True, exist_ok=True)

    def path_for(self, digest: str) -> Path:
        _digest(digest, "payload digest")
        return self.root / digest[:2] / digest

    def put(self, payload: bytes) -> str:
        if not isinstance(payload, (bytes, bytearray)):
            raise TypeError("payload must be bytes")
        digest = hashlib.sha256(payload).hexdigest()
        path = self.path_for(digest)
        if path.exists():
            return digest
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(f".{os.getpid()}.tmp")
        temporary.write_bytes(payload)
        os.replace(temporary, path)
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
        temporary = path.with_suffix(f".{os.getpid()}.tmp")
        temporary.write_text(
            strict_canonical_json(receipt.to_dict()), encoding="utf-8")
        os.replace(temporary, path)
        return path

    def read_receipt(self, manifest_root: str) -> "FetchReceipt":
        path = self.receipt_path(manifest_root)
        if not path.exists():
            raise KeyError(
                f"no fetch receipt for manifest {manifest_root}; payload "
                "transfer has not completed for this binding")
        return FetchReceipt.from_dict(
            strict_json_loads(path.read_text(encoding="utf-8")))


@dataclass(frozen=True)
class FetchReceipt:
    """What was actually transferred for one bound manifest.

    The receipt is the bridge to execution: it pins each asset's *content*
    digest, so a downstream task's result is fixed by content even when the
    provider only offered a weak ETag at binding time.
    """

    manifest_root: str
    source_id: str
    asset_digests: tuple[tuple[str, str], ...]
    total_bytes: int
    transient_retries: int

    def __post_init__(self) -> None:
        _digest(self.manifest_root, "receipt manifest_root")
        _required_text(self.source_id, "receipt source_id")
        if not isinstance(self.asset_digests, tuple) or not self.asset_digests:
            raise ValueError("a receipt needs at least one asset digest")
        for entry in self.asset_digests:
            if not isinstance(entry, tuple) or len(entry) != 2:
                raise TypeError("asset digests must be (asset_id, sha256) pairs")
            _required_text(entry[0], "receipt asset_id")
            _digest(entry[1], "receipt asset digest")
        ids = tuple(item[0] for item in self.asset_digests)
        if len(set(ids)) != len(ids):
            raise ValueError("a receipt cannot repeat an asset")
        for name in ("total_bytes", "transient_retries"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"receipt {name} must be a non-negative integer")

    @property
    def asset_ids(self) -> tuple[str, ...]:
        return tuple(item[0] for item in self.asset_digests)

    def digest_for(self, asset_id: str) -> str:
        for candidate, digest in self.asset_digests:
            if candidate == asset_id:
                return digest
        raise KeyError(f"asset {asset_id!r} is not in this receipt")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "stage5-fetch-receipt-v1",
            "manifest_root": self.manifest_root,
            "source_id": self.source_id,
            "asset_digests": [list(item) for item in self.asset_digests],
            "total_bytes": self.total_bytes,
            "transient_retries": self.transient_retries,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "FetchReceipt":
        raw = require_object_fields(
            value,
            {"schema", "manifest_root", "source_id", "asset_digests",
             "total_bytes", "transient_retries"},
            "FetchReceipt")
        if raw.pop("schema") != "stage5-fetch-receipt-v1":
            raise ValueError("FetchReceipt schema is not stage5-fetch-receipt-v1")
        if not isinstance(raw["asset_digests"], list):
            raise ValueError("FetchReceipt.asset_digests must be an array")
        raw["asset_digests"] = tuple(
            (item[0], item[1]) for item in raw["asset_digests"])
        return cls(**raw)


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
        existing = self.payload_store.receipt_path(bound.manifest_root)
        if existing.exists():
            return self.payload_store.read_receipt(bound.manifest_root)

        authorization = bound.authorization()
        selected = set(bound.asset_ids)
        digests: list[tuple[str, str]] = []
        total_bytes = 0
        retries = 0
        stale: list[StaleReason] = []

        for asset in bound.manifest.iter_assets(shard_store):
            if asset.asset_id not in selected:
                continue
            payload: bytes | None = None
            attempt = 0
            while True:
                try:
                    payload = connector.open_payload(
                        authorization, asset.asset_id, asset.locator,
                        asset.conditional_identity)
                    break
                except TransientSourceError:
                    # The same binding is retried; nothing about the plan or
                    # the manifest changes because the provider blinked.
                    if attempt >= self.max_transient_retries:
                        raise
                    attempt += 1
                    retries += 1
                    if self._backoff:
                        self._sleep(self._backoff * attempt)
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
            self.session_store.debit_quota(
                bound.source_id, self.quota, payload_calls=1,
                transferred_bytes=len(payload))
            digest = self.payload_store.put(payload)
            if asset.conditional_identity.content_addressed and \
                    digest != asset.conditional_identity.value:
                stale.append(StaleReason(
                    StaleReasonCode.ASSET_MUTATED, asset.asset_id,
                    asset.conditional_identity.value, digest))
                continue
            digests.append((asset.asset_id, digest))
            total_bytes += len(payload)

        if stale:
            raise BindingStaleError(BindingVerification(
                BindingStatus.BINDING_STALE,
                tuple(sorted(stale, key=lambda item: item.asset_id))))
        receipt = FetchReceipt(
            bound.manifest_root, bound.source_id, tuple(digests), total_bytes,
            retries)
        self.payload_store.write_receipt(receipt)
        return receipt


__all__ = [
    "BindingStaleError",
    "FetchReceipt",
    "PayloadFetcher",
    "PayloadStore",
    "RECEIPT_DIRECTORY",
]
