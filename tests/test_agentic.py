"""Agentic layer: cube catalog v2 migration, metacatalog filters,
deterministic planner selection/focusing, and the tool surface."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentic.metacatalog import (Coverage, CostModel, DatasetCard,
                                 MetaCatalog, ModelCard, Provenance, Quality)
from agentic.ontology import lookup, validate_name
from agentic.planner import (ComputeBudget, EventSpec, PlanError, plan,
                             targets_for_intent)
from agentic import tools
from cube.catalog import Catalog, TileRecord


# ====================================================================
# cube catalog v2
# ====================================================================
def test_catalog_v2_provenance_roundtrip(tmp_path):
    cat = Catalog(tmp_path / "c.duckdb")
    assert cat.schema_version() == 2
    cat.register_variable("dem", "static", units="m",
                          standard_name="dem", domain="terrain")
    cat.add_tile(TileRecord(variable="dem", t=None, source="usgs",
                            native_res_m=30.0, source_url="https://usgs.gov",
                            license="public", checksum="abc123",
                            run_id="run-1"))
    v = cat.get_variable("dem")
    assert v["standard_name"] == "dem" and v["domain"] == "terrain"
    t = cat.list_tiles("dem")[0]
    assert t["source_url"] == "https://usgs.gov"
    assert t["run_id"] == "run-1"
    cat.close()


def test_catalog_v1_migrates_in_place(tmp_path):
    import duckdb
    path = str(tmp_path / "old.duckdb")
    con = duckdb.connect(path)
    con.execute("""
        CREATE TABLE variables (name TEXT PRIMARY KEY, kind TEXT NOT NULL,
            units TEXT, dtype TEXT, description TEXT, producer TEXT);
        CREATE TABLE tiles (variable TEXT NOT NULL, t TIMESTAMP,
            source TEXT, native_res_m DOUBLE, version INTEGER DEFAULT 0,
            fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
    """)
    con.execute("INSERT INTO variables VALUES "
                "('dem','static','m','f4','', 'dem_driver')")
    con.execute("INSERT INTO tiles (variable, t, source, native_res_m) "
                "VALUES ('dem', NULL, 'usgs', 30.0)")
    con.close()

    cat = Catalog(path)                       # migration happens here
    assert cat.schema_version() == 2
    v = cat.get_variable("dem")
    assert v["producer"] == "dem_driver" and v["standard_name"] == ""
    assert cat.list_tiles("dem")[0]["source_url"] == ""
    # v2 writes work against the migrated table
    cat.add_tile(TileRecord(variable="dem", t=None, source="usgs",
                            native_res_m=30.0, run_id="r2", version=1))
    cat.close()


def test_catalog_export_readable(tmp_path):
    cat = Catalog(tmp_path / "c.duckdb")
    cat.register_variable("dem", "static", standard_name="dem")
    cat.add_tile(TileRecord("dem", None, "usgs", 30.0, license="public"))
    out = tmp_path / "catalog.json"
    cat.export_json_catalog(out)
    data = json.loads(out.read_text())
    assert data["schema_version"] == 2
    assert data["tiles"][0]["license"] == "public"
    cat.close()


# ====================================================================
# ontology
# ====================================================================
def test_ontology_lookup_and_strict_validation():
    assert lookup("economic_loss_usd").domain == "economy"
    assert validate_name("dem") == "dem"
    assert validate_name("not_a_variable") == "not_a_variable"
    with pytest.raises(KeyError):
        validate_name("not_a_variable", strict=True)


# ====================================================================
# metacatalog fixtures: a toy asteroid cascade
# ====================================================================
DALLAS = EventSpec(kind="asteroid_impact", lat=32.78, lon=-96.81,
                   time="2026-07-02T12:00:00", radius_m=50_000,
                   magnitude={"energy_mt": 5.0}, intent="economic")


def toy_metacat() -> MetaCatalog:
    mc = MetaCatalog()
    conus = (-125.0, 24.0, -66.0, 50.0)
    europe = (-11.0, 35.0, 30.0, 60.0)

    mc.add_dataset(DatasetCard(
        id="worldpop", title="WorldPop gridded population",
        variables=("population_density",), driver="worldpop",
        coverage=Coverage(), native_res_m=100.0,
        provenance=Provenance(license="CC-BY-4.0"),
        quality=Quality(trust_tier="reference")))
    mc.add_dataset(DatasetCard(
        id="eu_census", title="EU census population (wrong continent)",
        variables=("population_density",), driver="eu_census",
        coverage=Coverage(bbox=europe), native_res_m=50.0,
        quality=Quality(trust_tier="reference")))
    mc.add_dataset(DatasetCard(
        id="assets_conus", title="CONUS built-asset values",
        variables=("asset_value_usd",), driver="assets_conus",
        coverage=Coverage(bbox=conus), native_res_m=500.0,
        quality=Quality(trust_tier="validated")))

    mc.add_model(ModelCard(
        name="impact_scaling", title="Collins-style impact scaling laws",
        domain="impact-physics",
        produces=("impact_energy_j", "blast_overpressure_pa"),
        requires=(),
        valid_regimes={"energy_mt": (0.01, 100.0)},
        fidelity_tier="scaling-law",
        cost=CostModel(setup_s=1.0),
        quality=Quality(trust_tier="validated")))
    mc.add_model(ModelCard(
        name="damage_model", title="Blast -> building damage",
        domain="exposure",
        produces=("building_damage_frac",),
        requires=("blast_overpressure_pa", "asset_value_usd"),
        valid_res_m=(100.0, 9000.0),
        fidelity_tier="reduced-order",
        cost=CostModel(setup_s=5.0, cell_step_s=0.0),
        quality=Quality(trust_tier="validated")))
    # two competing economy models
    mc.add_model(ModelCard(
        name="econ_quick", title="Quick loss scaling",
        domain="economy",
        produces=("economic_loss_usd",),
        requires=("building_damage_frac", "population_density"),
        fidelity_tier="scaling-law",
        cost=CostModel(setup_s=1.0),
        quality=Quality(trust_tier="experimental")))
    mc.add_model(ModelCard(
        name="econ_io", title="Input-output economic loss model",
        domain="economy",
        produces=("economic_loss_usd",),
        requires=("building_damage_frac", "population_density"),
        valid_regimes={"energy_mt": (0.1, 50.0)},
        valid_res_m=(100.0, 9000.0),
        fidelity_tier="reduced-order",
        cost=CostModel(setup_s=30.0, cell_step_s=0.0),
        quality=Quality(trust_tier="validated")))
    # a fire model that must NOT be pulled in by an economic query
    mc.add_model(ModelCard(
        name="fire_model", title="Fire spread",
        domain="fire",
        produces=("arrival_s", "fire_area"),
        requires=("ignition_t0", "nfuel_cat", "dem"),
        fidelity_tier="full-physics",
        cost=CostModel(setup_s=600.0, cell_step_s=2e-6),
        quality=Quality(trust_tier="validated")))
    return mc


def test_metacat_rejects_unknown_variable():
    mc = MetaCatalog()
    with pytest.raises(KeyError):
        mc.add_dataset(DatasetCard(id="x", title="x", variables=("bogus_var",)))


def test_metacat_filters():
    mc = toy_metacat()
    # bbox filter: Dallas excludes the EU census
    hits = mc.find_datasets("population_density", bbox=DALLAS.bbox())
    assert [c.id for c in hits] == ["worldpop"]
    # regime filter: energy 500 Mt excludes econ_io (bounded at 50)
    models = mc.find_models("economic_loss_usd",
                            magnitude={"energy_mt": 500.0})
    assert [m.name for m in models] == ["econ_quick"]
    # trust floor
    models = mc.find_models("economic_loss_usd", min_trust="validated")
    assert [m.name for m in models] == ["econ_io"]


def test_metacat_json_roundtrip(tmp_path):
    mc = toy_metacat()
    out = tmp_path / "registry.json"
    mc.export_json(out)
    data = json.loads(out.read_text())
    assert len(data["datasets"]) == 3 and len(data["models"]) == 5
    mc2 = MetaCatalog()
    mc2.import_json(out)
    assert mc2.get("econ_io").valid_regimes["energy_mt"] == (0.1, 50.0)


# ====================================================================
# planner
# ====================================================================
def test_plan_economic_intent_focuses_chain():
    p = plan(DALLAS, toy_metacat())
    # only the econ chain, never the fire model
    assert set(p.producers) == {"econ_io", "damage_model", "impact_scaling",
                                "worldpop", "assets_conus"}
    b = p.bindings["economic_loss_usd"]
    assert b.producer == "econ_io"            # fidelity+trust beat econ_quick
    assert "econ_quick" in b.alternatives     # audit trail kept
    assert p.fits_budget


def test_plan_resolution_negotiates_with_budget():
    mc = toy_metacat()
    ev = EventSpec(kind="asteroid_impact", lat=32.78, lon=-96.81,
                   radius_m=50_000, magnitude={"energy_mt": 5.0},
                   intent="fire", duration_s=6 * 3600)
    # fire chain needs ignition/fuel/dem datasets; register cheap ones
    mc.add_dataset(DatasetCard(id="thermal", title="t",
                               variables=("ignition_t0",), driver="thermal"))
    mc.add_dataset(DatasetCard(id="landfire", title="l",
                               variables=("nfuel_cat",), driver="landfire",
                               coverage=Coverage(bbox=(-125, 24, -66, 50))))
    mc.add_dataset(DatasetCard(id="dem", title="d",
                               variables=("dem",), driver="dem"))
    rich = plan(ev, mc, budget=ComputeBudget(cores=112, wall_s=48 * 3600))
    poor = plan(ev, mc, budget=ComputeBudget(cores=4, wall_s=1800))
    assert rich.resolution_m < poor.resolution_m
    assert rich.fits_budget


def test_plan_error_names_variable_and_chain():
    mc = toy_metacat()
    ev = EventSpec(kind="asteroid_impact", lat=48.85, lon=2.35,  # Paris
                   radius_m=50_000, magnitude={"energy_mt": 5.0},
                   intent="economic")
    with pytest.raises(PlanError) as ei:
        plan(ev, mc)   # assets_conus doesn't cover Paris
    assert ei.value.variable == "asset_value_usd"
    assert "building_damage_frac" in ei.value.chain


def test_intent_mapping():
    assert targets_for_intent("economic") == ("economic_loss_usd",)
    with pytest.raises(PlanError):
        targets_for_intent("weather_on_mars")


# ====================================================================
# tool surface
# ====================================================================
def test_tools_resolve_plan_ok_and_error():
    mc = toy_metacat()
    r = tools.resolve_plan(mc, {"kind": "asteroid_impact",
                                "lat": 32.78, "lon": -96.81,
                                "radius_m": 50_000,
                                "magnitude": {"energy_mt": 5.0},
                                "intent": "economic"})
    assert r["ok"] and r["plan"]["bindings"]["economic_loss_usd"]["producer"] == "econ_io"

    bad = tools.resolve_plan(mc, {"kind": "asteroid_impact",
                                  "lat": 48.85, "lon": 2.35,
                                  "intent": "economic"})
    assert not bad["ok"] and bad["variable"] == "asset_value_usd"


def test_tools_search_and_ontology():
    mc = toy_metacat()
    r = tools.search_models(mc, produces="economic_loss_usd",
                            min_trust="validated")
    assert r["ok"] and r["count"] == 1
    o = tools.describe_ontology()
    assert "economy" in o["domains"]


# ====================================================================
# CascadeCatalog wiring
# ====================================================================
def test_cascade_catalog_seeds_and_builds_subset():
    from models.catalog import CascadeCatalog

    class FakeDriver:
        name = "fake_pop"
        produces = ["population_density"]
        is_static = True
        def fetch(self, cube, t_start=None, t_end=None):
            return list(self.produces)

    card = DatasetCard(id="fake_pop", title="fake",
                       variables=("population_density",), driver="fake_pop")
    cat = CascadeCatalog()
    cat.add_driver("fake_pop", lambda ctx: FakeDriver(), card=card)
    cat.add_driver("unused", lambda ctx: (_ for _ in ()).throw(
        AssertionError("must not be built")))

    mc = MetaCatalog()
    cat.seed_metacatalog(mc)
    assert mc.get("fake_pop").driver == "fake_pop"

    reg = cat.build_registry(ctx=None, only={"fake_pop"})
    assert reg.has_variable("population_density")
    assert not reg.has("unused")
