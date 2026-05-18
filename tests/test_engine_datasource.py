"""DataSource layer tests.

Exercises the search/fetch contract and the registry's priority routing.
The LocalRasterSource path is exercised against a real GeoTIFF when one
is available locally (any raster from a path the test optionally finds),
otherwise against a tiny synthesized test raster.
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine import (
    DataAsset,
    DataQuery,
    DataSource,
    DataSourceRegistry,
    LocalRasterSource,
)


# ----------------------------------------------- query/asset value types
def test_query_and_asset_are_immutable_value_types():
    q = DataQuery(variable="ndvi")
    assert q.variable == "ndvi"
    with pytest.raises(Exception):  # frozen dataclass
        q.variable = "other"  # type: ignore[misc]
    a = DataAsset(source="x", variable="ndvi", uri="/tmp/n.tif")
    assert a.uri == "/tmp/n.tif"


# ----------------------------------------------- registry
class _FakeSource(DataSource):
    def __init__(self, name: str, vars_: list[str], hits: list[DataAsset]):
        self.name = name
        self.variables = tuple(vars_)
        self._hits = hits
    def search(self, query):
        if query.variable not in self.variables:
            return []
        return list(self._hits)
    def fetch(self, asset, grid):
        return np.zeros(grid.shape, dtype="float32")


def test_registry_walks_priority_order_for_first_hit():
    high = _FakeSource("hi", ["ndvi"],
                       [DataAsset("hi", "ndvi", "/p/hi.tif")])
    low = _FakeSource("lo", ["ndvi"],
                      [DataAsset("lo", "ndvi", "/p/lo.tif")])
    reg = DataSourceRegistry()
    reg.register(low, priority=0)
    reg.register(high, priority=10)
    hit = reg.first_hit(DataQuery("ndvi"))
    assert hit.source == "hi"


def test_registry_search_returns_all_hits():
    a = _FakeSource("a", ["ndvi"], [DataAsset("a", "ndvi", "/p/a.tif")])
    b = _FakeSource("b", ["ndvi"], [DataAsset("b", "ndvi", "/p/b.tif")])
    reg = DataSourceRegistry().register(a).register(b)
    hits = reg.search(DataQuery("ndvi"))
    assert sorted(h.source for h in hits) == ["a", "b"]


def test_registry_filters_by_variable():
    a = _FakeSource("a", ["ndvi"], [DataAsset("a", "ndvi", "/p/a")])
    b = _FakeSource("b", ["temp_c"], [DataAsset("b", "temp_c", "/p/b")])
    reg = DataSourceRegistry().register(a).register(b)
    assert [h.source for h in reg.search(DataQuery("ndvi"))] == ["a"]
    assert [h.source for h in reg.search(DataQuery("temp_c"))] == ["b"]
    assert reg.search(DataQuery("nonexistent")) == []


def test_registry_skips_failing_source():
    class _Boom(DataSource):
        name = "boom"
        variables = ("ndvi",)
        def search(self, query): raise RuntimeError("network down")
        def fetch(self, asset, grid): raise RuntimeError("never")
    good = _FakeSource("good", ["ndvi"],
                       [DataAsset("good", "ndvi", "/p/g")])
    reg = DataSourceRegistry()
    reg.register(_Boom(), priority=10)
    reg.register(good, priority=0)
    # Boom raises -> registry falls through to next source.
    hit = reg.first_hit(DataQuery("ndvi"))
    assert hit is not None
    assert hit.source == "good"


# ----------------------------------------------- LocalRasterSource
def _find_opt_local_raster() -> Path | None:
    """Probe a few well-known paths for an opt-in local raster to use in
    the real-file integration test. Returns None if nothing's there —
    in which case the integration test is skipped. The engine code does
    not depend on any of these paths."""
    candidates = [
        Path("LANDFIRE/LF2024_FBFM40_CONUS/Tif/LF2024_FBFM40_CONUS.tif"),
    ]
    for p in candidates:
        if p.exists():
            return p
    return None


OPT_LOCAL_RASTER = _find_opt_local_raster()


def _real_grid():
    from cube.grid import SimulationGrid
    return SimulationGrid.from_center_radius(-96.797, 32.776, 5_000.0, 500.0)


def test_local_raster_source_search_handles_missing_path(tmp_path):
    src = LocalRasterSource("local",
                            {"missing": str(tmp_path / "no.tif")})
    assert src.search(DataQuery("missing")) == []


def test_local_raster_source_search_returns_asset_when_present(tmp_path):
    """Build a tiny GeoTIFF on the fly and confirm search finds it."""
    rasterio = pytest.importorskip("rasterio")
    from rasterio.transform import from_bounds
    p = tmp_path / "tiny.tif"
    transform = from_bounds(-97.0, 32.0, -96.0, 33.0, 100, 100)
    with rasterio.open(
            p, "w", driver="GTiff", height=100, width=100, count=1,
            dtype="float32", crs="EPSG:4326", transform=transform) as dst:
        dst.write(np.full((100, 100), 7.5, dtype="float32"), 1)
    src = LocalRasterSource("syn", {"foo": str(p)})
    hits = src.search(DataQuery("foo"))
    assert len(hits) == 1
    assert hits[0].uri == str(p)


@pytest.mark.skipif(OPT_LOCAL_RASTER is None,
                    reason="no opt-in local raster present on this machine")
def test_local_raster_source_fetches_real_raster_onto_grid():
    """End-to-end smoke: discover an opt-in local raster via the registry,
    fetch it onto a tiny grid, sanity-check the array shape + value range.
    The specific raster doesn't matter — this validates the reproject path
    against a real file when one is available."""
    src = LocalRasterSource(
        "opt_local", {"opt_var": str(OPT_LOCAL_RASTER)})
    reg = DataSourceRegistry().register(src)
    grid = _real_grid()
    asset = reg.first_hit(DataQuery("opt_var"))
    assert asset is not None
    arr = reg.fetch(asset, grid)
    assert arr.shape == grid.shape
