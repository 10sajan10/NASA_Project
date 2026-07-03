"""Controlled variable vocabulary (CF-style standard names).

Why this exists: with 100 drivers and 100 models, the planner can only
chain producer A's output into producer B's input if both mean the same
thing by a variable name. This module is that agreement. Every cube
variable that participates in cross-producer wiring should appear here;
`validate_name` is the gate at card-registration time.

This is intentionally a plain dict, not a database — the vocabulary is
code-reviewed, versioned with the repo, and small enough to read. The
metacatalog references these names; it never invents new ones.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class VarDef:
    name: str
    units: str
    domain: str            # impact-physics | atmosphere | fire | terrain |
                           # fuel | exposure | economy | air-quality
    kind: str = "static"   # static | time
    description: str = ""
    aliases: tuple[str, ...] = ()


_DEFS = [
    # --- terrain / fuel (existing drivers) ---
    VarDef("dem", "m", "terrain", "static", "Digital elevation model"),
    VarDef("slope_deg", "deg", "terrain", "static", "Terrain slope"),
    VarDef("aspect_deg", "deg", "terrain", "static", "Terrain aspect"),
    VarDef("nfuel_cat", "category", "fuel", "static",
           "Anderson 13 fuel category (WRF-SFIRE NFUEL_CAT)"),
    VarDef("fbfm13", "category", "fuel", "static",
           "LANDFIRE FBFM13 fuel model"),
    VarDef("burnable", "0|1", "fuel", "static",
           "Burnable-cell mask derived from the thermal footprint"),

    # --- atmosphere (existing drivers) ---
    VarDef("wind_speed_ms", "m/s", "atmosphere", "time", "10 m wind speed"),
    VarDef("wind_dir_deg", "deg", "atmosphere", "time",
           "10 m wind direction (meteorological)"),
    VarDef("era5_pressure_grib", "file", "atmosphere", "time",
           "ERA5 pressure-level GRIB for WPS"),
    VarDef("era5_single_grib", "file", "atmosphere", "time",
           "ERA5 single-level GRIB for WPS"),

    # --- impact physics (asteroid cascade, model 0) ---
    VarDef("impact_energy_j", "J", "impact-physics", "static",
           "Impact/airburst energy from impactor mass & velocity"),
    VarDef("thermal_fluence", "J/m^2", "impact-physics", "static",
           "Thermal fluence footprint of the airburst"),
    VarDef("thermal_power", "W/m^2", "impact-physics", "static",
           "Peak thermal power footprint"),
    VarDef("ignition_t0", "s", "impact-physics", "static",
           "Ignition time map derived from thermal footprint"),
    VarDef("blast_overpressure_pa", "Pa", "impact-physics", "static",
           "Peak blast overpressure footprint"),

    # --- fire (WRF-SFIRE outputs) ---
    VarDef("arrival_s", "s", "fire", "static",
           "Fire arrival time (TIGN_G)"),
    VarDef("fire_area", "0..1", "fire", "static",
           "Cell-wise burned fraction"),
    VarDef("ros_max", "m/s", "fire", "static", "Peak rate of spread"),
    VarDef("fire_intensity", "W/m", "fire", "static",
           "Peak fireline intensity"),
    VarDef("fuel_consumed", "kg/m^2", "fire", "static", "Fuel consumed"),

    # --- air quality (WRF-Chem outputs) ---
    VarDef("pm25_surface", "ug/m^3", "air-quality", "time",
           "Surface PM2.5 concentration"),
    VarDef("smoke_tracer", "ug/m^3", "air-quality", "time",
           "Smoke tracer concentration"),

    # --- exposure (drivers to be added) ---
    VarDef("population_density", "people/km^2", "exposure", "static",
           "Gridded population (e.g. WorldPop, GPW)"),
    VarDef("asset_value_usd", "USD/cell", "exposure", "static",
           "Built-asset replacement value"),
    VarDef("building_damage_frac", "0..1", "exposure", "static",
           "Building damage fraction from hazard footprints"),
    VarDef("population_exposure", "people", "exposure", "static",
           "People inside the hazard footprint"),

    # --- economy (models to be added) ---
    VarDef("economic_loss_usd", "USD", "economy", "static",
           "Direct + indirect economic loss estimate"),
]

VOCABULARY: dict[str, VarDef] = {d.name: d for d in _DEFS}
_ALIASES: dict[str, str] = {a: d.name for d in _DEFS for a in d.aliases}


def lookup(name: str) -> VarDef | None:
    """Resolve a variable (or alias) to its definition, or None."""
    if name in VOCABULARY:
        return VOCABULARY[name]
    canon = _ALIASES.get(name)
    return VOCABULARY.get(canon) if canon else None


def validate_name(name: str, *, strict: bool = False) -> str:
    """Return the canonical name; unknown names pass through unless strict.

    Non-strict lets scenario-local scratch variables exist without
    polluting the vocabulary; strict is for card registration, where an
    unknown name usually means a typo that would silently break chaining.
    """
    d = lookup(name)
    if d is not None:
        return d.name
    if strict:
        raise KeyError(
            f"variable {name!r} is not in the ontology; add a VarDef to "
            f"agentic/ontology.py (or fix the spelling) so producers can "
            f"be chained on it")
    return name


def domains() -> dict[str, list[str]]:
    """Vocabulary grouped by domain — readable summary for agents/humans."""
    out: dict[str, list[str]] = {}
    for d in _DEFS:
        out.setdefault(d.domain, []).append(d.name)
    return out
