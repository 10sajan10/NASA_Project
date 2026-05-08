"""RunManifest: per-run identity, lineage, and reproducibility metadata.

Stored in the same DuckDB file as the cube catalog so a single cube
artifact is fully self-describing. Tables are additive — existing catalog
tables are untouched.

Schema:
    runs(run_id, started_at, ended_at, status, git_sha, config_hash,
         python, platform_str, parent_run_id, notes)
    run_inputs(run_id, role, path, sha256, bytes)
    run_outputs(run_id, variable, version, producer)
    run_libs(run_id, name, version)

Usage:
    with RunManifest.create(cube.catalog.path, config) as run:
        run.record_input("kml", args.kml)
        ...
"""
from __future__ import annotations

import hashlib
import json
import platform
import subprocess
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import duckdb


_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id        VARCHAR PRIMARY KEY,
    started_at    TIMESTAMP NOT NULL,
    ended_at      TIMESTAMP,
    status        VARCHAR DEFAULT 'running',
    git_sha       VARCHAR,
    config_hash   VARCHAR,
    python        VARCHAR,
    platform_str  VARCHAR,
    parent_run_id VARCHAR,
    notes         VARCHAR
);
CREATE TABLE IF NOT EXISTS run_inputs (
    run_id   VARCHAR,
    role     VARCHAR,
    path     VARCHAR,
    sha256   VARCHAR,
    bytes    BIGINT
);
CREATE INDEX IF NOT EXISTS run_inputs_idx ON run_inputs (run_id);

CREATE TABLE IF NOT EXISTS run_outputs (
    run_id   VARCHAR,
    variable VARCHAR,
    version  INTEGER,
    producer VARCHAR
);
CREATE INDEX IF NOT EXISTS run_outputs_idx ON run_outputs (run_id, variable);

CREATE TABLE IF NOT EXISTS run_libs (
    run_id   VARCHAR,
    name     VARCHAR,
    version  VARCHAR
);
"""


def _git_sha(root: Path) -> Optional[str]:
    try:
        return subprocess.check_output(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            stderr=subprocess.DEVNULL).decode().strip()
    except Exception:
        return None


def _hash_file(p: Path, chunk: int = 1 << 20) -> tuple[str, int]:
    h = hashlib.sha256()
    n = 0
    with open(p, "rb") as fh:
        while True:
            b = fh.read(chunk)
            if not b:
                break
            h.update(b)
            n += len(b)
    return h.hexdigest(), n


def config_hash(config: dict) -> str:
    """Stable hash of a config dict — two runs with the same hash had the
    same resolved config."""
    return hashlib.sha256(
        json.dumps(config, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


def _captured_libs() -> list[tuple[str, str]]:
    libs = []
    for mod in ("numpy", "zarr", "xarray", "rasterio", "duckdb",
                "gcsfs", "dask", "distributed", "dask_jobqueue"):
        try:
            m = __import__(mod)
            libs.append((mod, getattr(m, "__version__", "unknown")))
        except Exception:
            pass
    return libs


@dataclass
class RunManifest:
    """Per-run identity record. Use as a context manager around the run."""
    run_id: str
    db_path: Path
    config: dict[str, Any]
    parent_run_id: Optional[str] = None
    started_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc))

    # ---- factory ---------------------------------------------------------
    @classmethod
    def create(cls,
               db_path: str | Path,
               config: dict,
               *,
               parent_run_id: Optional[str] = None,
               git_root: Optional[Path] = None) -> "RunManifest":
        m = cls(run_id=uuid.uuid4().hex[:12],
                db_path=Path(db_path),
                config=dict(config),
                parent_run_id=parent_run_id)
        with duckdb.connect(str(m.db_path)) as con:
            con.execute(_SCHEMA)
            con.execute(
                "INSERT INTO runs "
                "(run_id, started_at, status, git_sha, config_hash, "
                " python, platform_str, parent_run_id) "
                "VALUES (?, ?, 'running', ?, ?, ?, ?, ?)",
                [m.run_id,
                 m.started_at,
                 _git_sha(Path(git_root) if git_root else Path.cwd()),
                 config_hash(config),
                 sys.version.split()[0],
                 platform.platform(),
                 parent_run_id])
            for name, ver in _captured_libs():
                con.execute(
                    "INSERT INTO run_libs (run_id, name, version) VALUES (?, ?, ?)",
                    [m.run_id, name, ver])
        return m

    # ---- recording -------------------------------------------------------
    def record_input(self, role: str, path: str | Path) -> None:
        """Hash an input file and record it. Directories logged with sha='(dir)'.
        Missing paths silently skipped — caller decides whether absence is fatal."""
        p = Path(path)
        if not p.exists():
            return
        if p.is_file():
            sha, n = _hash_file(p)
        else:
            sha, n = "(dir)", 0
        with duckdb.connect(str(self.db_path)) as con:
            con.execute(_SCHEMA)
            con.execute(
                "INSERT INTO run_inputs (run_id, role, path, sha256, bytes) "
                "VALUES (?, ?, ?, ?, ?)",
                [self.run_id, role, str(p), sha, n])

    def record_output(self, variable: str, version: int,
                      producer: str) -> None:
        with duckdb.connect(str(self.db_path)) as con:
            con.execute(_SCHEMA)
            con.execute(
                "INSERT INTO run_outputs "
                "(run_id, variable, version, producer) VALUES (?, ?, ?, ?)",
                [self.run_id, variable, int(version), producer])

    def finalize(self, status: str = "ok", notes: str = "") -> None:
        with duckdb.connect(str(self.db_path)) as con:
            con.execute(
                "UPDATE runs SET ended_at = ?, status = ?, notes = ? "
                "WHERE run_id = ?",
                [datetime.now(timezone.utc), status, notes, self.run_id])

    # ---- introspection ---------------------------------------------------
    def summary(self) -> dict:
        with duckdb.connect(str(self.db_path)) as con:
            row = con.execute(
                "SELECT run_id, started_at, ended_at, status, git_sha, "
                "config_hash, parent_run_id, notes FROM runs "
                "WHERE run_id = ?", [self.run_id]).fetchone()
        keys = ["run_id", "started_at", "ended_at", "status", "git_sha",
                "config_hash", "parent_run_id", "notes"]
        return dict(zip(keys, row)) if row else {}

    # ---- context manager -------------------------------------------------
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.finalize(
            status="error" if exc_type is not None else "ok",
            notes=str(exc) if exc is not None else "")
        return False  # never suppress
