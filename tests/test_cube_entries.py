"""The cascade the cube exists to serve, and the questions a slot cannot answer.

The scenario throughout is the one the cube was designed for: temperature is
ingested from ERA5, a fire model perturbs it, and a downstream impacts model
asks for "temperature". Under last-writer-wins that request has one possible
answer and no way to ask for any other.
"""
from __future__ import annotations

import hashlib

import pytest

from contracts.types import GridDescriptor, SpatialScale
from cube.catalog import Catalog
from cube.entries import (
    CubeEntry,
    EntryInput,
    EntryNotFound,
    ResolutionPolicy,
)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _grid(cell=900.0, crs="EPSG:32614"):
    return GridDescriptor(
        crs, ("easting", "northing"), (10, 10),
        (repr(cell), "0", "0.0", "0", repr(-cell), "9000.0"),
        SpatialScale(repr(cell), repr(cell), "m"))


@pytest.fixture()
def catalog(tmp_path):
    cat = Catalog(tmp_path / "catalog.duckdb")
    yield cat
    cat.close()


def _cascade(catalog):
    """ERA5 temperature -> fire model perturbs it -> impacts model consumes."""
    baseline = catalog._commit_entry_for_test_fixture(CubeEntry.create(
        concept="temperature", kind="static", producer="era5",
        content_sha256=_sha("era5-baseline"), grid=_grid()))
    perturbed = catalog._commit_entry_for_test_fixture(CubeEntry.create(
        concept="temperature", kind="static", producer="wrf_sfire",
        content_sha256=_sha("fire-perturbed"), grid=_grid(),
        inputs=(EntryInput("temperature", baseline.entry_id),)))
    return baseline, perturbed


# -- the thing a slot cannot do ------------------------------------------


def test_both_temperatures_coexist(catalog):
    baseline, perturbed = _cascade(catalog)
    assert baseline.entry_id != perturbed.entry_id
    assert {item.entry_id for item in catalog.entries_for("temperature")} == {
        baseline.entry_id, perturbed.entry_id}


def test_latest_available_is_the_most_derived_not_the_last_written(catalog):
    baseline, perturbed = _cascade(catalog)
    assert catalog.resolve("temperature").entry_id == perturbed.entry_id
    assert catalog.resolve(
        "temperature", ResolutionPolicy.MOST_DERIVED).depth == 1


def test_the_unperturbed_value_is_still_askable(catalog):
    """'What would it have been without the fire?' — unanswerable before."""
    baseline, _ = _cascade(catalog)
    assert catalog.resolve(
        "temperature", ResolutionPolicy.BASELINE).entry_id == baseline.entry_id


def test_write_order_does_not_decide_the_answer(catalog):
    """Committing the baseline last must not make it win."""
    _, perturbed = _cascade(catalog)
    catalog._commit_entry_for_test_fixture(CubeEntry.create(
        concept="temperature", kind="static", producer="era5-refetch",
        content_sha256=_sha("era5-again"), grid=_grid()))
    assert catalog.resolve("temperature").entry_id == perturbed.entry_id


def test_a_consumer_can_reconstruct_what_it_consumed(catalog):
    baseline, perturbed = _cascade(catalog)
    impact = catalog._commit_entry_for_test_fixture(CubeEntry.create(
        concept="population_affected", kind="static", producer="impacts",
        content_sha256=_sha("impact"), grid=_grid(),
        inputs=(EntryInput("temperature", perturbed.entry_id),)))
    lineage = {item.entry_id for item in catalog.lineage(impact.entry_id)}
    assert lineage == {perturbed.entry_id, baseline.entry_id}
    assert impact.depth == 2


# -- immutability --------------------------------------------------------


def test_committing_the_same_derivation_twice_is_one_entry(catalog):
    baseline, _ = _cascade(catalog)
    again = catalog._commit_entry_for_test_fixture(CubeEntry.create(
        concept="temperature", kind="static", producer="era5",
        content_sha256=_sha("era5-baseline"), grid=_grid()))
    assert again.entry_id == baseline.entry_id
    assert len(catalog.entries_for("temperature")) == 2


def test_identity_covers_content_and_derivation(catalog):
    baseline, perturbed = _cascade(catalog)
    # Same bytes, different derivation -> a different entry.
    same_bytes_no_input = CubeEntry.create(
        concept="temperature", kind="static", producer="wrf_sfire",
        content_sha256=_sha("fire-perturbed"), grid=_grid())
    assert same_bytes_no_input.entry_id != perturbed.entry_id


def test_an_entry_id_cannot_be_forged():
    with pytest.raises(ValueError, match="identity does not verify"):
        CubeEntry("0" * 64, "temperature", "static", "era5", _sha("x"),
                  None, 0, ())


def test_an_edge_cannot_point_at_nothing(catalog):
    with pytest.raises(EntryNotFound, match="cannot point at nothing"):
        catalog._commit_entry_for_test_fixture(CubeEntry.create(
            concept="temperature", kind="static", producer="ghost",
            content_sha256=_sha("t"),
            inputs=(EntryInput("temperature", _sha("missing")),)))


# -- staleness, content-based rather than clock-based --------------------


def test_an_entry_is_current_when_its_inputs_are(catalog):
    _, perturbed = _cascade(catalog)
    assert not catalog.is_entry_stale(perturbed.entry_id)
    assert catalog.stale_inputs(perturbed.entry_id) == {}


def test_a_deeper_input_makes_a_consumer_stale(catalog):
    baseline, perturbed = _cascade(catalog)
    impact = catalog._commit_entry_for_test_fixture(CubeEntry.create(
        concept="population_affected", kind="static", producer="impacts",
        content_sha256=_sha("impact"),
        inputs=(EntryInput("temperature", baseline.entry_id),)))
    # It consumed the baseline, but a perturbed temperature now exists.
    drift = catalog.stale_inputs(impact.entry_id)
    assert drift == {"temperature": (baseline.entry_id, perturbed.entry_id)}


def test_refetching_identical_bytes_does_not_invalidate_downstream(catalog):
    """The wall-clock heuristic's worst failure, and the reason for this table.

    `is_output_stale` compares `fetched_at`, so re-fetching identical bytes
    marks everything downstream stale — potentially a 48-hour recompute for a
    file that did not change.
    """
    _, perturbed = _cascade(catalog)
    catalog._commit_entry_for_test_fixture(CubeEntry.create(
        concept="temperature", kind="static", producer="era5",
        content_sha256=_sha("era5-baseline"), grid=_grid()))
    assert not catalog.is_entry_stale(perturbed.entry_id)


def test_a_perturbing_model_is_not_stale_against_its_own_output(catalog):
    """The subtlest case in the design.

    The fire model both *consumes* temperature and *produces* temperature. So
    when asking whether its input is current, the most-derived temperature is
    its own output. Without excluding itself and its descendants it would be
    permanently stale with respect to itself, and every cascade would rerun
    forever.
    """
    baseline, perturbed = _cascade(catalog)
    assert perturbed.concept == baseline.concept == "temperature"
    assert catalog.resolve("temperature").entry_id == perturbed.entry_id
    assert not catalog.is_entry_stale(perturbed.entry_id)
    assert perturbed.entry_id in catalog.dependents(baseline.entry_id)


def test_dependents_are_transitive(catalog):
    baseline, perturbed = _cascade(catalog)
    impact = catalog._commit_entry_for_test_fixture(CubeEntry.create(
        concept="population_affected", kind="static", producer="impacts",
        content_sha256=_sha("impact"),
        inputs=(EntryInput("temperature", perturbed.entry_id),)))
    assert catalog.dependents(baseline.entry_id) == frozenset(
        {baseline.entry_id, perturbed.entry_id, impact.entry_id})
    # A leaf depends on nothing but itself.
    assert catalog.dependents(impact.entry_id) == frozenset({impact.entry_id})


def test_a_second_perturbation_makes_the_first_consumer_stale(catalog):
    """Two fire models in sequence: the impacts model must notice."""
    baseline, perturbed = _cascade(catalog)
    impact = catalog._commit_entry_for_test_fixture(CubeEntry.create(
        concept="population_affected", kind="static", producer="impacts",
        content_sha256=_sha("impact"),
        inputs=(EntryInput("temperature", perturbed.entry_id),)))
    assert not catalog.is_entry_stale(impact.entry_id)
    catalog._commit_entry_for_test_fixture(CubeEntry.create(
        concept="temperature", kind="static", producer="smoke_model",
        content_sha256=_sha("smoke-perturbed"), grid=_grid(),
        inputs=(EntryInput("temperature", perturbed.entry_id),)))
    assert catalog.is_entry_stale(impact.entry_id)


# -- per-entry grids -----------------------------------------------------


def test_an_entry_keeps_its_own_grid(catalog):
    """A Lambert intermediate stays Lambert; nothing is reprojected to store."""
    lambert = _grid(cell=90.0, crs="WRF-LCC:lat_1=32.78:lon_0=-96.8")
    entry = catalog._commit_entry_for_test_fixture(CubeEntry.create(
        concept="arrival_s", kind="static", producer="wrf_sfire",
        content_sha256=_sha("tign"), grid=lambert))
    stored = catalog.entry(entry.entry_id)
    assert stored.grid.crs == lambert.crs
    assert float(stored.grid.affine[0]) == 90.0


def test_two_entries_of_one_concept_may_differ_in_grid(catalog):
    catalog._commit_entry_for_test_fixture(CubeEntry.create(
        concept="temperature", kind="static", producer="era5",
        content_sha256=_sha("a"), grid=_grid(cell=900.0)))
    catalog._commit_entry_for_test_fixture(CubeEntry.create(
        concept="temperature", kind="static", producer="wrf",
        content_sha256=_sha("b"),
        grid=_grid(cell=90.0, crs="WRF-LCC:lat_1=32.78")))
    grids = {item.grid.crs for item in catalog.entries_for("temperature")}
    assert len(grids) == 2


def test_an_entry_may_declare_no_grid(catalog):
    entry = catalog._commit_entry_for_test_fixture(CubeEntry.create(
        concept="scalar_index", kind="static", producer="calc",
        content_sha256=_sha("s")))
    assert catalog.entry(entry.entry_id).grid is None


# -- resolution behaviour ------------------------------------------------


def test_resolution_is_deterministic_across_ties(catalog):
    for index in range(4):
        catalog._commit_entry_for_test_fixture(CubeEntry.create(
            concept="temperature", kind="static", producer=f"p{index}",
            content_sha256=_sha(f"v{index}"), grid=_grid()))
    first = catalog.resolve("temperature").entry_id
    assert all(catalog.resolve("temperature").entry_id == first
               for _ in range(5))


def test_an_absent_concept_is_refused_not_invented(catalog):
    with pytest.raises(EntryNotFound, match="no committed entry"):
        catalog.resolve("humidity")


def test_resolution_requires_a_typed_policy(catalog):
    _cascade(catalog)
    with pytest.raises(TypeError, match="typed ResolutionPolicy"):
        catalog.resolve("temperature", "MOST_DERIVED")


# -- migration -----------------------------------------------------------


def test_the_schema_is_v3_and_the_old_tables_survive(catalog):
    assert catalog.schema_version() == 3
    assert catalog.list_variables() == []
    catalog.register_variable("temperature", "static", units="K")
    assert catalog.get_variable("temperature")["units"] == "K"


def test_reopening_an_existing_cube_keeps_its_entries(tmp_path):
    path = tmp_path / "catalog.duckdb"
    first = Catalog(path)
    entry = first._commit_entry_for_test_fixture(CubeEntry.create(
        concept="temperature", kind="static", producer="era5",
        content_sha256=_sha("era5"), grid=_grid()))
    first.close()
    second = Catalog(path)
    try:
        assert second.entry(entry.entry_id).producer == "era5"
    finally:
        second.close()
