"""Scott & Burgan (2005) FBFM40 surface fuel models.

Loads are in kg/m^2 (oven-dry), SAV in m^-1, fuel-bed depth in m, extinction
moisture as a fraction (0..1), heat content in kJ/kg. Converted from the
original US-customary table:
    1 t/acre = 0.2241718 kg/m^2,   1 ft = 0.3048 m,   1 ft^-1 = 3.281 m^-1
    1 BTU/lb = 2.326 kJ/kg

`is_dynamic` controls whether live-herbaceous load shifts into dead-1h as
LFMC drops below 30 %.
"""
from __future__ import annotations
from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class Fuel:
    code: int
    name: str
    load_1h: float
    load_10h: float
    load_100h: float
    load_lh: float          # live herbaceous
    load_lw: float          # live woody
    sav_1h: float
    sav_lh: float
    sav_lw: float
    depth_m: float
    mx_dead: float          # dead extinction moisture (fraction)
    heat_kJkg: float
    is_dynamic: bool
    burnable: bool = True


# ---- core table for all 40 SB codes (cleaned, validated against Anderson + S&B) ----
_SB_RAW: list[Fuel] = [
    # NB - non-burnable
    Fuel(91, "NB1 Urban",          0,0,0,0,0, 0,0,0, 0,0,0, False, burnable=False),
    Fuel(92, "NB2 Snow/Ice",       0,0,0,0,0, 0,0,0, 0,0,0, False, burnable=False),
    Fuel(93, "NB3 Agricultural",   0,0,0,0,0, 0,0,0, 0,0,0, False, burnable=False),
    Fuel(98, "NB8 Open Water",     0,0,0,0,0, 0,0,0, 0,0,0, False, burnable=False),
    Fuel(99, "NB9 Bare Ground",    0,0,0,0,0, 0,0,0, 0,0,0, False, burnable=False),
    # GR - grass (dynamic)
    Fuel(101, "GR1 Short sparse dry-climate grass",
         0.0224, 0,0, 0.0673, 0,
         7218, 5577, 0, 0.122, 0.15, 18608, True),
    Fuel(102, "GR2 Low load dry-climate grass",
         0.0224, 0,0, 0.224, 0,
         6562, 5577, 0, 0.305, 0.15, 18608, True),
    Fuel(103, "GR3 Low load very coarse humid-climate grass",
         0.0224, 0.0448, 0, 0.673, 0,
         4921, 4921, 0, 0.610, 0.30, 18608, True),
    Fuel(104, "GR4 Moderate load dry-climate grass",
         0.0560, 0,0, 0.420, 0,
         6562, 5577, 0, 0.610, 0.15, 18608, True),
    Fuel(105, "GR5 Low load humid-climate grass",
         0.0897, 0.0224, 0, 0.560, 0,
         5905, 5577, 0, 0.457, 0.40, 18608, True),
    Fuel(106, "GR6 Moderate load humid-climate grass",
         0.0448, 0,0, 0.605, 0,
         5905, 6562, 0, 0.457, 0.40, 18608, True),
    Fuel(107, "GR7 High load dry-climate grass",
         0.224, 0,0, 1.121, 0,
         5577, 5577, 0, 0.914, 0.15, 18608, True),
    Fuel(108, "GR8 High load very coarse humid-climate grass",
         0.112, 0.224, 0, 1.345, 0,
         4921, 4921, 0, 1.219, 0.30, 18608, True),
    Fuel(109, "GR9 Very high load humid-climate grass",
         0.224, 0.448, 0, 1.569, 0,
         5577, 5577, 0, 1.524, 0.40, 18608, True),
    # GS - grass-shrub (dynamic)
    Fuel(121, "GS1 Low load dry-climate grass-shrub",
         0.0448, 0.0224, 0, 0.112, 0.112,
         6562, 5577, 5249, 0.274, 0.15, 18608, True),
    Fuel(122, "GS2 Moderate load dry-climate grass-shrub",
         0.112, 0.0224, 0, 0.135, 0.135,
         6562, 5577, 5249, 0.457, 0.15, 18608, True),
    Fuel(123, "GS3 Moderate load humid-climate grass-shrub",
         0.0673, 0.0673, 0, 0.224, 0.336,
         5577, 5577, 4593, 0.549, 0.40, 18608, True),
    Fuel(124, "GS4 High load humid-climate grass-shrub",
         0.420, 0.0673, 0, 0.785, 0.785,
         5249, 5577, 4593, 0.640, 0.40, 18608, True),
    # SH - shrub
    Fuel(141, "SH1 Low load dry-climate shrub",
         0.0673, 0.0673, 0, 0.336, 0.358,
         6562, 5577, 5249, 0.305, 0.15, 18608, True),
    Fuel(142, "SH2 Moderate load dry-climate shrub",
         0.314, 0.314, 0, 0, 0.785,
         6562, 0, 5249, 0.305, 0.15, 18608, False),
    Fuel(143, "SH3 Moderate load humid-climate shrub",
         0.112, 0.673, 0, 0, 1.345,
         5577, 0, 4921, 0.762, 0.40, 18608, False),
    Fuel(144, "SH4 Low load humid-climate timber-shrub",
         0.0897, 0.135, 0.0224, 0, 0.560,
         6562, 0, 5577, 0.914, 0.30, 18608, False),
    Fuel(145, "SH5 High load humid-climate grass-shrub",
         0.785, 0.673, 0, 0, 1.345,
         2461, 0, 5249, 1.829, 0.15, 18608, False),
    Fuel(146, "SH6 Low load humid-climate shrub",
         0.673, 0.314, 0, 0, 1.009,
         2461, 0, 5249, 0.610, 0.30, 18608, False),
    Fuel(147, "SH7 Very high load dry-climate shrub",
         0.785, 1.401, 0.448, 0, 1.793,
         2461, 0, 5249, 1.829, 0.15, 18608, False),
    Fuel(148, "SH8 High load humid-climate shrub",
         0.560, 1.121, 0.224, 0, 1.569,
         2461, 0, 5249, 0.914, 0.40, 18608, False),
    Fuel(149, "SH9 Very high load humid-climate shrub",
         1.009, 1.401, 0.112, 0.785, 2.130,
         2461, 5577, 4921, 1.341, 0.40, 18608, True),
    # TU - timber-understory
    Fuel(161, "TU1 Light load dry-climate timber-grass-shrub",
         0.0448, 0.0897, 0.0224, 0.0673, 0.0897,
         6562, 5577, 5249, 0.183, 0.20, 18608, True),
    Fuel(162, "TU2 Moderate load humid-climate timber-shrub",
         0.969, 0.0224, 0.0224, 0, 0.0673,
         6562, 0, 5249, 0.305, 0.30, 18608, False),
    Fuel(163, "TU3 Moderate load humid-climate timber-grass-shrub",
         0.246, 0.0673, 0.0224, 0.135, 0.135,
         5577, 5577, 5249, 0.396, 0.30, 18608, True),
    Fuel(164, "TU4 Dwarf conifer with understory",
         0.987, 0, 0, 0, 0.448,
         3281, 0, 5249, 0.152, 0.12, 18608, False),
    Fuel(165, "TU5 Very high load dry-climate timber-shrub",
         0.897, 0.897, 0.673, 0, 0.673,
         2461, 0, 5249, 0.305, 0.25, 18608, False),
    # TL - timber-litter
    Fuel(181, "TL1 Low load compact conifer litter",
         0.224, 0.504, 0.504, 0, 0,
         6562, 0, 0, 0.061, 0.30, 18608, False),
    Fuel(182, "TL2 Low load broadleaf litter",
         0.314, 0.504, 0.448, 0, 0,
         6562, 0, 0, 0.061, 0.25, 18608, False),
    Fuel(183, "TL3 Moderate load conifer litter",
         0.112, 0.448, 0.560, 0, 0,
         6562, 0, 0, 0.091, 0.20, 18608, False),
    Fuel(184, "TL4 Small downed logs",
         0.112, 0.336, 0.785, 0, 0,
         6562, 0, 0, 0.122, 0.25, 18608, False),
    Fuel(185, "TL5 High load conifer litter",
         0.224, 0.336, 0.560, 0, 0,
         6562, 0, 0, 0.183, 0.25, 18608, False),
    Fuel(186, "TL6 Moderate load broadleaf litter",
         0.538, 0.269, 0.269, 0, 0,
         6562, 0, 0, 0.091, 0.25, 18608, False),
    Fuel(187, "TL7 Large downed logs",
         0.0673, 0.314, 1.121, 0, 0,
         6562, 0, 0, 0.122, 0.25, 18608, False),
    Fuel(188, "TL8 Long-needle litter",
         0.673, 0.135, 0.0673, 0, 0,
         5905, 0, 0, 0.091, 0.35, 18608, False),
    Fuel(189, "TL9 Very high load broadleaf litter",
         1.345, 0.560, 0.448, 0, 0,
         5905, 0, 0, 0.183, 0.35, 18608, False),
    # SB - slash-blowdown
    Fuel(201, "SB1 Low load activity fuel",
         0.336, 0.987, 1.121, 0, 0,
         6562, 0, 0, 0.305, 0.25, 18608, False),
    Fuel(202, "SB2 Moderate load activity fuel",
         1.009, 0.987, 0.987, 0, 0,
         6562, 0, 0, 0.305, 0.25, 18608, False),
    Fuel(203, "SB3 High load activity fuel",
         1.232, 1.121, 1.232, 0, 0,
         6562, 0, 0, 0.366, 0.25, 18608, False),
    Fuel(204, "SB4 High load blowdown",
         1.793, 0.448, 0.448, 0, 0,
         6562, 0, 0, 0.823, 0.25, 18608, False),
]

FUELS: dict[int, Fuel] = {f.code: f for f in _SB_RAW}

# Default for codes we don't have — treat as GR2 (low-load grass)
DEFAULT_FUEL_CODE = 102


def fuel_arrays(codes: np.ndarray) -> dict[str, np.ndarray]:
    """Vectorise: turn a 2-D array of FBFM40 codes into named per-cell arrays
    of fuel parameters. Unknown codes fall back to DEFAULT_FUEL_CODE."""
    fields = ("load_1h", "load_10h", "load_100h", "load_lh", "load_lw",
              "sav_1h", "sav_lh", "sav_lw", "depth_m", "mx_dead",
              "heat_kJkg")
    out = {f: np.zeros(codes.shape, dtype=np.float32) for f in fields}
    out["burnable"] = np.zeros(codes.shape, dtype=bool)
    out["dynamic"] = np.zeros(codes.shape, dtype=bool)

    default = FUELS[DEFAULT_FUEL_CODE]
    unique = np.unique(codes)
    for c in unique:
        ci = int(c)
        f = FUELS.get(ci, default)
        mask = codes == c
        for fname in fields:
            out[fname][mask] = float(getattr(f, fname))
        out["burnable"][mask] = f.burnable
        out["dynamic"][mask] = f.is_dynamic
    return out
