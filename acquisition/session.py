"""Durable Stage-5 planning sessions: cursors, quota, cooldowns, candidates.

A planning session is restartable state, not a cache.  If the controller dies
half way through paginating a provider, the next process resumes on the page it
had already reached, having already spent the quota those earlier pages cost.
Resuming must never re-spend quota and must never quietly change an
availability snapshot that was already frozen.

Quota is deliberately keyed by *provider*, not by session: planning and payload
transfer draw down the same budget, because the provider does not care which
phase of our pipeline is calling it.
"""
from __future__ import annotations

import contextlib
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator

from capabilities.implementation import _digest, _required_text
from engine.runtime.identity import (
    require_object_fields,
    strict_canonical_json,
    strict_hash,
    strict_json_loads,
)

from .connector import AssetCandidate

_SCHEMA = """
CREATE TABLE IF NOT EXISTS planning_sessions (
  session_id TEXT PRIMARY KEY,
  created_at REAL NOT NULL,
  scope_id TEXT,
  scope_json TEXT,
  frozen_at REAL,
  snapshot_id TEXT
) STRICT;
CREATE TABLE IF NOT EXISTS query_cursors (
  session_id TEXT NOT NULL REFERENCES planning_sessions(session_id),
  query_id TEXT NOT NULL,
  query_json TEXT NOT NULL,
  cursor TEXT,
  pages_read INTEGER NOT NULL DEFAULT 0 CHECK(pages_read >= 0),
  exhausted INTEGER NOT NULL DEFAULT 0 CHECK(exhausted IN (0,1)),
  updated_at REAL NOT NULL,
  PRIMARY KEY(session_id, query_id)
) STRICT;
CREATE TABLE IF NOT EXISTS discovered_candidates (
  session_id TEXT NOT NULL REFERENCES planning_sessions(session_id),
  query_id TEXT NOT NULL,
  asset_id TEXT NOT NULL,
  ordinal INTEGER NOT NULL,
  candidate_json TEXT NOT NULL,
  PRIMARY KEY(session_id, query_id, asset_id)
) STRICT;
CREATE INDEX IF NOT EXISTS discovered_order_idx
  ON discovered_candidates(session_id, query_id, ordinal);
CREATE TABLE IF NOT EXISTS provider_quota (
  source_id TEXT PRIMARY KEY,
  metadata_calls INTEGER NOT NULL DEFAULT 0 CHECK(metadata_calls >= 0),
  payload_calls INTEGER NOT NULL DEFAULT 0 CHECK(payload_calls >= 0),
  bytes_transferred INTEGER NOT NULL DEFAULT 0 CHECK(bytes_transferred >= 0),
  updated_at REAL NOT NULL
) STRICT;
CREATE TABLE IF NOT EXISTS provider_cooldowns (
  source_id TEXT PRIMARY KEY,
  until_epoch REAL NOT NULL,
  reason TEXT NOT NULL
) STRICT;
CREATE TABLE IF NOT EXISTS session_limits (
  session_id TEXT NOT NULL REFERENCES planning_sessions(session_id),
  code TEXT NOT NULL,
  subject TEXT NOT NULL,
  PRIMARY KEY(session_id, code, subject)
) STRICT;
CREATE TABLE IF NOT EXISTS fetch_quota_reservations (
  manifest_root TEXT PRIMARY KEY,
  source_id TEXT NOT NULL,
  payload_calls INTEGER NOT NULL CHECK(payload_calls >= 0),
  reserved_bytes INTEGER NOT NULL CHECK(reserved_bytes >= 0),
  created_at REAL NOT NULL
) STRICT;
CREATE TABLE IF NOT EXISTS fetch_transfer_intents (
  manifest_root TEXT PRIMARY KEY,
  source_id TEXT NOT NULL,
  owner_token TEXT NOT NULL,
  quota_json TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('ACTIVE','COMPLETE')),
  updated_at REAL NOT NULL
) STRICT;
CREATE TABLE IF NOT EXISTS fetch_asset_checkpoints (
  manifest_root TEXT NOT NULL,
  asset_id TEXT NOT NULL,
  expected_bytes INTEGER NOT NULL CHECK(expected_bytes >= 0),
  attempts_reserved INTEGER NOT NULL CHECK(attempts_reserved >= 1),
  attempts_started INTEGER NOT NULL CHECK(attempts_started >= 0),
  blob_sha256 TEXT,
  byte_size INTEGER,
  completed_at REAL,
  PRIMARY KEY(manifest_root, asset_id),
  CHECK(attempts_started <= attempts_reserved),
  CHECK((blob_sha256 IS NULL AND byte_size IS NULL AND completed_at IS NULL)
     OR (blob_sha256 IS NOT NULL AND byte_size IS NOT NULL
         AND completed_at IS NOT NULL))
) STRICT;
""".replace(") STRICT;", ");")  # declared intent; this node's SQLite predates it


class QuotaExceededError(RuntimeError):
    """A provider budget would be exceeded by the requested work."""

    def __init__(self, source_id: str, dimension: str, limit: int) -> None:
        super().__init__(
            f"provider {source_id!r} exhausted its {dimension} budget of {limit}")
        self.source_id = source_id
        self.dimension = dimension
        self.limit = limit


class FrozenSessionError(RuntimeError):
    """A discovery write was attempted after its snapshot was sealed."""


@dataclass(frozen=True)
class ProviderQuota:
    """System-level budget shared by metadata search and payload transfer."""

    max_metadata_calls: int = 1_000
    max_payload_calls: int = 10_000
    max_bytes: int = 1 << 34

    def __post_init__(self) -> None:
        for name in ("max_metadata_calls", "max_payload_calls", "max_bytes"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")

    def to_dict(self) -> dict[str, int]:
        return {
            "max_metadata_calls": self.max_metadata_calls,
            "max_payload_calls": self.max_payload_calls,
            "max_bytes": self.max_bytes,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ProviderQuota":
        raw = require_object_fields(
            value,
            {"max_metadata_calls", "max_payload_calls", "max_bytes"},
            "ProviderQuota",
        )
        return cls(**raw)


@dataclass(frozen=True)
class QuotaUsage:
    metadata_calls: int
    payload_calls: int
    bytes_transferred: int

    def to_dict(self) -> dict[str, int]:
        return {
            "metadata_calls": self.metadata_calls,
            "payload_calls": self.payload_calls,
            "bytes_transferred": self.bytes_transferred,
        }


@dataclass(frozen=True)
class CursorState:
    cursor: str | None
    pages_read: int
    exhausted: bool


@dataclass(frozen=True)
class FetchAssetCheckpoint:
    """Durable transfer state for one manifest asset."""

    asset_id: str
    expected_bytes: int
    attempts_reserved: int
    attempts_started: int
    blob_sha256: str | None
    byte_size: int | None

    def __post_init__(self) -> None:
        _required_text(self.asset_id, "fetch checkpoint asset_id")
        for name in ("expected_bytes", "attempts_reserved",
                     "attempts_started"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"fetch checkpoint {name} must be an integer")
        if self.expected_bytes < 0 or self.attempts_reserved < 1:
            raise ValueError("fetch checkpoint sizes/reservations are invalid")
        if not 0 <= self.attempts_started <= self.attempts_reserved:
            raise ValueError("fetch checkpoint attempt counts are invalid")
        if self.blob_sha256 is None:
            if self.byte_size is not None:
                raise ValueError("an incomplete checkpoint cannot have a size")
        else:
            _digest(self.blob_sha256, "fetch checkpoint blob_sha256")
            if (isinstance(self.byte_size, bool)
                    or not isinstance(self.byte_size, int)
                    or self.byte_size < 0):
                raise ValueError("a complete checkpoint needs a valid size")

    @property
    def complete(self) -> bool:
        return self.blob_sha256 is not None


class PlanningSessionStore:
    """Single-writer SQLite home for restartable planning state."""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        if not self.db_path.is_absolute():
            raise ValueError("planning session database path must be absolute")
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # ``executescript`` commits implicitly, so schema creation runs outside
        # the explicit transaction the write path uses.
        connection = self._raw_connect()
        try:
            connection.executescript(_SCHEMA)
            # Additive migration for databases created before acquisition
            # scope became a durable pre-provider boundary.
            columns = {
                row[1] for row in connection.execute(
                    "PRAGMA table_info(planning_sessions)").fetchall()
            }
            if "scope_id" not in columns:
                connection.execute(
                    "ALTER TABLE planning_sessions ADD COLUMN scope_id TEXT")
            if "scope_json" not in columns:
                connection.execute(
                    "ALTER TABLE planning_sessions ADD COLUMN scope_json TEXT")
        finally:
            connection.close()

    def _raw_connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, isolation_level=None,
                                     timeout=10.0)
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            mode = connection.execute("PRAGMA journal_mode=WAL").fetchone()
            if mode is None or str(mode[0]).lower() != "wal":
                raise RuntimeError(
                    "planning session database requires SQLite WAL mode")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("PRAGMA busy_timeout=10000")
            return connection
        except BaseException:
            connection.close()
            raise

    @contextlib.contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = self._raw_connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.execute("COMMIT")
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    # -- sessions ---------------------------------------------------------

    def open_session(self, session_id: str, *, now: float | None = None) -> None:
        """Create the session if new; leave existing durable state untouched."""
        _required_text(session_id, "session_id")
        stamp = time.time() if now is None else now
        with self.connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO planning_sessions"
                "(session_id, created_at) VALUES(?, ?)", (session_id, stamp))

    def bind_scope(
        self,
        session_id: str,
        scope_id: str,
        scope_payload: dict[str, Any],
    ) -> None:
        """Bind one immutable discovery scope before provider access."""
        _required_text(session_id, "session_id")
        _digest(scope_id, "acquisition scope_id")
        encoded = strict_canonical_json(scope_payload)
        identity_payload = dict(scope_payload)
        declared_id = identity_payload.pop("scope_id", None)
        if declared_id != scope_id or strict_hash(identity_payload) != scope_id:
            raise ValueError("acquisition scope payload identity does not verify")
        with self.connect() as connection:
            row = connection.execute(
                "SELECT scope_id, scope_json, frozen_at FROM planning_sessions "
                "WHERE session_id=?", (session_id,)).fetchone()
            if row is None:
                raise KeyError(f"unknown planning session {session_id!r}")
            if row[2] is not None and row[0] is None:
                raise FrozenSessionError(
                    "a legacy frozen session cannot acquire a new scope")
            if row[0] is None:
                connection.execute(
                    "UPDATE planning_sessions SET scope_id=?, scope_json=? "
                    "WHERE session_id=?", (scope_id, encoded, session_id))
                return
            if row[0] != scope_id or row[1] != encoded:
                raise ValueError(
                    "planning session is already bound to another "
                    "acquisition scope")

    def scope_for(self, session_id: str) -> tuple[str, dict[str, Any]] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT scope_id, scope_json FROM planning_sessions "
                "WHERE session_id=?", (session_id,)).fetchone()
        if row is None or row[0] is None:
            return None
        if row[1] is None:
            raise RuntimeError("planning session scope record is incomplete")
        payload = strict_json_loads(row[1])
        identity_payload = dict(payload)
        declared_id = identity_payload.pop("scope_id", None)
        if declared_id != row[0] or strict_hash(identity_payload) != row[0]:
            raise RuntimeError("planning session scope identity is corrupt")
        return row[0], payload

    def is_frozen(self, session_id: str) -> bool:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT frozen_at FROM planning_sessions WHERE session_id=?",
                (session_id,)).fetchone()
        return bool(row and row[0] is not None)

    def freeze_session(self, session_id: str, snapshot_id: str, *,
                       now: float | None = None) -> None:
        """Seal the session against further discovery.

        Freezing is idempotent for the *same* snapshot and an error for a
        different one: a frozen availability snapshot cannot be silently
        replaced by a later, differently-truncated search.
        """
        stamp = time.time() if now is None else now
        with self.connect() as connection:
            row = connection.execute(
                "SELECT snapshot_id FROM planning_sessions WHERE session_id=?",
                (session_id,)).fetchone()
            if row is None:
                raise KeyError(f"unknown planning session {session_id!r}")
            if row[0] is not None and row[0] != snapshot_id:
                raise ValueError(
                    f"planning session {session_id!r} is already frozen at a "
                    "different availability snapshot")
            connection.execute(
                "UPDATE planning_sessions SET frozen_at=?, snapshot_id=? "
                "WHERE session_id=?", (stamp, snapshot_id, session_id))

    def snapshot_id_for(self, session_id: str) -> str | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT snapshot_id FROM planning_sessions WHERE session_id=?",
                (session_id,)).fetchone()
        return None if row is None else row[0]

    # -- cursors ----------------------------------------------------------

    def cursor_for(self, session_id: str, query_id: str) -> CursorState:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT cursor, pages_read, exhausted FROM query_cursors "
                "WHERE session_id=? AND query_id=?",
                (session_id, query_id)).fetchone()
        if row is None:
            return CursorState(None, 0, False)
        return CursorState(row[0], int(row[1]), bool(row[2]))

    def known_query_ids(self, session_id: str) -> tuple[str, ...]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT query_id FROM query_cursors WHERE session_id=? "
                "ORDER BY query_id", (session_id,)).fetchall()
        return tuple(row[0] for row in rows)

    def query_payload_for(
        self, session_id: str, query_id: str,
    ) -> dict[str, Any] | None:
        """Return the immutable query record that produced durable pages."""
        with self.connect() as connection:
            row = connection.execute(
                "SELECT query_json FROM query_cursors "
                "WHERE session_id=? AND query_id=?",
                (session_id, query_id),
            ).fetchone()
        return None if row is None else strict_json_loads(row[0])

    def record_page(self, session_id: str, query_id: str, query_payload: dict,
                    *, cursor: str | None, exhausted: bool,
                    candidates: Iterable[AssetCandidate],
                    now: float | None = None) -> int:
        """Persist one page atomically with its cursor advance.

        Candidates and the cursor move in a single transaction so a crash
        cannot leave a cursor past rows that were never stored.  Returns the
        number of newly stored candidates.
        """
        stamp = time.time() if now is None else now
        encoded_query = strict_canonical_json(query_payload)
        stored = 0
        with self.connect() as connection:
            session = connection.execute(
                "SELECT frozen_at FROM planning_sessions WHERE session_id=?",
                (session_id,)).fetchone()
            if session is None:
                raise KeyError(f"unknown planning session {session_id!r}")
            if session[0] is not None:
                raise FrozenSessionError(
                    f"planning session {session_id!r} is frozen and read-only")
            row = connection.execute(
                "SELECT pages_read, query_json FROM query_cursors "
                "WHERE session_id=? AND query_id=?",
                (session_id, query_id)).fetchone()
            pages_read = (0 if row is None else int(row[0])) + 1
            if row is not None and row[1] != encoded_query:
                raise ValueError(
                    "persisted query identity cannot be relabelled on resume")
            ordinal_row = connection.execute(
                "SELECT COALESCE(MAX(ordinal), -1) FROM discovered_candidates "
                "WHERE session_id=? AND query_id=?",
                (session_id, query_id)).fetchone()
            ordinal = int(ordinal_row[0])
            for candidate in candidates:
                ordinal += 1
                encoded_candidate = strict_canonical_json(candidate.to_dict())
                changed = connection.execute(
                    "INSERT OR IGNORE INTO discovered_candidates"
                    "(session_id, query_id, asset_id, ordinal, candidate_json) "
                    "VALUES(?, ?, ?, ?, ?)",
                    (session_id, query_id, candidate.asset_id, ordinal,
                     encoded_candidate)).rowcount
                if changed:
                    stored += 1
                else:
                    existing_candidate = connection.execute(
                        "SELECT candidate_json FROM discovered_candidates "
                        "WHERE session_id=? AND query_id=? AND asset_id=?",
                        (session_id, query_id, candidate.asset_id),
                    ).fetchone()
                    if (existing_candidate is None
                            or existing_candidate[0] != encoded_candidate):
                        raise ValueError(
                            "provider reused an asset ID for conflicting "
                            "metadata within one planning session")
                    ordinal -= 1
            connection.execute(
                "INSERT INTO query_cursors"
                "(session_id, query_id, query_json, cursor, pages_read,"
                " exhausted, updated_at) VALUES(?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(session_id, query_id) DO UPDATE SET "
                "cursor=excluded.cursor, pages_read=excluded.pages_read, "
                "exhausted=excluded.exhausted, updated_at=excluded.updated_at",
                (session_id, query_id, encoded_query, cursor, pages_read,
                 1 if exhausted else 0, stamp))
        return stored

    def candidates_for(self, session_id: str,
                       query_id: str) -> tuple[AssetCandidate, ...]:
        """Return stored candidates in discovery order."""
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT candidate_json FROM discovered_candidates "
                "WHERE session_id=? AND query_id=? ORDER BY ordinal",
                (session_id, query_id)).fetchall()
        return tuple(
            AssetCandidate.from_dict(strict_json_loads(row[0])) for row in rows)

    # -- quota and cooldowns ---------------------------------------------

    def quota_for(self, source_id: str) -> QuotaUsage:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT metadata_calls, payload_calls, bytes_transferred "
                "FROM provider_quota WHERE source_id=?", (source_id,)).fetchone()
        if row is None:
            return QuotaUsage(0, 0, 0)
        return QuotaUsage(int(row[0]), int(row[1]), int(row[2]))

    def debit_quota(self, source_id: str, quota: ProviderQuota, *,
                    metadata_calls: int = 0, payload_calls: int = 0,
                    transferred_bytes: int = 0,
                    now: float | None = None) -> QuotaUsage:
        """Charge a provider budget, refusing the work if it would overrun.

        The check and the debit share one transaction, so two phases racing on
        the same provider cannot both pass a limit check.
        """
        if not isinstance(quota, ProviderQuota):
            raise TypeError("debit_quota requires a ProviderQuota")
        stamp = time.time() if now is None else now
        with self.connect() as connection:
            row = connection.execute(
                "SELECT metadata_calls, payload_calls, bytes_transferred "
                "FROM provider_quota WHERE source_id=?", (source_id,)).fetchone()
            used = (0, 0, 0) if row is None else (
                int(row[0]), int(row[1]), int(row[2]))
            updated = (
                used[0] + metadata_calls,
                used[1] + payload_calls,
                used[2] + transferred_bytes,
            )
            for value, limit, dimension in (
                    (updated[0], quota.max_metadata_calls, "metadata call"),
                    (updated[1], quota.max_payload_calls, "payload call"),
                    (updated[2], quota.max_bytes, "byte")):
                if value > limit:
                    raise QuotaExceededError(source_id, dimension, limit)
            connection.execute(
                "INSERT INTO provider_quota"
                "(source_id, metadata_calls, payload_calls, bytes_transferred,"
                " updated_at) VALUES(?, ?, ?, ?, ?) "
                "ON CONFLICT(source_id) DO UPDATE SET "
                "metadata_calls=excluded.metadata_calls, "
                "payload_calls=excluded.payload_calls, "
                "bytes_transferred=excluded.bytes_transferred, "
                "updated_at=excluded.updated_at",
                (source_id, updated[0], updated[1], updated[2], stamp))
        return QuotaUsage(*updated)

    def reserve_fetch_quota(
        self,
        manifest_root: str,
        source_id: str,
        quota: ProviderQuota,
        *,
        payload_calls: int,
        reserved_bytes: int,
        now: float | None = None,
    ) -> QuotaUsage:
        """Atomically reserve a known transfer before its first byte moves.

        The manifest root is the idempotency key.  A restart sees the same
        reservation and cannot charge the provider twice; a conflicting reuse
        of that root fails closed.
        """
        _digest(manifest_root, "fetch reservation manifest root")
        _required_text(source_id, "fetch reservation source_id")
        if not isinstance(quota, ProviderQuota):
            raise TypeError("fetch reservation requires a ProviderQuota")
        for value, label in ((payload_calls, "payload_calls"),
                             (reserved_bytes, "reserved_bytes")):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"fetch reservation {label} must be non-negative")
        stamp = time.time() if now is None else now
        with self.connect() as connection:
            existing = connection.execute(
                "SELECT source_id, payload_calls, reserved_bytes "
                "FROM fetch_quota_reservations WHERE manifest_root=?",
                (manifest_root,)).fetchone()
            if existing is not None:
                if existing != (source_id, payload_calls, reserved_bytes):
                    raise ValueError(
                        "manifest root has a conflicting fetch quota reservation")
                row = connection.execute(
                    "SELECT metadata_calls, payload_calls, bytes_transferred "
                    "FROM provider_quota WHERE source_id=?", (source_id,)
                ).fetchone()
                used = (0, 0, 0) if row is None else tuple(int(v) for v in row)
                return QuotaUsage(*used)

            row = connection.execute(
                "SELECT metadata_calls, payload_calls, bytes_transferred "
                "FROM provider_quota WHERE source_id=?", (source_id,)
            ).fetchone()
            used = (0, 0, 0) if row is None else tuple(int(v) for v in row)
            updated = (used[0], used[1] + payload_calls,
                       used[2] + reserved_bytes)
            for value, limit, dimension in (
                    (updated[1], quota.max_payload_calls, "payload call"),
                    (updated[2], quota.max_bytes, "byte")):
                if value > limit:
                    raise QuotaExceededError(source_id, dimension, limit)
            connection.execute(
                "INSERT INTO provider_quota"
                "(source_id, metadata_calls, payload_calls, bytes_transferred,"
                " updated_at) VALUES(?, ?, ?, ?, ?) "
                "ON CONFLICT(source_id) DO UPDATE SET "
                "metadata_calls=excluded.metadata_calls, "
                "payload_calls=excluded.payload_calls, "
                "bytes_transferred=excluded.bytes_transferred, "
                "updated_at=excluded.updated_at",
                (source_id, *updated, stamp))
            connection.execute(
                "INSERT INTO fetch_quota_reservations"
                "(manifest_root, source_id, payload_calls, reserved_bytes,"
                " created_at) VALUES(?, ?, ?, ?, ?)",
                (manifest_root, source_id, payload_calls, reserved_bytes, stamp))
        return QuotaUsage(*updated)

    def initialize_fetch_transfer(
        self,
        manifest_root: str,
        source_id: str,
        owner_token: str,
        quota: ProviderQuota,
        assets: tuple[tuple[str, int], ...],
        *,
        now: float | None = None,
    ) -> QuotaUsage:
        """Claim a fetch intent and freeze its per-asset quota/checkpoints.

        The caller must hold the same-node manifest file lock supplied by
        :class:`PayloadStore`.  That lock proves a former process is gone
        before an ACTIVE intent is adopted; the SQLite row supplies durable
        attribution and checkpoint/retry accounting.
        """
        _digest(manifest_root, "fetch transfer manifest root")
        _required_text(source_id, "fetch transfer source_id")
        _required_text(owner_token, "fetch transfer owner_token")
        if not isinstance(quota, ProviderQuota):
            raise TypeError("fetch transfer requires a ProviderQuota")
        if (not isinstance(assets, tuple) or not assets):
            raise ValueError("fetch transfer needs a non-empty asset tuple")
        normalized: list[tuple[str, int]] = []
        for asset_id, byte_size in assets:
            _required_text(asset_id, "fetch transfer asset_id")
            if (isinstance(byte_size, bool) or not isinstance(byte_size, int)
                    or byte_size < 0):
                raise ValueError("fetch transfer asset size must be non-negative")
            normalized.append((asset_id, byte_size))
        frozen_assets = tuple(normalized)
        if frozen_assets != tuple(sorted(frozen_assets)):
            raise ValueError("fetch transfer assets must use canonical order")
        if len({item[0] for item in frozen_assets}) != len(frozen_assets):
            raise ValueError("fetch transfer assets must be unique")
        payload_calls = len(frozen_assets)
        reserved_bytes = sum(item[1] for item in frozen_assets)
        quota_json = strict_canonical_json(quota.to_dict())
        stamp = time.time() if now is None else now

        with self.connect() as connection:
            reservation = connection.execute(
                "SELECT source_id, payload_calls, reserved_bytes "
                "FROM fetch_quota_reservations WHERE manifest_root=?",
                (manifest_root,),
            ).fetchone()
            new_reservation = reservation is None
            if reservation is not None and reservation != (
                    source_id, payload_calls, reserved_bytes):
                raise ValueError(
                    "manifest root has a conflicting fetch quota reservation")

            usage_row = connection.execute(
                "SELECT metadata_calls, payload_calls, bytes_transferred "
                "FROM provider_quota WHERE source_id=?", (source_id,),
            ).fetchone()
            if reservation is not None and usage_row is None:
                raise RuntimeError(
                    "fetch reservation exists without provider quota authority")
            used = ((0, 0, 0) if usage_row is None
                    else tuple(int(value) for value in usage_row))
            if new_reservation:
                updated = (used[0], used[1] + payload_calls,
                           used[2] + reserved_bytes)
                for value, limit, dimension in (
                        (updated[1], quota.max_payload_calls, "payload call"),
                        (updated[2], quota.max_bytes, "byte")):
                    if value > limit:
                        raise QuotaExceededError(source_id, dimension, limit)
                connection.execute(
                    "INSERT INTO provider_quota"
                    "(source_id, metadata_calls, payload_calls, "
                    "bytes_transferred, updated_at) VALUES(?, ?, ?, ?, ?) "
                    "ON CONFLICT(source_id) DO UPDATE SET "
                    "metadata_calls=excluded.metadata_calls, "
                    "payload_calls=excluded.payload_calls, "
                    "bytes_transferred=excluded.bytes_transferred, "
                    "updated_at=excluded.updated_at",
                    (source_id, *updated, stamp),
                )
                connection.execute(
                    "INSERT INTO fetch_quota_reservations"
                    "(manifest_root, source_id, payload_calls, reserved_bytes, "
                    "created_at) VALUES(?, ?, ?, ?, ?)",
                    (manifest_root, source_id, payload_calls, reserved_bytes,
                     stamp),
                )
                used = updated

            intent = connection.execute(
                "SELECT source_id, quota_json FROM fetch_transfer_intents "
                "WHERE manifest_root=?", (manifest_root,),
            ).fetchone()
            if intent is not None and intent != (source_id, quota_json):
                raise ValueError(
                    "fetch transfer policy/source changed across restart")
            connection.execute(
                "INSERT INTO fetch_transfer_intents"
                "(manifest_root, source_id, owner_token, quota_json, status, "
                "updated_at) VALUES(?, ?, ?, ?, 'ACTIVE', ?) "
                "ON CONFLICT(manifest_root) DO UPDATE SET "
                "owner_token=excluded.owner_token, status='ACTIVE', "
                "updated_at=excluded.updated_at",
                (manifest_root, source_id, owner_token, quota_json, stamp),
            )

            rows = connection.execute(
                "SELECT asset_id, expected_bytes FROM fetch_asset_checkpoints "
                "WHERE manifest_root=? ORDER BY asset_id", (manifest_root,),
            ).fetchall()
            if rows and tuple((row[0], int(row[1])) for row in rows) \
                    != frozen_assets:
                raise ValueError(
                    "fetch checkpoint asset set changed across restart")
            if not rows:
                # A reservation without checkpoint rows was written by the
                # legacy implementation. Its provider calls may already have
                # happened, so treat each first attempt as consumed. A new
                # transfer can use its pre-reserved first attempts.
                initially_started = 0 if new_reservation else 1
                connection.executemany(
                    "INSERT INTO fetch_asset_checkpoints"
                    "(manifest_root, asset_id, expected_bytes, "
                    "attempts_reserved, attempts_started) VALUES(?, ?, ?, 1, ?)",
                    ((manifest_root, asset_id, byte_size, initially_started)
                     for asset_id, byte_size in frozen_assets),
                )
        return QuotaUsage(*used)

    @staticmethod
    def _fetch_checkpoint(row: tuple[Any, ...]) -> FetchAssetCheckpoint:
        return FetchAssetCheckpoint(
            asset_id=row[0], expected_bytes=int(row[1]),
            attempts_reserved=int(row[2]), attempts_started=int(row[3]),
            blob_sha256=row[4],
            byte_size=(None if row[5] is None else int(row[5])),
        )

    def fetch_checkpoint(
        self, manifest_root: str, asset_id: str,
    ) -> FetchAssetCheckpoint:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT asset_id, expected_bytes, attempts_reserved, "
                "attempts_started, blob_sha256, byte_size "
                "FROM fetch_asset_checkpoints WHERE manifest_root=? "
                "AND asset_id=?", (manifest_root, asset_id),
            ).fetchone()
        if row is None:
            raise KeyError(f"unknown fetch asset {asset_id!r}")
        return self._fetch_checkpoint(row)

    def begin_fetch_asset_attempt(
        self,
        manifest_root: str,
        owner_token: str,
        asset_id: str,
        *,
        now: float | None = None,
    ) -> FetchAssetCheckpoint:
        """Durably charge/record one provider call before it starts."""
        stamp = time.time() if now is None else now
        with self.connect() as connection:
            intent = connection.execute(
                "SELECT source_id, owner_token, quota_json, status "
                "FROM fetch_transfer_intents WHERE manifest_root=?",
                (manifest_root,),
            ).fetchone()
            if intent is None or intent[1] != owner_token \
                    or intent[3] != "ACTIVE":
                raise RuntimeError("fetch attempt does not own the active intent")
            row = connection.execute(
                "SELECT asset_id, expected_bytes, attempts_reserved, "
                "attempts_started, blob_sha256, byte_size "
                "FROM fetch_asset_checkpoints WHERE manifest_root=? "
                "AND asset_id=?", (manifest_root, asset_id),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown fetch asset {asset_id!r}")
            checkpoint = self._fetch_checkpoint(row)
            if checkpoint.complete:
                return checkpoint
            reserved = checkpoint.attempts_reserved
            started = checkpoint.attempts_started
            if started >= reserved:
                quota = ProviderQuota.from_dict(strict_json_loads(intent[2]))
                usage_row = connection.execute(
                    "SELECT metadata_calls, payload_calls, bytes_transferred "
                    "FROM provider_quota WHERE source_id=?", (intent[0],),
                ).fetchone()
                used = ((0, 0, 0) if usage_row is None
                        else tuple(int(value) for value in usage_row))
                updated = (used[0], used[1] + 1,
                           used[2] + checkpoint.expected_bytes)
                for value, limit, dimension in (
                        (updated[1], quota.max_payload_calls, "payload call"),
                        (updated[2], quota.max_bytes, "byte")):
                    if value > limit:
                        raise QuotaExceededError(intent[0], dimension, limit)
                connection.execute(
                    "UPDATE provider_quota SET payload_calls=?, "
                    "bytes_transferred=?, updated_at=? WHERE source_id=?",
                    (updated[1], updated[2], stamp, intent[0]),
                )
                reserved += 1
            started += 1
            connection.execute(
                "UPDATE fetch_asset_checkpoints SET attempts_reserved=?, "
                "attempts_started=? WHERE manifest_root=? AND asset_id=?",
                (reserved, started, manifest_root, asset_id),
            )
            row = connection.execute(
                "SELECT asset_id, expected_bytes, attempts_reserved, "
                "attempts_started, blob_sha256, byte_size "
                "FROM fetch_asset_checkpoints WHERE manifest_root=? "
                "AND asset_id=?", (manifest_root, asset_id),
            ).fetchone()
        return self._fetch_checkpoint(row)

    def complete_fetch_asset(
        self,
        manifest_root: str,
        owner_token: str,
        asset_id: str,
        blob_sha256: str,
        byte_size: int,
        *,
        now: float | None = None,
    ) -> FetchAssetCheckpoint:
        """Atomically publish one verified blob as a reusable checkpoint."""
        _digest(blob_sha256, "fetch checkpoint blob sha256")
        stamp = time.time() if now is None else now
        with self.connect() as connection:
            intent = connection.execute(
                "SELECT owner_token, status FROM fetch_transfer_intents "
                "WHERE manifest_root=?", (manifest_root,),
            ).fetchone()
            if intent != (owner_token, "ACTIVE"):
                raise RuntimeError("fetch completion does not own active intent")
            row = connection.execute(
                "SELECT asset_id, expected_bytes, attempts_reserved, "
                "attempts_started, blob_sha256, byte_size "
                "FROM fetch_asset_checkpoints WHERE manifest_root=? "
                "AND asset_id=?", (manifest_root, asset_id),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown fetch asset {asset_id!r}")
            checkpoint = self._fetch_checkpoint(row)
            if byte_size != checkpoint.expected_bytes:
                raise ValueError("fetch checkpoint size differs from manifest")
            if checkpoint.attempts_started < 1:
                raise RuntimeError("a fetch cannot complete before an attempt")
            if checkpoint.complete:
                if (checkpoint.blob_sha256, checkpoint.byte_size) != (
                        blob_sha256, byte_size):
                    raise ValueError("fetch checkpoint content conflicts")
                return checkpoint
            connection.execute(
                "UPDATE fetch_asset_checkpoints SET blob_sha256=?, "
                "byte_size=?, completed_at=? WHERE manifest_root=? "
                "AND asset_id=?",
                (blob_sha256, byte_size, stamp, manifest_root, asset_id),
            )
        return self.fetch_checkpoint(manifest_root, asset_id)

    def fetch_retry_count(self, manifest_root: str) -> int:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT COALESCE(SUM(CASE WHEN attempts_started > 1 "
                "THEN attempts_started - 1 ELSE 0 END), 0) "
                "FROM fetch_asset_checkpoints WHERE manifest_root=?",
                (manifest_root,),
            ).fetchone()
        return int(row[0])

    def complete_fetch_transfer(
        self, manifest_root: str, owner_token: str, *, now: float | None = None,
    ) -> None:
        stamp = time.time() if now is None else now
        with self.connect() as connection:
            intent = connection.execute(
                "SELECT owner_token, status FROM fetch_transfer_intents "
                "WHERE manifest_root=?", (manifest_root,),
            ).fetchone()
            if intent is None:
                # A verified legacy receipt predates transfer intents.
                return
            if intent[0] != owner_token and intent[1] != "COMPLETE":
                raise RuntimeError("fetch completion does not own active intent")
            missing = connection.execute(
                "SELECT COUNT(*) FROM fetch_asset_checkpoints "
                "WHERE manifest_root=? AND blob_sha256 IS NULL",
                (manifest_root,),
            ).fetchone()[0]
            if int(missing):
                raise RuntimeError("fetch transfer has incomplete assets")
            connection.execute(
                "UPDATE fetch_transfer_intents SET status='COMPLETE', "
                "updated_at=? WHERE manifest_root=?", (stamp, manifest_root),
            )

    def reconcile_fetch_receipt(
        self, manifest_root: str, *, now: float | None = None,
    ) -> None:
        """Close the crash window after receipt rename but before DB status."""
        stamp = time.time() if now is None else now
        with self.connect() as connection:
            intent = connection.execute(
                "SELECT status FROM fetch_transfer_intents "
                "WHERE manifest_root=?", (manifest_root,),
            ).fetchone()
            if intent is None or intent[0] == "COMPLETE":
                return
            counts = connection.execute(
                "SELECT COUNT(*), SUM(CASE WHEN blob_sha256 IS NULL THEN 1 "
                "ELSE 0 END) FROM fetch_asset_checkpoints "
                "WHERE manifest_root=?", (manifest_root,),
            ).fetchone()
            if int(counts[0]) < 1 or int(counts[1] or 0) != 0:
                raise RuntimeError(
                    "a fetch receipt exists before all durable checkpoints")
            connection.execute(
                "UPDATE fetch_transfer_intents SET status='COMPLETE', "
                "updated_at=? WHERE manifest_root=?", (stamp, manifest_root),
            )

    def set_cooldown(self, source_id: str, until_epoch: float,
                     reason: str) -> None:
        _required_text(reason, "cooldown reason")
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO provider_cooldowns(source_id, until_epoch, reason) "
                "VALUES(?, ?, ?) ON CONFLICT(source_id) DO UPDATE SET "
                "until_epoch=excluded.until_epoch, reason=excluded.reason",
                (source_id, float(until_epoch), reason))

    def cooldown_for(self, source_id: str) -> tuple[float, str] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT until_epoch, reason FROM provider_cooldowns "
                "WHERE source_id=?", (source_id,)).fetchone()
        return None if row is None else (float(row[0]), row[1])

    def in_cooldown(self, source_id: str, *, now: float | None = None) -> bool:
        state = self.cooldown_for(source_id)
        if state is None:
            return False
        return (time.time() if now is None else now) < state[0]

    # -- persisted truncation --------------------------------------------

    def record_limit(self, session_id: str, code: str, subject: str) -> None:
        """Persist one typed truncation so a restart cannot forget it."""
        _required_text(code, "limit code")
        _required_text(subject, "limit subject")
        with self.connect() as connection:
            session = connection.execute(
                "SELECT frozen_at FROM planning_sessions WHERE session_id=?",
                (session_id,)).fetchone()
            if session is None:
                raise KeyError(f"unknown planning session {session_id!r}")
            if session[0] is not None:
                raise FrozenSessionError(
                    f"planning session {session_id!r} is frozen and read-only")
            connection.execute(
                "INSERT OR IGNORE INTO session_limits(session_id, code, subject)"
                " VALUES(?, ?, ?)", (session_id, code, subject))

    def limits_for(self, session_id: str) -> tuple[tuple[str, str], ...]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT code, subject FROM session_limits WHERE session_id=? "
                "ORDER BY code, subject", (session_id,)).fetchall()
        return tuple((row[0], row[1]) for row in rows)

    def session_digest(self, session_id: str) -> str:
        """Content identity of the exact discovered metadata and frontier.

        Timestamps and provider-global quota/cooldown state are intentionally
        excluded, but the complete query and candidate records are included.
        Hashing only IDs here previously allowed a provider to change extent,
        size, or conditional identity without changing the discovery snapshot.
        """
        with self.connect() as connection:
            session = connection.execute(
                "SELECT scope_id, scope_json FROM planning_sessions "
                "WHERE session_id=?", (session_id,)).fetchone()
            if session is None:
                raise KeyError(f"unknown planning session {session_id!r}")
            cursors = connection.execute(
                "SELECT query_id, query_json, cursor, pages_read, exhausted "
                "FROM query_cursors WHERE session_id=? ORDER BY query_id",
                (session_id,)).fetchall()
            candidates = connection.execute(
                "SELECT query_id, asset_id, ordinal, candidate_json "
                "FROM discovered_candidates "
                "WHERE session_id=? ORDER BY query_id, ordinal",
                (session_id,)).fetchall()
            limits = connection.execute(
                "SELECT code, subject FROM session_limits WHERE session_id=? "
                "ORDER BY code, subject", (session_id,)).fetchall()
        return strict_hash({
            "schema": "stage8r-planning-session-content-v3",
            "session_id": session_id,
            "scope_id": session[0],
            "scope": (None if session[1] is None
                      else strict_json_loads(session[1])),
            "cursors": [list(row) for row in cursors],
            "candidates": [list(row) for row in candidates],
            "limits": [list(row) for row in limits],
        })


__all__ = [
    "CursorState",
    "FetchAssetCheckpoint",
    "FrozenSessionError",
    "PlanningSessionStore",
    "ProviderQuota",
    "QuotaExceededError",
    "QuotaUsage",
]
