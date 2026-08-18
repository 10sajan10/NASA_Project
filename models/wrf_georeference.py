"""Derive a verified `GridDescriptor` from a wrfout file's own metadata.

`contracts/placement.py` can decide any publication, but only once the producer
declares which grid its array is on.  WRF-SFIRE declares no such thing, which
is why the recorded `arrival_s: array shape (253, 253) != grid (1001, 1001)`
failure had nothing to work with but two shapes.  This module is the producer
half: it reads what WRF already wrote and states the grid explicitly.

Three facts about real WRF-SFIRE output drive the design.  All three were read
off `wrf-sfire-stack/WRF-SFIRE/test/em_real/wrfout_d03_2019-09-04_12:00:00`,
not assumed:

1. **Fire arrays are allocated on the staggered dimension and padded.**
   `west_east` is 213, `west_east_stag` is 214, and `west_east_subgrid` is
   2140 = 214 x 10.  Only 2130 = 213 x 10 columns are real fire cells: the
   fire-state fields `TIGN_G` and `LFN` are exactly zero beyond 2130.

   Worse, the arrays on that subgrid do not agree with each other about where
   they end.  `FXLONG` runs one halo column further, to 2131, and `NFUEL_CAT`
   is populated across the entire allocated 2140 because it is an ingested
   input rather than fire state.  So an array's own extent cannot settle its
   grid -- three arrays on one subgrid give three answers, and reducing the
   full 2140 folds ten columns of padded fuel into the result.
   `valid_fire_shape` is 2130 and :func:`valid_fire_slice` drops the strip.

2. **The origin cannot be computed from `CEN_LAT`/`CEN_LON`.** Doing so puts
   the domain 1.7 degrees of latitude out.  The origin is therefore taken from
   the file's own `XLONG`/`XLAT` corner and then *verified* against the whole
   coordinate field; a derivation that does not reproduce what WRF wrote is
   refused rather than returned.

3. **WRF's projection is a domain-centred Lambert Conformal on a sphere**,
   with no EPSG code.  It is emitted as a deterministic whitespace-free token
   so that identity is stable across a run's nests, and so that placement
   against a UTM analysis cube reports `CRS_MISMATCH` rather than silently
   comparing incompatible coordinates.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from pyproj import CRS, Transformer

from contracts.types import GridDescriptor, SpatialScale

# WRF's idealized sphere.  Not WGS84, and the difference is kilometres.
WRF_EARTH_RADIUS_M = 6_370_000.0

# Projection parameters are stored as float32, so they are rounded before they
# reach the CRS token.  Without this, the same projection read from two nests
# could differ in the last bit and present as two different CRSs.
_PARAM_DECIMALS = 6

# The derived affine must reproduce WRF's own coordinates to better than this.
# Measured agreement on the reference file is ~3.4 m, which is a small
# fraction of one 90 m fire cell; the gate is set well above that but far
# below a cell, so a genuinely wrong projection cannot pass.
_COORDINATE_TOLERANCE_M = 30.0

_PROJECTIONS = {
    1: "lcc",       # Lambert Conformal
    2: "stere",     # polar stereographic
    3: "merc",      # Mercator
    6: "longlat",   # regular lat-lon
}


class WrfGeoreferenceError(ValueError):
    """The file's georeference could not be established."""


class WrfGeoreferenceUnverified(WrfGeoreferenceError):
    """A grid was derived but does not reproduce the file's own coordinates."""


def _number(value: Any) -> float:
    return float(np.asarray(value).reshape(-1)[0])


def _round(value: float) -> float:
    return round(_number(value), _PARAM_DECIMALS)


def proj4_string(attrs: dict[str, Any]) -> str:
    """WRF's projection as proj4. Refuses map projections it does not model."""
    try:
        code = int(_number(attrs["MAP_PROJ"]))
    except KeyError as exc:
        raise WrfGeoreferenceError("wrfout declares no MAP_PROJ") from exc
    if code not in _PROJECTIONS:
        raise WrfGeoreferenceError(
            f"MAP_PROJ {code} is not modelled here; refusing to guess a "
            "projection for output that would then be placed on a cube")
    name = _PROJECTIONS[code]
    lat_1 = _round(attrs["TRUELAT1"])
    lat_2 = _round(attrs.get("TRUELAT2", attrs["TRUELAT1"]))
    lat_0 = _round(attrs["MOAD_CEN_LAT"])
    lon_0 = _round(attrs["STAND_LON"])
    parts = [f"+proj={name}"]
    if code == 1:
        parts += [f"+lat_1={lat_1}", f"+lat_2={lat_2}"]
    elif code == 2:
        parts += [f"+lat_ts={lat_1}", f"+lat_0={90.0 if lat_0 >= 0 else -90.0}"]
    elif code == 3:
        parts += [f"+lat_ts={lat_1}"]
    if code != 2:
        parts += [f"+lat_0={lat_0}"]
    parts += [f"+lon_0={lon_0}", "+x_0=0", "+y_0=0",
              f"+R={WRF_EARTH_RADIUS_M:.0f}", "+units=m", "+no_defs"]
    return " ".join(parts)


def crs_token(attrs: dict[str, Any]) -> str:
    """A stable whitespace-free CRS identity for a WRF projection.

    `contracts.identity.canonical_crs` refuses whitespace, and WRF's
    domain-centred projection has no EPSG code, so the proj4 parameters are
    folded into one deterministic token.  Every nest of a run shares it,
    because they share the projection; nothing else does, which is the point.
    """
    code = int(_number(attrs["MAP_PROJ"]))
    name = _PROJECTIONS[code]
    return (f"WRF-{name.upper()}:lat_1={_round(attrs['TRUELAT1'])}"
            f":lat_2={_round(attrs.get('TRUELAT2', attrs['TRUELAT1']))}"
            f":lat_0={_round(attrs['MOAD_CEN_LAT'])}"
            f":lon_0={_round(attrs['STAND_LON'])}"
            f":R={WRF_EARTH_RADIUS_M:.0f}")


@dataclass(frozen=True)
class WrfGeoreference:
    """Everything needed to place a WRF domain's output, and nothing more."""

    domain_id: int
    proj4: str
    atmospheric: GridDescriptor
    fire: GridDescriptor | None
    subgrid_ratio: tuple[int, int] | None
    allocated_fire_shape: tuple[int, int] | None
    max_coordinate_residual_m: float

    @property
    def valid_fire_shape(self) -> tuple[int, int] | None:
        return None if self.fire is None else tuple(self.fire.shape)

    def valid_fire_slice(self) -> tuple[slice, slice]:
        """The region of an allocated fire array that actually holds data.

        Fire arrays come back shaped `allocated_fire_shape`; everything outside
        this slice is the zero-padded staggered strip.
        """
        if self.fire is None:
            raise WrfGeoreferenceError("this domain has no fire subgrid")
        rows, cols = self.fire.shape
        return (slice(0, rows), slice(0, cols))

    def describes(self, array_shape: tuple[int, int]) -> GridDescriptor | None:
        """Which declared grid an array of this shape is on, if any.

        Returns `None` rather than guessing when the shape matches neither the
        atmospheric grid nor either fire shape.
        """
        shape = tuple(array_shape)
        if shape == tuple(self.atmospheric.shape):
            return self.atmospheric
        if self.fire is not None and shape == tuple(self.fire.shape):
            return self.fire
        return None


def _grid(crs: str, shape: tuple[int, int], x0: float, y1: float,
          cell: float) -> GridDescriptor:
    # x0/y1 are outer edges derived above; GridDescriptor's canonical affine
    # stores the first sample centre in array order.
    return GridDescriptor(
        crs, ("easting", "northing"), (int(shape[0]), int(shape[1])),
        (repr(float(cell)), "0", repr(float(x0 + cell / 2.0)),
         "0", repr(-float(cell)), repr(float(y1 - cell / 2.0))),
        SpatialScale(repr(float(cell)), repr(float(cell)), "m"))


def georeference_from_dataset(dataset: Any) -> WrfGeoreference:
    """Derive and verify a georeference from an open netCDF4 Dataset."""
    attrs = {name: dataset.getncattr(name) for name in dataset.ncattrs()}
    proj4 = proj4_string(attrs)
    token = crs_token(attrs)
    for required in ("XLONG", "XLAT"):
        if required not in dataset.variables:
            raise WrfGeoreferenceError(
                f"wrfout has no {required}; the origin cannot be established "
                "from CEN_LAT/CEN_LON alone, so no grid is derived")

    lon = np.asarray(dataset.variables["XLONG"][0], dtype="float64")
    lat = np.asarray(dataset.variables["XLAT"][0], dtype="float64")
    rows, cols = lon.shape
    cell = _number(attrs["DX"])
    if _number(attrs["DY"]) != cell:
        raise WrfGeoreferenceError(
            "anisotropic DX/DY is not modelled; refusing to assume square "
            "cells for output that would then be placed on a cube")

    crs = CRS.from_proj4(proj4)
    to_crs = Transformer.from_crs(4326, crs, always_xy=True)
    from_crs = Transformer.from_crs(crs, 4326, always_xy=True)

    # XLONG/XLAT index [0, 0] is the south-west mass point, so its projected
    # position fixes the grid's lower-left corner and hence its top edge.
    corner_x, corner_y = to_crs.transform(lon[0, 0], lat[0, 0])
    x0 = corner_x - cell / 2.0
    y0 = corner_y - cell / 2.0
    y1 = y0 + rows * cell

    residual = _verify(from_crs, x0, y0, cell, rows, cols, lon, lat)
    if residual > _COORDINATE_TOLERANCE_M:
        raise WrfGeoreferenceUnverified(
            f"the derived grid misses WRF's own XLONG/XLAT by {residual:.1f} m, "
            f"above the {_COORDINATE_TOLERANCE_M:.0f} m tolerance; the "
            "projection or the origin is wrong and no grid is returned")

    atmospheric = _grid(token, (rows, cols), x0, y1, cell)
    fire, ratio, allocated = _fire_grid(
        dataset, attrs, token, x0, y1, cell, rows, cols)

    return WrfGeoreference(
        domain_id=int(_number(attrs.get("GRID_ID", 0))),
        proj4=proj4, atmospheric=atmospheric, fire=fire,
        subgrid_ratio=ratio, allocated_fire_shape=allocated,
        max_coordinate_residual_m=residual)


def _verify(from_crs: Transformer, x0: float, y0: float, cell: float,
            rows: int, cols: int, lon: np.ndarray,
            lat: np.ndarray) -> float:
    """Largest distance between the derived cell centres and WRF's own."""
    xs = x0 + (np.arange(cols) + 0.5) * cell
    ys = y0 + (np.arange(rows) + 0.5) * cell
    grid_x, grid_y = np.meshgrid(xs, ys)
    derived_lon, derived_lat = from_crs.transform(grid_x, grid_y)
    # Degrees to metres, with longitude shortened by latitude.
    metres_per_degree = np.pi * WRF_EARTH_RADIUS_M / 180.0
    dy = (derived_lat - lat) * metres_per_degree
    dx = (derived_lon - lon) * metres_per_degree * np.cos(np.radians(lat))
    return float(np.max(np.hypot(dx, dy)))


def _fire_grid(dataset: Any, attrs: dict[str, Any], token: str, x0: float,
               y1: float, cell: float, rows: int, cols: int
               ) -> tuple[GridDescriptor | None, tuple[int, int] | None,
                          tuple[int, int] | None]:
    """The valid fire subgrid, with the staggered padding strip excluded."""
    dims = dataset.dimensions
    if ("west_east_subgrid" not in dims or "south_north_subgrid" not in dims):
        return None, None, None
    allocated = (len(dims["south_north_subgrid"]),
                 len(dims["west_east_subgrid"]))

    # The subgrid is allocated against the *staggered* dimension, so the ratio
    # divides the staggered count, while only the mass count carries data.
    stag_x = int(_number(attrs["WEST-EAST_GRID_DIMENSION"]))
    stag_y = int(_number(attrs["SOUTH-NORTH_GRID_DIMENSION"]))
    if allocated[1] % stag_x or allocated[0] % stag_y:
        raise WrfGeoreferenceError(
            f"fire subgrid {allocated} is not an integer multiple of the "
            f"staggered dimensions ({stag_y}, {stag_x}); the refinement ratio "
            "is undefined and no fire grid is returned")
    ratio = (allocated[1] // stag_x, allocated[0] // stag_y)
    if ratio[0] < 1 or ratio[1] < 1:
        raise WrfGeoreferenceError(f"nonsensical fire refinement ratio {ratio}")

    valid = (rows * ratio[1], cols * ratio[0])
    if valid[0] > allocated[0] or valid[1] > allocated[1]:
        raise WrfGeoreferenceError(
            f"valid fire extent {valid} exceeds the allocated array "
            f"{allocated}")
    return (_grid(token, valid, x0, y1, cell / ratio[0]), ratio, allocated)


def read_wrf_georeference(path: Path | str) -> WrfGeoreference:
    """Open a wrfout file and derive its verified georeference."""
    import netCDF4  # local: only this entry point needs the reader

    dataset = netCDF4.Dataset(str(path))
    try:
        return georeference_from_dataset(dataset)
    finally:
        dataset.close()


__all__ = [
    "WRF_EARTH_RADIUS_M",
    "WrfGeoreference",
    "WrfGeoreferenceError",
    "WrfGeoreferenceUnverified",
    "crs_token",
    "georeference_from_dataset",
    "proj4_string",
    "read_wrf_georeference",
]
