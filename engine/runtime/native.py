"""Closed JSON declaration for a native file produced outside object storage."""
from __future__ import annotations

import hashlib
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
        path = Path(self.path)
        if path.is_symlink():
            raise ValueError("native output cannot be a symbolic link")
        before = path.stat()
        if not path.is_file():
            raise ValueError("native output is not a regular file")
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
                digest.update(block)
        after = path.stat()
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            raise RuntimeError("native output changed during verification")
        if after.st_size != self.size_bytes:
            raise ValueError("native output size does not verify")
        if digest.hexdigest() != self.content_sha256:
            raise ValueError("native output content does not verify")

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


__all__ = ["NATIVE_FILE_POINTER_VALIDATOR_KIND", "NativeFilePointer"]
