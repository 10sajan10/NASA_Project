"""WRF configured-domain checks that run without WRF/MPI/Slurm."""
from datetime import datetime

from models.wrf_config import WRFScenario
from models.wrf_publication_preflight import (
    ConfiguredPublicationStatus,
    ConfiguredWrfGridKind,
    PlannedWrfPublication,
    configured_grid_metadata,
    preflight_configured_wrf_publications,
)


def _scenario():
    return WRFScenario.from_simple(
        center_lon=-96.81,
        center_lat=32.78,
        start=datetime(2019, 9, 4, 12),
        extent_km=1000,
        resolutions_m=[9000, 3000, 1000],
        nest_fraction=0.5,
        fire_mesh_ratio=10,
    )


def test_configured_shapes_are_known_without_starting_wrf():
    scenario = _scenario()
    domain = scenario.domains[-1]
    atmospheric = configured_grid_metadata(
        scenario, 3, ConfiguredWrfGridKind.ATMOSPHERIC_MASS)
    fire = configured_grid_metadata(
        scenario, 3, ConfiguredWrfGridKind.FIRE_VALID)
    assert atmospheric.shape == (domain.ny, domain.nx)
    assert atmospheric.spacing_m == 1000
    assert fire.shape == (domain.ny * 10, domain.nx * 10)
    assert fire.spacing_m == 100
    assert not atmospheric.georeference_established
    assert not fire.georeference_established


def test_shape_mismatch_and_unconfigured_fire_grid_fail_preflight():
    result = preflight_configured_wrf_publications(_scenario(), (
        PlannedWrfPublication(
            "arrival", 3, ConfiguredWrfGridKind.FIRE_VALID, (253, 253)),
        PlannedWrfPublication(
            "outer-fire", 1, ConfiguredWrfGridKind.FIRE_VALID, (10, 10)),
    ))
    assert not result.dimensions_ok
    assert [item.status for item in result.assessments] == [
        ConfiguredPublicationStatus.DIMENSIONS_MISMATCH,
        ConfiguredPublicationStatus.FIRE_GRID_NOT_CONFIGURED,
    ]
    assert not result.placement_established


def test_matching_dimensions_never_claim_geospatial_placement():
    scenario = _scenario()
    expected = configured_grid_metadata(
        scenario, 3, ConfiguredWrfGridKind.FIRE_VALID)
    result = preflight_configured_wrf_publications(scenario, (
        PlannedWrfPublication(
            "arrival", 3, ConfiguredWrfGridKind.FIRE_VALID, expected.shape),
    ))
    assert result.dimensions_ok
    assert not result.placement_established
    assert not result.assessments[0].placement_established

