"""Cube schema versioning tests."""
from __future__ import annotations

import sys
from pathlib import Path

import duckdb
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine import schema as eng_schema


def test_brand_new_cube_gets_stamped(tmp_path):
    db = tmp_path / "catalog.duckdb"
    assert eng_schema.get_schema_version(db) == 0
    final = eng_schema.ensure_schema(db, target=eng_schema.CURRENT_SCHEMA_VERSION)
    assert final == eng_schema.CURRENT_SCHEMA_VERSION
    assert eng_schema.get_schema_version(db) == eng_schema.CURRENT_SCHEMA_VERSION


def test_idempotent_when_already_current(tmp_path):
    db = tmp_path / "catalog.duckdb"
    eng_schema.ensure_schema(db)
    eng_schema.ensure_schema(db)  # should be a no-op
    eng_schema.ensure_schema(db)
    assert eng_schema.get_schema_version(db) == eng_schema.CURRENT_SCHEMA_VERSION


def test_refuses_to_downgrade(tmp_path):
    db = tmp_path / "catalog.duckdb"
    # Pretend the cube is from a future engine: stamp version 99.
    with duckdb.connect(str(db)) as con:
        con.execute(eng_schema._META_SQL)
        con.execute(
            "INSERT OR REPLACE INTO cube_meta (key, value) VALUES "
            "('schema_version', '99')")
    with pytest.raises(RuntimeError, match="newer than"):
        eng_schema.ensure_schema(db, target=eng_schema.CURRENT_SCHEMA_VERSION)


def test_missing_migration_raises(tmp_path):
    db = tmp_path / "catalog.duckdb"
    # Stamp at v1, target v3 — no v1->v2 migration registered.
    with duckdb.connect(str(db)) as con:
        con.execute(eng_schema._META_SQL)
        con.execute(
            "INSERT OR REPLACE INTO cube_meta (key, value) VALUES "
            "('schema_version', '1')")
    with pytest.raises(RuntimeError, match="no migration registered"):
        eng_schema.ensure_schema(db, target=3)


def test_register_migration_runs_in_order(tmp_path):
    """Register two ad-hoc migrations and confirm they run forward in order."""
    db = tmp_path / "catalog.duckdb"
    # Establish v1 baseline first.
    eng_schema.ensure_schema(db, target=1)
    audit: list[int] = []

    @eng_schema.register_migration(1, 2)
    def _to_v2(con):
        audit.append(2)
        con.execute(
            "CREATE TABLE IF NOT EXISTS migration_test_v2 (x INTEGER)")

    @eng_schema.register_migration(2, 3)
    def _to_v3(con):
        audit.append(3)
        con.execute(
            "CREATE TABLE IF NOT EXISTS migration_test_v3 (y INTEGER)")

    try:
        final = eng_schema.ensure_schema(db, target=3)
        assert final == 3
        assert audit == [2, 3]
        # Verify both tables landed.
        with duckdb.connect(str(db)) as con:
            con.execute("SELECT * FROM migration_test_v2").fetchall()
            con.execute("SELECT * FROM migration_test_v3").fetchall()
    finally:
        # Clean up the registered migrations so other tests aren't affected.
        eng_schema._MIGRATIONS.pop((1, 2), None)
        eng_schema._MIGRATIONS.pop((2, 3), None)
