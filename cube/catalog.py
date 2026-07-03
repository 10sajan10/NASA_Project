"""DuckDB catalog: tracks variables and tiles in the cube.

Schema v2 adds the metadata an autonomous planner needs to reason about
what is already in the cube:

  * variables gain semantics: `standard_name` (controlled vocabulary,
    see agentic.ontology), `domain`, and free-form `tags`.
  * tiles gain provenance: `source_url`, `license`, `checksum`, and the
    `run_id` of the engine run that wrote them, so every slab of data is
    traceable back to its origin and the lineage record that produced it.

Opening a v1 catalog migrates it in place (additive columns only), so
existing cubes keep working unchanged.
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

import duckdb


SCHEMA_VERSION = 2

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS variables (
    name          TEXT PRIMARY KEY,
    kind          TEXT NOT NULL,      -- 'static' | 'time'
    units         TEXT,
    dtype         TEXT,
    description   TEXT,
    producer      TEXT,
    standard_name TEXT DEFAULT '',    -- controlled-vocabulary name
    domain        TEXT DEFAULT '',    -- impact-physics | atmosphere | fire | economy ...
    tags          TEXT DEFAULT '[]'   -- JSON array of free-form tags
);

CREATE TABLE IF NOT EXISTS tiles (
    variable     TEXT NOT NULL,
    t            TIMESTAMP,           -- NULL for static
    source       TEXT,
    native_res_m DOUBLE,
    version      INTEGER DEFAULT 0,
    fetched_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    source_url   TEXT DEFAULT '',     -- where the bytes came from
    license      TEXT DEFAULT '',
    checksum     TEXT DEFAULT '',     -- SHA-256 of the source payload
    run_id       TEXT DEFAULT ''      -- engine run lineage id
);
CREATE INDEX IF NOT EXISTS tiles_var_t ON tiles (variable, t, version);

CREATE TABLE IF NOT EXISTS scenarios (
    name           TEXT PRIMARY KEY,
    grid_epsg      INTEGER,
    grid_pixel_m   DOUBLE,
    grid_width     INTEGER,
    grid_height    INTEGER,
    grid_x0        DOUBLE,
    grid_y1        DOUBLE,
    scenario_date  TIMESTAMP,
    created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS catalog_meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

# Additive v1 -> v2 migration. `ADD COLUMN IF NOT EXISTS` makes this a
# no-op on fresh databases and on already-migrated ones.
MIGRATION_SQL = """
ALTER TABLE variables ADD COLUMN IF NOT EXISTS standard_name TEXT DEFAULT '';
ALTER TABLE variables ADD COLUMN IF NOT EXISTS domain        TEXT DEFAULT '';
ALTER TABLE variables ADD COLUMN IF NOT EXISTS tags          TEXT DEFAULT '[]';
ALTER TABLE tiles     ADD COLUMN IF NOT EXISTS source_url    TEXT DEFAULT '';
ALTER TABLE tiles     ADD COLUMN IF NOT EXISTS license       TEXT DEFAULT '';
ALTER TABLE tiles     ADD COLUMN IF NOT EXISTS checksum      TEXT DEFAULT '';
ALTER TABLE tiles     ADD COLUMN IF NOT EXISTS run_id        TEXT DEFAULT '';
"""

_VARIABLE_COLS = ["name", "kind", "units", "dtype", "description",
                  "producer", "standard_name", "domain", "tags"]
_TILE_COLS = ["variable", "t", "source", "native_res_m", "version",
              "fetched_at", "source_url", "license", "checksum", "run_id"]


@dataclass
class TileRecord:
    variable: str
    t: Optional[datetime]
    source: str
    native_res_m: float
    version: int = 0
    # provenance (all optional; empty string = unknown)
    source_url: str = ""
    license: str = ""
    checksum: str = ""
    run_id: str = ""


class Catalog:
    def __init__(self, path: Path | str):
        self.path = str(path)
        self.con = duckdb.connect(self.path)
        self.con.execute(SCHEMA_SQL)
        self.con.execute(MIGRATION_SQL)
        self.con.execute(
            "INSERT OR REPLACE INTO catalog_meta VALUES ('schema_version', ?)",
            [str(SCHEMA_VERSION)])

    def schema_version(self) -> int:
        row = self.con.execute(
            "SELECT value FROM catalog_meta WHERE key='schema_version'"
        ).fetchone()
        return int(row[0]) if row else 1

    def register_variable(self, name: str, kind: str, units: str = "",
                          dtype: str = "f4", description: str = "",
                          producer: str = "", standard_name: str = "",
                          domain: str = "", tags: str = "[]") -> None:
        self.con.execute(
            "INSERT OR REPLACE INTO variables "
            "(name, kind, units, dtype, description, producer, "
            " standard_name, domain, tags) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [name, kind, units, dtype, description, producer,
             standard_name, domain, tags])

    def add_tile(self, rec: TileRecord) -> None:
        # explicit dedupe (DuckDB PRIMARY KEY columns must be NOT NULL,
        # but our `t` column is nullable for static variables).
        if rec.t is None:
            self.con.execute(
                "DELETE FROM tiles WHERE variable=? AND t IS NULL AND version=?",
                [rec.variable, rec.version])
        else:
            self.con.execute(
                "DELETE FROM tiles WHERE variable=? AND t=? AND version=?",
                [rec.variable, rec.t, rec.version])
        self.con.execute(
            "INSERT INTO tiles "
            "(variable, t, source, native_res_m, version, fetched_at, "
            " source_url, license, checksum, run_id) "
            "VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP, ?, ?, ?, ?)",
            [rec.variable, rec.t, rec.source, rec.native_res_m, rec.version,
             rec.source_url, rec.license, rec.checksum, rec.run_id])

    def has(self, variable: str, t: Optional[datetime] = None) -> bool:
        if t is None:
            r = self.con.execute(
                "SELECT 1 FROM tiles WHERE variable=? AND t IS NULL LIMIT 1",
                [variable]).fetchone()
        else:
            r = self.con.execute(
                "SELECT 1 FROM tiles WHERE variable=? AND t=? LIMIT 1",
                [variable, t]).fetchone()
        return r is not None

    def list_times(self, variable: str) -> list[datetime]:
        rs = self.con.execute(
            "SELECT t FROM tiles WHERE variable=? AND t IS NOT NULL ORDER BY t",
            [variable]).fetchall()
        return [r[0] for r in rs]

    def list_variables(self) -> list[dict]:
        rs = self.con.execute(
            f"SELECT {', '.join(_VARIABLE_COLS)} "
            "FROM variables ORDER BY name").fetchall()
        return [dict(zip(_VARIABLE_COLS, r)) for r in rs]

    def get_variable(self, name: str) -> Optional[dict]:
        row = self.con.execute(
            f"SELECT {', '.join(_VARIABLE_COLS)} "
            "FROM variables WHERE name=?", [name]).fetchone()
        if row is None:
            return None
        return dict(zip(_VARIABLE_COLS, row))

    def native_resolution_m(self, variable: str) -> Optional[float]:
        row = self.con.execute(
            "SELECT MAX(native_res_m) FROM tiles WHERE variable=?",
            [variable]).fetchone()
        if row is None or row[0] is None:
            return None
        return float(row[0])

    def list_tiles(self, variable: Optional[str] = None) -> list[dict]:
        sql = f"SELECT {', '.join(_TILE_COLS)} FROM tiles "
        params: list = []
        if variable is not None:
            sql += "WHERE variable=? "
            params.append(variable)
        sql += "ORDER BY variable, t"
        rs = self.con.execute(sql, params).fetchall()
        return [dict(zip(_TILE_COLS, r)) for r in rs]

    def is_output_stale(self, output_var: str,
                         required_vars: list[str]) -> bool:
        """True if any required input has been written more recently than
        `output_var`. Lightweight dirty-propagation check; no explicit
        provenance table needed — leverages the existing tiles.fetched_at
        timestamp.

        Conservative on edges:
          * If `output_var` has no tiles, returns False (the standard
            'absent -> not satisfied' check kicks in higher up).
          * If `required_vars` is empty, returns False.
          * If a required input is missing, it can't have bumped, so
            returns False.
        """
        if not required_vars:
            return False
        out_row = self.con.execute(
            "SELECT MAX(fetched_at) FROM tiles WHERE variable = ?",
            [output_var]).fetchone()
        if not out_row or out_row[0] is None:
            return False
        out_t = out_row[0]
        placeholders = ",".join("?" * len(required_vars))
        in_row = self.con.execute(
            f"SELECT MAX(fetched_at) FROM tiles "
            f"WHERE variable IN ({placeholders})",
            list(required_vars)).fetchone()
        if not in_row or in_row[0] is None:
            return False
        return in_row[0] > out_t

    def save_scenario(self, name: str, grid, scenario_date: Optional[datetime]) -> None:
        self.con.execute(
            "INSERT OR REPLACE INTO scenarios "
            "(name, grid_epsg, grid_pixel_m, grid_width, grid_height, "
            " grid_x0, grid_y1, scenario_date, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
            [name, grid.crs_epsg, grid.pixel_m, grid.width, grid.height,
             grid.x0, grid.y1, scenario_date])

    def export_json_catalog(self, path: Path | str) -> None:
        """Mirror catalog state to a human-readable JSON file."""
        import json
        data = {
            "schema_version": self.schema_version(),
            "scenarios": [
                dict(zip(["name", "grid_epsg", "grid_pixel_m", "grid_width",
                          "grid_height", "grid_x0", "grid_y1",
                          "scenario_date", "created_at"], r))
                for r in self.con.execute("SELECT * FROM scenarios").fetchall()
            ],
            "variables": self.list_variables(),
            "tiles": [
                {**t, "t": t["t"].isoformat() if t["t"] else None,
                 "fetched_at": t["fetched_at"].isoformat() if t["fetched_at"] else None}
                for t in self.list_tiles()
            ],
        }

        def _fix(v):
            if isinstance(v, datetime):
                return v.isoformat()
            return v

        for s in data["scenarios"]:
            for k, v in s.items():
                s[k] = _fix(v)
        Path(path).write_text(json.dumps(data, indent=2, default=str))

    def close(self) -> None:
        self.con.close()
