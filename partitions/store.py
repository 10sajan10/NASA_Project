"""Durable partition state whose admission is a single atomic transaction.

The correctness property this file exists for, from Section 8.7:

> Admission is one authoritative control-store transaction: deterministically
> generate the next partition window, idempotently upsert its logical tasks by
> stable key, and advance the cursor/version only if those upserts succeed. A
> crash before commit repeats harmless upserts; a crash after commit resumes at
> the next cursor. Cursor advancement is never a separate write that can skip a
> window.

So the upserts and the cursor advance share one transaction, the upserts are
``INSERT OR IGNORE`` on a stable key, and a uniqueness constraint on
``(collection, partition_index)`` means even a key collision cannot multiply a
logical partition.  A crash anywhere inside the window rolls back both halves,
and the retry re-admits exactly the same partitions.
"""
from __future__ import annotations

import contextlib
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Iterator

from capabilities.implementation import _required_text
from engine.runtime.identity import strict_canonical_json

from .manifest import CollectionManifest, CollectionState
from .packet import MemberOutcome
from .space import PartitionSetSpec
from .template import PartitionTaskTemplate

_SCHEMA = """
CREATE TABLE IF NOT EXISTS collections (
  collection_id TEXT PRIMARY KEY,
  manifest_json TEXT NOT NULL,
  spec_json TEXT NOT NULL,
  template_json TEXT NOT NULL,
  created_at REAL NOT NULL
) STRICT;
CREATE TABLE IF NOT EXISTS cursors (
  collection_id TEXT PRIMARY KEY REFERENCES collections(collection_id),
  next_index INTEGER NOT NULL CHECK(next_index >= 0),
  version INTEGER NOT NULL CHECK(version >= 0),
  updated_at REAL NOT NULL
) STRICT;
CREATE TABLE IF NOT EXISTS logical_tasks (
  collection_id TEXT NOT NULL REFERENCES collections(collection_id),
  logical_task_key TEXT NOT NULL,
  partition_index INTEGER NOT NULL CHECK(partition_index >= 0),
  state TEXT NOT NULL CHECK(state IN
    ('ADMITTED','COMMITTED','FAILED')),
  updated_at REAL NOT NULL,
  PRIMARY KEY(collection_id, logical_task_key),
  UNIQUE(collection_id, partition_index)
) STRICT;
CREATE INDEX IF NOT EXISTS logical_task_state_idx
  ON logical_tasks(collection_id, state, partition_index);
""".replace(") STRICT;", ");")  # declared intent; this node's SQLite predates it


class CursorConflictError(RuntimeError):
    """Another writer advanced the cursor while this admission was running."""


@dataclass(frozen=True)
class CursorState:
    next_index: int
    version: int


@dataclass(frozen=True)
class AdmissionResult:
    """What one admission transaction actually did."""

    admitted: int
    first_index: int
    next_index: int
    version: int
    exhausted: bool

    @property
    def indices(self) -> range:
        return range(self.first_index, self.first_index + self.admitted)


class PartitionStore:
    """Single-writer SQLite home for cursors and logical partition tasks."""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        if not self.db_path.is_absolute():
            raise ValueError("partition store path must be absolute")
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
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
                raise RuntimeError("partition store requires SQLite WAL mode")
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

    # -- collections ------------------------------------------------------

    def open_collection(self, manifest: CollectionManifest,
                        spec: PartitionSetSpec,
                        template: PartitionTaskTemplate, *,
                        now: float | None = None) -> None:
        """Register a collection and its cursor. Idempotent and consistent."""
        if manifest.set_id != spec.set_id:
            raise ValueError("manifest does not describe this partition set")
        if manifest.template_id != template.template_id:
            raise ValueError("manifest does not describe this task template")
        if manifest.expected != spec.total:
            raise ValueError(
                f"manifest expects {manifest.expected} partitions but the "
                f"space contains {spec.total}")
        stamp = time.time() if now is None else now
        with self.connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO collections"
                "(collection_id, manifest_json, spec_json, template_json,"
                " created_at) VALUES(?, ?, ?, ?, ?)",
                (manifest.collection_id,
                 strict_canonical_json(manifest.to_dict()),
                 strict_canonical_json(spec.to_dict()),
                 strict_canonical_json(template.to_dict()), stamp))
            connection.execute(
                "INSERT OR IGNORE INTO cursors"
                "(collection_id, next_index, version, updated_at)"
                " VALUES(?, 0, 0, ?)", (manifest.collection_id, stamp))

    def cursor(self, collection_id: str) -> CursorState:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT next_index, version FROM cursors WHERE collection_id=?",
                (collection_id,)).fetchone()
        if row is None:
            raise KeyError(f"unknown collection {collection_id!r}")
        return CursorState(int(row[0]), int(row[1]))

    # -- the atomic admission --------------------------------------------

    def admit_window(
        self,
        collection_id: str,
        spec: PartitionSetSpec,
        template: PartitionTaskTemplate,
        window_size: int,
        *,
        fault: Callable[[str], None] | None = None,
        now: float | None = None,
    ) -> AdmissionResult:
        """Admit the next bounded window of partitions in one transaction.

        ``fault`` is a test seam: it is called at named points inside the
        transaction so a crash can be injected exactly where it would be most
        damaging.  Raising from it rolls the whole window back, which is the
        behaviour the exit gate demands.
        """
        _required_text(collection_id, "collection_id")
        if (isinstance(window_size, bool) or not isinstance(window_size, int)
                or window_size < 1):
            raise ValueError("window_size must be a positive integer")
        stamp = time.time() if now is None else now

        with self.connect() as connection:
            row = connection.execute(
                "SELECT next_index, version FROM cursors WHERE collection_id=?",
                (collection_id,)).fetchone()
            if row is None:
                raise KeyError(f"unknown collection {collection_id!r}")
            next_index, version = int(row[0]), int(row[1])
            if fault is not None:
                fault("after_cursor_read")

            admitted = 0
            # The window is a generator: at most ``window_size`` partition keys
            # exist at once, regardless of how large the space is.
            for key in spec.iter_keys(next_index, window_size):
                connection.execute(
                    "INSERT OR IGNORE INTO logical_tasks"
                    "(collection_id, logical_task_key, partition_index, state,"
                    " updated_at) VALUES(?, ?, ?, 'ADMITTED', ?)",
                    (collection_id, template.logical_task_key(key), key.index,
                     stamp))
                admitted += 1
            if fault is not None:
                fault("before_cursor_advance")

            # Guarded by the version we read, so a concurrent admission cannot
            # advance the cursor twice over the same window.
            changed = connection.execute(
                "UPDATE cursors SET next_index=?, version=?, updated_at=? "
                "WHERE collection_id=? AND version=?",
                (next_index + admitted, version + 1, stamp, collection_id,
                 version)).rowcount
            if changed != 1:
                raise CursorConflictError(
                    f"cursor for {collection_id!r} moved during admission")
            if fault is not None:
                fault("after_cursor_advance")

        return AdmissionResult(
            admitted=admitted, first_index=next_index,
            next_index=next_index + admitted, version=version + 1,
            exhausted=(next_index + admitted) >= spec.total)

    # -- outcomes and state ----------------------------------------------

    def record_outcome(self, collection_id: str, logical_task_key: str,
                       outcome: MemberOutcome, *,
                       now: float | None = None) -> bool:
        """Record one partition's terminal state. Committed is final.

        Returns whether the row changed.  A committed partition is never moved
        back to failed, so a duplicate or late packet result cannot un-commit
        work that already landed.
        """
        if not isinstance(outcome, MemberOutcome):
            raise TypeError("outcome must be a MemberOutcome")
        if outcome is MemberOutcome.NOT_ATTEMPTED:
            return False
        state = ("COMMITTED" if outcome is MemberOutcome.COMMITTED else "FAILED")
        stamp = time.time() if now is None else now
        with self.connect() as connection:
            changed = connection.execute(
                "UPDATE logical_tasks SET state=?, updated_at=? "
                "WHERE collection_id=? AND logical_task_key=? "
                "AND state != 'COMMITTED'",
                (state, stamp, collection_id, logical_task_key)).rowcount
        return bool(changed)

    def record_outcomes(self, collection_id: str,
                        items: Iterable[tuple[str, MemberOutcome]], *,
                        now: float | None = None) -> int:
        """Record many partition outcomes in **one** transaction.

        Per-partition durability is correct but one fsync per partition makes a
        10^4-partition collection dominated by commit overhead.  Batching keeps
        the same semantics -- committed is still final, and the whole batch is
        atomic -- while paying for one flush instead of thousands.
        """
        stamp = time.time() if now is None else now
        changed = 0
        with self.connect() as connection:
            for logical_task_key, outcome in items:
                if not isinstance(outcome, MemberOutcome):
                    raise TypeError("outcome must be a MemberOutcome")
                if outcome is MemberOutcome.NOT_ATTEMPTED:
                    continue
                state = ("COMMITTED" if outcome is MemberOutcome.COMMITTED
                         else "FAILED")
                changed += connection.execute(
                    "UPDATE logical_tasks SET state=?, updated_at=? "
                    "WHERE collection_id=? AND logical_task_key=? "
                    "AND state != 'COMMITTED'",
                    (state, stamp, collection_id, logical_task_key)).rowcount
        return changed

    def state(self, collection_id: str) -> CollectionState:
        with self.connect() as connection:
            manifest_row = connection.execute(
                "SELECT manifest_json FROM collections WHERE collection_id=?",
                (collection_id,)).fetchone()
            if manifest_row is None:
                raise KeyError(f"unknown collection {collection_id!r}")
            counts = dict(connection.execute(
                "SELECT state, COUNT(*) FROM logical_tasks "
                "WHERE collection_id=? GROUP BY state",
                (collection_id,)).fetchall())
            expected = int(connection.execute(
                "SELECT json_extract(manifest_json, '$.expected') "
                "FROM collections WHERE collection_id=?",
                (collection_id,)).fetchone()[0])
        admitted = sum(int(value) for value in counts.values())
        return CollectionState(
            expected=expected, admitted=admitted,
            committed=int(counts.get("COMMITTED", 0)),
            failed=int(counts.get("FAILED", 0)))

    def in_flight(self, collection_id: str) -> int:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) FROM logical_tasks "
                "WHERE collection_id=? AND state='ADMITTED'",
                (collection_id,)).fetchone()
        return int(row[0])

    def iter_admitted(self, collection_id: str, limit: int
                      ) -> Iterator[tuple[str, int]]:
        """Stream admitted-but-unresolved partitions, bounded by ``limit``."""
        if limit < 1:
            raise ValueError("limit must be positive")
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT logical_task_key, partition_index FROM logical_tasks "
                "WHERE collection_id=? AND state='ADMITTED' "
                "ORDER BY partition_index LIMIT ?",
                (collection_id, limit)).fetchall()
        for key, index in rows:
            yield (key, int(index))

    def partition_indices(self, collection_id: str) -> tuple[int, ...]:
        """Every admitted partition index. Test helper; not a runtime path."""
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT partition_index FROM logical_tasks "
                "WHERE collection_id=? ORDER BY partition_index",
                (collection_id,)).fetchall()
        return tuple(int(row[0]) for row in rows)


__all__ = [
    "AdmissionResult",
    "CursorConflictError",
    "CursorState",
    "PartitionStore",
]
