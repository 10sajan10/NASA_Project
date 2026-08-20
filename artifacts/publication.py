"""Adapter-facing declarations for native artifact publication.

This module deliberately stops before registry publication.  An adapter may
describe bytes that it produced and obtain a content-verified immutable
proposal.  A coordinator must still compare that proposal with its execution
authority and publish the enclosed record through the normal registry path.
"""
from __future__ import annotations

import hashlib
import os
import re
import stat
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from contracts import ArtifactDescriptor
from engine.runtime.identity import freeze_json, strict_copy, strict_hash

from .records import ArtifactInput, ArtifactRecord


_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_PROPOSAL_MINT = object()


def _text(value: str, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be non-empty text")


def _digest(value: str, label: str) -> None:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")


class PublicationProducerRole(str, Enum):
    """Whether an invocation is a root source or consumes artifact inputs."""

    SOURCE = "SOURCE"
    DERIVED = "DERIVED"


@dataclass(frozen=True)
class NativePublicationDeclaration:
    """Complete adapter declaration for one native output.

    ``declared_input_ports`` is independent of ``inputs`` so an adapter cannot
    accidentally make an incomplete lineage list appear complete.  The two
    port sets must match exactly.  Source invocations are the sole exception:
    they declare no input ports and no upstream artifacts.
    """

    descriptor: ArtifactDescriptor
    location: str
    media_type: str
    producer_id: str
    producer_version: str
    invocation_id: str
    output_port_id: str
    producer_role: PublicationProducerRole
    declared_input_ports: tuple[str, ...]
    inputs: tuple[ArtifactInput, ...]
    evidence_profile_id: str
    expected_content_sha256: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.descriptor, ArtifactDescriptor):
            raise TypeError(
                "native publication requires a complete ArtifactDescriptor")
        for value, label in (
            (self.location, "native publication location"),
            (self.media_type, "native publication media_type"),
            (self.producer_id, "native publication producer_id"),
            (self.producer_version, "native publication producer_version"),
            (self.invocation_id, "native publication invocation_id"),
            (self.output_port_id, "native publication output_port_id"),
            (self.evidence_profile_id,
             "native publication evidence_profile_id"),
        ):
            _text(value, label)
        if not Path(self.location).is_absolute():
            raise ValueError(
                "native publication location must be an absolute path")
        if self.media_type != self.descriptor.representation:
            raise ValueError(
                "native publication media_type must equal descriptor "
                "representation")
        if not isinstance(self.producer_role, PublicationProducerRole):
            raise TypeError("native publication producer_role must be typed")
        if (not isinstance(self.declared_input_ports, tuple)
                or any(not isinstance(value, str) or not value.strip()
                       for value in self.declared_input_ports)
                or self.declared_input_ports
                != tuple(sorted(set(self.declared_input_ports)))):
            raise ValueError(
                "declared input ports must be unique sorted non-empty text")
        if (not isinstance(self.inputs, tuple)
                or not all(isinstance(value, ArtifactInput)
                           for value in self.inputs)
                or self.inputs != tuple(sorted(
                    self.inputs,
                    key=lambda value: (value.port_id, value.artifact_id)))):
            raise ValueError(
                "native publication inputs must be typed and sorted")
        represented_ports = tuple(sorted(
            {value.port_id for value in self.inputs}))
        if represented_ports != self.declared_input_ports:
            raise ValueError(
                "native publication lineage must cover every declared input "
                "port exactly")
        if self.producer_role is PublicationProducerRole.SOURCE:
            if self.declared_input_ports or self.inputs:
                raise ValueError(
                    "a declared source producer cannot carry input lineage")
        elif not self.declared_input_ports or not self.inputs:
            raise ValueError(
                "only a declared source producer may have empty input lineage")
        if self.expected_content_sha256 is not None:
            _digest(
                self.expected_content_sha256,
                "native publication expected content digest",
            )
        frozen_metadata = freeze_json(self.metadata)
        if not isinstance(frozen_metadata, dict):
            raise TypeError("native publication metadata must be a JSON object")
        object.__setattr__(self, "metadata", frozen_metadata)

    def identity_payload(self) -> dict[str, Any]:
        return {
            "schema": "stage10d-native-publication-declaration-v1",
            "descriptor": self.descriptor.to_dict(),
            "location": self.location,
            "media_type": self.media_type,
            "producer_id": self.producer_id,
            "producer_version": self.producer_version,
            "invocation_id": self.invocation_id,
            "output_port_id": self.output_port_id,
            "producer_role": self.producer_role.value,
            "declared_input_ports": list(self.declared_input_ports),
            "inputs": [value.to_dict() for value in self.inputs],
            "evidence_profile_id": self.evidence_profile_id,
            "expected_content_sha256": self.expected_content_sha256,
            "metadata": strict_copy(self.metadata),
        }

    @property
    def declaration_id(self) -> str:
        return strict_hash(self.identity_payload())


@dataclass(frozen=True)
class NativePublicationProposal:
    """Content-verified record input, not permission to publish it."""

    proposal_id: str
    declaration_id: str
    invocation_id: str
    producer_role: PublicationProducerRole
    record: ArtifactRecord
    _mint: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._mint is not _PROPOSAL_MINT:
            raise PermissionError(
                "native publication proposals must be content-verified")
        _digest(self.proposal_id, "native publication proposal_id")
        _digest(self.declaration_id, "native publication declaration_id")
        _text(self.invocation_id, "native publication invocation_id")
        if not isinstance(self.producer_role, PublicationProducerRole):
            raise TypeError("native publication producer_role must be typed")
        if not isinstance(self.record, ArtifactRecord):
            raise TypeError("native publication proposal requires a record")
        if self.proposal_id != self.expected_id():
            raise ValueError("native publication proposal identity does not verify")

    @classmethod
    def _verified(
        cls,
        declaration: NativePublicationDeclaration,
        record: ArtifactRecord,
    ) -> "NativePublicationProposal":
        values = {
            "declaration_id": declaration.declaration_id,
            "invocation_id": declaration.invocation_id,
            "producer_role": declaration.producer_role,
            "record": record,
        }
        proposal_id = strict_hash(cls._identity_payload(**values))
        return cls(proposal_id=proposal_id, _mint=_PROPOSAL_MINT, **values)

    @staticmethod
    def _identity_payload(
        *,
        declaration_id: str,
        invocation_id: str,
        producer_role: PublicationProducerRole,
        record: ArtifactRecord,
    ) -> dict[str, Any]:
        return {
            "schema": "stage10d-native-publication-proposal-v1",
            "declaration_id": declaration_id,
            "invocation_id": invocation_id,
            "producer_role": producer_role.value,
            "record": record.to_dict(),
        }

    def expected_id(self) -> str:
        return strict_hash(self._identity_payload(
            declaration_id=self.declaration_id,
            invocation_id=self.invocation_id,
            producer_role=self.producer_role,
            record=self.record,
        ))


def _fingerprint(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _verify_native_file(path: Path) -> tuple[str, int]:
    """Hash one stable regular file without following symbolic links."""
    try:
        resolved = path.resolve(strict=True)
    except FileNotFoundError:
        raise FileNotFoundError(f"native publication file does not exist: {path}")
    if resolved != path:
        raise ValueError(
            "native publication location must be canonical and cannot contain "
            "symbolic links")
    before_path = path.lstat()
    if stat.S_ISLNK(before_path.st_mode):
        raise ValueError("native publication location cannot be a symbolic link")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("native publication location must be a regular file")
        if _fingerprint(before_path) != _fingerprint(before):
            raise RuntimeError(
                "native publication file changed while it was opened")
        digest = hashlib.sha256()
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
                digest.update(block)
        after = os.fstat(descriptor)
        after_path = path.lstat()
    finally:
        os.close(descriptor)
    fingerprint = _fingerprint(before)
    if (fingerprint != _fingerprint(after)
            or fingerprint != _fingerprint(after_path)
            or path.resolve(strict=True) != path):
        raise RuntimeError(
            "native publication file changed while its content was verified")
    return digest.hexdigest(), after.st_size


def prepare_native_publication(
    declaration: NativePublicationDeclaration,
) -> NativePublicationProposal:
    """Verify native bytes and prepare a record without registering anything."""
    if not isinstance(declaration, NativePublicationDeclaration):
        raise TypeError(
            "prepare_native_publication requires a typed declaration")
    digest, size = _verify_native_file(Path(declaration.location))
    if (declaration.expected_content_sha256 is not None
            and digest != declaration.expected_content_sha256):
        raise ValueError(
            "native publication bytes do not match the expected content digest")
    record = ArtifactRecord.bind(
        descriptor=declaration.descriptor,
        location=declaration.location,
        media_type=declaration.media_type,
        content_sha256=digest,
        size_bytes=size,
        producer_id=declaration.producer_id,
        producer_version=declaration.producer_version,
        output_port_id=declaration.output_port_id,
        inputs=declaration.inputs,
        evidence_profile_id=declaration.evidence_profile_id,
        metadata=declaration.metadata,
    )
    return NativePublicationProposal._verified(declaration, record)


__all__ = [
    "NativePublicationDeclaration",
    "NativePublicationProposal",
    "PublicationProducerRole",
    "prepare_native_publication",
]
