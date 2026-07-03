"""Impact scaling laws (Collins-style) — cascade model 0.

Cheap, no-network scaling relations from the Earth Impact Effects
Program family (Collins, Melosh & Marcus 2005): given the event energy,
produce the blast-overpressure footprint on the cube grid. This is the
`scaling-law` fidelity tier — a fast first answer that the planner can
later refine with a full-physics producer bound to the same variables.

The event is taken to be at the grid centre (run_cascade builds the
grid around the event location), so distance is pure pixel geometry —
no CRS math.
"""
from __future__ import annotations

import numpy as np

MT_TO_J = 4.184e15
KT_TO_J = 4.184e12

# 1 kt surface-burst reference overpressure curve (Collins et al. 2005):
# p(r1) = PX * (RX / (4 r1)) * (1 + 3 (RX / r1)^1.3), r1 in m kt^-1/3.
_PX = 75_000.0     # Pa
_RX = 290.0        # m


def overpressure_pa(r_m: np.ndarray, energy_mt: float) -> np.ndarray:
    """Peak blast overpressure at ground range r for a given yield."""
    e_kt = max(energy_mt, 1e-6) * 1000.0
    r1 = np.maximum(r_m, 1.0) / e_kt ** (1.0 / 3.0)
    return _PX * (_RX / (4.0 * r1)) * (1.0 + 3.0 * (_RX / r1) ** 1.3)


def _radii_m(grid) -> np.ndarray:
    H, W = grid.shape
    ii, jj = np.indices((H, W), dtype="float64")
    r = np.hypot(ii - (H - 1) / 2.0, jj - (W - 1) / 2.0) * grid.pixel_m
    return np.maximum(r, grid.pixel_m / 2.0)   # avoid the r=0 singularity


def run_impact_scaling(cube, request, *, energy_mt: float) -> list[str]:
    """Producer body: write impact_energy_j + blast_overpressure_pa."""
    grid = cube.grid
    r = _radii_m(grid)

    energy = np.full(grid.shape, energy_mt * MT_TO_J, dtype="float32")
    cube.write_static("impact_energy_j", energy,
                      source=f"collins_scaling:E={energy_mt}Mt",
                      native_res_m=float(grid.pixel_m), units="J",
                      producer="impact_scaling",
                      description="Impact energy (uniform)")

    p = overpressure_pa(r, energy_mt).astype("float32")
    cube.write_static("blast_overpressure_pa", p,
                      source=f"collins_scaling:E={energy_mt}Mt",
                      native_res_m=float(grid.pixel_m), units="Pa",
                      producer="impact_scaling",
                      description="Peak blast overpressure "
                                  "(Collins et al. 2005 surface-burst fit)")
    return ["impact_energy_j", "blast_overpressure_pa"]
