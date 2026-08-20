"""The producer half of the 253/1001 fix, checked against real WRF output.

Two lanes:

- **Real files.** `wrf-sfire-stack/.../wrfout_d0?_2019-09-04_12:00:00` are real
  WRF-SFIRE output. They are gitignored (131 MB each), so these tests skip when
  absent. Everything they assert was read off the files, not assumed.
- **Synthetic.** A consistent fake wrfout exercises every refusal path, and
  runs everywhere.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from pyproj import CRS, Transformer

from contracts.placement import PlacementStatus, assess_placement
from cube.grid import SimulationGrid
from cube.preflight import grid_descriptor
from models.wrf_georeference import (
    WRF_EARTH_RADIUS_M,
    WrfGeoreferenceError,
    WrfGeoreferenceUnverified,
    crs_token,
    georeference_from_dataset,
    proj4_string,
    read_wrf_georeference,
)

WRFOUT = Path("wrf-sfire-stack/WRF-SFIRE/test/em_real")


def _real(domain: str) -> Path:
    path = WRFOUT / f"wrfout_{domain}_2019-09-04_12:00:00"
    if not path.exists():
        pytest.skip(f"{path} absent (gitignored, 131 MB)")
    return path


# -- a consistent synthetic wrfout ---------------------------------------


class _Dim:
    def __init__(self, size: int) -> None:
        self._size = size

    def __len__(self) -> int:
        return self._size


class _Dataset:
    def __init__(self, attrs: dict, variables: dict, dimensions: dict) -> None:
        self._attrs = attrs
        self.variables = variables
        self.dimensions = dimensions

    def ncattrs(self):
        return list(self._attrs)

    def getncattr(self, name):
        return self._attrs[name]


def _synthetic(*, cols: int = 21, rows: int = 21, cell: float = 900.0,
               sr: int | None = 10, map_proj: int = 1, dy: float | None = None,
               subgrid: tuple[int, int] | None = None,
               stretch_lat_deg: float = 0.0) -> _Dataset:
    """A wrfout whose XLONG/XLAT are consistent with its own attributes."""
    truelat, stand_lon, cen_lat = 32.78, -96.808891, 32.779987
    attrs = {
        "MAP_PROJ": map_proj, "TRUELAT1": truelat, "TRUELAT2": truelat,
        "MOAD_CEN_LAT": cen_lat, "STAND_LON": stand_lon,
        "DX": cell, "DY": cell if dy is None else dy, "GRID_ID": 3,
        "WEST-EAST_GRID_DIMENSION": cols + 1,
        "SOUTH-NORTH_GRID_DIMENSION": rows + 1,
    }
    crs = CRS.from_proj4(proj4_string(attrs))
    inverse = Transformer.from_crs(crs, 4326, always_xy=True)
    xs = -5_000.0 + (np.arange(cols) + 0.5) * cell
    ys = -6_000.0 + (np.arange(rows) + 0.5) * cell
    grid_x, grid_y = np.meshgrid(xs, ys)
    lon, lat = inverse.transform(grid_x, grid_y)
    # A gradient, not a translation: a uniform offset would simply be absorbed
    # into the derived origin, so it would prove nothing about verification.
    ramp = np.linspace(0.0, stretch_lat_deg, rows)[:, None]
    variables = {"XLONG": np.asarray(lon)[None, ...],
                 "XLAT": np.asarray(lat)[None, ...] + ramp}
    dimensions = {"west_east": _Dim(cols), "south_north": _Dim(rows)}
    if subgrid is not None:
        dimensions["south_north_subgrid"] = _Dim(subgrid[0])
        dimensions["west_east_subgrid"] = _Dim(subgrid[1])
    elif sr is not None:
        dimensions["south_north_subgrid"] = _Dim((rows + 1) * sr)
        dimensions["west_east_subgrid"] = _Dim((cols + 1) * sr)
        fire_cell = cell / sr
        fire_xs = -5_000.0 + (np.arange((cols + 1) * sr) + 0.5) * fire_cell
        fire_ys = -6_000.0 + (np.arange((rows + 1) * sr) + 0.5) * fire_cell
        fire_x, fire_y = np.meshgrid(fire_xs, fire_ys)
        fire_lon, fire_lat = inverse.transform(fire_x, fire_y)
        variables["FXLONG"] = np.asarray(fire_lon)[None, ...]
        variables["FXLAT"] = np.asarray(fire_lat)[None, ...]
    return _Dataset(attrs, variables, dimensions)


def _replay_coordinates(grid, projection: str):
    """Coordinate fields obtained from the descriptor's exact affine."""
    inverse = Transformer.from_crs(
        CRS.from_proj4(projection), 4326, always_xy=True)
    xs = float(grid.affine[2]) + np.arange(grid.shape[1]) * float(grid.affine[0])
    ys = float(grid.affine[5]) + np.arange(grid.shape[0]) * float(grid.affine[4])
    return inverse.transform(*np.meshgrid(xs, ys))


# -- real WRF output -----------------------------------------------------


def test_the_real_innermost_domain_declares_a_verified_grid():
    georeference = read_wrf_georeference(_real("d03"))
    assert tuple(georeference.atmospheric.shape) == (213, 213)
    # The derived affine reproduces WRF's own XLONG/XLAT closely.
    assert georeference.max_coordinate_residual_m < 10.0


def test_the_real_fire_subgrid_excludes_the_staggered_padding():
    """west_east_subgrid is 2140 = 214 x 10, but only 2130 columns hold data.

    FXLONG is exactly 0.0 in the trailing strip. Reducing the allocated 2140
    would fold that padding into the answer.
    """
    georeference = read_wrf_georeference(_real("d03"))
    assert georeference.allocated_fire_shape == (2140, 2140)
    assert georeference.valid_fire_shape == (2130, 2130)
    assert georeference.subgrid_ratio == (10, 10)
    rows, cols = georeference.valid_fire_slice()
    assert (rows.stop, cols.stop) == (2130, 2130)


def test_the_fire_state_variables_end_exactly_at_the_declared_extent():
    """TIGN_G and LFN are zero beyond 2130, confirming the declared extent."""
    netCDF4 = pytest.importorskip("netCDF4")
    path = _real("d03")
    georeference = read_wrf_georeference(path)
    dataset = netCDF4.Dataset(str(path))
    try:
        state = {name: np.asarray(dataset.variables[name][0])
                 for name in ("TIGN_G", "LFN")}
    finally:
        dataset.close()
    rows, cols = georeference.valid_fire_slice()
    for name, array in state.items():
        assert np.all(array[:, cols.stop:] == 0.0), name
        assert np.all(array[rows.stop:, :] == 0.0), name
        assert np.any(array[rows, cols] != 0.0), name


def test_three_fire_variables_disagree_about_their_own_extent():
    """Why an array's shape can never establish its grid.

    In one real file, on one allocated (2140, 2140) subgrid:

    - `TIGN_G` / `LFN` hold data to 2130 -- the true 213 x 10 fire extent;
    - `FXLONG` runs one halo column further, to 2131;
    - `NFUEL_CAT` is populated across the entire allocated array, padding
      included, because it is an ingested input rather than fire state.

    A block-reduce of the full 2140 therefore folds ten columns of padded fuel
    into the answer, silently. Only the declared grid settles this.
    """
    netCDF4 = pytest.importorskip("netCDF4")
    path = _real("d03")
    georeference = read_wrf_georeference(path)
    dataset = netCDF4.Dataset(str(path))
    try:
        extents = {}
        for name in ("TIGN_G", "FXLONG", "NFUEL_CAT"):
            array = np.asarray(dataset.variables[name][0])
            populated = (array != 0).any(axis=0)
            extents[name] = int(np.max(np.nonzero(populated)[0])) + 1
    finally:
        dataset.close()
    rows, cols = georeference.valid_fire_slice()
    assert extents["TIGN_G"] == cols.stop == 2130
    assert extents["FXLONG"] == 2131
    assert extents["NFUEL_CAT"] == georeference.allocated_fire_shape[1] == 2140
    # Three different answers from three arrays on one grid.
    assert len(set(extents.values())) == 3


def test_the_real_fire_mesh_is_a_tenth_of_the_atmospheric_cell():
    georeference = read_wrf_georeference(_real("d03"))
    assert float(georeference.atmospheric.affine[0]) == 900.0
    assert float(georeference.fire.affine[0]) == 90.0


def test_the_real_fire_affine_replays_native_row_coordinates():
    """The fire descriptor indexes the same cells as FXLONG/FXLAT.

    Sampling both y extremes makes this a regression for a vertical mirror,
    while avoiding allocation of another full 2130x2130 coordinate field.
    """
    netCDF4 = pytest.importorskip("netCDF4")
    path = _real("d03")
    georeference = read_wrf_georeference(path)
    dataset = netCDF4.Dataset(str(path))
    inverse = Transformer.from_crs(
        CRS.from_proj4(georeference.proj4), 4326, always_xy=True)
    samples = ((0, 0), (0, 2129), (1065, 1065),
               (2129, 0), (2129, 2129))
    try:
        native_lon = dataset.variables["FXLONG"][0]
        native_lat = dataset.variables["FXLAT"][0]
        for row, col in samples:
            x = (float(georeference.fire.affine[2])
                 + col * float(georeference.fire.affine[0]))
            y = (float(georeference.fire.affine[5])
                 + row * float(georeference.fire.affine[4]))
            replay_lon, replay_lat = inverse.transform(x, y)
            metres_per_degree = np.pi * WRF_EARTH_RADIUS_M / 180.0
            dy = (replay_lat - float(native_lat[row, col])) * metres_per_degree
            dx = ((replay_lon - float(native_lon[row, col]))
                  * metres_per_degree
                  * np.cos(np.radians(float(native_lat[row, col]))))
            assert np.hypot(dx, dy) < 10.0
    finally:
        dataset.close()


def test_the_real_outer_domains_carry_no_fire_grid():
    """Fire lives only on the innermost nest; nothing is invented for d01/d02."""
    for domain, shape in (("d01", (148, 148)), ("d02", (177, 177))):
        georeference = read_wrf_georeference(_real(domain))
        assert tuple(georeference.atmospheric.shape) == shape
        assert georeference.fire is None
        assert georeference.subgrid_ratio is None


def test_every_real_nest_shares_one_crs_identity():
    """Nests differ in resolution and extent, never in projection."""
    tokens = {read_wrf_georeference(_real(d)).atmospheric.crs
              for d in ("d01", "d02", "d03")}
    assert len(tokens) == 1
    assert tokens.pop().startswith("WRF-LCC:")


# -- what this means for publication -------------------------------------


def test_real_wrf_output_does_not_place_onto_a_utm_cube():
    """The finding: the blocker is the projection, not the shape.

    WRF writes a domain-centred Lambert Conformal on a 6,370 km sphere. The
    analysis cube is UTM. No reshape reconciles that, and the old shape check
    could never have said so.
    """
    georeference = read_wrf_georeference(_real("d03"))
    cube = grid_descriptor(SimulationGrid(
        crs_epsg=32614, pixel_m=900.0, width=1001, height=1001,
        x0=0.0, y1=900_900.0))
    for source in (georeference.fire, georeference.atmospheric):
        assessment = assess_placement(source, cube)
        assert assessment.status is PlacementStatus.CRS_MISMATCH
        assert not assessment.placeable


def test_the_geometry_itself_was_never_the_problem():
    """Declared in WRF's own CRS, the 90 m fire mesh places exactly.

    900/90 = 10, aligned and whole-blocked. So the fire mesh always could have
    been published; what was missing was a declared reprojection.
    """
    georeference = read_wrf_georeference(_real("d03"))
    atmospheric = georeference.atmospheric
    cube = type(atmospheric)(
        atmospheric.crs, ("easting", "northing"), (213, 213),
        atmospheric.affine, atmospheric.spacing)
    assessment = assess_placement(georeference.fire, cube)
    assert assessment.status is PlacementStatus.INTEGER_REFINEMENT
    assert assessment.placeable
    assert (assessment.refinement_x, assessment.refinement_y) == (10, 10)


def test_an_array_shape_resolves_to_the_grid_it_is_on():
    georeference = read_wrf_georeference(_real("d03"))
    assert georeference.describes((213, 213)) is georeference.atmospheric
    assert georeference.describes((2130, 2130)) is georeference.fire
    # The allocated shape is not a declared grid, so it is refused, not guessed.
    assert georeference.describes((2140, 2140)) is None
    assert georeference.describes((253, 253)) is None


# -- synthetic: the refusal paths ----------------------------------------


def test_a_consistent_synthetic_file_verifies():
    georeference = georeference_from_dataset(_synthetic())
    assert tuple(georeference.atmospheric.shape) == (21, 21)
    assert georeference.valid_fire_shape == (210, 210)
    assert georeference.subgrid_ratio == (10, 10)
    assert georeference.max_coordinate_residual_m < 1.0


def test_atmospheric_affine_replays_native_array_coordinates():
    dataset = _synthetic()
    georeference = georeference_from_dataset(dataset)
    replay_lon, replay_lat = _replay_coordinates(
        georeference.atmospheric, georeference.proj4)
    np.testing.assert_allclose(replay_lon, dataset.variables["XLONG"][0],
                               atol=1e-9)
    np.testing.assert_allclose(replay_lat, dataset.variables["XLAT"][0],
                               atol=1e-9)
    assert float(georeference.atmospheric.affine[4]) > 0


def test_fire_affine_replays_native_array_coordinates():
    dataset = _synthetic()
    georeference = georeference_from_dataset(dataset)
    replay_lon, replay_lat = _replay_coordinates(
        georeference.fire, georeference.proj4)
    rows, cols = georeference.valid_fire_slice()
    np.testing.assert_allclose(
        replay_lon, dataset.variables["FXLONG"][0, rows, cols], atol=1e-9)
    np.testing.assert_allclose(
        replay_lat, dataset.variables["FXLAT"][0, rows, cols], atol=1e-9)
    assert float(georeference.fire.affine[4]) > 0


def test_a_grid_that_misses_the_files_own_coordinates_is_refused():
    """Verification is the point: a plausible-looking grid that does not
    reproduce WRF's coordinates is an error, not an approximation."""
    with pytest.raises(WrfGeoreferenceUnverified, match="XLONG/XLAT"):
        georeference_from_dataset(_synthetic(stretch_lat_deg=0.01))


def test_a_uniform_offset_is_absorbed_because_the_origin_is_derived():
    """The complement: origin comes from the file, so a translated domain is
    described correctly rather than rejected."""
    georeference = georeference_from_dataset(_synthetic())
    assert georeference.max_coordinate_residual_m < 1.0


def test_an_unmodelled_projection_is_refused():
    with pytest.raises(WrfGeoreferenceError, match="MAP_PROJ"):
        georeference_from_dataset(_synthetic(map_proj=99))


def test_anisotropic_cells_are_refused():
    with pytest.raises(WrfGeoreferenceError, match="anisotropic"):
        georeference_from_dataset(_synthetic(dy=450.0))


def test_a_subgrid_that_is_not_an_integer_refinement_is_refused():
    with pytest.raises(WrfGeoreferenceError, match="integer multiple"):
        georeference_from_dataset(_synthetic(subgrid=(215, 215)))


def test_a_file_without_coordinates_is_refused():
    dataset = _synthetic()
    del dataset.variables["XLAT"]
    with pytest.raises(WrfGeoreferenceError, match="XLAT"):
        georeference_from_dataset(dataset)


def test_a_domain_without_a_subgrid_reports_no_fire_grid():
    georeference = georeference_from_dataset(_synthetic(sr=None))
    assert georeference.fire is None
    with pytest.raises(WrfGeoreferenceError, match="no fire subgrid"):
        georeference.valid_fire_slice()


# -- the CRS token -------------------------------------------------------


def test_the_crs_token_is_whitespace_free_and_deterministic():
    """`canonical_crs` refuses whitespace, so proj4 cannot be the identity."""
    token = crs_token(_synthetic()._attrs)
    assert not any(char.isspace() for char in token)
    assert token == crs_token(_synthetic()._attrs)


def test_the_crs_token_is_not_confusable_with_an_authority_code():
    token = crs_token(_synthetic()._attrs)
    assert not token.upper().startswith("EPSG")
    assert str(WRF_EARTH_RADIUS_M).split(".")[0] in token


def test_float32_jitter_does_not_split_one_projection_in_two():
    """Projection attributes are float32; rounding keeps nests together."""
    first = _synthetic()._attrs
    second = dict(first)
    second["TRUELAT1"] = first["TRUELAT1"] + 1e-9
    second["STAND_LON"] = first["STAND_LON"] - 1e-9
    assert crs_token(first) == crs_token(second)


def test_a_genuinely_different_projection_gets_a_different_token():
    first = _synthetic()._attrs
    second = dict(first)
    second["STAND_LON"] = first["STAND_LON"] + 5.0
    assert crs_token(first) != crs_token(second)
