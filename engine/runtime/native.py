"""Closed JSON declaration for a native file produced outside object storage."""
from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .identity import (
    freeze_json,
    require_object_fields,
    strict_copy,
    strict_hash,
)


NATIVE_FILE_POINTER_VALIDATOR_KIND = "native_file_pointer_v1"


def _fingerprint(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _read_verified(
        path: str | Path, *, content_sha256: str, size_bytes: int,
        retain_bytes: bool,
) -> bytes | None:
    """Open one stable regular file without following its final component."""
    location = Path(path)
    before_path = location.lstat()
    if stat.S_ISLNK(before_path.st_mode):
        raise ValueError("native artifact cannot be a symbolic link")
    flags = os.O_RDONLY | int(getattr(os, "O_CLOEXEC", 0))
    flags |= int(getattr(os, "O_NOFOLLOW", 0))
    descriptor = os.open(location, flags)
    blocks: list[bytes] | None = [] if retain_bytes else None
    digest = hashlib.sha256()
    observed_size = 0
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("native artifact is not a regular file")
        if _fingerprint(before_path) != _fingerprint(before):
            raise RuntimeError("native artifact changed while it was opened")
        while True:
            block = os.read(descriptor, 4 * 1024 * 1024)
            if not block:
                break
            digest.update(block)
            observed_size += len(block)
            if blocks is not None:
                blocks.append(block)
        after = os.fstat(descriptor)
        after_path = location.lstat()
    finally:
        os.close(descriptor)
    observed = _fingerprint(after)
    if (observed != _fingerprint(before)
            or observed != _fingerprint(after_path)):
        raise RuntimeError("native artifact changed during verification")
    if (isinstance(size_bytes, bool) or not isinstance(size_bytes, int)
            or size_bytes < 0 or observed_size != size_bytes):
        raise ValueError("native artifact size does not verify")
    if digest.hexdigest() != content_sha256:
        raise ValueError("native artifact content does not verify")
    return b"".join(blocks) if blocks is not None else None


def verify_native_file(
        path: str | Path, *, content_sha256: str, size_bytes: int,
) -> None:
    """Verify exact stable bytes without retaining or relocating them."""
    _read_verified(
        path, content_sha256=content_sha256, size_bytes=size_bytes,
        retain_bytes=False)


def read_verified_native_bytes(
        path: str | Path, *, content_sha256: str, size_bytes: int,
) -> bytes:
    """Read the exact bytes verified by the same no-follow file descriptor."""
    value = _read_verified(
        path, content_sha256=content_sha256, size_bytes=size_bytes,
        retain_bytes=True)
    assert value is not None
    return value


@dataclass(frozen=True)
class NativeFilePointer:
    """Exact bytes at a producer-owned absolute path.

    Scientific meaning is deliberately absent here. It comes from the output
    recipe's independently identity-checked ``ScientificArtifactBinding``.
    """

    pointer_id: str
    path: str
    media_type: str
    content_sha256: str
    size_bytes: int
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.path, str) or not Path(self.path).is_absolute():
            raise ValueError("native file pointer path must be absolute")
        if not isinstance(self.media_type, str) or not self.media_type.strip():
            raise ValueError("native file pointer media_type must be text")
        if (not isinstance(self.content_sha256, str)
                or len(self.content_sha256) != 64
                or any(value not in "0123456789abcdef"
                       for value in self.content_sha256)):
            raise ValueError("native file pointer digest must be SHA-256")
        if (isinstance(self.size_bytes, bool)
                or not isinstance(self.size_bytes, int)
                or self.size_bytes < 0):
            raise ValueError("native file pointer size must be non-negative")
        object.__setattr__(self, "metadata", freeze_json(self.metadata))
        if not isinstance(self.metadata, dict):
            raise TypeError("native file pointer metadata must be an object")
        if self.pointer_id != self.expected_id():
            raise ValueError("native file pointer identity does not verify")

    @classmethod
    def bind(cls, path: str | Path, media_type: str, *,
             content_sha256: str, size_bytes: int,
             metadata: dict[str, Any] | None = None) -> "NativeFilePointer":
        values = {
            "path": str(Path(path).resolve()),
            "media_type": media_type,
            "content_sha256": content_sha256,
            "size_bytes": size_bytes,
            "metadata": strict_copy(metadata or {}),
        }
        return cls(strict_hash(cls._payload(values)), **values)

    @staticmethod
    def _payload(values: dict[str, Any]) -> dict[str, Any]:
        return {"schema": "stage10c-native-file-pointer-v1", **values}

    def expected_id(self) -> str:
        return strict_hash(self._payload({
            "path": self.path,
            "media_type": self.media_type,
            "content_sha256": self.content_sha256,
            "size_bytes": self.size_bytes,
            "metadata": strict_copy(self.metadata),
        }))

    def verify_file(self) -> None:
        verify_native_file(
            self.path,
            content_sha256=self.content_sha256,
            size_bytes=self.size_bytes,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "pointer_id": self.pointer_id,
            "path": self.path,
            "media_type": self.media_type,
            "content_sha256": self.content_sha256,
            "size_bytes": self.size_bytes,
            "metadata": strict_copy(self.metadata),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "NativeFilePointer":
        return cls(**require_object_fields(value, {
            "pointer_id", "path", "media_type", "content_sha256",
            "size_bytes", "metadata",
        }, "NativeFilePointer"))


__all__ = [
    "NATIVE_FILE_POINTER_VALIDATOR_KIND",
    "NativeFilePointer",
    "read_verified_native_bytes",
    "verify_native_file",
]
