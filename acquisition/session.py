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
from typing import Iterable, Iterator

from capabilities.implementation import _required_text
from engine.runtime.identity import (
    strict_canonical_json,
    strict_hash,
    strict_json_loads,
)

from .connector import AssetCandidate

_SCHEMA = """
CREATE TABLE IF NOT EXISTS planning_sessions (
  session_id TEXT PRIMARY KEY,
  created_at REAL NOT NULL,
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
""".replace(") STRICT;", ");")  # declared intent; this node's SQLite predates it


class QuotaExceededError(RuntimeError):
    """A provider budget would be exceeded by the requested work."""

    def __init__(self, source_id: str, dimension: str, limit: int) -> None:
        super().__init__(
            f"provider {source_id!r} exhausted its {dimension} budget of {limit}")
        self.source_id = source_id
        self.dimension = dimension
        self.limit = limit


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
            row = connection.execute(
                "SELECT pages_read FROM query_cursors "
                "WHERE session_id=? AND query_id=?",
                (session_id, query_id)).fetchone()
            pages_read = (0 if row is None else int(row[0])) + 1
            ordinal_row = connection.execute(
                "SELECT COALESCE(MAX(ordinal), -1) FROM discovered_candidates "
                "WHERE session_id=? AND query_id=?",
                (session_id, query_id)).fetchone()
            ordinal = int(ordinal_row[0])
            for candidate in candidates:
                ordinal += 1
                changed = connection.execute(
                    "INSERT OR IGNORE INTO discovered_candidates"
                    "(session_id, query_id, asset_id, ordinal, candidate_json) "
                    "VALUES(?, ?, ?, ?, ?)",
                    (session_id, query_id, candidate.asset_id, ordinal,
                     strict_canonical_json(candidate.to_dict()))).rowcount
                if changed:
                    stored += 1
                else:
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
        """A content digest over everything durable in this session."""
        with self.connect() as connection:
            cursors = connection.execute(
                "SELECT query_id, cursor, pages_read, exhausted "
                "FROM query_cursors WHERE session_id=? ORDER BY query_id",
                (session_id,)).fetchall()
            candidates = connection.execute(
                "SELECT query_id, asset_id, ordinal FROM discovered_candidates "
                "WHERE session_id=? ORDER BY query_id, ordinal",
                (session_id,)).fetchall()
            limits = connection.execute(
                "SELECT code, subject FROM session_limits WHERE session_id=? "
                "ORDER BY code, subject", (session_id,)).fetchall()
        return strict_hash({
            "schema": "stage5-planning-session-digest-v1",
            "session_id": session_id,
            "cursors": [list(row) for row in cursors],
            "candidates": [list(row) for row in candidates],
            "limits": [list(row) for row in limits],
        })


__all__ = [
    "CursorState",
    "PlanningSessionStore",
    "ProviderQuota",
    "QuotaExceededError",
    "QuotaUsage",
]
