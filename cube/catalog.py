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

from engine.runtime.identity import strict_canonical_json, strict_json_loads

from .entries import (
    CubeEntry,
    ProjectionAuthority,
    EntryInput,
    EntryNotFound,
    ResolutionPolicy,
    grid_from_json,
    order_key,
)


SCHEMA_VERSION = 3

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

-- v3: immutable entries and the cascade edges between them.
--
-- `variables` stays exactly as it is: it is the concept vocabulary (units,
-- standard_name, domain, tags) and that part of the design is right. What it
-- cannot express is that one concept has several values of different cascade
-- depth. `entries` holds those; `entry_inputs` holds the edges.
--
-- Nothing here is ever updated in place. There is deliberately no
-- `superseded` flag, because that would reintroduce the mutable state this
-- table exists to remove. "Latest" is a query, not a column.
CREATE TABLE IF NOT EXISTS entries (
    entry_id       TEXT PRIMARY KEY,   -- hash of content AND derivation
    concept        TEXT NOT NULL,      -- joins to variables.name
    kind           TEXT NOT NULL,      -- 'static' | 'time'
    producer       TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    grid_json      TEXT DEFAULT '',    -- this entry's OWN grid; '' = cube grid
    depth          INTEGER NOT NULL,   -- 0 = ingested, n = n models deep
    run_id         TEXT DEFAULT '',
    committed_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS entries_concept ON entries (concept, depth);

CREATE TABLE IF NOT EXISTS entry_inputs (
    entry_id       TEXT NOT NULL,
    port           TEXT NOT NULL,
    input_entry_id TEXT NOT NULL,
    PRIMARY KEY (entry_id, port)
);
CREATE INDEX IF NOT EXISTS entry_inputs_input ON entry_inputs (input_entry_id);

-- Stage 8R: authoritative RuntimeStore -> Cube projection receipts.  This is
-- additive to the v3 immutable-entry model.  The receipt stores the complete
-- scientific descriptor and exact runtime/artifact/lineage identities; it is
-- committed in the same DuckDB transaction as the entry and all input edges.
CREATE TABLE IF NOT EXISTS entry_projections (
    projection_id  TEXT PRIMARY KEY,
    entry_id       TEXT NOT NULL,
    run_id         TEXT NOT NULL,
    recipe_id      TEXT NOT NULL,
    artifact_id    TEXT NOT NULL,
    descriptor_id  TEXT NOT NULL,
    projection_json TEXT NOT NULL,
    projected_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (run_id, recipe_id),
    UNIQUE (run_id, artifact_id)
);
CREATE INDEX IF NOT EXISTS entry_projections_artifact
    ON entry_projections (run_id, artifact_id);
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

    # ------------------------------------------------------------------
    # v3: immutable entries, cascade lineage, and resolution by policy
    # ------------------------------------------------------------------

    def commit_entry(self, entry: CubeEntry) -> CubeEntry:
        """Refuse caller-authored publication.

        A content digest and a free-form producer string are not artifact
        authority.  Production entries can only arrive through
        :class:`cube.projection.CubeProjector`, which reconstructs and replays
        the RuntimeStore commit.  The private fixture helper below exists only
        so the Cube's resolution/lineage algebra can be tested in isolation.
        """
        if not isinstance(entry, CubeEntry):
            raise TypeError("Cube publication requires a typed CubeEntry")
        raise PermissionError(
            "raw CubeEntry publication is forbidden; an authoritative "
            "RuntimeStore artifact projection is required")

    def _insert_entry_rows(self, entry: CubeEntry, depth: int) -> None:
        """The single place an entry row and its cascade edges are written.

        Both the fixture path and the authoritative projection path go through
        here, so the isolated semantic tests exercise exactly the rows
        production writes rather than a parallel implementation that could
        drift from it.
        """
        self.con.execute(
            "INSERT INTO entries (entry_id, concept, kind, producer, "
            "content_sha256, grid_json, depth, run_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [entry.entry_id, entry.concept, entry.kind, entry.producer,
             entry.content_sha256, entry.grid_json(), depth, entry.run_id])
        for edge in entry.inputs:
            self.con.execute(
                "INSERT INTO entry_inputs (entry_id, port, input_entry_id) "
                "VALUES (?, ?, ?)",
                [entry.entry_id, edge.port, edge.entry_id])

    def _commit_entry_for_test_fixture(self, entry: CubeEntry) -> CubeEntry:
        """Internal-only constructor for isolated Cube semantic tests.

        Runtime or application code must never use this path.  Its leading
        underscore and explicit name make that non-authoritative scope visible
        at every call site; no public alias exists.
        """
        if not isinstance(entry, CubeEntry):
            raise TypeError("fixture publication requires a typed CubeEntry")
        existing = self.entry(entry.entry_id)
        if existing is not None:
            return existing
        depth = 0
        for edge in entry.inputs:
            parent = self.entry(edge.entry_id)
            if parent is None:
                raise EntryNotFound(
                    f"entry {entry.entry_id[:12]} declares input "
                    f"{edge.entry_id[:12]} on port {edge.port!r}, which is not "
                    "committed; a cascade edge cannot point at nothing")
            depth = max(depth, parent.depth + 1)
        self._insert_entry_rows(entry, depth)
        return self.entry(entry.entry_id)

    def _commit_authoritative_projection(self, projection, authority):
        """Apply a verified projection receipt atomically (projector only).

        `authority` is unforgeable proof that the artifact was re-read and
        re-hashed from disk.  Without it this boundary replayed only the
        projection's *internal* identity, so a self-consistent receipt built by
        hand published a Cube entry for an artifact that never existed.
        """
        # Local import avoids a catalog <-> projector import cycle.
        from .projection import CubeEntryProjection

        if not isinstance(projection, CubeEntryProjection):
            raise TypeError("authoritative projection has the wrong type")
        if not isinstance(authority, ProjectionAuthority):
            raise PermissionError(
                "authoritative Cube publication requires a minted "
                "ProjectionAuthority; internal consistency is not authority")
        if not authority.authorizes(projection.run_id, projection.artifact_id,
                                    projection.content_sha256):
            raise PermissionError(
                "ProjectionAuthority was minted for a different artifact")
        # The constructor verifies both projection_id and entry_id, but replay
        # at this boundary protects against object construction tricks.
        if (projection.expected_id() != projection.projection_id
                or projection.entry().entry_id != projection.entry_id):
            raise ValueError("authoritative Cube projection identity is invalid")
        encoded = strict_canonical_json(projection.to_dict())
        entry = projection.entry()

        self.con.execute("BEGIN TRANSACTION")
        try:
            existing = self.con.execute(
                "SELECT projection_json,entry_id FROM entry_projections "
                "WHERE projection_id=?", [projection.projection_id],
            ).fetchone()
            if existing is not None:
                if existing[0] != encoded or existing[1] != projection.entry_id:
                    raise RuntimeError("Cube projection identity conflict")
                stored = self.entry(projection.entry_id)
                if stored is None or stored.entry_id != entry.entry_id:
                    raise RuntimeError(
                        "Cube projection receipt exists without its exact entry")
                self.con.execute("COMMIT")
                return stored

            conflict = self.con.execute(
                "SELECT projection_id FROM entry_projections WHERE "
                "(run_id=? AND recipe_id=?) OR (run_id=? AND artifact_id=?)",
                [projection.run_id, projection.recipe_id,
                 projection.run_id, projection.artifact_id],
            ).fetchone()
            if conflict is not None:
                raise RuntimeError(
                    "another Cube projection already owns this runtime artifact")

            depth = 0
            for edge, source in zip(entry.inputs, projection.inputs):
                parent_projection = self.con.execute(
                    "SELECT recipe_id,entry_id FROM entry_projections "
                    "WHERE run_id=? AND artifact_id=?",
                    [source.source_run_id, source.artifact_id],
                ).fetchone()
                if (parent_projection is None
                        or parent_projection[0] != source.recipe_id
                        or parent_projection[1] != source.entry_id
                        or edge.entry_id != source.entry_id
                        or edge.port != source.port):
                    raise EntryNotFound(
                        "authoritative Cube lineage does not match the exact "
                        "upstream artifact projection")
                parent = self.entry(edge.entry_id)
                if parent is None:
                    raise EntryNotFound(
                        "authoritative Cube lineage parent is not committed")
                depth = max(depth, parent.depth + 1)

            stored_entry = self.entry(entry.entry_id)
            if stored_entry is None:
                self._insert_entry_rows(entry, depth)
            else:
                # A repeat derivation in another run may legitimately reuse
                # the same immutable Cube entry.  It must already have an
                # authoritative receipt, and every entry field/edge/depth must
                # be identical; fixture-authored rows cannot be promoted.
                receipts = int(self.con.execute(
                    "SELECT COUNT(*) FROM entry_projections WHERE entry_id=?",
                    [entry.entry_id],
                ).fetchone()[0])
                if (receipts < 1
                        or stored_entry.concept != entry.concept
                        or stored_entry.kind != entry.kind
                        or stored_entry.producer != entry.producer
                        or stored_entry.content_sha256 != entry.content_sha256
                        or stored_entry.grid != entry.grid
                        or stored_entry.depth != depth
                        or stored_entry.inputs != entry.inputs):
                    raise RuntimeError(
                        "existing Cube entry does not match this authoritative "
                        "repeat derivation")
            self.con.execute(
                "INSERT INTO entry_projections"
                "(projection_id,entry_id,run_id,recipe_id,artifact_id,"
                "descriptor_id,projection_json) VALUES(?,?,?,?,?,?,?)",
                [projection.projection_id, projection.entry_id,
                 projection.run_id, projection.recipe_id,
                 projection.artifact_id, projection.descriptor_id, encoded],
            )
            self.con.execute("COMMIT")
        except BaseException:
            self.con.execute("ROLLBACK")
            raise
        result = self.entry(entry.entry_id)
        if result is None:  # pragma: no cover - transaction invariant
            raise RuntimeError("Cube projection committed without an entry")
        return result

    def projection_for_artifact(self, run_id: str, artifact_id: str):
        """Return the verified projection receipt for one runtime artifact."""
        from .projection import CubeEntryProjection

        row = self.con.execute(
            "SELECT projection_id,entry_id,projection_json FROM entry_projections "
            "WHERE run_id=? AND artifact_id=?", [run_id, artifact_id],
        ).fetchone()
        if row is None:
            return None
        value = CubeEntryProjection.from_dict(strict_json_loads(row[2]))
        if value.projection_id != row[0] or value.entry_id != row[1]:
            raise RuntimeError("persisted Cube projection receipt is corrupt")
        return value

    def projection(self, projection_id: str):
        """Return one verified projection receipt by its content identity."""
        from .projection import CubeEntryProjection

        row = self.con.execute(
            "SELECT entry_id,projection_json FROM entry_projections "
            "WHERE projection_id=?", [projection_id],
        ).fetchone()
        if row is None:
            return None
        value = CubeEntryProjection.from_dict(strict_json_loads(row[1]))
        if value.projection_id != projection_id or value.entry_id != row[0]:
            raise RuntimeError("persisted Cube projection receipt is corrupt")
        return value

    def _entry_from_row(self, row) -> CubeEntry:
        edges = tuple(
            EntryInput(port, input_id) for port, input_id in self.con.execute(
                "SELECT port, input_entry_id FROM entry_inputs "
                "WHERE entry_id = ? ORDER BY port", [row[0]]).fetchall())
        return CubeEntry(
            entry_id=row[0], concept=row[1], kind=row[2], producer=row[3],
            content_sha256=row[4], grid=grid_from_json(row[5] or ""),
            depth=int(row[6]), inputs=edges, run_id=row[7] or "",
            committed_at=row[8])

    _ENTRY_COLS = ("entry_id, concept, kind, producer, content_sha256, "
                   "grid_json, depth, run_id, committed_at")

    def entry(self, entry_id: str) -> Optional[CubeEntry]:
        row = self.con.execute(
            f"SELECT {self._ENTRY_COLS} FROM entries WHERE entry_id = ?",
            [entry_id]).fetchone()
        return None if row is None else self._entry_from_row(row)

    def entries_for(self, concept: str) -> list[CubeEntry]:
        rows = self.con.execute(
            f"SELECT {self._ENTRY_COLS} FROM entries WHERE concept = ?",
            [concept]).fetchall()
        return [self._entry_from_row(row) for row in rows]

    def resolve(self, concept: str,
                policy: ResolutionPolicy = ResolutionPolicy.MOST_DERIVED
                ) -> CubeEntry:
        """Which value of `concept` a consumer should get, under `policy`.

        This is the replacement for reading a mutable slot. `MOST_DERIVED` is
        what "latest available" meant -- the deepest entry in the cascade --
        but stated as a derivation question, so the answer no longer depends on
        which model happened to write last.
        """
        if not isinstance(policy, ResolutionPolicy):
            raise TypeError("resolution requires a typed ResolutionPolicy")
        return self._resolve_among(concept, policy, frozenset())

    def _resolve_among(self, concept: str, policy: ResolutionPolicy,
                       exclude: frozenset) -> CubeEntry:
        candidates = [item for item in self.entries_for(concept)
                      if item.entry_id not in exclude]
        if not candidates:
            raise EntryNotFound(f"no committed entry for concept {concept!r}")
        return max(candidates, key=order_key(policy))

    def dependents(self, entry_id: str) -> frozenset:
        """`entry_id` and everything that transitively consumes it.

        Needed because a cascade model routinely *perturbs* a concept: the
        fire model both consumes `temperature` and produces `temperature`. So
        when asking whether its own input is still current, it and everything
        downstream of it must be excluded, or it would resolve to itself and be
        permanently stale with respect to its own output.
        """
        seen = {entry_id}
        frontier = [entry_id]
        while frontier:
            current = frontier.pop()
            for (child,) in self.con.execute(
                    "SELECT entry_id FROM entry_inputs WHERE input_entry_id=?",
                    [current]).fetchall():
                if child not in seen:
                    seen.add(child)
                    frontier.append(child)
        return frozenset(seen)

    def lineage(self, entry_id: str) -> list[CubeEntry]:
        """Every entry that transitively fed this one, deepest first.

        This is what makes a cascade reproducible: it answers "what did this
        model actually consume", which a last-writer-wins slot cannot.
        """
        start = self.entry(entry_id)
        if start is None:
            raise EntryNotFound(f"unknown entry {entry_id!r}")
        seen: dict[str, CubeEntry] = {}
        frontier = [edge.entry_id for edge in start.inputs]
        while frontier:
            current = frontier.pop()
            if current in seen:
                continue
            found = self.entry(current)
            if found is None:
                raise EntryNotFound(
                    f"lineage of {entry_id[:12]} references uncommitted "
                    f"entry {current[:12]}")
            seen[current] = found
            frontier.extend(edge.entry_id for edge in found.inputs)
        return sorted(seen.values(), key=lambda item: (-item.depth,
                                                       item.entry_id))

    def stale_inputs(self, entry_id: str,
                     policy: ResolutionPolicy = ResolutionPolicy.MOST_DERIVED
                     ) -> dict[str, tuple[str, str]]:
        """Ports whose recorded input is no longer what `policy` resolves to.

        Content-based, and exact. `is_output_stale` compares wall-clock
        `fetched_at` values, so re-fetching identical bytes marks everything
        downstream stale and forces a recompute that may be many hours of
        model time. Here an input only counts as changed when the entry a
        consumer would now receive genuinely differs from the one it used.

        Returns `{port: (recorded_entry_id, current_entry_id)}`, empty when
        the entry is still current.
        """
        target = self.entry(entry_id)
        if target is None:
            raise EntryNotFound(f"unknown entry {entry_id!r}")
        # A perturbing model produces the same concept it consumes, so it must
        # not resolve its own output as its own input.
        blocked = self.dependents(entry_id)
        drifted: dict[str, tuple[str, str]] = {}
        for edge in target.inputs:
            recorded = self.entry(edge.entry_id)
            if recorded is None:
                raise EntryNotFound(
                    f"port {edge.port!r} references uncommitted entry")
            try:
                current = self._resolve_among(recorded.concept, policy, blocked)
            except EntryNotFound:
                continue
            if current.entry_id != edge.entry_id:
                drifted[edge.port] = (edge.entry_id, current.entry_id)
        return drifted

    def is_entry_stale(self, entry_id: str,
                       policy: ResolutionPolicy = ResolutionPolicy.MOST_DERIVED
                       ) -> bool:
        return bool(self.stale_inputs(entry_id, policy))

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
