"""LANDFIRE drivers: read local LANDFIRE fuel GeoTIFFs and reproject+clip
them onto the simulation grid.

`LandfireDriver` keeps the existing FBFM40 path used by the built-in Rothermel
model. `LandfireFBFM13Driver` provides Anderson 13 fuel categories and a
WRF-Fire-style `nfuel_cat` layer for external fire-model adapters.
"""
from __future__ import annotations
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np
import rasterio
from rasterio.transform import Affine
from rasterio.warp import reproject, Resampling

from cube.store import Cube
from drivers.base import Driver


class LandfireDriver(Driver):
    name = "landfire"
    produces = ["fbfm40"]
    is_static = True

    def __init__(self, src_path: str | Path):
        self.src_path = str(src_path)

    def fetch(self, cube: Cube,
              t_start: Optional[datetime] = None,
              t_end: Optional[datetime] = None) -> list[str]:
        grid = cube.grid
        dst_transform = Affine(grid.pixel_m, 0, grid.x0,
                               0, -grid.pixel_m, grid.y1)
        dst = np.zeros(grid.shape, dtype=np.int16)

        with rasterio.open(self.src_path) as src:
            reproject(
                source=rasterio.band(src, 1),
                destination=dst,
                src_transform=src.transform, src_crs=src.crs,
                dst_transform=dst_transform, dst_crs=grid.crs,
                resampling=Resampling.nearest,
            )
            native = float(abs(src.transform.a))

        # remap nodata-ish values to FBFM40 NB1 (urban, non-burnable)
        # so missing pixels don't break the spread model
        dst = np.where((dst <= 0) | (dst > 204), 91, dst).astype(np.int16)

        cube.write_static(
            "fbfm40", dst,
            source=f"LANDFIRE_LF2024:{Path(self.src_path).name}",
            native_res_m=native,
            units="FBFM40_code",
            producer=self.name,
            description="Scott & Burgan 40 surface fire behavior fuel model code")
        return ["fbfm40"]


class LandfireFBFM13Driver(Driver):
    """Read LANDFIRE FBFM13 and write WRF-Fire-ready fuel categories.

    LANDFIRE FBFM13 source values are 1..13 for Anderson fuel categories plus
    special non-burnable classes such as urban/water/barren. WRF-Fire's
    default examples expect `NFUEL_CAT` values 1..13 and category 14 for
    no-fuel, so this driver writes both:

      * `fbfm13`    - source fuel-model codes, with invalid cells as -9999
      * `nfuel_cat` - 1..13 burnable fuels, 14 for non-burnable/no-data
    """

    name = "landfire_fbfm13"
    produces = ["fbfm13", "nfuel_cat"]
    is_static = True

    def __init__(self, src_path: str | Path):
        self.src_path = str(src_path)

    def fetch(self, cube: Cube,
              t_start: Optional[datetime] = None,
              t_end: Optional[datetime] = None) -> list[str]:
        grid = cube.grid
        dst_transform = Affine(grid.pixel_m, 0, grid.x0,
                               0, -grid.pixel_m, grid.y1)
        raw = np.full(grid.shape, -9999, dtype=np.int16)

        with rasterio.open(self.src_path) as src:
            reproject(
                source=rasterio.band(src, 1),
                destination=raw,
                src_transform=src.transform, src_crs=src.crs,
                src_nodata=src.nodata,
                dst_transform=dst_transform, dst_crs=grid.crs,
                dst_nodata=-9999,
                resampling=Resampling.nearest,
            )
            native = float(abs(src.transform.a))

        valid_source = ((raw >= 1) & (raw <= 13)) | np.isin(
            raw, [91, 92, 93, 98, 99])
        fbfm13 = np.where(valid_source, raw, -9999).astype(np.int16)
        nfuel_cat = np.where((fbfm13 >= 1) & (fbfm13 <= 13),
                             fbfm13, 14).astype(np.int16)

        source = f"LANDFIRE_FBFM13:{Path(self.src_path).name}"
        cube.write_static(
            "fbfm13", fbfm13,
            source=source, native_res_m=native,
            units="FBFM13_code", producer=self.name,
            description="Anderson 13 surface fire behavior fuel model code")
        cube.write_static(
            "nfuel_cat", nfuel_cat,
            source=source, native_res_m=native,
            units="WRF_Fire_NFUEL_CAT", producer=self.name,
            description="WRF-Fire NFUEL_CAT: Anderson 1..13, 14 = no fuel")
        return list(self.produces)
