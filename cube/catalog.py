"""DuckDB catalog: tracks variables and tiles in the cube."""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

import duckdb


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS variables (
    name        TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,        -- 'static' | 'time'
    units       TEXT,
    dtype       TEXT,
    description TEXT,
    producer    TEXT
);

CREATE TABLE IF NOT EXISTS tiles (
    variable     TEXT NOT NULL,
    t            TIMESTAMP,           -- NULL for static
    source       TEXT,
    native_res_m DOUBLE,
    version      INTEGER DEFAULT 0,
    fetched_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
"""


@dataclass
class TileRecord:
    variable: str
    t: Optional[datetime]
    source: str
    native_res_m: float
    version: int = 0


class Catalog:
    def __init__(self, path: Path | str):
        self.path = str(path)
        self.con = duckdb.connect(self.path)
        self.con.execute(SCHEMA_SQL)

    def register_variable(self, name: str, kind: str, units: str = "",
                          dtype: str = "f4", description: str = "",
                          producer: str = "") -> None:
        self.con.execute(
            "INSERT OR REPLACE INTO variables VALUES (?, ?, ?, ?, ?, ?)",
            [name, kind, units, dtype, description, producer])

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
            "(variable, t, source, native_res_m, version, fetched_at) "
            "VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
            [rec.variable, rec.t, rec.source, rec.native_res_m, rec.version])

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
            "SELECT name, kind, units, dtype, description, producer "
            "FROM variables ORDER BY name").fetchall()
        keys = ["name", "kind", "units", "dtype", "description", "producer"]
        return [dict(zip(keys, r)) for r in rs]

    def get_variable(self, name: str) -> Optional[dict]:
        row = self.con.execute(
            "SELECT name, kind, units, dtype, description, producer "
            "FROM variables WHERE name=?", [name]).fetchone()
        if row is None:
            return None
        keys = ["name", "kind", "units", "dtype", "description", "producer"]
        return dict(zip(keys, row))

    def native_resolution_m(self, variable: str) -> Optional[float]:
        row = self.con.execute(
            "SELECT MAX(native_res_m) FROM tiles WHERE variable=?",
            [variable]).fetchone()
        if row is None or row[0] is None:
            return None
        return float(row[0])

    def list_tiles(self, variable: Optional[str] = None) -> list[dict]:
        sql = ("SELECT variable, t, source, native_res_m, version, fetched_at "
               "FROM tiles ")
        params: list = []
        if variable is not None:
            sql += "WHERE variable=? "
            params.append(variable)
        sql += "ORDER BY variable, t"
        rs = self.con.execute(sql, params).fetchall()
        keys = ["variable", "t", "source", "native_res_m", "version", "fetched_at"]
        return [dict(zip(keys, r)) for r in rs]

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
