"""Cascade catalog — the pluggable model/driver registry.

This is the "plug a model" surface for the asteroid-impact cascade
system. It separates *what producers exist* (this file) from *how the
DAG runs* (engine) and *what the scenario is* (config).

Mental model
------------
Everything that can materialise a cube variable is a **producer**:

  * **Data drivers** fetch external bytes (ERA5 wind, LANDFIRE fuel,
    DEM, KML thermal pulse) and write a cube variable.
  * **Models** compute a cube variable from *other* cube variables
    (WRF-SFIRE consumes ignition_t0/nfuel_cat/dem, produces arrival_s).

The engine does not distinguish them — both implement the ProducerV2
contract (``produces`` / ``requires`` / ``run``). So a model that needs
another model's output just lists that output in its ``requires``; the
scheduler resolves the chain automatically (see
``engine.data_adapter.DataAdapter.resolve`` and
``engine.pipeline.Pipeline.from_targets``).

Adding model 2 (e.g. a flood or smoke-dispersion model that consumes
WRF-SFIRE's ``arrival_s`` / ``fire_area``)::

    catalog.add_model("flood", build_flood)

where ``build_flood(ctx)`` returns a ``ModelAdapter`` whose
``data_adapter`` declares ``DataNeed("arrival_s")``. Nothing else
changes — request the flood output as a target and the runner pulls
WRF-SFIRE in front of it.

The catalog is HPC-agnostic: model factories receive a ``BuildContext``
carrying the resolved :class:`~hpc.profiles.HPCProfile`, so the WRF MPI
launch line is correct on whatever cluster the run lands on.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from engine import DataDriverAdapter, ProducerRegistry
from engine.log import get_logger
from hpc.profiles import HPCProfile, detect_profile

_log = get_logger(__name__)


# ====================================================================
# Scenario configuration
# ====================================================================
@dataclass
class ScenarioConfig:
    """Everything that defines one cascade scenario.

    Geometry, time window, and resolution are model-agnostic — they
    describe the cube grid every producer writes into.
    """
    # --- area of interest ---
    kml: Path
    city: str = "Dallas TX USA"
    band: str = "Mean"
    center_lon: float = -96.80889
    center_lat: float = 32.77998
    radius_m: float = 50_000.0

    # --- cube grid ---
    pixel_m: float = 900.0          # atmosphere-mesh resolution
    fire_mesh_ratio: int = 10       # fire mesh = pixel_m / fire_mesh_ratio

    # --- time ---
    start: datetime = field(default_factory=lambda: datetime(2019, 9, 4, 12))
    sim_seconds: int = 24 * 3600    # WRF-SFIRE run length
    wind_days: int = 10             # ERA5 wind window cached in cube

    # --- event magnitude ---
    energy_mt: float = 5.0          # impact/airburst yield (megatons TNT)

    # --- WRF-SFIRE run mode ---
    use_real: bool = False          # real.exe (met_em) vs ideal.exe
    pulse_seconds: float = 10.0     # asteroid thermal pulse duration

    @property
    def fire_pixel_m(self) -> float:
        return self.pixel_m / self.fire_mesh_ratio


# ====================================================================
# Build context handed to every factory
# ====================================================================
@dataclass
class BuildContext:
    """Resolved environment passed to each producer factory.

    Carries the scenario config, the detected HPC profile (so MPI
    launch lines are correct), and the install layout (where the
    compiled WRF-SFIRE / WPS / WPS_GEOG live).
    """
    config: ScenarioConfig
    profile: HPCProfile
    install_root: Path
    templates_dir: Path
    np: int = 0                     # MPI ranks; 0 -> profile.default_np
    met_em_dir: Optional[Path] = None
    work_dir: Path = field(default_factory=lambda: Path("data/work"))
    # Optional config-driven WRF scenario (nested domains, &chem). When
    # present, the WRF-SFIRE factory generates namelist.input from it
    # instead of the single-domain template.
    wrf_scenario: object = None
    # Requested cube targets — lets factories enable the matching opt-in
    # outputs (ros_max/fire_intensity/fuel_consumed; pm25/smoke_tracer).
    targets: tuple[str, ...] = ()

    def ranks(self) -> int:
        return self.np if self.np > 0 else self.profile.default_np

    # Convenience accessors for the compiled stack
    @property
    def sfire_root(self) -> Path:
        return self.install_root / "WRF-SFIRE"

    @property
    def wps_root(self) -> Path:
        return self.install_root / "WPS"

    @property
    def geog_dir(self) -> Path:
        return self.install_root / "WPS_GEOG"

    @property
    def wrf_bin(self) -> Path:
        return self.sfire_root / "main" / "wrf.exe"

    @property
    def real_bin(self) -> Path:
        return self.sfire_root / "main" / "real.exe"

    @property
    def ideal_bin(self) -> Path:
        return self.sfire_root / "main" / "ideal.exe"


# Producer factories take a BuildContext and return a producer object.
DriverFactory = Callable[[BuildContext], object]
ModelFactory = Callable[[BuildContext], object]


# ====================================================================
# Catalog
# ====================================================================
class CascadeCatalog:
    """Ordered registry of data-driver and model factories.

    The order of registration does NOT determine execution order — the
    engine derives that from the produces/requires DAG. Order here only
    controls a stable, readable registry listing.
    """

    def __init__(self) -> None:
        self._drivers: list[tuple[str, DriverFactory]] = []
        self._models: list[tuple[str, ModelFactory]] = []
        self._cards: dict[str, object] = {}   # name -> DatasetCard | ModelCard

    # ---- registration ----
    def add_driver(self, name: str, factory: DriverFactory,
                   card: object = None) -> "CascadeCatalog":
        self._drivers.append((name, factory))
        if card is not None:
            self._cards[name] = card
        return self

    def add_model(self, name: str, factory: ModelFactory,
                  card: object = None) -> "CascadeCatalog":
        self._models.append((name, factory))
        if card is not None:
            self._cards[name] = card
        return self

    # ---- introspection ----
    def driver_names(self) -> list[str]:
        return [n for n, _ in self._drivers]

    def model_names(self) -> list[str]:
        return [n for n, _ in self._models]

    def card_for(self, name: str):
        return self._cards.get(name)

    def seed_metacatalog(self, metacat) -> None:
        """Publish every registered card into an agentic MetaCatalog so
        the planner can search/filter producers without importing or
        instantiating any of them."""
        from agentic.metacatalog import DatasetCard, ModelCard
        for name, card in self._cards.items():
            if isinstance(card, DatasetCard):
                metacat.add_dataset(card)
            elif isinstance(card, ModelCard):
                metacat.add_model(card)

    # ---- build ----
    def build_registry(self, ctx: BuildContext,
                       only: Optional[set[str]] = None) -> ProducerRegistry:
        """Instantiate factories and return an engine registry.

        Data drivers are wrapped in ``DataDriverAdapter`` so the
        ``fetch``-style protocol becomes a ProducerV2. Models are
        already ProducerV2 (via ``ModelAdapter``) and register directly.

        ``only`` restricts the build to the named producers — this is
        how a RunPlan's bindings become a registry: pass
        ``only=set(plan.producer_names())`` and competing producers for
        the same variable never collide, because exactly one was chosen
        at plan time.
        """
        reg = ProducerRegistry()
        for name, factory in self._drivers:
            if only is not None and name not in only:
                continue
            drv = factory(ctx)
            reg.register(DataDriverAdapter(driver=drv))
            _log.info("[catalog] driver registered: %s -> %s",
                      name, list(getattr(drv, "produces", [])))
        for name, factory in self._models:
            if only is not None and name not in only:
                continue
            model = factory(ctx)
            reg.register(model)
            _log.info("[catalog] model registered: %s -> %s",
                      name, [getattr(v, "name", v)
                             for v in getattr(model, "produces", [])])
        return reg


# ====================================================================
# Built-in producers
# ====================================================================
def build_thermal_driver(ctx: BuildContext):
    from drivers.thermal import ThermalDriver
    cfg = ctx.config
    return ThermalDriver(kml_path=str(cfg.kml), city=cfg.city,
                         band=cfg.band, pulse_seconds=cfg.pulse_seconds)


def build_landfire_driver(ctx: BuildContext):
    from drivers.landfire_fbfm13 import LandfireFBFM13Driver
    return LandfireFBFM13Driver()


def build_dem_driver(ctx: BuildContext):
    from drivers.dem import DEMDriver
    return DEMDriver()


def build_era5_wind_driver(ctx: BuildContext):
    from drivers.era5_wind import ERA5WindDriver
    return ERA5WindDriver()


def build_wrf_sfire(ctx: BuildContext):
    """Construct WRF-SFIRE as cascade model 1.

    The MPI launch line is built from the detected HPC profile, so the
    same factory yields ``mpirun -np 56`` on CHPC, ``ibrun`` on
    Stampede, ``srun`` on Frontier, etc. ``wrf_cmd`` is fully injectable
    on the adapter, so no adapter code is HPC-specific.
    """
    from models.wrf_sfire_adapter import WRFSFireAdapter
    from drivers.dem import DEMDriver

    cfg = ctx.config
    prof = ctx.profile
    nranks = ctx.ranks()

    wrf_cmd = prof.mpi_cmd(ctx.wrf_bin, nranks)
    real_cmd = prof.mpi_cmd(ctx.real_bin, nranks) if cfg.use_real else None

    # Fire-mesh DEM sampled at native resolution (separate cache).
    fire_dem = DEMDriver(cache_dir="data/raw/dem_fire")

    # Enable opt-in outputs only when the corresponding targets are asked
    # for, so a fake/idealised binary that omits them isn't required to.
    targets = set(ctx.targets)
    extra_outputs = bool(targets & {"ros_max", "fire_intensity",
                                    "fuel_consumed"})
    smoke_outputs = bool(targets & {"pm25_surface", "smoke_tracer"})

    # Config-driven nested namelist when a WRF scenario is supplied.
    namelist_builder = None
    fire_domain_id = 1
    sim_seconds = cfg.sim_seconds
    fire_mesh_ratio = cfg.fire_mesh_ratio
    if ctx.wrf_scenario is not None:
        from models.wrf_config import NamelistBuilder
        sc = ctx.wrf_scenario
        namelist_builder = NamelistBuilder(sc)
        fire_domain_id = sc.fire_domain_index
        sim_seconds = int(sc.duration_days * 86400)
        fire_mesh_ratio = sc.fire_mesh_ratio

    return WRFSFireAdapter(
        sfire_dir=ctx.templates_dir,         # corrected ifire=1 templates
        ideal_cmd=prof.mpi_cmd(ctx.ideal_bin, nranks),
        real_cmd=real_cmd,
        wrf_cmd=wrf_cmd,
        met_em_dir=ctx.met_em_dir if cfg.use_real else None,
        sim_seconds=sim_seconds,
        fire_mesh_ratio=fire_mesh_ratio,
        fire_dem_driver=fire_dem,
        extra_outputs=extra_outputs,
        smoke_outputs=smoke_outputs,
        namelist_builder=namelist_builder,
        fire_domain_id=fire_domain_id,
        stage_root=ctx.work_dir / "wrf_stage",
        keep_stage=True,   # TEMP debug: preserve stage to inspect real.exe rsl logs
    )


def _builtin_cards() -> dict:
    """Machine-readable metadata for the built-in producers.

    These cards are what the agentic planner searches and filters —
    coverage, provenance, trust, regimes, cost — without importing or
    running any producer code. Cost coefficients are cold-start guesses;
    refit them from run lineage as real runs accumulate.
    """
    from agentic.metacatalog import (Coverage, CostModel, DatasetCard,
                                     ModelCard, Provenance, Quality)

    conus = (-125.0, 24.0, -66.0, 50.0)
    return {
        "thermal": DatasetCard(
            id="thermal", title="PDC asteroid thermal-damage footprint",
            description="Airburst thermal fluence/power + ignition map "
                        "parsed from a PDC exercise KML",
            variables=("thermal_fluence", "thermal_power",
                       "ignition_t0", "burnable"),
            driver="thermal",
            provenance=Provenance(source_org="NASA/JPL PDC exercise",
                                  retrieval="file", license="public"),
            quality=Quality(trust_tier="validated",
                            validation="PDC 2019 exercise product"),
            cost=CostModel(setup_s=5.0)),
        "landfire": DatasetCard(
            id="landfire", title="LANDFIRE FBFM13 fuel model",
            variables=("fbfm13", "nfuel_cat"), driver="landfire",
            coverage=Coverage(bbox=conus),
            native_res_m=30.0,
            provenance=Provenance(source_org="USGS LANDFIRE",
                                  url="https://landfire.gov",
                                  retrieval="api", license="public"),
            quality=Quality(trust_tier="reference"),
            cost=CostModel(setup_s=60.0)),
        "dem": DatasetCard(
            id="dem", title="Digital elevation model (SRTM/3DEP)",
            variables=("dem", "slope_deg", "aspect_deg"), driver="dem",
            coverage=Coverage(bbox=(-180.0, -60.0, 180.0, 60.0)),
            native_res_m=30.0,
            provenance=Provenance(source_org="USGS", retrieval="api",
                                  license="public"),
            quality=Quality(trust_tier="reference"),
            cost=CostModel(setup_s=60.0)),
        "era5_wind": DatasetCard(
            id="era5_wind", title="ERA5 reanalysis 10 m wind",
            variables=("wind_speed_ms", "wind_dir_deg"), driver="era5_wind",
            coverage=Coverage(t_start="1940-01-01T00:00:00"),
            native_res_m=31_000.0, cadence_s=3600.0,
            provenance=Provenance(source_org="ECMWF/Copernicus",
                                  url="https://cds.climate.copernicus.eu",
                                  retrieval="api",
                                  license="CC-BY-4.0"),
            quality=Quality(trust_tier="reference"),
            cost=CostModel(setup_s=300.0)),
        "exposure": DatasetCard(
            id="exposure", title="Synthetic population + asset exposure",
            description="Monocentric-city placeholder for population "
                        "density and built-asset value; replace with "
                        "WorldPop/HAZUS drivers for real estimates",
            variables=("population_density", "asset_value_usd"),
            driver="exposure",
            provenance=Provenance(source_org="synthetic",
                                  retrieval="computed", license="public"),
            quality=Quality(trust_tier="experimental",
                            uncertainty="parametric placeholder"),
            cost=CostModel(setup_s=1.0)),
        "impact_scaling": ModelCard(
            name="impact_scaling",
            title="Collins-style impact scaling laws",
            description="Blast-overpressure footprint from event energy "
                        "(Earth Impact Effects Program relations)",
            domain="impact-physics",
            produces=("impact_energy_j", "blast_overpressure_pa"),
            requires=(),
            valid_regimes={"energy_mt": (0.01, 100.0)},
            fidelity_tier="scaling-law",
            cost=CostModel(setup_s=1.0),
            provenance=Provenance(source_org="Collins, Melosh & Marcus "
                                             "2005",
                                  doi="10.1111/j.1945-5100.2005."
                                      "tb00157.x"),
            quality=Quality(trust_tier="validated",
                            validation="published scaling relations")),
        "blast_damage": ModelCard(
            name="blast_damage", title="Blast -> building damage",
            description="Logistic overpressure vulnerability curve "
                        "(HAZUS-flavoured, p50=35 kPa)",
            domain="exposure",
            produces=("building_damage_frac",),
            requires=("blast_overpressure_pa",),
            fidelity_tier="reduced-order",
            cost=CostModel(setup_s=1.0),
            quality=Quality(trust_tier="experimental",
                            uncertainty="generic fragility curve, "
                                        "not structure-specific")),
        "econ_loss": ModelCard(
            name="econ_loss", title="HAZUS-style economic loss",
            description="Direct loss = damage x asset value; flat "
                        "indirect multiplier; population exposure "
                        "inside the damage footprint",
            domain="economy",
            produces=("economic_loss_usd", "population_exposure"),
            requires=("building_damage_frac", "asset_value_usd",
                      "population_density"),
            fidelity_tier="reduced-order",
            cost=CostModel(setup_s=1.0),
            quality=Quality(trust_tier="experimental",
                            uncertainty="flat indirect multiplier; no "
                                        "sectoral IO table yet")),
        "wrf_sfire": ModelCard(
            name="wrf_sfire", title="WRF-SFIRE coupled fire-atmosphere",
            description="Full-physics coupled atmosphere + fire spread "
                        "(+chem smoke when enabled)",
            domain="fire",
            produces=("arrival_s", "fire_area", "ros_max",
                      "fire_intensity", "fuel_consumed",
                      "pm25_surface", "smoke_tracer"),
            requires=("ignition_t0", "nfuel_cat", "dem",
                      "wind_speed_ms", "wind_dir_deg"),
            valid_res_m=(100.0, 12_000.0),   # atmosphere mesh
            fidelity_tier="full-physics",
            cost=CostModel(setup_s=600.0, cell_step_s=2e-6,
                           parallel_alpha=0.9),
            provenance=Provenance(source_org="openwfm.org",
                                  url="https://github.com/openwfm/WRF-SFIRE",
                                  license="public"),
            quality=Quality(trust_tier="validated",
                            validation="published model; site-specific "
                                       "validation pending")),
    }


def build_exposure_driver(ctx: BuildContext):
    from drivers.exposure import SyntheticExposureDriver
    return SyntheticExposureDriver()


def build_impact_scaling(ctx: BuildContext):
    """Collins-style scaling laws — cascade model 0 (scaling-law tier)."""
    from engine import ModelFunctionAdapter, VarSpec
    from models.impact_scaling import run_impact_scaling

    energy_mt = getattr(ctx.config, "energy_mt", 5.0)
    return ModelFunctionAdapter(
        name="impact_scaling",
        produces=[VarSpec("impact_energy_j", kind="static", units="J"),
                  VarSpec("blast_overpressure_pa", kind="static",
                          units="Pa")],
        requires=[],
        func=lambda cube, req: run_impact_scaling(cube, req,
                                                  energy_mt=energy_mt))


def build_blast_damage(ctx: BuildContext):
    from engine import MergePolicy, ModelFunctionAdapter, VarSpec
    from models.consequence import run_blast_damage
    return ModelFunctionAdapter(
        name="blast_damage",
        produces=[VarSpec("building_damage_frac", kind="static",
                          units="0..1",
                          merge_policy=MergePolicy.MONOTONE_MAX)],
        requires=["blast_overpressure_pa"],
        func=run_blast_damage)


def build_econ_loss(ctx: BuildContext):
    from engine import ModelFunctionAdapter, VarSpec
    from models.consequence import run_econ_loss
    return ModelFunctionAdapter(
        name="econ_loss",
        produces=[VarSpec("economic_loss_usd", kind="static", units="USD"),
                  VarSpec("population_exposure", kind="static",
                          units="people")],
        requires=["building_damage_frac", "asset_value_usd",
                  "population_density"],
        func=run_econ_loss)


def default_catalog() -> CascadeCatalog:
    """The built-in cascade.

    Data drivers + WRF-SFIRE as **model 1**. Future models append here::

        cat.add_model("smoke",  build_smoke_dispersion)   # model 2
        cat.add_model("flood",  build_flood)              # model 3

    A model that consumes WRF-SFIRE output declares e.g.
    ``DataNeed("arrival_s")`` in its ``data_adapter``; the engine then
    runs WRF-SFIRE before it, automatically.

    Every producer registers with a card so the agentic planner can
    select it by coverage/regime/cost (``seed_metacatalog``).
    """
    cards = _builtin_cards()
    cat = CascadeCatalog()
    cat.add_driver("thermal",   build_thermal_driver,   card=cards["thermal"])
    cat.add_driver("landfire",  build_landfire_driver,  card=cards["landfire"])
    cat.add_driver("dem",       build_dem_driver,       card=cards["dem"])
    cat.add_driver("era5_wind", build_era5_wind_driver, card=cards["era5_wind"])
    cat.add_driver("exposure",  build_exposure_driver,  card=cards["exposure"])
    cat.add_model("wrf_sfire",  build_wrf_sfire,        card=cards["wrf_sfire"])  # <-- model 1
    # consequence chain: impact physics -> damage -> economy
    cat.add_model("impact_scaling", build_impact_scaling,
                  card=cards["impact_scaling"])
    cat.add_model("blast_damage",   build_blast_damage,
                  card=cards["blast_damage"])
    cat.add_model("econ_loss",      build_econ_loss,
                  card=cards["econ_loss"])
    return cat


def make_context(config: ScenarioConfig,
                 *,
                 install_root: str | Path,
                 templates_dir: str | Path,
                 hpc_profile: str | None = None,
                 np: int = 0,
                 met_em_dir: str | Path | None = None,
                 work_dir: str | Path = "data/work",
                 wrf_scenario: object = None,
                 targets: tuple[str, ...] = ()) -> BuildContext:
    """Resolve an HPC profile and assemble a BuildContext."""
    profile = detect_profile(hpc_profile)
    return BuildContext(
        config=config,
        profile=profile,
        install_root=Path(install_root),
        templates_dir=Path(templates_dir),
        np=np,
        met_em_dir=Path(met_em_dir) if met_em_dir else None,
        work_dir=Path(work_dir),
        wrf_scenario=wrf_scenario,
        targets=tuple(targets),
    )
