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

    # ---- registration ----
    def add_driver(self, name: str, factory: DriverFactory) -> "CascadeCatalog":
        self._drivers.append((name, factory))
        return self

    def add_model(self, name: str, factory: ModelFactory) -> "CascadeCatalog":
        self._models.append((name, factory))
        return self

    # ---- introspection ----
    def driver_names(self) -> list[str]:
        return [n for n, _ in self._drivers]

    def model_names(self) -> list[str]:
        return [n for n, _ in self._models]

    # ---- build ----
    def build_registry(self, ctx: BuildContext) -> ProducerRegistry:
        """Instantiate every factory and return an engine registry.

        Data drivers are wrapped in ``DataDriverAdapter`` so the
        ``fetch``-style protocol becomes a ProducerV2. Models are
        already ProducerV2 (via ``ModelAdapter``) and register directly.
        """
        reg = ProducerRegistry()
        for name, factory in self._drivers:
            drv = factory(ctx)
            reg.register(DataDriverAdapter(driver=drv))
            _log.info("[catalog] driver registered: %s -> %s",
                      name, list(getattr(drv, "produces", [])))
        for name, factory in self._models:
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
    )


def default_catalog() -> CascadeCatalog:
    """The built-in cascade.

    Data drivers + WRF-SFIRE as **model 1**. Future models append here::

        cat.add_model("smoke",  build_smoke_dispersion)   # model 2
        cat.add_model("flood",  build_flood)              # model 3

    A model that consumes WRF-SFIRE output declares e.g.
    ``DataNeed("arrival_s")`` in its ``data_adapter``; the engine then
    runs WRF-SFIRE before it, automatically.
    """
    cat = CascadeCatalog()
    cat.add_driver("thermal",   build_thermal_driver)
    cat.add_driver("landfire",  build_landfire_driver)
    cat.add_driver("dem",       build_dem_driver)
    cat.add_driver("era5_wind", build_era5_wind_driver)
    cat.add_model("wrf_sfire",  build_wrf_sfire)        # <-- model 1
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
