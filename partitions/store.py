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
import math
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Iterator

from capabilities.implementation import _required_text
from engine.runtime.identity import strict_canonical_json, strict_json_loads

from .manifest import CollectionManifest, CollectionState
from .packet import (
    MemberOutcome,
    PacketAttempt,
    PacketResult,
    WorkPacket,
    expected_deployment_binding_id,
)
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
  attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
  updated_at REAL NOT NULL,
  PRIMARY KEY(collection_id, logical_task_key),
  UNIQUE(collection_id, partition_index)
) STRICT;
CREATE TABLE IF NOT EXISTS partition_attempts (
  collection_id TEXT NOT NULL REFERENCES collections(collection_id),
  logical_task_key TEXT NOT NULL,
  attempt_number INTEGER NOT NULL CHECK(attempt_number >= 1),
  attempt_id TEXT NOT NULL,
  packet_id TEXT NOT NULL,
  fence_token TEXT NOT NULL,
  outcome TEXT NOT NULL CHECK(outcome IN
    ('COMMITTED','FAILED','NOT_ATTEMPTED')),
  created_at REAL NOT NULL,
  PRIMARY KEY(collection_id, logical_task_key, attempt_number)
) STRICT;
CREATE TABLE IF NOT EXISTS packet_results (
  collection_id TEXT NOT NULL REFERENCES collections(collection_id),
  attempt_id TEXT NOT NULL,
  result_id TEXT NOT NULL,
  packet_id TEXT NOT NULL,
  attempt_number INTEGER NOT NULL CHECK(attempt_number >= 1),
  consumes_attempt INTEGER NOT NULL CHECK(consumes_attempt IN (0,1)),
  decision_json TEXT NOT NULL,
  created_at REAL NOT NULL,
  PRIMARY KEY(collection_id, attempt_id),
  UNIQUE(collection_id, result_id)
) STRICT;
CREATE TABLE IF NOT EXISTS packet_attempt_intents (
  collection_id TEXT NOT NULL REFERENCES collections(collection_id),
  attempt_id TEXT NOT NULL,
  packet_id TEXT NOT NULL,
  packet_json TEXT NOT NULL,
  attempt_number INTEGER NOT NULL CHECK(attempt_number >= 1),
  fence_token TEXT NOT NULL,
  runtime_run_id TEXT NOT NULL,
  runtime_plan_id TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN
    ('ACTIVE','COMPLETED','RELEASED','EXPIRED')),
  lease_expires_at REAL NOT NULL,
  reconciliation_kind TEXT CHECK(reconciliation_kind IS NULL OR
    reconciliation_kind IN ('PROVEN_NOT_LAUNCHED','RUNTIME_TERMINAL')),
  reconciled_at REAL,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  PRIMARY KEY(collection_id, attempt_id)
) STRICT;
CREATE TABLE IF NOT EXISTS packet_member_leases (
  collection_id TEXT NOT NULL REFERENCES collections(collection_id),
  logical_task_key TEXT NOT NULL,
  attempt_id TEXT NOT NULL,
  packet_id TEXT NOT NULL,
  lease_expires_at REAL NOT NULL,
  PRIMARY KEY(collection_id, logical_task_key)
) STRICT;
CREATE UNIQUE INDEX IF NOT EXISTS packet_attempt_active_idx
  ON packet_attempt_intents(collection_id, packet_id)
  WHERE status='ACTIVE';
CREATE INDEX IF NOT EXISTS logical_task_state_idx
  ON logical_tasks(collection_id, state, partition_index);
""".replace(") STRICT;", ");")  # declared intent; this node's SQLite predates it


class CursorConflictError(RuntimeError):
    """Another writer advanced the cursor while this admission was running."""


class PacketResultConflictError(RuntimeError):
    """One stable packet attempt was replayed with different member outcomes."""


class PacketAttemptAuthorityError(ValueError):
    """An attempt was not the store-registered, currently fenced lease."""


class PacketAttemptConflictError(RuntimeError):
    """A live packet lease prevents a second provider submission."""


@dataclass(frozen=True)
class CursorState:
    next_index: int
    version: int


@dataclass(frozen=True)
class RetryDecision:
    """What one packet result did to each of its members."""

    committed: tuple[str, ...]
    requeued: tuple[str, ...]
    exhausted: tuple[str, ...]

    @property
    def retried(self) -> int:
        return len(self.requeued)

    def to_dict(self) -> dict[str, list[str]]:
        return {
            "committed": list(self.committed),
            "requeued": list(self.requeued),
            "exhausted": list(self.exhausted),
        }


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

    def __init__(self, db_path: Path | str, *,
                 clock: Callable[[], float] = time.time) -> None:
        self.db_path = Path(db_path)
        if not self.db_path.is_absolute():
            raise ValueError("partition store path must be absolute")
        if not callable(clock):
            raise TypeError("partition store clock must be callable")
        # Lease time is store authority. Result callers cannot backdate a
        # delivery to revive an expired fence; tests inject a controlled clock
        # when they need to advance time deterministically.
        self._clock = clock
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = self._raw_connect()
        try:
            connection.executescript(_SCHEMA)
            # Additive migration for pre-8R control stores.  Existing packet
            # intents intentionally receive NULL authority: they cannot be
            # completed or reissued automatically and therefore remain a
            # reconciliation obligation rather than being guessed safe.
            columns = {
                row[1] for row in connection.execute(
                    "PRAGMA table_info(packet_attempt_intents)")
            }
            for name in ("runtime_run_id", "runtime_plan_id",
                         "reconciliation_kind", "reconciled_at"):
                if name not in columns:
                    column_type = ("REAL" if name == "reconciled_at"
                                   else "TEXT")
                    connection.execute(
                        f"ALTER TABLE packet_attempt_intents "
                        f"ADD COLUMN {name} {column_type}")
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
        stamp = self._verified_time(
            time.time() if now is None else now, "now")
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

    def collection_contract(
            self, collection_id: str,
    ) -> tuple[PartitionSetSpec, PartitionTaskTemplate]:
        """Reconstruct the immutable partition space and task template."""
        with self.connect() as connection:
            row = connection.execute(
                "SELECT spec_json,template_json FROM collections "
                "WHERE collection_id=?", (collection_id,)).fetchone()
        if row is None:
            raise KeyError(f"unknown collection {collection_id!r}")
        return (
            PartitionSetSpec.from_dict(strict_json_loads(row[0])),
            PartitionTaskTemplate.from_dict(strict_json_loads(row[1])),
        )

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
            # The collection's own definitions are authoritative.  Admitting
            # with a different spec or template would advance the cursor while
            # storing tasks for work nobody registered, and the wrong task
            # would then be permanent.
            registered = connection.execute(
                "SELECT json_extract(spec_json, '$.set_id'),"
                "       json_extract(template_json, '$.template_id') "
                "FROM collections WHERE collection_id=?",
                (collection_id,)).fetchone()
            if registered is None:
                raise KeyError(f"unknown collection {collection_id!r}")
            if registered[0] != spec.set_id:
                raise ValueError(
                    f"collection {collection_id!r} is registered against "
                    f"partition set {registered[0][:12]}, not {spec.set_id[:12]}")
            if registered[1] != template.template_id:
                raise ValueError(
                    f"collection {collection_id!r} is registered against "
                    f"template {registered[1][:12]}, not "
                    f"{template.template_id[:12]}")
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

    # -- provider-submission authority ---------------------------------

    @staticmethod
    def _verified_time(value: float, name: str) -> float:
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(float(value))):
            raise ValueError(f"{name} must be finite")
        return float(value)

    @staticmethod
    def _verify_packet_authority(
        connection: sqlite3.Connection,
        collection_id: str,
        packet: WorkPacket,
        *,
        require_admitted: bool = False,
    ) -> PartitionTaskTemplate:
        """Reconstruct every packet member from frozen collection authority."""
        if not isinstance(packet, WorkPacket):
            raise TypeError("packet must be a WorkPacket")
        if packet.collection_id != collection_id:
            raise ValueError("packet does not belong to this collection")
        registered_row = connection.execute(
            "SELECT spec_json, template_json FROM collections "
            "WHERE collection_id=?", (collection_id,)).fetchone()
        if registered_row is None:
            raise KeyError(f"unknown collection {collection_id!r}")
        spec = PartitionSetSpec.from_dict(strict_json_loads(registered_row[0]))
        template = PartitionTaskTemplate.from_dict(
            strict_json_loads(registered_row[1]))
        if packet.template_id != template.template_id:
            raise ValueError(
                f"collection {collection_id!r} is registered against "
                f"template {template.template_id!r}, not packet template "
                f"{packet.template_id!r}")

        expected_binding = expected_deployment_binding_id(
            collection_id, template.template_id)
        for member in packet.members:
            row = connection.execute(
                "SELECT partition_index,state FROM logical_tasks "
                "WHERE collection_id=? AND logical_task_key=?",
                (collection_id, member.logical_task_key)).fetchone()
            if row is None:
                raise PacketAttemptAuthorityError(
                    f"packet member {member.logical_task_key!r} is not an "
                    "authoritative logical task of this collection")
            durable_index = int(row[0])
            if member.partition_index != durable_index:
                raise PacketAttemptAuthorityError(
                    f"packet member {member.logical_task_key!r} claims "
                    f"partition index {member.partition_index}, but the "
                    f"collection records {durable_index}")
            expected_key = template.logical_task_key(
                spec.key_at(member.partition_index))
            if member.logical_task_key != expected_key:
                raise PacketAttemptAuthorityError(
                    "packet member logical identity does not reconstruct from "
                    "the registered partition set and template")
            if member.deployment_binding_id != expected_binding:
                raise PacketAttemptAuthorityError(
                    f"packet member {member.logical_task_key!r} has a forged "
                    "deployment binding")
            if require_admitted and row[1] != "ADMITTED":
                raise PacketAttemptAuthorityError(
                    f"packet member {member.logical_task_key!r} is {row[1]}; "
                    "only currently admitted work can be submitted")
        return template

    def register_packet_attempt(
        self,
        collection_id: str,
        packet: WorkPacket,
        *,
        fence_token: str,
        runtime_run_id: str,
        runtime_plan_id: str,
        lease_duration_s: float = 300.0,
        expected_attempt_number: int | None = None,
    ) -> PacketAttempt:
        """Durably fence one exact runtime run before any work begins.

        The store, rather than a result sender, derives the current attempt
        number.  A live lease is exclusive for the packet.  An expired lease
        is durably marked but remains member-fenced: without a provider proof
        that it never launched, automatic reissue would risk duplicate work.
        """
        _required_text(collection_id, "collection_id")
        _required_text(fence_token, "attempt fence_token")
        _required_text(runtime_run_id, "packet runtime_run_id")
        if (not isinstance(runtime_plan_id, str)
                or len(runtime_plan_id) != 64
                or any(value not in "0123456789abcdef"
                       for value in runtime_plan_id)):
            raise ValueError(
                "packet runtime_plan_id must be a lowercase SHA-256 digest")
        duration = self._verified_time(lease_duration_s, "lease_duration_s")
        if duration <= 0:
            raise ValueError("lease_duration_s must be positive")
        stamp = self._verified_time(self._clock(), "store clock")
        expires_at = stamp + duration
        if not math.isfinite(expires_at):
            raise ValueError("packet lease expiration must be finite")
        if expected_attempt_number is not None and (
                isinstance(expected_attempt_number, bool)
                or not isinstance(expected_attempt_number, int)
                or expected_attempt_number < 1):
            raise ValueError(
                "expected_attempt_number must be a positive integer")

        with self.connect() as connection:
            self._verify_packet_authority(
                connection, collection_id, packet, require_admitted=True)
            packet_json = strict_canonical_json(packet.to_dict())

            # Expiration is made durable before looking for the exclusive
            # active lease, so a restarted controller can fence the old worker.
            # This is collection-wide because a retry may packetise the same
            # logical member differently; exclusivity belongs to each member,
            # not merely to one caller-chosen packet grouping.
            connection.execute(
                "UPDATE packet_attempt_intents "
                "SET status='EXPIRED', updated_at=? "
                "WHERE collection_id=? AND status='ACTIVE' "
                "AND lease_expires_at<=?",
                (stamp, collection_id, stamp))
            active = connection.execute(
                "SELECT attempt_id,attempt_number,fence_token,packet_json,"
                "runtime_run_id,runtime_plan_id "
                "FROM packet_attempt_intents WHERE collection_id=? "
                "AND packet_id=? AND status='ACTIVE'",
                (collection_id, packet.packet_id)).fetchone()
            if active is not None:
                if (active[2] == fence_token and active[3] == packet_json
                        and active[4] == runtime_run_id
                        and active[5] == runtime_plan_id):
                    # Registration is safe to replay after an uncertain client
                    # acknowledgement; it cannot create a second submission.
                    return PacketAttempt(
                        active[0], packet.packet_id, int(active[1]), active[2])
                raise PacketAttemptConflictError(
                    "packet already has an unexpired registered attempt")

            for member in packet.members:
                unresolved = connection.execute(
                    "SELECT i.packet_id FROM packet_attempt_intents i, "
                    "json_each(i.packet_json, '$.members') AS m "
                    "WHERE i.collection_id=? AND i.status='EXPIRED' "
                    "AND json_extract(m.value, '$.logical_task_key')=? "
                    "LIMIT 1",
                    (collection_id, member.logical_task_key),
                ).fetchone()
                if unresolved is not None:
                    raise PacketAttemptConflictError(
                        f"logical task {member.logical_task_key!r} has an "
                        f"expired packet {unresolved[0]!r}; provider launch "
                        "must be reconciled before any reissue")
                leased = connection.execute(
                    "SELECT packet_id FROM packet_member_leases "
                    "WHERE collection_id=? AND logical_task_key=?",
                    (collection_id, member.logical_task_key)).fetchone()
                if leased is not None:
                    raise PacketAttemptConflictError(
                        f"logical task {member.logical_task_key!r} already "
                        f"belongs to packet {leased[0]!r}; an expired lease "
                        "remains fenced until provider launch is reconciled")

            previous_number = connection.execute(
                "SELECT MAX(attempt_number) FROM packet_results "
                "WHERE collection_id=? AND packet_id=? AND consumes_attempt=1",
                (collection_id, packet.packet_id)).fetchone()[0]
            attempt_number = (1 if previous_number is None
                              else int(previous_number) + 1)
            if (expected_attempt_number is not None
                    and expected_attempt_number != attempt_number):
                raise PacketAttemptAuthorityError(
                    "packet attempt number is not the next durable sequence "
                    f"value: expected {attempt_number}, observed "
                    f"{expected_attempt_number}")
            attempt = PacketAttempt.bind(
                packet, attempt_number, fence_token=fence_token)
            prior = connection.execute(
                "SELECT status FROM packet_attempt_intents "
                "WHERE collection_id=? AND attempt_id=?",
                (collection_id, attempt.attempt_id)).fetchone()
            if prior is not None:
                raise PacketAttemptAuthorityError(
                    "packet attempt fence was already used and cannot be "
                    "registered again")
            connection.execute(
                "INSERT INTO packet_attempt_intents"
                "(collection_id, attempt_id, packet_id, packet_json,"
                " attempt_number, fence_token,runtime_run_id,runtime_plan_id,"
                " status, lease_expires_at,"
                " created_at, updated_at) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE', ?, ?, ?)",
                (collection_id, attempt.attempt_id, packet.packet_id,
                 packet_json, attempt.attempt_number, attempt.fence_token,
                 runtime_run_id, runtime_plan_id,
                 expires_at, stamp, stamp))
            connection.executemany(
                "INSERT INTO packet_member_leases"
                "(collection_id, logical_task_key, attempt_id, packet_id,"
                " lease_expires_at) VALUES(?, ?, ?, ?, ?)",
                ((collection_id, member.logical_task_key, attempt.attempt_id,
                  packet.packet_id, expires_at) for member in packet.members))
        return attempt

    def reconcile_expired_packet_not_launched(
        self,
        collection_id: str,
        packet: WorkPacket,
        attempt: PacketAttempt,
        runtime_store: object,
    ) -> tuple[str, ...]:
        """Release an expired fence only after a zero-launch runtime proof.

        This is the deliberately narrow liveness complement to the existing
        fail-safe fence.  A live controller, any runtime attempt/lease, any
        task attempt count, or any artifact commit refuses the proof.  The
        RuntimeStore cancellation happens first; if the process crashes before
        this SQLite release, replay observes the same cancelled run and
        completes the second half idempotently.
        """
        from engine.runtime.state import RuntimeStore

        if not isinstance(runtime_store, RuntimeStore):
            raise TypeError("runtime_store must be a RuntimeStore authority")
        if packet.collection_id != collection_id:
            raise ValueError("packet does not belong to this collection")
        if attempt.packet_id != packet.packet_id:
            raise ValueError("attempt does not belong to this packet")
        stamp = self._verified_time(self._clock(), "store clock")
        packet_json = strict_canonical_json(packet.to_dict())
        with self.connect() as connection:
            self._verify_packet_authority(
                connection, collection_id, packet, require_admitted=True)
            connection.execute(
                "UPDATE packet_attempt_intents SET status='EXPIRED', "
                "updated_at=? WHERE collection_id=? AND attempt_id=? "
                "AND status='ACTIVE' AND lease_expires_at<=?",
                (stamp, collection_id, attempt.attempt_id, stamp),
            )
            intent = connection.execute(
                "SELECT packet_id,packet_json,attempt_number,fence_token,"
                "runtime_run_id,runtime_plan_id,status,reconciliation_kind "
                "FROM packet_attempt_intents WHERE collection_id=? "
                "AND attempt_id=?",
                (collection_id, attempt.attempt_id),
            ).fetchone()
            if intent is None:
                raise PacketAttemptAuthorityError(
                    "packet attempt was never registered")
            expected = (
                packet.packet_id, packet_json, attempt.attempt_number,
                attempt.fence_token)
            if tuple(intent[:4]) != expected:
                raise PacketAttemptAuthorityError(
                    "expired reconciliation identity does not match packet")
            if intent[6] == "RELEASED" \
                    and intent[7] == "PROVEN_NOT_LAUNCHED":
                return packet.logical_task_keys
            if intent[6] != "EXPIRED":
                raise PacketAttemptAuthorityError(
                    "packet attempt is not an expired reconciliation target")
            runtime_run_id, runtime_plan_id = intent[4], intent[5]
            if (not isinstance(runtime_run_id, str) or not runtime_run_id
                    or not isinstance(runtime_plan_id, str)
                    or len(runtime_plan_id) != 64):
                raise PacketAttemptAuthorityError(
                    "expired packet lacks exact runtime authority")

        runtime_store.cancel_run_if_never_launched(
            runtime_run_id, runtime_plan_id)

        with self.connect() as connection:
            intent = connection.execute(
                "SELECT status,runtime_run_id,runtime_plan_id "
                "FROM packet_attempt_intents WHERE collection_id=? "
                "AND attempt_id=?", (collection_id, attempt.attempt_id),
            ).fetchone()
            if intent is None:
                raise PacketAttemptAuthorityError(
                    "expired packet authority disappeared during reconciliation")
            if intent[0] == "RELEASED":
                return packet.logical_task_keys
            if intent != ("EXPIRED", runtime_run_id, runtime_plan_id):
                raise PacketAttemptAuthorityError(
                    "expired packet authority changed during reconciliation")
            connection.execute(
                "UPDATE packet_attempt_intents SET status='RELEASED', "
                "reconciliation_kind='PROVEN_NOT_LAUNCHED', reconciled_at=?, "
                "updated_at=? WHERE collection_id=? AND attempt_id=? "
                "AND status='EXPIRED'",
                (stamp, stamp, collection_id, attempt.attempt_id),
            )
            connection.execute(
                "DELETE FROM packet_member_leases WHERE collection_id=? "
                "AND attempt_id=?",
                (collection_id, attempt.attempt_id),
            )
        return packet.logical_task_keys

    # -- outcomes and state ----------------------------------------------

    def record_outcome(self, collection_id: str, logical_task_key: str,
                       outcome: MemberOutcome, *,
                       now: float | None = None) -> bool:
        """Refuse caller-authored scientific completion.

        Production completion must be reconstructed from RuntimeStore commit
        authority by :meth:`record_runtime_packet_completion`.  The private
        helper retains the old transition solely for isolated control-plane
        fixtures whose test subject is cursor/completeness algebra.
        """
        raise PermissionError(
            "raw partition outcomes are forbidden; record an authoritative "
            "runtime packet completion")

    def _record_outcome_for_test_fixture(
            self, collection_id: str, logical_task_key: str,
            outcome: MemberOutcome, *, now: float | None = None) -> bool:
        """Fixture-only state transition; never artifact authority."""
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
        """Refuse a caller-authored batch of scientific completion claims."""
        raise PermissionError(
            "raw partition outcomes are forbidden; record an authoritative "
            "runtime packet completion")

    def _record_outcomes_for_test_fixture(
            self, collection_id: str,
            items: Iterable[tuple[str, MemberOutcome]], *,
            now: float | None = None) -> int:
        """Fixture-only batched transition; never artifact authority."""
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

    def record_packet_result(
        self,
        collection_id: str,
        packet: "WorkPacket",
        attempt: "PacketAttempt",
        result: "PacketResult",
        *,
        retry_safe: bool | None = None,
        max_attempts: int | None = None,
    ) -> "RetryDecision":
        """Refuse a provider/caller-authored result vector.

        ``PacketResult`` remains a useful immutable receipt format for unit
        testing the retry state machine.  It is not production authority.
        """
        raise PermissionError(
            "caller packet results are forbidden; completion must be derived "
            "from the bound RuntimeStore run and artifact commits")

    def record_runtime_packet_completion(
        self,
        collection_id: str,
        packet: "WorkPacket",
        attempt: "PacketAttempt",
        runtime_store: object,
    ) -> "RetryDecision":
        """Derive every member outcome from one exact terminal runtime run.

        The packet intent fixes ``run_id`` and ``plan_id`` before submission.
        Completion then replays the immutable graph, exact task rows, and each
        successful task's complete artifact-commit vector.  No caller supplies
        a success/failure vector at this production boundary.
        """
        from engine.runtime.state import RuntimeStore
        from engine.runtime.operations import operation_component
        from engine.runtime.types import (
            ArtifactRecipe,
            BoundExecutionGraph,
            OutputSpec,
            ResourceRequest,
            RunState,
            ScientificArtifactBinding,
            TaskTemplate,
            TaskState,
        )
        from contracts import ArtifactDescriptor, direct_match

        if not isinstance(runtime_store, RuntimeStore):
            raise TypeError("runtime_store must be a RuntimeStore authority")
        stamp = self._verified_time(self._clock(), "store clock")
        with self.connect() as connection:
            connection.execute(
                "UPDATE packet_attempt_intents SET status='EXPIRED', "
                "updated_at=? WHERE collection_id=? AND attempt_id=? "
                "AND status='ACTIVE' AND lease_expires_at<=?",
                (stamp, collection_id, attempt.attempt_id, stamp),
            )
            intent = connection.execute(
                "SELECT runtime_run_id,runtime_plan_id,status,lease_expires_at "
                "FROM packet_attempt_intents WHERE collection_id=? "
                "AND attempt_id=? AND packet_id=?",
                (collection_id, attempt.attempt_id, packet.packet_id),
            ).fetchone()
        if intent is None:
            raise PacketAttemptAuthorityError(
                "packet completion has no bound runtime intent")
        if intent[2] not in (
                "ACTIVE", "EXPIRED", "COMPLETED", "RELEASED"):
            raise PacketAttemptAuthorityError(
                f"packet attempt is stale ({intent[2]})")
        if (not isinstance(intent[0], str) or not intent[0]
                or not isinstance(intent[1], str)
                or len(intent[1]) != 64
                or any(value not in "0123456789abcdef"
                       for value in intent[1])):
            raise PacketAttemptAuthorityError(
                "legacy packet intent lacks an exact runtime run/plan "
                "binding and requires reconciliation")
        runtime_run_id, runtime_plan_id = str(intent[0]), str(intent[1])
        _spec, registered_template = self.collection_contract(collection_id)

        with runtime_store.connect() as connection:
            run = connection.execute(
                "SELECT r.plan_id,r.state,b.graph_json FROM runs r "
                "JOIN bound_graphs b ON b.plan_id=r.plan_id "
                "WHERE r.run_id=?", (runtime_run_id,),
            ).fetchone()
            if run is None:
                raise PacketAttemptAuthorityError(
                    "bound runtime run does not exist in this RuntimeStore")
            if run[0] != runtime_plan_id:
                raise PacketAttemptAuthorityError(
                    "bound runtime run resolves to a different plan")
            run_state = RunState(run[1])
            if run_state is RunState.RUNNING:
                raise PacketAttemptAuthorityError(
                    "bound runtime run is not terminal")
            graph = BoundExecutionGraph.from_dict(strict_json_loads(run[2]))
            if graph.plan_id != runtime_plan_id:
                raise PacketAttemptAuthorityError(
                    "bound runtime graph identity does not verify")
            if {task.key for task in graph.tasks} != set(
                    packet.logical_task_keys):
                raise PacketAttemptAuthorityError(
                    "bound runtime plan is not exactly this packet membership")

            # Recompile the collection's immutable scientific invocation over
            # the graph's exact external artifact IDs.  This prevents a caller
            # from binding the packet intent to a different operation that
            # merely reused the partition task keys and produced valid bytes.
            expected_input_names = {
                use.port_id for use in
                registered_template.invocation.input_uses
            }
            expected_outputs = tuple(OutputSpec(
                name=port.port_id,
                scientific_binding=ScientificArtifactBinding(
                    bound_plan_id=registered_template.template_id,
                    invocation_id=(
                        registered_template.invocation.invocation_key),
                    output_port=port.port_id,
                    descriptor_id=port.descriptor.descriptor_id,
                    descriptor=port.descriptor.to_dict(),
                ),
            ) for port in registered_template.invocation.outputs)
            expected_templates: list[TaskTemplate] = []
            for member in packet.members:
                observed_task = graph.task_by_key(member.logical_task_key)
                if observed_task.inputs or {
                        value.input_name
                        for value in observed_task.external_inputs
                } != expected_input_names:
                    raise PacketAttemptAuthorityError(
                        "runtime packet inputs differ from the registered "
                        "scientific invocation")
                expected_templates.append(TaskTemplate(
                    key=member.logical_task_key,
                    component=operation_component(
                        registered_template.operation_key),
                    parameters=dict(registered_template.parameters),
                    external_inputs=observed_task.external_inputs,
                    outputs=expected_outputs,
                    resources=ResourceRequest.from_dict(dict(
                        registered_template.deployment_binding
                        .resource_request)),
                    max_attempts=(
                        registered_template.retry_policy.max_attempts),
                ))
            expected_graph = BoundExecutionGraph.bind(
                f"partition-packet-{packet.packet_id[:12]}",
                expected_templates,
            )
            if expected_graph != graph:
                raise PacketAttemptAuthorityError(
                    "bound runtime plan does not recompile from collection "
                    "template authority")

            outcomes: list[tuple[str, MemberOutcome]] = []
            for member in packet.members:
                task = graph.task_by_key(member.logical_task_key)
                row = connection.execute(
                    "SELECT task_json,state,attempt_count FROM tasks "
                    "WHERE run_id=? AND task_id=? AND task_key=?",
                    (runtime_run_id, task.task_id, task.key),
                ).fetchone()
                if (row is None or row[0]
                        != strict_canonical_json(task.to_dict())):
                    raise PacketAttemptAuthorityError(
                        "runtime task binding differs from the packet plan")
                use_by_port = {
                    use.port_id: use
                    for use in registered_template.invocation.input_uses
                }
                external_rows = connection.execute(
                    "SELECT e.input_name,e.artifact_id,r.recipe_json "
                    "FROM task_external_inputs e "
                    "JOIN artifact_commits c ON c.run_id=e.source_run_id "
                    "AND c.recipe_id=e.recipe_id "
                    "AND c.artifact_id=e.artifact_id "
                    "JOIN artifact_recipes r ON r.recipe_id=e.recipe_id "
                    "WHERE e.run_id=? AND e.task_id=? "
                    "ORDER BY e.input_name",
                    (runtime_run_id, task.task_id),
                ).fetchall()
                if {value[0] for value in external_rows} != expected_input_names:
                    raise PacketAttemptAuthorityError(
                        "runtime external inputs lost their exact committed "
                        "artifact authority")
                for external in external_rows:
                    recipe = ArtifactRecipe.from_dict(
                        strict_json_loads(external[2]))
                    scientific = recipe.scientific_binding
                    use = use_by_port[external[0]]
                    if (scientific is None or not direct_match(
                            ArtifactDescriptor.from_dict(
                                scientific.to_dict()["descriptor"]),
                            use.requirement).satisfied):
                        raise PacketAttemptAuthorityError(
                            "runtime external artifact does not satisfy its "
                            "registered RequirementUse")
                state = TaskState(row[1])
                commits = connection.execute(
                    "SELECT c.output_name,c.recipe_id,c.artifact_id "
                    "FROM artifact_commits c "
                    "JOIN artifacts a ON a.artifact_id=c.artifact_id "
                    "AND a.recipe_id=c.recipe_id "
                    "JOIN validation_records v ON v.validation_id=c.validation_id "
                    "AND v.attempt_id=c.attempt_id "
                    "AND v.recipe_id=c.recipe_id "
                    "AND v.content_sha256=a.content_sha256 AND v.passed=1 "
                    "WHERE c.run_id=? AND c.task_id=? "
                    "ORDER BY c.output_name",
                    (runtime_run_id, task.task_id),
                ).fetchall()
                expected = {
                    (recipe.output_name, recipe.recipe_id)
                    for recipe in task.outputs
                }
                actual = {(row[0], row[1]) for row in commits}
                if state is TaskState.SUCCEEDED:
                    if not expected or actual != expected:
                        raise PacketAttemptAuthorityError(
                            "successful runtime task lacks its exact artifact "
                            "commit and validation vector")
                    outcome = MemberOutcome.COMMITTED
                else:
                    if commits:
                        raise PacketAttemptAuthorityError(
                            "non-successful runtime task has contradictory "
                            "artifact commit authority")
                    outcome = (
                        MemberOutcome.NOT_ATTEMPTED
                        if int(row[2]) == 0
                        else MemberOutcome.FAILED
                    )
                outcomes.append((member.logical_task_key, outcome))

        derived = PacketResult.bind(attempt, packet, outcomes)
        return self._record_verified_packet_result(
            collection_id, packet, attempt, derived,
            allow_expired_runtime=(intent[2] == "EXPIRED"))

    def _record_packet_result_for_test_fixture(
        self,
        collection_id: str,
        packet: "WorkPacket",
        attempt: "PacketAttempt",
        result: "PacketResult",
        *,
        retry_safe: bool | None = None,
        max_attempts: int | None = None,
    ) -> "RetryDecision":
        """Fixture-only injection for retry/control-plane unit tests."""
        return self._record_verified_packet_result(
            collection_id, packet, attempt, result,
            retry_safe=retry_safe, max_attempts=max_attempts)

    def _record_verified_packet_result(
        self,
        collection_id: str,
        packet: "WorkPacket",
        attempt: "PacketAttempt",
        result: "PacketResult",
        *,
        retry_safe: bool | None = None,
        max_attempts: int | None = None,
        allow_expired_runtime: bool = False,
    ) -> "RetryDecision":
        """Durably record one packet attempt and decide each member's fate.

        This is the transition the audit found missing.  Previously a failed
        member was marked FAILED while packet generation only ever selected
        ADMITTED rows, so ``retryable_keys`` reported work that could never
        come back.  Now a retry-safe failure below the attempt ceiling is
        returned to ADMITTED -- which is what makes it eligible for a new
        packet -- and everything is written in one transaction alongside a
        durable per-partition attempt record.

        Committed is final: a late or duplicate result cannot un-commit work.
        """
        if result.attempt_id != attempt.attempt_id:
            raise ValueError("result does not belong to this attempt")
        if attempt.packet_id != packet.packet_id:
            raise ValueError("attempt does not belong to this packet")
        if result.packet_id != packet.packet_id:
            raise ValueError("result does not belong to this packet")
        if packet.collection_id != collection_id:
            raise ValueError("packet does not belong to this collection")
        if {key for key, _ in result.outcomes} != set(
                packet.logical_task_keys):
            raise ValueError("result does not report exactly this packet")
        stamp = self._verified_time(self._clock(), "store clock")
        committed: list[str] = []
        requeued: list[str] = []
        exhausted: list[str] = []

        with self.connect() as connection:
            registered_template = self._verify_packet_authority(
                connection, collection_id, packet)
            policy = registered_template.retry_policy
            # Compatibility claims are ignored policy inputs.  Old callers
            # may still pass them, but even a contradictory value cannot
            # widen or narrow immutable collection authority.  Validate only
            # their basic types so malformed API use remains visible.
            if retry_safe is not None:
                if type(retry_safe) is not bool:
                    raise TypeError("retry_safe claim must be bool")
            if max_attempts is not None:
                if (isinstance(max_attempts, bool)
                        or not isinstance(max_attempts, int)
                        or max_attempts < 1):
                    raise ValueError(
                        "max_attempts claim must be a positive integer")

            replay = connection.execute(
                "SELECT result_id, packet_id, decision_json "
                "FROM packet_results WHERE collection_id=? AND attempt_id=?",
                (collection_id, attempt.attempt_id)).fetchone()
            if replay is not None:
                if replay[0] != result.result_id or replay[1] != packet.packet_id:
                    raise PacketResultConflictError(
                        "packet attempt was already recorded with a different "
                        "result")
                decision = strict_json_loads(replay[2])
                return RetryDecision(
                    committed=tuple(decision["committed"]),
                    requeued=tuple(decision["requeued"]),
                    exhausted=tuple(decision["exhausted"]),
                )

            intent = connection.execute(
                "SELECT packet_id, packet_json, attempt_number, fence_token, "
                "status, lease_expires_at FROM packet_attempt_intents "
                "WHERE collection_id=? AND attempt_id=?",
                (collection_id, attempt.attempt_id)).fetchone()
            if intent is None:
                raise PacketAttemptAuthorityError(
                    "packet result attempt was never registered before "
                    "provider submission")
            expected_packet_json = strict_canonical_json(packet.to_dict())
            if (intent[0] != packet.packet_id
                    or intent[1] != expected_packet_json
                    or int(intent[2]) != attempt.attempt_number
                    or intent[3] != attempt.fence_token):
                raise PacketAttemptAuthorityError(
                    "packet result does not match its registered packet, "
                    "attempt number, and fence")
            allowed_status = (
                intent[4] == "ACTIVE"
                or (allow_expired_runtime and intent[4] == "EXPIRED"))
            if not allowed_status:
                raise PacketAttemptAuthorityError(
                    f"packet attempt is stale ({intent[4]})")
            if not allow_expired_runtime and float(intent[5]) <= stamp:
                raise PacketAttemptAuthorityError(
                    "packet attempt lease expired before result ingestion")
            for member in packet.members:
                member_lease = connection.execute(
                    "SELECT attempt_id, packet_id, lease_expires_at "
                    "FROM packet_member_leases WHERE collection_id=? "
                    "AND logical_task_key=?",
                    (collection_id, member.logical_task_key)).fetchone()
                if (member_lease is None
                        or member_lease[0] != attempt.attempt_id
                        or member_lease[1] != packet.packet_id):
                    raise PacketAttemptAuthorityError(
                        "packet attempt no longer owns every member lease")
                if (not allow_expired_runtime
                        and float(member_lease[2]) <= stamp):
                    raise PacketAttemptAuthorityError(
                        "packet member lease expired before result ingestion")

            previous_number = connection.execute(
                "SELECT MAX(attempt_number) FROM packet_results "
                "WHERE collection_id=? AND packet_id=? AND consumes_attempt=1",
                (collection_id, packet.packet_id)).fetchone()[0]
            expected_number = (1 if previous_number is None
                               else int(previous_number) + 1)
            if attempt.attempt_number != expected_number:
                raise ValueError(
                    "packet attempt number is not the next durable sequence "
                    f"value: expected {expected_number}, observed "
                    f"{attempt.attempt_number}")

            for key, outcome in result.outcomes:
                row = connection.execute(
                    "SELECT state, attempt_count FROM logical_tasks "
                    "WHERE collection_id=? AND logical_task_key=?",
                    (collection_id, key)).fetchone()
                if row is None:
                    raise KeyError(
                        f"{key!r} is not an admitted partition of "
                        f"{collection_id!r}")
                state, attempts = row[0], int(row[1])
                if state == "COMMITTED":
                    continue          # already landed; nothing can undo it
                if outcome is MemberOutcome.NOT_ATTEMPTED:
                    # The provider never began this member.  Keep it admitted
                    # without consuming a scientific retry attempt.
                    requeued.append(key)
                    continue
                attempts += 1
                connection.execute(
                    "INSERT OR IGNORE INTO partition_attempts"
                    "(collection_id, logical_task_key, attempt_number,"
                    " attempt_id, packet_id, fence_token, outcome, created_at)"
                    " VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                    (collection_id, key, attempts, attempt.attempt_id,
                     packet.packet_id, attempt.fence_token, outcome.value,
                     stamp))
                if outcome is MemberOutcome.COMMITTED:
                    next_state = "COMMITTED"
                    committed.append(key)
                elif (policy.retry_safe
                      and attempts < policy.max_attempts):
                    # Back to ADMITTED so the next packet picks it up again.
                    next_state = "ADMITTED"
                    requeued.append(key)
                else:
                    next_state = "FAILED"
                    exhausted.append(key)
                connection.execute(
                    "UPDATE logical_tasks SET state=?, attempt_count=?,"
                    " updated_at=? WHERE collection_id=? AND logical_task_key=?",
                    (next_state, attempts, stamp, collection_id, key))
            decision = RetryDecision(
                committed=tuple(sorted(committed)),
                requeued=tuple(sorted(requeued)),
                exhausted=tuple(sorted(exhausted)))
            consumes_attempt = any(
                outcome is not MemberOutcome.NOT_ATTEMPTED
                for _key, outcome in result.outcomes)
            connection.execute(
                "INSERT INTO packet_results"
                "(collection_id, attempt_id, result_id, packet_id,"
                " attempt_number, consumes_attempt, decision_json, created_at) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                (collection_id, attempt.attempt_id, result.result_id,
                 packet.packet_id, attempt.attempt_number,
                 int(consumes_attempt),
                 strict_canonical_json(decision.to_dict()), stamp))
            terminal_intent = (
                "COMPLETED" if consumes_attempt else "RELEASED")
            connection.execute(
                "UPDATE packet_attempt_intents SET status=?, "
                "reconciliation_kind=?, reconciled_at=?, updated_at=? "
                "WHERE collection_id=? AND attempt_id=? AND status IN "
                "('ACTIVE','EXPIRED')",
                (terminal_intent,
                 ("RUNTIME_TERMINAL" if allow_expired_runtime else None),
                 (stamp if allow_expired_runtime else None), stamp,
                 collection_id, attempt.attempt_id))
            connection.execute(
                "DELETE FROM packet_member_leases WHERE collection_id=? "
                "AND attempt_id=?",
                (collection_id, attempt.attempt_id))
        return RetryDecision(
            committed=tuple(sorted(committed)),
            requeued=tuple(sorted(requeued)),
            exhausted=tuple(sorted(exhausted)))

    def attempts_for(self, collection_id: str,
                     logical_task_key: str) -> tuple[tuple[int, str], ...]:
        """Every durable attempt for one partition, as (number, outcome)."""
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT attempt_number, outcome FROM partition_attempts "
                "WHERE collection_id=? AND logical_task_key=? "
                "ORDER BY attempt_number",
                (collection_id, logical_task_key)).fetchall()
        return tuple((int(row[0]), row[1]) for row in rows)

    def attempt_count(self, collection_id: str,
                      logical_task_key: str) -> int:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT attempt_count FROM logical_tasks "
                "WHERE collection_id=? AND logical_task_key=?",
                (collection_id, logical_task_key)).fetchone()
        if row is None:
            raise KeyError(logical_task_key)
        return int(row[0])

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
                "SELECT t.logical_task_key, t.partition_index "
                "FROM logical_tasks AS t "
                "WHERE t.collection_id=? AND t.state='ADMITTED' "
                "AND NOT EXISTS ("
                "  SELECT 1 FROM packet_member_leases AS l "
                "  WHERE l.collection_id=t.collection_id "
                "  AND l.logical_task_key=t.logical_task_key"
                ") ORDER BY t.partition_index LIMIT ?",
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
    "RetryDecision",
    "CursorConflictError",
    "PacketAttemptAuthorityError",
    "PacketAttemptConflictError",
    "PacketResultConflictError",
    "CursorState",
    "PartitionStore",
]
