"""Checkpoint/resume for iterative producers.

Producers that integrate forward in time (KBDI, fire spread, anything with
internal state) can periodically save their state and resume from the last
checkpoint after a crash or interrupt — instead of restarting from scratch.

Checkpoints live as a BLOB column in the cube's DuckDB so the cube artifact
stays self-contained: snapshot the cube, you snapshot the checkpoints too.

Usage pattern inside a producer's compute or process_tile:

    store = CheckpointStore(cube.catalog.path)
    last = store.load_latest(self.name)
    start = (last[0] + 1) if last else 0
    state = last[1] if last else self._init_state()
    for step in range(start, n_steps):
        state = self._advance(state)
        if step % self.checkpoint_every == 0:
            store.save(self.name, step, state)

The checkpoint format is plain pickle. State dicts containing numpy arrays
work transparently; non-picklable state (open files, sockets) does not.
"""
from __future__ import annotations

import pickle
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import duckdb


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS checkpoints (
    producer  VARCHAR NOT NULL,
    step_id   INTEGER NOT NULL,
    saved_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    state     BLOB,
    PRIMARY KEY (producer, step_id)
);
"""


@dataclass
class CheckpointStore:
    """DuckDB-backed checkpoint store.

    Connections are short-lived (open per call) so the store is safe to use
    from multiple processes; DuckDB serializes file-level writes via its
    OS lock.
    """
    db_path: str | Path

    def __post_init__(self):
        self.db_path = str(self.db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with duckdb.connect(self.db_path) as con:
            con.execute(_SCHEMA_SQL)

    # ---- writes ----------------------------------------------------------
    def save(self, producer: str, step_id: int, state: Any) -> None:
        blob = pickle.dumps(state, protocol=pickle.HIGHEST_PROTOCOL)
        with duckdb.connect(self.db_path) as con:
            con.execute(_SCHEMA_SQL)
            con.execute(
                "DELETE FROM checkpoints WHERE producer=? AND step_id=?",
                [producer, int(step_id)])
            con.execute(
                "INSERT INTO checkpoints (producer, step_id, state) "
                "VALUES (?, ?, ?)",
                [producer, int(step_id), blob])

    def delete(self, producer: str) -> int:
        """Wipe all checkpoints for a producer. Returns count removed."""
        with duckdb.connect(self.db_path) as con:
            con.execute(_SCHEMA_SQL)
            n = con.execute(
                "SELECT COUNT(*) FROM checkpoints WHERE producer=?",
                [producer]).fetchone()[0]
            con.execute(
                "DELETE FROM checkpoints WHERE producer=?", [producer])
            return int(n)

    # ---- reads -----------------------------------------------------------
    def load_step(self, producer: str, step_id: int) -> Optional[Any]:
        with duckdb.connect(self.db_path) as con:
            con.execute(_SCHEMA_SQL)
            row = con.execute(
                "SELECT state FROM checkpoints "
                "WHERE producer=? AND step_id=?",
                [producer, int(step_id)]).fetchone()
        if row is None:
            return None
        return pickle.loads(bytes(row[0]))

    def load_latest(self, producer: str) -> Optional[tuple[int, Any]]:
        """Return (step_id, state) for the highest step_id stored, or None."""
        with duckdb.connect(self.db_path) as con:
            con.execute(_SCHEMA_SQL)
            row = con.execute(
                "SELECT step_id, state FROM checkpoints "
                "WHERE producer=? ORDER BY step_id DESC LIMIT 1",
                [producer]).fetchone()
        if row is None:
            return None
        return int(row[0]), pickle.loads(bytes(row[1]))

    def list_steps(self, producer: str) -> list[int]:
        with duckdb.connect(self.db_path) as con:
            con.execute(_SCHEMA_SQL)
            rows = con.execute(
                "SELECT step_id FROM checkpoints WHERE producer=? "
                "ORDER BY step_id",
                [producer]).fetchall()
        return [int(r[0]) for r in rows]
