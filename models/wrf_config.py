"""Simple, declarative WRF-SFIRE scenario configuration + namelist generator.

The goal: configure a (possibly nested, possibly chem-enabled) WRF-SFIRE
run from a handful of human-friendly knobs — domain extent in km,
duration in days, resolutions per nest — and emit correct
``namelist.input`` and ``namelist.wps`` files, including the per-domain
arrays that nesting requires.

Nesting is **explicit**: you list the resolutions (or full
:class:`DomainSpec`s) you want. The builder does not guess nests from
the impact footprint — it only computes the WRF index arrays
(``e_we``/``parent_grid_ratio``/``i_parent_start`` …) from what you
declare, which is the fiddly, error-prone part.

Everything here is pure Python (stdlib only) so it is unit-testable
without numpy / the cube / a compiled WRF.

Example
-------
    sc = WRFScenario.from_simple(
        center_lon=-96.81, center_lat=32.78,
        extent_km=1000, resolutions_m=[9000, 3000, 1000],
        nest_fraction=0.5, fire_mesh_ratio=10,
        start=datetime(2019, 9, 4, 12), duration_days=30,
        smoke=True)
    NamelistBuilder(sc).write_input("namelist.input")
    WPSNamelistBuilder(sc).write("namelist.wps")
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional


# ====================================================================
# Domain + scenario dataclasses
# ====================================================================
@dataclass
class DomainSpec:
    """One WRF domain (grid). Indices are 1-based, WRF convention.

    ``e_we`` / ``e_sn`` are the *staggered* west-east / south-north
    dimensions (number of cells + 1), matching WRF's namelist.
    """
    dx_m: float
    e_we: int
    e_sn: int
    parent_id: int = 1
    parent_grid_ratio: int = 1
    i_parent_start: int = 1
    j_parent_start: int = 1
    # Fire mesh refinement on THIS domain (0 = no fire mesh). SFIRE
    # normally runs only on the finest nest.
    sr: int = 0

    @property
    def nx(self) -> int:
        return self.e_we - 1

    @property
    def ny(self) -> int:
        return self.e_sn - 1


@dataclass
class WRFScenario:
    """All knobs for a WRF-SFIRE run, nesting + chem included."""

    # --- geography ---
    center_lon: float
    center_lat: float
    start: datetime
    domains: list[DomainSpec]

    extent_km: float = 1000.0
    map_proj: str = "lambert"

    # --- vertical ---
    e_vert: int = 45
    p_top_pa: float = 5000.0
    num_metgrid_levels: int = 33
    num_metgrid_soil_levels: int = 4

    # --- time ---
    duration_days: float = 30.0
    history_interval_min: int = 60
    interval_seconds: int = 21600          # boundary update cadence
    restart_interval_min: int = 1440       # daily checkpoints
    time_step_s: Optional[int] = None      # auto from coarsest dx if None
    frames_per_outfile: int = 1

    # --- fire ---
    ifire: int = 1                         # 1 = full SFIRE physics
    fire_mesh_ratio: int = 10
    fire_atm_feedback: float = 1.0
    fire_tign_in_time: float = 1.0
    fire_fuel_read: int = -1               # read NFUEL_CAT from wrfinput

    # --- chem / smoke ---
    smoke: bool = False
    chem_opt: int = 0                      # set when smoke is on (see below)
    emiss_opt: int = 0
    tracer_opt: int = 0

    # --- physics suite ---
    physics_suite: str = "CONUS"
    num_land_cat: int = 21

    def __post_init__(self) -> None:
        if not self.domains:
            raise ValueError("WRFScenario needs at least one domain")
        # Smoke defaults: a lightweight fire-emission tracer set. These
        # values require a WRF-Chem-enabled binary (bootstrap with
        # enable_chem=True); on a non-chem build WRF ignores &chem.
        if self.smoke and self.chem_opt == 0:
            self.chem_opt = 17        # GOCART simple aerosol w/ fire emis
            self.emiss_opt = 5        # biomass-burning emissions
            self.tracer_opt = 0

    # ------------------------------------------------------------------
    @property
    def max_dom(self) -> int:
        return len(self.domains)

    @property
    def fire_domain_index(self) -> int:
        """1-based index of the domain carrying the fire mesh (finest
        nest with sr > 0, else the last domain)."""
        for i, d in enumerate(self.domains, start=1):
            if d.sr > 0:
                fire_i = i
        try:
            return fire_i
        except NameError:
            return self.max_dom

    @property
    def end(self) -> datetime:
        return self.start + timedelta(days=self.duration_days)

    def coarse_time_step(self) -> int:
        """CFL-stable integer time step for the COARSEST domain.

        Rule of thumb ~6 s per km of dx. Child domains derive their step
        from ``parent_time_step_ratio`` automatically inside WRF.
        """
        if self.time_step_s is not None:
            return int(self.time_step_s)
        dx_km = self.domains[0].dx_m / 1000.0
        return max(1, int(round(6.0 * dx_km)))

    # ------------------------------------------------------------------
    @classmethod
    def from_simple(cls,
                    *,
                    center_lon: float,
                    center_lat: float,
                    start: datetime,
                    extent_km: float = 1000.0,
                    resolutions_m: Optional[list[float]] = None,
                    nest_fraction: float = 0.5,
                    fire_mesh_ratio: int = 10,
                    duration_days: float = 30.0,
                    smoke: bool = False,
                    **kwargs) -> "WRFScenario":
        """Build a centered nest stack from a list of resolutions.

        This is still *explicit* configuration — you choose the
        resolutions and the nest size fraction. The builder only solves
        the WRF index arithmetic (ratios, parent-start offsets, grid
        sizes divisible by the ratio).

        resolutions_m : coarsest → finest, e.g. [9000, 3000, 1000].
                         Consecutive ratios must be integers.
        nest_fraction : each child covers this fraction of its parent's
                         span (centered).
        fire_mesh_ratio : SFIRE refinement on the innermost domain.
        """
        res = resolutions_m or [3000.0]
        domains = _build_centered_nests(
            extent_km=extent_km, resolutions_m=res,
            nest_fraction=nest_fraction)
        # Fire mesh only on the innermost domain.
        domains[-1].sr = fire_mesh_ratio
        return cls(
            center_lon=center_lon, center_lat=center_lat, start=start,
            domains=domains, extent_km=extent_km,
            fire_mesh_ratio=fire_mesh_ratio,
            duration_days=duration_days, smoke=smoke, **kwargs)


# ====================================================================
# Nest geometry solver
# ====================================================================
def _build_centered_nests(*, extent_km: float,
                          resolutions_m: list[float],
                          nest_fraction: float) -> list[DomainSpec]:
    """Solve WRF index arrays for a stack of centered nests.

    Returns one DomainSpec per resolution, coarsest first.
    """
    if not resolutions_m:
        raise ValueError("need at least one resolution")

    # --- d01 from extent + coarsest dx ---
    dx0 = float(resolutions_m[0])
    n0 = int(round(extent_km * 1000.0 / dx0))
    n0 = max(20, n0)                     # sane floor
    domains = [DomainSpec(dx_m=dx0, e_we=n0 + 1, e_sn=n0 + 1,
                          parent_id=1, parent_grid_ratio=1,
                          i_parent_start=1, j_parent_start=1)]

    # --- nests ---
    for level in range(1, len(resolutions_m)):
        dx_child = float(resolutions_m[level])
        dx_parent = float(resolutions_m[level - 1])
        ratio = int(round(dx_parent / dx_child))
        if ratio < 2 or abs(ratio * dx_child - dx_parent) > 1e-6:
            raise ValueError(
                f"resolution {dx_parent}->{dx_child} is not an integer "
                f"refinement (got ratio {dx_parent/dx_child:.3f}); WRF "
                "needs integer parent_grid_ratio")
        parent = domains[-1]
        # child covers `nest_fraction` of the parent's cells, centered.
        parent_cells = max(4, int(round(nest_fraction * parent.nx)))
        # e_we_child - 1 must be divisible by ratio.
        e_we_child = parent_cells * ratio + 1
        e_sn_child = parent_cells * ratio + 1
        i_start = max(1, (parent.nx - parent_cells) // 2 + 1)
        j_start = max(1, (parent.ny - parent_cells) // 2 + 1)
        domains.append(DomainSpec(
            dx_m=dx_child, e_we=e_we_child, e_sn=e_sn_child,
            parent_id=level, parent_grid_ratio=ratio,
            i_parent_start=i_start, j_parent_start=j_start))
    return domains


# ====================================================================
# namelist.input builder (multi-domain, optional chem)
# ====================================================================
class NamelistBuilder:
    """Emit a complete ``namelist.input`` from a :class:`WRFScenario`."""

    def __init__(self, scenario: WRFScenario):
        self.s = scenario

    @property
    def scenario(self) -> WRFScenario:
        """Alias so consumers (e.g. the WRF-SFIRE adapter) can read/patch
        the underlying scenario without knowing the private field name."""
        return self.s

    # ---- helpers ----
    @staticmethod
    def _row(values) -> str:
        return ", ".join(str(v) for v in values) + ","

    def _per_dom(self, value) -> str:
        return self._row([value] * self.s.max_dom)

    # ---- sections ----
    def time_control(self) -> str:
        s = self.s
        total_s = int(s.duration_days * 86400)
        run_days = total_s // 86400
        run_hours = (total_s % 86400) // 3600
        return f"""\
 &time_control
 run_days                = {run_days},
 run_hours               = {run_hours},
 run_minutes             = 0,
 run_seconds             = 0,
 start_year              = {self._per_dom(f"{s.start.year:04d}")}
 start_month             = {self._per_dom(f"{s.start.month:02d}")}
 start_day               = {self._per_dom(f"{s.start.day:02d}")}
 start_hour              = {self._per_dom(f"{s.start.hour:02d}")}
 start_minute            = {self._per_dom("00")}
 start_second            = {self._per_dom("00")}
 end_year                = {self._per_dom(f"{s.end.year:04d}")}
 end_month               = {self._per_dom(f"{s.end.month:02d}")}
 end_day                 = {self._per_dom(f"{s.end.day:02d}")}
 end_hour                = {self._per_dom(f"{s.end.hour:02d}")}
 end_minute              = {self._per_dom("00")}
 end_second              = {self._per_dom("00")}
 interval_seconds        = {s.interval_seconds},
 input_from_file         = {self._per_dom(".true.")}
 history_interval        = {self._per_dom(s.history_interval_min)}
 frames_per_outfile      = {self._per_dom(s.frames_per_outfile)}
 restart                 = .false.,
 restart_interval        = {s.restart_interval_min},
 io_form_history         = 2,
 io_form_restart         = 2,
 io_form_input           = 2,
 io_form_boundary        = 2,
 /
"""

    def domains(self) -> str:
        s = self.s
        d = s.domains
        return f"""\
 &domains
 time_step               = {s.coarse_time_step()},
 time_step_fract_num     = 0,
 time_step_fract_den     = 1,
 max_dom                 = {s.max_dom},
 e_we                    = {self._row(x.e_we for x in d)}
 e_sn                    = {self._row(x.e_sn for x in d)}
 e_vert                  = {self._per_dom(s.e_vert)}
 p_top_requested         = {s.p_top_pa:.0f},
 num_metgrid_levels      = {s.num_metgrid_levels},
 num_metgrid_soil_levels = {s.num_metgrid_soil_levels},
 dx                      = {self._row(f"{x.dx_m:.0f}" for x in d)}
 dy                      = {self._row(f"{x.dx_m:.0f}" for x in d)}
 grid_id                 = {self._row(range(1, s.max_dom + 1))}
 parent_id               = {self._row(x.parent_id for x in d)}
 i_parent_start          = {self._row(x.i_parent_start for x in d)}
 j_parent_start          = {self._row(x.j_parent_start for x in d)}
 parent_grid_ratio       = {self._row(x.parent_grid_ratio for x in d)}
 parent_time_step_ratio  = {self._row(x.parent_grid_ratio for x in d)}
 feedback                = 1,
 smooth_option           = 0,
 sr_x                    = {self._row(x.sr for x in d)}
 sr_y                    = {self._row(x.sr for x in d)}
 sfcp_to_sfcp            = .true.,
 /
"""

    def physics(self) -> str:
        s = self.s
        return f"""\
 &physics
 physics_suite           = '{s.physics_suite}',
 mp_physics              = {self._per_dom(-1)}
 cu_physics              = {self._per_dom(-1)}
 ra_lw_physics           = {self._per_dom(-1)}
 ra_sw_physics           = {self._per_dom(-1)}
 bl_pbl_physics          = {self._per_dom(-1)}
 sf_sfclay_physics       = {self._per_dom(-1)}
 sf_surface_physics      = {self._per_dom(-1)}
 radt                    = {self._per_dom(15)}
 bldt                    = {self._per_dom(0)}
 cudt                    = {self._per_dom(0)}
 icloud                  = 1,
 num_land_cat            = {s.num_land_cat},
 sf_urban_physics        = {self._per_dom(0)}
 fractional_seaice       = 1,
 /
"""

    def chem(self) -> str:
        """&chem section — only emitted when smoke is enabled.

        Requires a WRF-Chem-enabled binary (bootstrap enable_chem=True).
        On a non-chem build WRF simply ignores this section.
        """
        s = self.s
        if not s.smoke:
            return ""
        return f"""\
 &chem
 kemit                   = 1,
 chem_opt                = {self._per_dom(s.chem_opt)}
 bioemdt                 = {self._per_dom(0)}
 photdt                  = {self._per_dom(0)}
 chemdt                  = {self._per_dom(0)}
 emiss_opt               = {self._per_dom(s.emiss_opt)}
 emiss_opt_vol           = {self._per_dom(0)}
 chem_in_opt             = {self._per_dom(0)}
 phot_opt                = {self._per_dom(0)}
 gas_drydep_opt          = {self._per_dom(0)}
 aer_drydep_opt          = {self._per_dom(1)}
 biomass_burn_opt        = {self._per_dom(1)}
 plumerisefire_frq       = {self._per_dom(30)}
 tracer_opt              = {self._per_dom(s.tracer_opt)}
 have_bcs_chem           = {self._per_dom(".false.")}
 /
"""

    def dynamics(self) -> str:
        s = self.s
        return f"""\
 &dynamics
 hybrid_opt              = 2,
 w_damping               = 1,
 diff_opt                = {self._per_dom(2)}
 km_opt                  = {self._per_dom(4)}
 diff_6th_opt            = {self._per_dom(0)}
 diff_6th_factor         = {self._per_dom(0.12)}
 base_temp               = 290.,
 damp_opt                = 3,
 zdamp                   = {self._per_dom(5000.)}
 dampcoef                = {self._per_dom(0.2)}
 khdif                   = {self._per_dom(0)}
 kvdif                   = {self._per_dom(0)}
 non_hydrostatic         = {self._per_dom(".true.")}
 moist_adv_opt           = {self._per_dom(1)}
 scalar_adv_opt          = {self._per_dom(1)}
 gwd_opt                 = {self._per_dom(1)}
 /
"""

    def bdy_control(self) -> str:
        return f"""\
 &bdy_control
 spec_bdy_width          = 5,
 specified               = .true.,
 nested                  = {self._row(['.false.'] + ['.true.'] * (self.s.max_dom - 1))}
 /
"""

    def fire(self) -> str:
        s = self.s
        # ifire is per-domain: only the fire domain gets ifire; others 0.
        ifire_row = [0] * s.max_dom
        ifire_row[s.fire_domain_index - 1] = s.ifire
        return f"""\
 &fire
 ifire                   = {self._row(ifire_row)}
 fire_fuel_read          = {self._per_dom(s.fire_fuel_read)}
 fire_num_ignitions      = {self._per_dom(0)}
 fire_tign_in_time       = {s.fire_tign_in_time:.3f},
 fire_print_msg          = 0,
 fire_print_file         = 0,
 fmoist_run              = .true.,
 fmoist_interp           = .true.,
 fmoist_only             = .false.,
 fmoist_freq             = 0,
 fmoist_dt               = 600.,
 fire_fmc_read           = 0,
 fire_boundary_guard     = -1,
 fire_fuel_left_method   = 1,
 fire_fuel_left_irl      = 2,
 fire_fuel_left_jrl      = 2,
 fire_atm_feedback       = {s.fire_atm_feedback},
 fire_grows_only         = 1,
 fire_viscosity          = 0.4,
 fire_upwinding          = 3,
 fire_lfn_ext_up         = 1.0,
 fire_test_steps         = 0,
 fire_topo_from_atm      = 1,
 /
"""

    def tail(self) -> str:
        return """\
 &fdda
 /

 &grib2
 /

 &namelist_quilt
 nio_tasks_per_group     = 0,
 nio_groups              = 1,
 /
"""

    # ---- assemble ----
    def render(self) -> str:
        parts = [
            self.time_control(),
            self.domains(),
            self.physics(),
            self.chem(),            # empty unless smoke
            "\n &fdda\n /\n",
            self.dynamics(),
            self.bdy_control(),
            self.fire(),
            "\n &grib2\n /\n",
            "\n &namelist_quilt\n nio_tasks_per_group = 0,\n"
            " nio_groups = 1,\n /\n",
        ]
        return "\n".join(p for p in parts if p.strip())

    def write_input(self, path: str | Path) -> Path:
        p = Path(path)
        p.write_text(self.render())
        return p


# ====================================================================
# namelist.wps builder (multi-domain)
# ====================================================================
class WPSNamelistBuilder:
    """Emit ``namelist.wps`` from a :class:`WRFScenario`."""

    def __init__(self, scenario: WRFScenario, geog_data_path: str = ""):
        self.s = scenario
        self.geog = geog_data_path

    def render(self) -> str:
        s = self.s
        d = s.domains
        fmt = "%Y-%m-%d_%H:%M:%S"
        row = lambda vals: ", ".join(str(v) for v in vals) + ","
        return f"""\
 &share
 wrf_core = 'ARW',
 max_dom = {s.max_dom},
 start_date = {row([f"'{s.start.strftime(fmt)}'"] * s.max_dom)}
 end_date   = {row([f"'{s.end.strftime(fmt)}'"] * s.max_dom)}
 interval_seconds = {s.interval_seconds},
 io_form_geogrid = 2,
 /

 &geogrid
 parent_id         = {row(x.parent_id for x in d)}
 parent_grid_ratio = {row(x.parent_grid_ratio for x in d)}
 i_parent_start    = {row(x.i_parent_start for x in d)}
 j_parent_start    = {row(x.j_parent_start for x in d)}
 e_we              = {row(x.e_we for x in d)}
 e_sn              = {row(x.e_sn for x in d)}
 geog_data_res     = {row(["'default'"] * s.max_dom)}
 dx = {d[0].dx_m:.0f},
 dy = {d[0].dx_m:.0f},
 map_proj = '{s.map_proj}',
 ref_lat   = {s.center_lat:.6f},
 ref_lon   = {s.center_lon:.6f},
 truelat1  = {s.center_lat:.6f},
 truelat2  = {s.center_lat:.6f},
 stand_lon = {s.center_lon:.6f},
 geog_data_path = '{self.geog}',
 /

 &ungrib
 out_format = 'WPS',
 prefix = 'PRES',
 /

 &metgrid
 fg_name = 'PRES','SFC',
 io_form_metgrid = 2,
 /
"""

    def write(self, path: str | Path) -> Path:
        p = Path(path)
        p.write_text(self.render())
        return p


# ====================================================================
# YAML scenario loader
# ====================================================================
@dataclass
class LoadedScenario:
    """Everything a run needs, parsed from configs/wildfire_scenario.yaml.

    Bundles the WRF namelist scenario with the cube/AOI and execution
    settings so the runner can wire the catalog without re-reading YAML.
    """
    wrf: WRFScenario
    kml: Path
    city: str
    band: str
    pulse_seconds: float
    center_lon: float
    center_lat: float
    radius_m: float                # cube AOI radius (= extent_km/2)
    pixel_m: float                 # cube analysis-grid resolution
    targets: list[str]
    hpc_profile: Optional[str]
    np: int
    backend: str
    use_real: bool
    build_met_em: bool
    submit_slurm: bool
    enable_chem: bool

    @property
    def sim_seconds(self) -> int:
        return int(self.wrf.duration_days * 86400)


def _parse_dt(v: Any) -> datetime:
    if isinstance(v, datetime):
        return v
    return datetime.fromisoformat(str(v))


def load_scenario_yaml(path: str | Path) -> LoadedScenario:
    """Parse a wildfire scenario YAML into a :class:`LoadedScenario`.

    Only ``scenario.kml`` is strictly required; every other field falls
    back to a sensible default mirroring configs/wildfire_scenario.yaml.
    """
    import yaml  # pyyaml is in requirements.txt

    with open(path) as fh:
        doc = yaml.safe_load(fh) or {}

    sc = doc.get("scenario", {})
    dom = doc.get("domain", {})
    tm = doc.get("time", {})
    fire = doc.get("fire", {})
    chem = doc.get("chem", {})
    exe = doc.get("execution", {})
    out = doc.get("outputs", {})

    center = sc.get("center", [-96.80889, 32.77998])
    extent_km = float(dom.get("extent_km", 1000.0))
    smoke = bool(chem.get("smoke", False))

    wrf = WRFScenario.from_simple(
        center_lon=float(center[0]),
        center_lat=float(center[1]),
        start=_parse_dt(tm.get("start", "2019-09-04T12:00")),
        extent_km=extent_km,
        resolutions_m=[float(r) for r in dom.get(
            "resolutions_m", [9000, 3000, 1000])],
        nest_fraction=float(dom.get("nest_fraction", 0.5)),
        fire_mesh_ratio=int(dom.get("fire_mesh_ratio", 10)),
        duration_days=float(tm.get("duration_days", 30.0)),
        smoke=smoke,
        map_proj=str(dom.get("map_proj", "lambert")),
        e_vert=int(dom.get("e_vert", 45)),
        p_top_pa=float(dom.get("p_top_pa", 5000)),
        num_metgrid_levels=int(dom.get("num_metgrid_levels", 33)),
        num_metgrid_soil_levels=int(dom.get("num_metgrid_soil_levels", 4)),
        history_interval_min=int(tm.get("history_interval_min", 60)),
        interval_seconds=int(tm.get("interval_seconds", 21600)),
        restart_interval_min=int(tm.get("restart_interval_min", 1440)),
        time_step_s=tm.get("time_step_s"),
        ifire=int(fire.get("ifire", 1)),
        fire_atm_feedback=float(fire.get("fire_atm_feedback", 1.0)),
        fire_tign_in_time=float(fire.get("fire_tign_in_time", 1.0)),
        chem_opt=int(chem.get("chem_opt", 0)),
    )

    hpc = exe.get("hpc_profile", "auto")
    return LoadedScenario(
        wrf=wrf,
        kml=Path(sc.get("kml", "Dallas.kml")),
        city=str(sc.get("city", "Dallas TX USA")),
        band=str(sc.get("band", "Mean")),
        pulse_seconds=float(sc.get("pulse_seconds", 10.0)),
        center_lon=float(center[0]),
        center_lat=float(center[1]),
        radius_m=extent_km * 1000.0 / 2.0,
        pixel_m=float(out.get("pixel_m", 900.0)),
        targets=list(out.get("targets",
                             ["arrival_s", "fire_area"])),
        hpc_profile=None if hpc in ("auto", None) else str(hpc),
        np=int(exe.get("np", 0)),
        backend=str(exe.get("backend", "serial")),
        use_real=bool(exe.get("use_real", False)),
        build_met_em=bool(exe.get("build_met_em", False)),
        submit_slurm=bool(exe.get("submit_slurm", False)),
        enable_chem=smoke,
    )
