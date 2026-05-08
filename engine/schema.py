"""Cube schema versioning + forward migrations.

The cube's DuckDB file holds a `cube_meta` table with a single
`schema_version` row. New runs read it before opening the catalog; if
it's older than the engine's `CURRENT_SCHEMA_VERSION` we run the
declared migrations forward in order.

Today there's only version 1 (the schema baked into cube/catalog.py +
this module's `cube_meta` table itself). The table exists so that future
schema changes can land non-disruptively: register a migration callable,
bump CURRENT_SCHEMA_VERSION, old cubes get auto-migrated on next open.

Cube data layout (per-variable Zarrs, catalog tables) is intentionally
NOT versioned by this module — that's the catalog's concern. This is
just the meta layer that says "what version of the engine produced
this cube".
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable

import duckdb


CURRENT_SCHEMA_VERSION = 1


_META_SQL = """
CREATE TABLE IF NOT EXISTS cube_meta (
    key   VARCHAR PRIMARY KEY,
    value VARCHAR
);
"""


# Forward migrations. Each entry is `(from_version, to_version): callable(con)`.
# A callable runs against an open DuckDB connection inside a transaction.
# Add migrations here as schema evolves; the engine runs them in order.
_MIGRATIONS: dict[tuple[int, int], Callable] = {}


def register_migration(from_version: int, to_version: int):
    """Decorator: register a migration step (from_version -> to_version)."""
    if to_version != from_version + 1:
        raise ValueError(
            f"migrations must step by +1 (got {from_version} -> {to_version})")

    def deco(fn: Callable):
        _MIGRATIONS[(from_version, to_version)] = fn
        return fn
    return deco


def get_schema_version(db_path: str | Path) -> int:
    """Return the cube's schema version, or 0 if none recorded."""
    p = Path(db_path)
    if not p.exists():
        return 0
    with duckdb.connect(str(p)) as con:
        con.execute(_META_SQL)
        row = con.execute(
            "SELECT value FROM cube_meta WHERE key='schema_version'"
        ).fetchone()
        if row is None:
            return 0
        try:
            return int(row[0])
        except (TypeError, ValueError):
            return 0


def _set_schema_version(con, version: int) -> None:
    con.execute(
        "INSERT OR REPLACE INTO cube_meta (key, value) VALUES "
        "('schema_version', ?)", [str(version)])


def ensure_schema(db_path: str | Path,
                   target: int = CURRENT_SCHEMA_VERSION) -> int:
    """Bring the cube's schema up to `target`. Returns the final version.

    Brand-new cubes are stamped to `target` directly. Existing cubes step
    through registered migrations one version at a time. Raises if there's
    no migration path forward.
    """
    p = Path(db_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    current = get_schema_version(p)
    if current == target:
        return current
    if current > target:
        raise RuntimeError(
            f"cube at {p} has schema_version={current} which is newer than "
            f"this engine's CURRENT_SCHEMA_VERSION={target}; refusing to "
            "downgrade")
    with duckdb.connect(str(p)) as con:
        con.execute(_META_SQL)
        # Brand-new file: stamp directly without migrations.
        if current == 0:
            _set_schema_version(con, target)
            return target
        # Step forward.
        while current < target:
            step = _MIGRATIONS.get((current, current + 1))
            if step is None:
                raise RuntimeError(
                    f"no migration registered for schema {current} -> "
                    f"{current + 1}; cannot upgrade cube at {p}")
            con.execute("BEGIN")
            try:
                step(con)
                _set_schema_version(con, current + 1)
                con.execute("COMMIT")
            except Exception:
                con.execute("ROLLBACK")
                raise
            current += 1
    return current
