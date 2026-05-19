"""UniformFuelDriver: fill nfuel_cat with one configured category.

The substrate doesn't ship a fuel-classification model. This driver is
an explicit no-physics placeholder so the WRF-SFIRE adapter (and any
other producer that declares `requires=["nfuel_cat"]`) has *something*
to consume during baseline runs and tests. Real scenarios should
replace this with a driver that reads a fuel raster you trust.

Configure the category and a non-burnable mask if you want a sentinel
elsewhere:

    UniformFuelDriver(category=3)
        -> nfuel_cat is `category` everywhere (e.g. 3 = "Tall grass" in
           the Anderson 13 convention; you decide what your downstream
           model treats as that index).

    UniformFuelDriver(category=3, no_fuel_category=14,
                      no_fuel_mask=mask_array)
        -> nfuel_cat is `category` where mask is False, `no_fuel_category`
           where mask is True. Useful for stamping out water / urban /
           pre-existing burns.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

import numpy as np

from cube.store import Cube
from drivers.base import Driver


class UniformFuelDriver(Driver):
    name = "uniform_fuel"
    produces = ["nfuel_cat"]
    is_static = True

    def __init__(self,
                 *,
                 category: int = 3,
                 no_fuel_category: int = 14,
                 no_fuel_mask: Optional[np.ndarray] = None,
                 description: str = "") -> None:
        """
        category : integer fuel index written everywhere by default.
        no_fuel_category : integer index used where `no_fuel_mask` is True.
        no_fuel_mask : optional 2D bool array matching the cube grid
                        shape; cells where it is True get
                        `no_fuel_category` instead.
        description : free-form note recorded in the catalog.
        """
        if category < 0:
            raise ValueError("category must be >= 0")
        if no_fuel_category < 0:
            raise ValueError("no_fuel_category must be >= 0")
        self.category = int(category)
        self.no_fuel_category = int(no_fuel_category)
        self.no_fuel_mask = (None if no_fuel_mask is None
                              else np.asarray(no_fuel_mask, dtype=bool))
        self.description = description or (
            f"Uniform fuel category={self.category}"
            + (f" with {int(np.count_nonzero(self.no_fuel_mask))} "
               f"no-fuel cells (={self.no_fuel_category})"
               if self.no_fuel_mask is not None else ""))

    def fetch(self, cube: Cube,
              t_start: Optional[datetime] = None,
              t_end: Optional[datetime] = None) -> list[str]:
        H, W = cube.grid.shape
        arr = np.full((H, W), self.category, dtype="int16")
        if self.no_fuel_mask is not None:
            if self.no_fuel_mask.shape != (H, W):
                raise ValueError(
                    f"no_fuel_mask shape {self.no_fuel_mask.shape} != "
                    f"cube grid {(H, W)}")
            arr[self.no_fuel_mask] = self.no_fuel_category

        cube.write_static(
            "nfuel_cat", arr,
            source=f"uniform_fuel(cat={self.category})",
            native_res_m=float(cube.grid.pixel_m),
            units="fuel_category_index",
            producer=self.name,
            description=self.description)
        return ["nfuel_cat"]
