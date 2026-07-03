"""MetaCatalog: DuckDB registry of dataset and model cards.

This is the installation-level "what exists" store, distinct from the
per-cube catalog ("what has been materialised"). At 100 drivers x 100
models, producer selection is mostly a structured query problem:

    coverage intersects the event bbox/time window
    AND regime bounds contain the event magnitude
    AND native resolution satisfies the consumer
    AND license permits use

Those filters are SQL + a little Python here; an LLM only arbitrates
among the survivors. Cards are stored as one JSON blob per card plus
indexed scalar columns for filtering, and a junction table mapping
cards to the ontology variables they produce/require — so "who can
produce economic_loss_usd over Dallas in 2026?" is a single query.

Everything round-trips through `export_json` / `import_json`, so the
whole registry is also a human-readable, diffable file.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

import duckdb

from .ontology import validate_name

BBox = tuple[float, float, float, float]   # (min_lon, min_lat, max_lon, max_lat)
GLOBAL_BBOX: BBox = (-180.0, -90.0, 180.0, 90.0)

# Ordered trust tiers; higher is more trusted.
TRUST_TIERS = ("unverified", "experimental", "validated", "reference")


def trust_rank(tier: str) -> int:
    try:
        return TRUST_TIERS.index(tier)
    except ValueError:
        return 0


# ====================================================================
# Card building blocks
# ====================================================================
@dataclass(frozen=True)
class Coverage:
    """Spatial + temporal extent a source can serve."""
    bbox: BBox = GLOBAL_BBOX
    t_start: Optional[str] = None    # ISO 8601; None = open
    t_end: Optional[str] = None

    def intersects(self, bbox: Optional[BBox] = None,
                   t_start: Optional[str] = None,
                   t_end: Optional[str] = None) -> bool:
        if bbox is not None:
            a, b = self.bbox, bbox
            if a[0] > b[2] or b[0] > a[2] or a[1] > b[3] or b[1] > a[3]:
                return False
        if t_start is not None and self.t_end is not None \
                and t_start > self.t_end:
            return False
        if t_end is not None and self.t_start is not None \
                and t_end < self.t_start:
            return False
        return True


@dataclass(frozen=True)
class Provenance:
    source_org: str = ""
    url: str = ""
    doi: str = ""
    license: str = ""
    retrieval: str = ""     # api | download | computed | ...


@dataclass(frozen=True)
class Quality:
    trust_tier: str = "unverified"   # see TRUST_TIERS
    uncertainty: str = ""            # free-text or quantified
    validation: str = ""             # how/against what it was validated


@dataclass(frozen=True)
class CostModel:
    """runtime_s ~= setup_s + cell_step_s * cells * steps / cores^alpha.

    Coefficients are meant to be fit from run lineage (the engine logs
    grid size, timesteps, ranks, and wall time for every run); the
    defaults are only a cold-start guess.
    """
    setup_s: float = 0.0
    cell_step_s: float = 0.0
    parallel_alpha: float = 1.0      # 1 = perfect scaling, 0 = serial
    max_cores: int = 0               # 0 = unlimited

    def estimate_s(self, cells: float, steps: float, cores: int) -> float:
        usable = max(1, min(cores, self.max_cores) if self.max_cores else cores)
        work = self.cell_step_s * cells * max(steps, 1.0)
        return self.setup_s + work / (usable ** self.parallel_alpha)


@dataclass
class DatasetCard:
    """One external data source, served by a registered driver."""
    id: str                          # e.g. "era5_wind"
    title: str
    description: str = ""
    variables: tuple[str, ...] = ()  # ontology names the driver produces
    driver: str = ""                 # CascadeCatalog driver name
    coverage: Coverage = field(default_factory=Coverage)
    native_res_m: float = 0.0        # 0 = resolution-free (e.g. vector)
    cadence_s: float = 0.0           # temporal cadence; 0 = static
    provenance: Provenance = field(default_factory=Provenance)
    quality: Quality = field(default_factory=Quality)
    cost: CostModel = field(default_factory=CostModel)   # fetch cost
    tags: tuple[str, ...] = ()


@dataclass
class ModelCard:
    """One registered model adapter."""
    name: str                        # CascadeCatalog model name
    title: str
    description: str = ""
    domain: str = ""                 # impact-physics | fire | economy | ...
    produces: tuple[str, ...] = ()
    requires: tuple[str, ...] = ()
    coverage: Coverage = field(default_factory=Coverage)  # applicability area
    valid_res_m: tuple[float, float] = (0.0, 0.0)  # (finest, coarsest); 0 = open
    valid_regimes: dict = field(default_factory=dict)
    #   e.g. {"energy_mt": (0.1, 50.0), "impactor_diam_m": (10, 1000)}
    fidelity_tier: str = "reduced-order"  # scaling-law | reduced-order | full-physics
    cost: CostModel = field(default_factory=CostModel)
    provenance: Provenance = field(default_factory=Provenance)
    quality: Quality = field(default_factory=Quality)
    tags: tuple[str, ...] = ()

    def regime_contains(self, magnitude: dict) -> bool:
        """True when every declared bound covers the event's value.

        Keys the card doesn't declare are unconstrained; keys the event
        doesn't provide can't be checked and pass (conservative would
        fail them — selection prefers cards that *do* match, via score).
        """
        for key, (lo, hi) in (self.valid_regimes or {}).items():
            if key in magnitude:
                v = magnitude[key]
                if not (lo <= v <= hi):
                    return False
        return True


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS cards (
    id          TEXT PRIMARY KEY,     -- dataset id or model name
    kind        TEXT NOT NULL,        -- 'dataset' | 'model'
    domain      TEXT DEFAULT '',
    fidelity    TEXT DEFAULT '',
    trust       INTEGER DEFAULT 0,
    license     TEXT DEFAULT '',
    native_res_m DOUBLE DEFAULT 0,
    bbox_minx   DOUBLE, bbox_miny DOUBLE, bbox_maxx DOUBLE, bbox_maxy DOUBLE,
    t_start     TEXT, t_end TEXT,
    card        TEXT NOT NULL         -- full card as JSON
);

CREATE TABLE IF NOT EXISTS card_variables (
    card_id   TEXT NOT NULL,
    variable  TEXT NOT NULL,
    role      TEXT NOT NULL           -- 'produces' | 'requires'
);
CREATE INDEX IF NOT EXISTS cv_var_role ON card_variables (variable, role);
"""


def _card_to_json(card) -> str:
    return json.dumps(asdict(card), default=str)


def _dataset_from_json(s: str) -> DatasetCard:
    d = json.loads(s)
    d["coverage"] = Coverage(bbox=tuple(d["coverage"]["bbox"]),
                             t_start=d["coverage"]["t_start"],
                             t_end=d["coverage"]["t_end"])
    d["provenance"] = Provenance(**d["provenance"])
    d["quality"] = Quality(**d["quality"])
    d["cost"] = CostModel(**d["cost"])
    d["variables"] = tuple(d["variables"])
    d["tags"] = tuple(d["tags"])
    return DatasetCard(**d)


def _model_from_json(s: str) -> ModelCard:
    d = json.loads(s)
    d["coverage"] = Coverage(bbox=tuple(d["coverage"]["bbox"]),
                             t_start=d["coverage"]["t_start"],
                             t_end=d["coverage"]["t_end"])
    d["provenance"] = Provenance(**d["provenance"])
    d["quality"] = Quality(**d["quality"])
    d["cost"] = CostModel(**d["cost"])
    d["produces"] = tuple(d["produces"])
    d["requires"] = tuple(d["requires"])
    d["valid_res_m"] = tuple(d["valid_res_m"])
    d["valid_regimes"] = {k: tuple(v) for k, v in d["valid_regimes"].items()}
    d["tags"] = tuple(d["tags"])
    return ModelCard(**d)


# ====================================================================
# The catalog
# ====================================================================
class MetaCatalog:
    def __init__(self, path: Path | str = ":memory:"):
        self.path = str(path)
        self.con = duckdb.connect(self.path)
        self.con.execute(SCHEMA_SQL)

    # ---- registration --------------------------------------------------
    def add_dataset(self, card: DatasetCard) -> None:
        for v in card.variables:
            validate_name(v, strict=True)
        self._upsert(card.id, "dataset", card,
                     produces=card.variables, requires=())

    def add_model(self, card: ModelCard) -> None:
        for v in (*card.produces, *card.requires):
            validate_name(v, strict=True)
        self._upsert(card.name, "model", card,
                     produces=card.produces, requires=card.requires)

    def _upsert(self, cid: str, kind: str, card,
                produces: tuple, requires: tuple) -> None:
        cov = card.coverage
        self.con.execute("DELETE FROM cards WHERE id=?", [cid])
        self.con.execute("DELETE FROM card_variables WHERE card_id=?", [cid])
        self.con.execute(
            "INSERT INTO cards (id, kind, domain, fidelity, trust, license, "
            " native_res_m, bbox_minx, bbox_miny, bbox_maxx, bbox_maxy, "
            " t_start, t_end, card) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [cid, kind,
             getattr(card, "domain", ""),
             getattr(card, "fidelity_tier", ""),
             trust_rank(card.quality.trust_tier),
             card.provenance.license,
             getattr(card, "native_res_m", 0.0),
             cov.bbox[0], cov.bbox[1], cov.bbox[2], cov.bbox[3],
             cov.t_start, cov.t_end,
             _card_to_json(card)])
        for v in produces:
            self.con.execute(
                "INSERT INTO card_variables VALUES (?, ?, 'produces')", [cid, v])
        for v in requires:
            self.con.execute(
                "INSERT INTO card_variables VALUES (?, ?, 'requires')", [cid, v])

    # ---- lookup ---------------------------------------------------------
    def get(self, cid: str):
        row = self.con.execute(
            "SELECT kind, card FROM cards WHERE id=?", [cid]).fetchone()
        if row is None:
            return None
        kind, blob = row
        return _dataset_from_json(blob) if kind == "dataset" \
            else _model_from_json(blob)

    def _query(self, kind: str, variable: Optional[str],
               bbox: Optional[BBox], t_start: Optional[str],
               t_end: Optional[str], min_trust: str,
               domain: Optional[str] = None,
               max_native_res_m: Optional[float] = None) -> list[tuple]:
        sql = ["SELECT c.kind, c.card FROM cards c"]
        params: list = []
        if variable is not None:
            sql.append("JOIN card_variables cv ON cv.card_id = c.id "
                       "AND cv.role = 'produces' AND cv.variable = ?")
            params.append(variable)
        where = ["c.kind = ?"]
        params.append(kind)
        where.append("c.trust >= ?")
        params.append(trust_rank(min_trust))
        if domain:
            where.append("c.domain = ?")
            params.append(domain)
        if max_native_res_m is not None:
            # 0 means resolution-free -> always acceptable
            where.append("(c.native_res_m = 0 OR c.native_res_m <= ?)")
            params.append(max_native_res_m)
        if bbox is not None:
            where.append("NOT (c.bbox_minx > ? OR ? > c.bbox_maxx "
                         "OR c.bbox_miny > ? OR ? > c.bbox_maxy)")
            params.extend([bbox[2], bbox[0], bbox[3], bbox[1]])
        if t_start is not None:
            where.append("(c.t_end IS NULL OR c.t_end >= ?)")
            params.append(t_start)
        if t_end is not None:
            where.append("(c.t_start IS NULL OR c.t_start <= ?)")
            params.append(t_end)
        sql.append("WHERE " + " AND ".join(where))
        return self.con.execute(" ".join(sql), params).fetchall()

    def find_datasets(self, variable: Optional[str] = None, *,
                      bbox: Optional[BBox] = None,
                      t_start: Optional[str] = None,
                      t_end: Optional[str] = None,
                      max_native_res_m: Optional[float] = None,
                      min_trust: str = "unverified") -> list[DatasetCard]:
        rows = self._query("dataset", variable, bbox, t_start, t_end,
                           min_trust, max_native_res_m=max_native_res_m)
        return [_dataset_from_json(b) for _, b in rows]

    def find_models(self, produces: Optional[str] = None, *,
                    domain: Optional[str] = None,
                    bbox: Optional[BBox] = None,
                    t_start: Optional[str] = None,
                    t_end: Optional[str] = None,
                    magnitude: Optional[dict] = None,
                    fidelity: Optional[str] = None,
                    min_trust: str = "unverified") -> list[ModelCard]:
        rows = self._query("model", produces, bbox, t_start, t_end,
                           min_trust, domain=domain)
        cards = [_model_from_json(b) for _, b in rows]
        if fidelity is not None:
            cards = [c for c in cards if c.fidelity_tier == fidelity]
        if magnitude:
            cards = [c for c in cards if c.regime_contains(magnitude)]
        return cards

    def candidates_for(self, variable: str, *,
                       bbox: Optional[BBox] = None,
                       t_start: Optional[str] = None,
                       t_end: Optional[str] = None,
                       magnitude: Optional[dict] = None,
                       min_trust: str = "unverified") -> list:
        """Every card (dataset or model) that can produce `variable` and
        survives the deterministic filters. This is the planner's main
        entry point."""
        out: list = []
        out += self.find_datasets(variable, bbox=bbox, t_start=t_start,
                                  t_end=t_end, min_trust=min_trust)
        out += self.find_models(variable, bbox=bbox, t_start=t_start,
                                t_end=t_end, magnitude=magnitude,
                                min_trust=min_trust)
        return out

    def list_ids(self, kind: Optional[str] = None) -> list[str]:
        sql, params = "SELECT id FROM cards", []
        if kind:
            sql += " WHERE kind=?"
            params.append(kind)
        return [r[0] for r in self.con.execute(sql + " ORDER BY id",
                                               params).fetchall()]

    # ---- human-readable round trip ---------------------------------------
    def export_json(self, path: Path | str) -> None:
        rows = self.con.execute(
            "SELECT kind, card FROM cards ORDER BY kind, id").fetchall()
        data = {"datasets": [json.loads(b) for k, b in rows if k == "dataset"],
                "models": [json.loads(b) for k, b in rows if k == "model"]}
        Path(path).write_text(json.dumps(data, indent=2))

    def import_json(self, path: Path | str) -> None:
        data = json.loads(Path(path).read_text())
        for d in data.get("datasets", ()):
            self.add_dataset(_dataset_from_json(json.dumps(d)))
        for m in data.get("models", ()):
            self.add_model(_model_from_json(json.dumps(m)))

    def close(self) -> None:
        self.con.close()
