"""Consequence models: hazard footprint -> damage -> economic loss.

Two reduced-order producers closing the asteroid cascade:

  * blast_damage : blast_overpressure_pa -> building_damage_frac via a
    logistic vulnerability curve (HAZUS-flavoured: ~50% of buildings
    severely damaged near 35 kPa peak overpressure).
  * econ_loss    : building_damage_frac x asset_value_usd (+ an
    indirect-loss multiplier, HAZUS-style) -> economic_loss_usd, and
    population_density -> population_exposure inside the damage
    footprint.

Both are pure cube-in/cube-out functions wrapped by
ModelFunctionAdapter in models/catalog.py — no external binaries.
Coefficients are deliberately simple and named; replace with fitted
fragility/IO tables when validation data lands.
"""
from __future__ import annotations

import numpy as np

# Vulnerability curve: damage = 1 / (1 + (P_HALF / p)^BETA)
P_HALF_PA = 35_000.0     # overpressure at 50% structural damage
BETA = 2.2               # curve steepness

INDIRECT_MULTIPLIER = 1.45   # direct -> total loss (business interruption
                             # etc.; HAZUS-style flat factor)
EXPOSURE_DAMAGE_MIN = 0.1    # cells above this damage count as exposed


def run_blast_damage(cube, request) -> list[str]:
    p = cube.read_static("blast_overpressure_pa")
    p = np.maximum(np.asarray(p, dtype="float64"), 1.0)
    damage = (1.0 / (1.0 + (P_HALF_PA / p) ** BETA)).astype("float32")

    cube.write_static("building_damage_frac", damage,
                      source="blast_damage:logistic",
                      native_res_m=float(cube.grid.pixel_m), units="0..1",
                      producer="blast_damage",
                      description=f"Logistic blast vulnerability "
                                  f"(p50={P_HALF_PA/1000:.0f} kPa, "
                                  f"beta={BETA})")
    return ["building_damage_frac"]


def run_econ_loss(cube, request) -> list[str]:
    damage = np.asarray(cube.read_static("building_damage_frac"),
                        dtype="float64")
    assets = np.asarray(cube.read_static("asset_value_usd"),
                        dtype="float64")
    density = np.asarray(cube.read_static("population_density"),
                         dtype="float64")

    loss = (damage * assets * INDIRECT_MULTIPLIER).astype("float32")
    cube.write_static("economic_loss_usd", loss,
                      source="econ_loss:hazus_style",
                      native_res_m=float(cube.grid.pixel_m),
                      units="USD", producer="econ_loss",
                      description=f"Per-cell loss = damage x assets x "
                                  f"{INDIRECT_MULTIPLIER} (indirect "
                                  f"multiplier)")

    cell_km2 = (cube.grid.pixel_m / 1000.0) ** 2
    exposed = np.where(damage >= EXPOSURE_DAMAGE_MIN,
                       density * cell_km2, 0.0).astype("float32")
    cube.write_static("population_exposure", exposed,
                      source="econ_loss:hazus_style",
                      native_res_m=float(cube.grid.pixel_m),
                      units="people", producer="econ_loss",
                      description=f"People in cells with damage >= "
                                  f"{EXPOSURE_DAMAGE_MIN}")
    return ["economic_loss_usd", "population_exposure"]
