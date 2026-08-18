"""A target-driven run whose producer emits a file, not an array.

`Pipeline.from_targets` resolves the DAG and runs producers; publication then
went one way only -- write the array onto the cube's single grid. A producer
like WRF-SFIRE emits netCDF on its own grid, so that path could not carry it.

Returning a `DatasetRef` says "the result is this file, on this grid". The
engine catalogs it where it already lives: nothing copied, reprojected,
resampled or re-encoded.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import numpy as np
import pytest

from contracts.types import GridDescriptor, SpatialScale
from cube.entries import DatasetRef, ResolutionPolicy
from cube.grid import SimulationGrid
from cube.store import Cube


def _grid():
    return GridDescriptor(
        "WRF-LCC:lat_1=32.78:lat_2=32.78:lat_0=32.78:lon_0=-96.81:R=6370000",
        ("easting", "northing"), (2130, 2130),
        ("90.0", "0", "45.0", "0", "-90.0", "-45.0"),
        SpatialScale("90.0", "90.0", "m"))


@pytest.fixture()
def cube(tmp_path):
    grid = SimulationGrid(crs_epsg=32614, pixel_m=900.0, width=10, height=10,
                          x0=0.0, y1=9000.0)
    store = Cube(tmp_path / "cube", grid)
    yield store
    store.close()


@pytest.fixture()
def wrfout(tmp_path):
    path = tmp_path / "wrfout_d03_2019-09-04_12:00:00"
    path.write_bytes(b"pretend-netcdf-bytes" * 512)
    return path


def _spec(name="arrival_s", units="s",
          description="ignition time on ground"):
    from engine.contracts import VarSpec
    return VarSpec(name, kind="static", dtype="float32", units=units,
                   description=description)


def _publish(cube, ref, spec=None):
    """Drive the real publication path with one declared output."""
    from engine.contracts import ProducerV2

    spec = spec or _spec()

    class _Producer(ProducerV2):
        name = "wrf_sfire"
        produces = (spec,)
        requires = ()

        def compute(self, inputs, request):
            return {spec.name: ref}

    request = type("R", (), {"context": {}})()
    return _Producer().update(cube, {spec.name: ref}, request)


# -- a file output is catalogued, not written ----------------------------


def test_a_file_producer_is_catalogued_on_its_own_grid(cube, wrfout):
    ref = DatasetRef(str(wrfout), "application/x-netcdf", grid=_grid(),
                     detail={"source_variable": "TIGN_G"})
    _publish(cube, ref)

    entry = cube.catalog.resolve("arrival_s")
    assert entry.media_type == "application/x-netcdf"
    assert Path(entry.location) == wrfout.resolve()
    # WRF's own 90 m Lambert survives; the cube's 900 m UTM is untouched.
    assert entry.grid.crs.startswith("WRF-LCC:")
    assert float(entry.grid.affine[0]) == 90.0
    assert tuple(entry.grid.shape) == (2130, 2130)
    assert tuple(cube.grid.shape) == (10, 10)


def test_the_catalogued_digest_matches_the_file(cube, wrfout):
    ref = DatasetRef(str(wrfout), "application/x-netcdf", grid=_grid())
    _publish(cube, ref)
    entry = cube.catalog.resolve("arrival_s")
    assert entry.content_sha256 == hashlib.sha256(
        wrfout.read_bytes()).hexdigest()


def test_producer_metadata_reaches_the_catalogue(cube, wrfout):
    ref = DatasetRef(str(wrfout), "application/x-netcdf", grid=_grid(),
                     detail={"source_variable": "TIGN_G"})
    _publish(cube, ref)
    detail = cube.catalog.resolve("arrival_s").detail
    assert detail["source_variable"] == "TIGN_G"
    # The declared VarSpec fills in what the producer did not say.
    assert detail["units"] == "s"
    assert detail["description"] == "ignition time on ground"


def test_nothing_is_written_to_the_array_store(cube, wrfout):
    ref = DatasetRef(str(wrfout), "application/x-netcdf", grid=_grid())
    _publish(cube, ref)
    assert not cube.has("arrival_s")


# -- arrays still work exactly as before ---------------------------------


def test_an_array_producer_is_unchanged(cube):
    _publish(cube, np.zeros((10, 10), dtype="float32"))
    assert cube.has("arrival_s")
    assert cube.read_static("arrival_s").shape == (10, 10)


def test_both_kinds_can_coexist(cube, wrfout):
    _publish(cube, np.ones((10, 10), dtype="float32"))
    other = _spec("fire_area", units="1",
                  description="fraction of cell area on fire")
    _publish(cube, DatasetRef(str(wrfout), "application/x-netcdf",
                              grid=_grid()), spec=other)
    assert cube.has("arrival_s")
    assert cube.catalog.resolve("fire_area").media_type == \
        "application/x-netcdf"


# -- refusals -------------------------------------------------------------


def test_a_reference_must_declare_its_media_type():
    with pytest.raises(ValueError, match="media type"):
        DatasetRef("/tmp/x.nc", "")


def test_a_reference_must_name_a_path():
    with pytest.raises(ValueError, match="must name a path"):
        DatasetRef("", "application/x-netcdf")


def test_a_missing_produced_file_is_refused(cube, tmp_path):
    ref = DatasetRef(str(tmp_path / "never-written.nc"),
                     "application/x-netcdf", grid=_grid())
    with pytest.raises((FileNotFoundError, OSError)):
        _publish(cube, ref)
