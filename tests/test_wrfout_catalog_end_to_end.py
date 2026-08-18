"""Cataloguing real WRF-SFIRE output, end to end, with no reprojection.

The cube's job here is not to hold arrays. It is to record *where* a dataset
lives, *what* it contains, *what produced it*, and *what it was derived from*,
so a downstream consumer can find it and read the bytes natively.

Every assertion below runs against the real
`wrf-sfire-stack/WRF-SFIRE/test/em_real/wrfout_d03_2019-09-04_12:00:00`
(gitignored, 131 MB), and skips when it is absent. Nothing is reprojected,
resampled, or re-encoded, and the entry keeps WRF's own Lambert grid.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from cube.catalog import Catalog
from cube.entries import CubeEntry, EntryInput, ResolutionPolicy
from models.wrf_georeference import read_wrf_georeference

WRFOUT = Path("wrf-sfire-stack/WRF-SFIRE/test/em_real/"
              "wrfout_d03_2019-09-04_12:00:00")


def _digest(path: Path) -> str:
    sha = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            sha.update(block)
    return sha.hexdigest()


@pytest.fixture(scope="module")
def wrfout() -> Path:
    if not WRFOUT.exists():
        pytest.skip(f"{WRFOUT} absent (gitignored, 131 MB)")
    return WRFOUT


@pytest.fixture(scope="module")
def digest(wrfout) -> str:
    return _digest(wrfout)


@pytest.fixture()
def catalog(tmp_path):
    cat = Catalog(tmp_path / "cube.duckdb")
    yield cat
    cat.close()


def _arrival_entry(wrfout: Path, digest: str) -> CubeEntry:
    """`arrival_s` as it really exists: TIGN_G on WRF's own fire mesh."""
    georeference = read_wrf_georeference(wrfout)
    rows, cols = georeference.valid_fire_slice()
    return CubeEntry.create(
        concept="arrival_s",
        kind="static",
        producer="wrf_sfire",
        content_sha256=digest,
        grid=georeference.fire,
        location=str(wrfout.resolve()),
        media_type="application/x-netcdf",
        detail={
            "source_variable": "TIGN_G",
            "units": "s",
            "description": "ignition time on ground",
            "domain": f"d0{georeference.domain_id}",
            "allocated_shape": list(georeference.allocated_fire_shape),
            "valid_slice": [[rows.start, rows.stop], [cols.start, cols.stop]],
            "subgrid_ratio": list(georeference.subgrid_ratio),
            "crs_proj4": georeference.proj4,
            "coordinate_residual_m": georeference.max_coordinate_residual_m,
        },
    )


# -- catalogue, then find and retrieve ------------------------------------


def test_real_fire_arrival_is_catalogued_and_retrievable(catalog, wrfout,
                                                         digest):
    entry = catalog.register_dataset(
        _arrival_entry(wrfout, digest), wrfout)

    found = catalog.resolve("arrival_s")
    assert found.entry_id == entry.entry_id
    assert found.media_type == "application/x-netcdf"
    # The catalogue answers "where is it", which is the point.
    assert Path(found.location).is_file()
    assert _digest(Path(found.location)) == found.content_sha256


def test_the_entry_keeps_wrfs_own_grid_untouched(catalog, wrfout, digest):
    catalog.register_dataset(_arrival_entry(wrfout, digest), wrfout)
    grid = catalog.resolve("arrival_s").grid
    # Lambert, not UTM; 90 m fire cells; nothing reprojected or resampled.
    assert grid.crs.startswith("WRF-LCC:")
    assert float(grid.affine[0]) == 90.0
    assert tuple(grid.shape) == (2130, 2130)


def test_the_detail_is_enough_to_read_the_variable_back(catalog, wrfout,
                                                        digest):
    """Discovery metadata must be sufficient to open the file and slice it."""
    netCDF4 = pytest.importorskip("netCDF4")
    catalog.register_dataset(_arrival_entry(wrfout, digest), wrfout)
    found = catalog.resolve("arrival_s")

    detail = found.detail
    (y0, y1), (x0, x1) = detail["valid_slice"]
    dataset = netCDF4.Dataset(found.location)
    try:
        array = dataset.variables[detail["source_variable"]][0][y0:y1, x0:x1]
    finally:
        dataset.close()
    assert array.shape == tuple(found.grid.shape)
    assert detail["units"] == "s"
    assert detail["allocated_shape"] == [2140, 2140]


# -- provenance across a cascade ------------------------------------------


def test_a_derived_product_records_what_it_consumed(catalog, wrfout, digest):
    """fire_area derived from the same run keeps a lineage edge to it."""
    arrival = catalog.register_dataset(_arrival_entry(wrfout, digest), wrfout)
    derived = catalog.register_dataset(
        CubeEntry.create(
            concept="burned_area_summary", kind="static", producer="analysis",
            content_sha256=digest, location=str(wrfout.resolve()),
            media_type="application/x-netcdf",
            inputs=(EntryInput("arrival_s", arrival.entry_id),),
            detail={"note": "derived from the catalogued arrival field"}),
        wrfout)
    assert derived.depth == 1
    lineage = catalog.lineage(derived.entry_id)
    assert [item.entry_id for item in lineage] == [arrival.entry_id]
    assert lineage[0].location == str(wrfout.resolve())


def test_baseline_and_derived_are_both_askable(catalog, wrfout, digest):
    arrival = catalog.register_dataset(_arrival_entry(wrfout, digest), wrfout)
    catalog.register_dataset(
        CubeEntry.create(
            concept="arrival_s", kind="static", producer="reanalysis_blend",
            content_sha256=digest, grid=arrival.grid,
            location=str(wrfout.resolve()), media_type="application/x-netcdf",
            inputs=(EntryInput("arrival_s", arrival.entry_id),)),
        wrfout)
    assert catalog.resolve(
        "arrival_s", ResolutionPolicy.BASELINE).entry_id == arrival.entry_id
    assert catalog.resolve("arrival_s").depth == 1
    assert len(catalog.datasets_for("arrival_s")) == 2


# -- the catalogue refuses to point at bytes it cannot vouch for ----------


def test_a_wrong_digest_is_refused(catalog, wrfout):
    bad = CubeEntry.create(
        concept="arrival_s", kind="static", producer="wrf_sfire",
        content_sha256="b" * 64, location=str(wrfout.resolve()),
        media_type="application/x-netcdf")
    with pytest.raises(ValueError, match="does not match its recorded content"):
        catalog.register_dataset(bad, wrfout)


def test_a_missing_file_is_refused(catalog, tmp_path):
    entry = CubeEntry.create(
        concept="arrival_s", kind="static", producer="wrf_sfire",
        content_sha256="c" * 64, location=str(tmp_path / "gone.nc"),
        media_type="application/x-netcdf")
    with pytest.raises(FileNotFoundError):
        catalog.register_dataset(entry, tmp_path / "gone.nc")


def test_an_entry_without_a_location_is_refused(catalog, wrfout, digest):
    entry = CubeEntry.create(
        concept="arrival_s", kind="static", producer="wrf_sfire",
        content_sha256=digest, media_type="application/x-netcdf")
    with pytest.raises(ValueError, match="must record its location"):
        catalog.register_dataset(entry, wrfout)
