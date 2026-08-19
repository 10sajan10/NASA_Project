"""Durable, queryable registry for native artifact pointers."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, Iterable

from contracts import (
    ArtifactDescriptor,
    CompatibilityProof,
    EvidenceProfile,
    EvidenceSnapshot,
    Requirement,
    direct_match,
)
from engine.runtime.identity import strict_canonical_json

from .records import (
    ArtifactAvailability,
    ArtifactInput,
    ArtifactRecord,
    ArtifactRegistrySnapshot,
    ArtifactSnapshotEntry,
)


_SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS artifact_records (
    record_id       TEXT PRIMARY KEY,
    artifact_id     TEXT NOT NULL,
    leaf_id         TEXT NOT NULL UNIQUE,
    concept_id      TEXT NOT NULL,
    location        TEXT NOT NULL,
    record_json     TEXT NOT NULL,
    registered_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS artifact_records_concept
    ON artifact_records(concept_id);
CREATE INDEX IF NOT EXISTS artifact_records_artifact
    ON artifact_records(artifact_id);
CREATE TABLE IF NOT EXISTS artifact_search_index (
    record_id        TEXT PRIMARY KEY REFERENCES artifact_records(record_id),
    concept_id       TEXT NOT NULL,
    descriptor_id    TEXT NOT NULL,
    schema_version   TEXT NOT NULL,
    representation   TEXT NOT NULL,
    units            TEXT NOT NULL,
    spatial_min_x    TEXT NOT NULL,
    spatial_min_y    TEXT NOT NULL,
    spatial_max_x    TEXT NOT NULL,
    spatial_max_y    TEXT NOT NULL,
    spatial_crs      TEXT NOT NULL,
    temporal_kind    TEXT NOT NULL,
    temporal_start   TEXT,
    temporal_end     TEXT,
    vertical_kind    TEXT,
    grid_crs         TEXT,
    native_resolution_value TEXT,
    native_resolution_unit  TEXT,
    origin           TEXT NOT NULL,
    component_names_json TEXT NOT NULL,
    producer_id      TEXT NOT NULL,
    producer_version TEXT NOT NULL,
    output_port_id   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS artifact_search_science
    ON artifact_search_index(concept_id, schema_version, representation, units);
CREATE INDEX IF NOT EXISTS artifact_search_producer
    ON artifact_search_index(producer_id, producer_version, output_port_id);
CREATE TABLE IF NOT EXISTS artifact_verifications (
    record_id       TEXT PRIMARY KEY REFERENCES artifact_records(record_id),
    stat_device     INTEGER NOT NULL,
    stat_inode      INTEGER NOT NULL,
    stat_size       INTEGER NOT NULL,
    stat_mtime_ns   INTEGER NOT NULL,
    content_sha256  TEXT NOT NULL,
    available       INTEGER NOT NULL,
    reason          TEXT NOT NULL,
    verified_at     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


def _hash_file(path: Path) -> tuple[str, int]:
    """Hash one stable regular file and reject mutation during registration."""
    if path.is_symlink():
        raise ValueError("artifact location cannot be a symbolic link")
    before = path.stat()
    if not path.is_file():
        raise FileNotFoundError(f"artifact location is not a file: {path}")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(block)
    after = path.stat()
    stable = (
        before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns,
    ) == (
        after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns,
    )
    if not stable:
        raise RuntimeError("artifact changed while its content was being verified")
    return digest.hexdigest(), after.st_size


def _stat_fingerprint(path: Path) -> tuple[int, int, int, int]:
    if path.is_symlink():
        raise ValueError("artifact location cannot be a symbolic link")
    value = path.stat()
    if not path.is_file():
        raise FileNotFoundError(f"artifact location is not a file: {path}")
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns


class VerificationPolicy(str, Enum):
    """How a registry snapshot re-establishes local byte availability."""

    REHASH_ON_STAT_CHANGE = "REHASH_ON_STAT_CHANGE"
    ALWAYS_REHASH = "ALWAYS_REHASH"


@dataclass(frozen=True)
class ArtifactQuery:
    """Indexed metadata prefilter; scientific acceptance still uses direct_match."""

    concept_id: str | None = None
    descriptor_id: str | None = None
    schema_version: str | None = None
    representation: str | None = None
    units: str | None = None
    origin: str | None = None
    producer_id: str | None = None
    producer_version: str | None = None
    output_port_id: str | None = None
    grid_crs: str | None = None
    component_name: str | None = None
    spatial_crs: str | None = None
    intersects_bounds: tuple[str, str, str, str] | None = None
    temporal_start: str | None = None
    temporal_end: str | None = None
    vertical_kind: str | None = None
    native_resolution_unit: str | None = None

    def __post_init__(self) -> None:
        for name, value in self.__dict__.items():
            if name == "intersects_bounds":
                if value is not None and (
                        not isinstance(value, tuple) or len(value) != 4):
                    raise ValueError(
                        "artifact query intersects_bounds needs four values")
                if value is not None:
                    try:
                        bounds = tuple(Decimal(item) for item in value)
                    except Exception as exc:
                        raise ValueError(
                            "artifact query bounds must be decimal strings") from exc
                    if bounds[0] >= bounds[2] or bounds[1] >= bounds[3]:
                        raise ValueError("artifact query bounds are invalid")
                continue
            if value is not None and (
                    not isinstance(value, str) or not value.strip()):
                raise ValueError(f"artifact query {name} must be text")
        if ((self.temporal_start is None) != (self.temporal_end is None)):
            raise ValueError("artifact query time interval needs start and end")
        if self.temporal_start is not None:
            start = datetime.fromisoformat(
                self.temporal_start.replace("Z", "+00:00"))
            end = datetime.fromisoformat(
                self.temporal_end.replace("Z", "+00:00"))
            if start >= end:
                raise ValueError("artifact query time interval is invalid")


class ArtifactMatchStatus(str, Enum):
    COMPATIBLE = "COMPATIBLE"
    COMPATIBLE_WITH_CAVEATS = "COMPATIBLE_WITH_CAVEATS"
    INCOMPATIBLE = "INCOMPATIBLE"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True)
class ArtifactMatch:
    status: ArtifactMatchStatus
    record: ArtifactRecord
    proof: CompatibilityProof | None
    reason: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.status, ArtifactMatchStatus):
            raise TypeError("artifact match status must be typed")
        if not isinstance(self.record, ArtifactRecord):
            raise TypeError("artifact match requires an ArtifactRecord")
        if self.proof is not None and not isinstance(
                self.proof, CompatibilityProof):
            raise TypeError("artifact match proof must be typed")
        if self.status is ArtifactMatchStatus.UNAVAILABLE and not self.reason:
            raise ValueError("unavailable match must explain why")


class ArtifactRegistry:
    """Immutable-record registry; payloads stay at their native locations.

    Registration verifies exact bytes and stores only canonical metadata.
    Every snapshot re-verifies the file, so disappearance or mutation becomes
    an explicit UNAVAILABLE state rather than a stale committed candidate.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(_SCHEMA)
            self._backfill_indexes(connection)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path), timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA synchronous = FULL")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    @staticmethod
    def _index_values(record: ArtifactRecord) -> tuple[Any, ...]:
        descriptor = record.descriptor
        bounds = descriptor.spatial_support.bounds
        temporal = descriptor.temporal_support
        vertical = descriptor.vertical_support
        native = descriptor.native_resolution
        return (
            record.record_id, descriptor.concept_id,
            descriptor.descriptor_id, descriptor.schema_version,
            descriptor.representation, descriptor.units,
            *bounds, descriptor.spatial_support.crs,
            temporal.kind.value, temporal.start, temporal.end,
            vertical.kind.value if vertical is not None else None,
            descriptor.grid.crs if descriptor.grid is not None else None,
            native.x if native is not None else None,
            native.unit if native is not None else None,
            descriptor.origin.value,
            strict_canonical_json(list(descriptor.component_names)),
            record.producer_id, record.producer_version,
            record.output_port_id,
        )

    @classmethod
    def _write_index(cls, connection: sqlite3.Connection,
                     record: ArtifactRecord) -> None:
        connection.execute(
            "INSERT OR REPLACE INTO artifact_search_index "
            "(record_id,concept_id,descriptor_id,schema_version,representation,"
            "units,spatial_min_x,spatial_min_y,spatial_max_x,spatial_max_y,"
            "spatial_crs,temporal_kind,temporal_start,temporal_end,"
            "vertical_kind,grid_crs,native_resolution_value,"
            "native_resolution_unit,origin,component_names_json,producer_id,"
            "producer_version,output_port_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,"
            "?,?,?,?,?,?,?,?,?,?,?)",
            cls._index_values(record),
        )

    @classmethod
    def _write_verification(cls, connection: sqlite3.Connection,
                            record: ArtifactRecord, *, available: bool,
                            reason: str = "") -> None:
        device, inode, size, mtime_ns = _stat_fingerprint(
            Path(record.location))
        connection.execute(
            "INSERT OR REPLACE INTO artifact_verifications "
            "(record_id,stat_device,stat_inode,stat_size,stat_mtime_ns,"
            "content_sha256,available,reason,verified_at) "
            "VALUES (?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)",
            (record.record_id, device, inode, size, mtime_ns,
             record.content_sha256, int(available), reason),
        )

    @classmethod
    def _backfill_indexes(cls, connection: sqlite3.Connection) -> None:
        rows = connection.execute(
            "SELECT record_json FROM artifact_records WHERE record_id NOT IN "
            "(SELECT record_id FROM artifact_search_index)").fetchall()
        for row in rows:
            cls._write_index(connection, ArtifactRecord.from_dict(
                json.loads(row["record_json"])))

    def register_file(
        self,
        path: str | Path,
        descriptor: ArtifactDescriptor,
        *,
        media_type: str,
        producer_id: str,
        producer_version: str,
        output_port_id: str,
        inputs: Iterable[ArtifactInput] = (),
        evidence_profile_id: str = "evidence:unknown",
        metadata: dict[str, Any] | None = None,
        expected_content_sha256: str | None = None,
    ) -> ArtifactRecord:
        record = self.prepare_file(
            path,
            descriptor,
            media_type=media_type,
            producer_id=producer_id,
            producer_version=producer_version,
            output_port_id=output_port_id,
            inputs=inputs,
            evidence_profile_id=evidence_profile_id,
            metadata=metadata,
            expected_content_sha256=expected_content_sha256,
        )
        return self.register_records((record,))[0]

    def prepare_file(
        self,
        path: str | Path,
        descriptor: ArtifactDescriptor,
        *,
        media_type: str,
        producer_id: str,
        producer_version: str,
        output_port_id: str,
        inputs: Iterable[ArtifactInput] = (),
        evidence_profile_id: str = "evidence:unknown",
        metadata: dict[str, Any] | None = None,
        expected_content_sha256: str | None = None,
    ) -> ArtifactRecord:
        """Verify and bind one record without making it discoverable yet."""
        if not isinstance(descriptor, ArtifactDescriptor):
            raise TypeError("artifact registration requires an ArtifactDescriptor")
        located = Path(path)
        if located.is_symlink():
            raise ValueError("artifact location cannot be a symbolic link")
        if not located.is_absolute():
            located = located.resolve()
        else:
            located = located.resolve(strict=False)
        digest, size = _hash_file(located)
        if (expected_content_sha256 is not None
                and digest != expected_content_sha256):
            raise ValueError(
                "artifact bytes do not match expected content digest")
        return ArtifactRecord.bind(
            descriptor=descriptor,
            location=str(located),
            media_type=media_type,
            content_sha256=digest,
            size_bytes=size,
            producer_id=producer_id,
            producer_version=producer_version,
            output_port_id=output_port_id,
            inputs=inputs,
            evidence_profile_id=evidence_profile_id,
            metadata=metadata,
        )

    def register_records(
        self, records: Iterable[ArtifactRecord],
    ) -> tuple[ArtifactRecord, ...]:
        """Atomically publish a verified co-produced record set."""
        values = tuple(records)
        if not values:
            raise ValueError("artifact registration set cannot be empty")
        if not all(isinstance(value, ArtifactRecord) for value in values):
            raise TypeError("artifact registration set must contain records")
        if len({value.record_id for value in values}) != len(values):
            raise ValueError("artifact registration set cannot repeat records")
        if len({value.leaf.leaf_id for value in values}) != len(values):
            raise ValueError("artifact registration set cannot repeat leaves")
        for record in values:
            available, reason = self.verify_record(record)
            if not available:
                raise ValueError(
                    "prepared artifact is no longer available: " + reason)
        published: list[ArtifactRecord] = []
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for record in values:
                encoded = strict_canonical_json(record.to_dict())
                existing = connection.execute(
                    "SELECT record_json FROM artifact_records WHERE record_id=?",
                    (record.record_id,),
                ).fetchone()
                if existing is not None:
                    if existing["record_json"] != encoded:
                        raise ValueError(
                            "artifact record identity has conflicting durable bytes")
                    published.append(ArtifactRecord.from_dict(
                        json.loads(existing["record_json"])))
                    continue
                same_leaf = connection.execute(
                    "SELECT record_json FROM artifact_records WHERE leaf_id=?",
                    (record.leaf.leaf_id,),
                ).fetchone()
                if same_leaf is not None:
                    raise ValueError(
                        "artifact leaf identity has conflicting registry metadata")
                connection.execute(
                    "INSERT INTO artifact_records "
                    "(record_id,artifact_id,leaf_id,concept_id,location,record_json) "
                    "VALUES (?,?,?,?,?,?)",
                    (
                        record.record_id,
                        record.artifact_id,
                        record.leaf.leaf_id,
                        record.descriptor.concept_id,
                        record.location,
                        encoded,
                    ),
                )
                self._write_index(connection, record)
                self._write_verification(
                    connection, record, available=True)
                published.append(record)
            connection.commit()
        return tuple(published)

    def record(self, record_id: str) -> ArtifactRecord:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT record_json FROM artifact_records WHERE record_id=?",
                (record_id,),
            ).fetchone()
        if row is None:
            raise KeyError(record_id)
        return ArtifactRecord.from_dict(json.loads(row["record_json"]))

    def records(self, *, concept_id: str | None = None
                ) -> tuple[ArtifactRecord, ...]:
        sql = "SELECT record_json FROM artifact_records"
        values: tuple[str, ...] = ()
        if concept_id is not None:
            sql += " WHERE concept_id=?"
            values = (concept_id,)
        sql += " ORDER BY record_id"
        with self._connect() as connection:
            rows = connection.execute(sql, values).fetchall()
        return tuple(ArtifactRecord.from_dict(json.loads(row["record_json"]))
                     for row in rows)

    def query_records(self, query: ArtifactQuery) -> tuple[ArtifactRecord, ...]:
        """Use normalized metadata columns without opening payload files."""
        if not isinstance(query, ArtifactQuery):
            raise TypeError("artifact query must be ArtifactQuery")
        clauses: list[str] = []
        values: list[str] = []
        for field_name in (
                "concept_id", "descriptor_id", "schema_version",
                "representation", "units",
                "origin", "producer_id", "producer_version",
                "output_port_id", "grid_crs", "spatial_crs",
                "vertical_kind", "native_resolution_unit"):
            value = getattr(query, field_name)
            if value is not None:
                clauses.append(f"i.{field_name}=?")
                values.append(value)
        if query.component_name is not None:
            clauses.append(
                "EXISTS (SELECT 1 FROM json_each(i.component_names_json) "
                "WHERE json_each.value=?)")
            values.append(query.component_name)
        sql = ("SELECT r.record_json FROM artifact_records r JOIN "
               "artifact_search_index i USING(record_id)")
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY r.record_id"
        with self._connect() as connection:
            rows = connection.execute(sql, tuple(values)).fetchall()
        records = tuple(ArtifactRecord.from_dict(json.loads(row["record_json"]))
                        for row in rows)
        if query.intersects_bounds is not None:
            sought = tuple(Decimal(value) for value in query.intersects_bounds)
            records = tuple(record for record in records if (
                (query.spatial_crs is None
                 or record.descriptor.spatial_support.crs == query.spatial_crs)
                and (lambda offered: not (
                    offered[2] <= sought[0] or offered[0] >= sought[2]
                    or offered[3] <= sought[1] or offered[1] >= sought[3]))(
                        tuple(Decimal(value) for value in
                              record.descriptor.spatial_support.bounds))))
        if query.temporal_start is not None:
            start = datetime.fromisoformat(
                query.temporal_start.replace("Z", "+00:00"))
            end = datetime.fromisoformat(
                query.temporal_end.replace("Z", "+00:00"))
            records = tuple(record for record in records if (
                record.descriptor.temporal_support.start is not None
                and record.descriptor.temporal_support.end is not None
                and datetime.fromisoformat(
                    record.descriptor.temporal_support.end.replace(
                        "Z", "+00:00")) > start
                and datetime.fromisoformat(
                    record.descriptor.temporal_support.start.replace(
                        "Z", "+00:00")) < end))
        return records

    @staticmethod
    def verify_record(record: ArtifactRecord) -> tuple[bool, str]:
        try:
            location = Path(record.location)
            digest, size = _hash_file(location)
        except (FileNotFoundError, OSError, RuntimeError, ValueError) as exc:
            return False, f"LOCATION_UNAVAILABLE:{type(exc).__name__}"
        if size != record.size_bytes:
            return False, "SIZE_CHANGED"
        if digest != record.content_sha256:
            return False, "CONTENT_CHANGED"
        return True, ""

    def _verify_cached(
        self, record: ArtifactRecord, policy: VerificationPolicy,
    ) -> tuple[bool, str]:
        try:
            fingerprint = _stat_fingerprint(Path(record.location))
        except (FileNotFoundError, OSError, RuntimeError, ValueError) as exc:
            return False, f"LOCATION_UNAVAILABLE:{type(exc).__name__}"
        with self._connect() as connection:
            receipt = connection.execute(
                "SELECT * FROM artifact_verifications WHERE record_id=?",
                (record.record_id,),
            ).fetchone()
        unchanged = receipt is not None and fingerprint == (
            receipt["stat_device"], receipt["stat_inode"],
            receipt["stat_size"], receipt["stat_mtime_ns"])
        if (policy is VerificationPolicy.REHASH_ON_STAT_CHANGE and unchanged
                and receipt["content_sha256"] == record.content_sha256
                and receipt["available"]):
            return True, ""
        available, reason = self.verify_record(record)
        if available:
            with self._connect() as connection:
                self._write_verification(
                    connection, record, available=True)
        return available, reason

    def snapshot(
        self,
        *,
        verification_policy: VerificationPolicy =
            VerificationPolicy.REHASH_ON_STAT_CHANGE,
    ) -> ArtifactRegistrySnapshot:
        if not isinstance(verification_policy, VerificationPolicy):
            raise TypeError("verification_policy must be typed")
        entries = []
        for record in self.records():
            available, reason = self._verify_cached(
                record, verification_policy)
            entries.append(ArtifactSnapshotEntry(
                record,
                ArtifactAvailability.COMMITTED
                if available else ArtifactAvailability.UNAVAILABLE,
                reason,
            ))
        return ArtifactRegistrySnapshot.freeze(entries)

    def search(
        self,
        requirement: Requirement,
        *,
        evidence_profiles: Iterable[EvidenceProfile] = (),
        evidence_snapshot: EvidenceSnapshot | None = None,
        include_incompatible: bool = False,
        include_unavailable: bool = False,
    ) -> tuple[ArtifactMatch, ...]:
        if not isinstance(requirement, Requirement):
            raise TypeError("artifact search requires a Requirement")
        profiles = {value.profile_id: value for value in evidence_profiles}
        snapshot = self.snapshot()
        matches: list[ArtifactMatch] = []
        for entry in snapshot.entries:
            record = entry.record
            if record.descriptor.concept_id != requirement.concept_id:
                continue
            if entry.availability is ArtifactAvailability.UNAVAILABLE:
                if include_unavailable:
                    matches.append(ArtifactMatch(
                        ArtifactMatchStatus.UNAVAILABLE,
                        record,
                        None,
                        entry.reason,
                    ))
                continue
            profile = profiles.get(record.evidence_profile_id)
            if (profile is None
                    and record.evidence_profile_id != "evidence:unknown"):
                if include_incompatible:
                    matches.append(ArtifactMatch(
                        ArtifactMatchStatus.INCOMPATIBLE,
                        record,
                        None,
                        "EVIDENCE_PROFILE_UNBOUND",
                    ))
                continue
            if profile is None:
                proof = direct_match(record.descriptor, requirement)
            else:
                from capabilities import artifact_evidence_subject
                proof = direct_match(
                    record.descriptor,
                    requirement,
                    profile,
                    evidence_snapshot=evidence_snapshot,
                    evidence_subject=artifact_evidence_subject(record.leaf),
                )
            if proof.satisfied:
                status = (
                    ArtifactMatchStatus.COMPATIBLE_WITH_CAVEATS
                    if proof.caveat_codes
                    else ArtifactMatchStatus.COMPATIBLE)
                matches.append(ArtifactMatch(status, record, proof))
            elif include_incompatible:
                matches.append(ArtifactMatch(
                    ArtifactMatchStatus.INCOMPATIBLE,
                    record,
                    proof,
                    ",".join(value.value for value in proof.rejection_codes),
                ))
        return tuple(sorted(matches, key=lambda value: value.record.record_id))

    def close(self) -> None:
        """Connections are operation-scoped; retained for context symmetry."""

    def __enter__(self) -> "ArtifactRegistry":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


__all__ = [
    "ArtifactQuery",
    "ArtifactMatch",
    "ArtifactMatchStatus",
    "ArtifactRegistry",
    "VerificationPolicy",
]
