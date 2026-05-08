"""Data discovery + access abstraction.

Today every driver hard-codes its source: LANDFIRE local path,
ARCO-ERA5 GCS URI, Planetary Computer STAC for Landsat/Sentinel, USGS
3DEP for DEM. Eleven drivers, eleven access patterns. This module
introduces a unified discovery layer:

    query = DataQuery("ndvi", bbox=..., t_start=..., t_end=...)
    asset = registry.first_hit(query)              # search
    arr   = source.fetch(asset, grid)              # access

A `DataSource` answers two questions: does it cover a query, and what
assets satisfy it. A `DataSourceRegistry` ranks sources per variable
so the engine can fall back across providers.

Phase F deliverables (this file):
  * DataQuery / DataAsset value types
  * DataSource abstract base
  * DataSourceRegistry with priority + per-variable filters
  * LocalRasterSource backend (file-on-disk, GeoTIFF/etc) — proven
    against the LANDFIRE workflow

Future Phase F follow-ups (not yet built):
  * StacSource (Microsoft Planetary Computer, Earth Search)
  * GcsZarrSource (ARCO-ERA5)
  * Discovery-driven producer wiring (today producers still wire their
    own inputs; the next step is a generic producer that consumes
    `requires=["ndvi"]` and routes through the registry)
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import numpy as np


# --------------------------------------------------------------- types
Bbox = tuple[float, float, float, float]   # (lon_min, lat_min, lon_max, lat_max), EPSG:4326


@dataclass(frozen=True)
class DataQuery:
    """A request for data on a canonical variable, scoped to AOI + time."""
    variable: str
    bbox: Optional[Bbox] = None
    t_start: Optional[datetime] = None
    t_end: Optional[datetime] = None
    constraints: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DataAsset:
    """Search hit: a handle to fetchable data. Doesn't load anything yet."""
    source: str
    variable: str
    uri: str
    t_start: Optional[datetime] = None
    t_end: Optional[datetime] = None
    native_res_m: Optional[float] = None
    metadata: dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------- abstract base
class DataSource(ABC):
    """A data source. Implementations declare which variables they serve
    and how to search/fetch them."""

    name: str = ""
    variables: tuple[str, ...] = ()

    def covers(self, query: DataQuery) -> bool:
        """Cheap pre-filter: does this source even consider this query?"""
        return query.variable in self.variables

    @abstractmethod
    def search(self, query: DataQuery) -> list[DataAsset]:
        """Return zero or more assets matching the query."""

    @abstractmethod
    def fetch(self, asset: DataAsset, grid) -> np.ndarray:
        """Load the asset, reproject onto `grid` (a SimulationGrid),
        return ndarray of grid.shape."""


# --------------------------------------------------------------- registry
class DataSourceRegistry:
    """Ranked sources per variable. `search()` walks them in priority order
    and concatenates hits; `first_hit()` short-circuits on the first source
    that returns anything."""

    def __init__(self):
        # variable -> list of (priority, source). Higher priority first.
        self._by_var: dict[str, list[tuple[int, DataSource]]] = {}
        self._all: list[DataSource] = []

    def register(self, source: DataSource, *,
                 priority: int = 0,
                 variables: Optional[list[str]] = None) -> "DataSourceRegistry":
        """Register a source. If `variables` is None, uses source.variables.
        Higher `priority` wins."""
        vars_ = list(variables) if variables is not None else list(source.variables)
        if not vars_:
            raise ValueError(
                f"source {source.name!r} declares no variables")
        for var in vars_:
            self._by_var.setdefault(var, []).append((priority, source))
            # Stable sort: highest priority first, ties keep insertion order.
            self._by_var[var].sort(key=lambda p_s: -p_s[0])
        self._all.append(source)
        return self

    def sources_for(self, variable: str) -> list[DataSource]:
        return [s for _, s in self._by_var.get(variable, [])]

    def search(self, query: DataQuery) -> list[DataAsset]:
        out: list[DataAsset] = []
        for source in self.sources_for(query.variable):
            if not source.covers(query):
                continue
            try:
                hits = source.search(query)
            except Exception:
                # Failing source shouldn't kill the search; skip and try next.
                continue
            out.extend(hits)
        return out

    def first_hit(self, query: DataQuery) -> Optional[DataAsset]:
        """Walk sources in priority order; return the first asset found."""
        for source in self.sources_for(query.variable):
            if not source.covers(query):
                continue
            try:
                hits = source.search(query)
            except Exception:
                continue
            if hits:
                return hits[0]
        return None

    def fetch(self, asset: DataAsset, grid) -> np.ndarray:
        """Look up the named source and delegate fetch."""
        for source in self._all:
            if source.name == asset.source:
                return source.fetch(asset, grid)
        raise KeyError(f"no source named {asset.source!r} in registry")


# --------------------------------------------------------------- backends
class LocalRasterSource(DataSource):
    """File-on-disk raster source.

    Configured with a mapping `{variable: path}` of canonical variable
    names to local raster files (GeoTIFF or any rasterio-readable format).
    Search ignores AOI / time and returns the configured asset (or none);
    fetch reprojects onto the target grid via rasterio.

    Useful for static layers (LANDFIRE FBFM40/FBFM13, DEM cache, urban
    masks). For time-varying or AOI-tiled archives use a richer source.
    """

    def __init__(self, name: str, paths: dict[str, str | Path]):
        self.name = name
        self.paths = {k: str(v) for k, v in paths.items()}
        self.variables = tuple(self.paths.keys())

    def search(self, query: DataQuery) -> list[DataAsset]:
        path = self.paths.get(query.variable)
        if not path:
            return []
        if not Path(path).exists():
            return []
        return [DataAsset(
            source=self.name,
            variable=query.variable,
            uri=path,
            metadata={"format": "raster"})]

    def fetch(self, asset: DataAsset, grid) -> np.ndarray:
        # Defer rasterio import: keeps the engine module light when the
        # data layer isn't in use.
        import rasterio
        from rasterio.transform import Affine
        from rasterio.warp import Resampling, reproject

        H, W = grid.shape
        dst_transform = Affine(grid.pixel_m, 0, grid.x0,
                               0, -grid.pixel_m, grid.y1)

        with rasterio.open(asset.uri) as src:
            # Choose dtype + resampling from the source: integer-coded
            # categorical rasters need nearest; continuous float -> bilinear.
            dtype = src.dtypes[0] if src.dtypes else "float32"
            categorical = np.dtype(dtype).kind in ("i", "u")
            resampling = (Resampling.nearest if categorical
                          else Resampling.bilinear)
            dst = np.zeros((H, W), dtype=dtype)
            reproject(
                source=rasterio.band(src, 1),
                destination=dst,
                src_transform=src.transform, src_crs=src.crs,
                dst_transform=dst_transform, dst_crs=grid.crs,
                src_nodata=src.nodata,
                resampling=resampling,
            )
        return dst
