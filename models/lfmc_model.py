"""LFMC: live fuel moisture content from NDWI via Yebra et al. (2013).

LFMC_pct = 125 + 288 * NDWI

Reads `ndwi` from cube; writes `lfmc_pct` (static).
"""
from __future__ import annotations
import numpy as np

from cube.store import Cube
from fusion.on_the_fly import get_static


def run(cube: Cube) -> str:
    ndwi = get_static(cube, "ndwi")
    lfmc = 125.0 + 288.0 * ndwi
    # NaN cells (cloud-covered for the whole window) get the mosaic median
    median = np.nanmedian(lfmc)
    if not np.isfinite(median):
        median = 100.0       # default if everything was NaN
    lfmc = np.where(np.isfinite(lfmc), lfmc, median).astype(np.float32)
    lfmc = np.clip(lfmc, 30.0, 250.0)
    cube.write_static(
        "lfmc_pct", lfmc,
        source="Yebra2013(LFMC=125+288*NDWI) on cube/ndwi",
        native_res_m=10.0, units="%", producer="lfmc_model",
        description="Live fuel moisture content (Yebra 2013 regression)")
    return "lfmc_pct"
