"""Exact block aggregation as a declared Stage-4 transformation.

The bilinear kinds admit only `SCALAR_CONTINUOUS_INTENSIVE`, and their
docstring says why: "Categorical and extensive fields are outside the bilinear
MVP instead of being silently interpolated." That leaves the published fire
targets with no admissible transformation at all — an arrival time, an
extremum, an areal fraction and a category label are none of them
interpolatable.

They are, however, exactly *aggregatable* over a block partition. This kind
admits precisely that case and refuses everything else: same CRS, shared
origin, whole blocks, and an aggregation the value class itself determines.
"""
from __future__ import annotations

import dataclasses

import pytest

from contracts import (
    BBoxSupport,
    GridDescriptor,
    Missingness,
    MissingnessStatus,
    OriginClass,
    SpatialScale,
    TemporalKind,
    TemporalSupport,
)
from contracts.types import ArtifactDescriptor
from engine.runtime.operations import execute_component, operation_component
from stage4.fixtures import SCHEMA_VERSION, _profile
from transformations import (
    TransformationKind,
    TransformationPort,
    TransformationSpec,
    ValueSemantics,
)
from transformations.model import block_aggregation_for
from transformations.resampling import AggregationKind

CONCEPT = "example.fire.arrival"


def _grid(shape, cell, *, crs="EPSG:32614", origin=(0.0, 9000.0)):
    """Grid from outer upper-left edge; descriptor stores first centre."""
    return GridDescriptor(
        crs, ("easting", "northing"), shape,
        (repr(float(cell)), "0", repr(float(origin[0] + cell / 2)),
         "0", repr(-float(cell)), repr(float(origin[1] - cell / 2))),
        SpatialScale(repr(float(cell)), repr(float(cell)), "m"))


def _descriptor(grid, *, units="s", origin=OriginClass.SYNTHETIC):
    return ArtifactDescriptor(
        concept_id=CONCEPT,
        schema_version=SCHEMA_VERSION,
        representation="application/json",
        units=units,
        spatial_support=BBoxSupport(
            grid.crs, grid.axis_order, grid.support_bounds),
        temporal_support=TemporalSupport(TemporalKind.TIME_INVARIANT),
        vertical_support=None,
        grid=grid,
        native_resolution=None,
        origin=origin,
        missingness=Missingness(MissingnessStatus.COMPLETE),
        component_names=("value",),
    )


def _spec(*, block=3, aggregation=None,
          semantics=ValueSemantics.FIRST_OCCURRENCE_TIME,
          source_grid=None, result_grid=None, out_semantics=None):
    source_grid = source_grid or _grid((9, 9), 100.0)
    result_grid = result_grid or _grid((3, 3), 300.0)
    if aggregation is None:
        aggregation = block_aggregation_for(semantics).value
    return TransformationSpec.bind(
        transformation_id="block-aggregate-check",
        transformation_version="1.0.0",
        kind=TransformationKind.SPATIAL_BLOCK_AGGREGATE,
        execution_profile=_profile("transform.spatial_block_aggregate.v1"),
        input_ports=(TransformationPort(
            "source", _descriptor(source_grid), semantics),),
        output_ports=(TransformationPort(
            "result", _descriptor(result_grid, origin=OriginClass.DERIVED),
            out_semantics or semantics),),
        parameters={"block_x": block, "block_y": block,
                    "aggregation": aggregation},
        cost_units=1,
    )


# -- the declaration -----------------------------------------------------


def test_the_value_class_determines_the_aggregation():
    """A bijection: declare the semantics and the aggregation follows."""
    assert block_aggregation_for(ValueSemantics.FIRST_OCCURRENCE_TIME) \
        is AggregationKind.FIRST_OCCURRENCE_MIN
    assert block_aggregation_for(ValueSemantics.CATEGORICAL_LABEL) \
        is AggregationKind.CATEGORICAL_MAJORITY
    assert block_aggregation_for(ValueSemantics.SCALAR_EXTREMUM) \
        is AggregationKind.EXTREMUM_MAX


def test_an_undeclared_value_class_admits_no_aggregation():
    with pytest.raises(ValueError, match="no admissible block aggregation"):
        block_aggregation_for(ValueSemantics.UNSPECIFIED)
    with pytest.raises(ValueError, match="no admissible block aggregation"):
        block_aggregation_for(ValueSemantics.CANONICAL_UV_VECTOR)


def test_a_well_formed_block_aggregation_binds():
    spec = _spec()
    assert spec.kind is TransformationKind.SPATIAL_BLOCK_AGGREGATE
    assert spec.semantic_rule_id == "semantic:block-aggregation-registry-v1"
    assert spec.scientific_assumption_ids == (
        "grid:sample-centres-axis-aligned-v1",
        "partition:exact-integer-block-cover-v1",
    )
    assert spec.spec_id == spec.expected_id()


def test_the_parameter_cannot_override_the_declared_semantics():
    """The defect this guards: writing 'mean' next to an arrival time."""
    with pytest.raises(ValueError, match="admits only FIRST_OCCURRENCE_MIN"):
        _spec(aggregation="AREAL_FRACTION_MEAN")


def test_aggregation_must_preserve_the_value_class():
    with pytest.raises(ValueError, match="preserve the declared value class"):
        _spec(out_semantics=ValueSemantics.SCALAR_CONTINUOUS_INTENSIVE)


# -- the refusals --------------------------------------------------------


def test_it_refuses_to_change_crs():
    """Aggregation is an index operation; reprojection is a different claim."""
    with pytest.raises(ValueError, match="cannot change CRS"):
        _spec(result_grid=_grid((3, 3), 300.0, crs="EPSG:32610"))


def test_a_partial_trailing_block_is_refused():
    """10 does not divide into 3x3 blocks; the edge cell is incomplete."""
    with pytest.raises(ValueError, match="exactly cover"):
        _spec(source_grid=_grid((10, 10), 100.0))


def test_cell_sizes_must_agree_with_the_block_factors():
    """Right cell counts, wrong ground."""
    with pytest.raises(ValueError, match="disagree with the declared cell"):
        _spec(result_grid=_grid((3, 3), 500.0))


def test_an_offset_lattice_is_refused():
    with pytest.raises(ValueError, match="exact block centre"):
        _spec(result_grid=_grid((3, 3), 300.0, origin=(50.0, 9000.0)))


# -- execution -----------------------------------------------------------


def _field(values):
    size = len(values)
    return {
        "schema": "field-json-v2",
        "crs": "EPSG:32614",
        "axis_order": ["easting", "northing"],
        "x": [float(index) for index in range(size)],
        "y": [float(index) for index in range(size)],
        "time": ["2019-09-04T12:00:00Z"],
        "components": {"value": [values]},
    }


def _run(parameters, field):
    component = operation_component("transform.spatial_block_aggregate.v1")
    return execute_component(component, parameters, {"source": field})["result"]


def test_arrival_time_takes_the_earliest_in_each_block():
    values = [[float(row * 4 + col) for col in range(4)] for row in range(4)]
    result = _run({"block_x": 2, "block_y": 2,
                   "aggregation": "FIRST_OCCURRENCE_MIN"}, _field(values))
    # Blocks are [[0,1],[4,5]] -> 0, [[2,3],[6,7]] -> 2, and so on.
    assert result["components"]["value"][0] == [[0.0, 2.0], [8.0, 10.0]]


def test_an_extremum_takes_the_largest():
    values = [[float(row * 4 + col) for col in range(4)] for row in range(4)]
    result = _run({"block_x": 2, "block_y": 2,
                   "aggregation": "EXTREMUM_MAX"}, _field(values))
    assert result["components"]["value"][0] == [[5.0, 7.0], [13.0, 15.0]]


def test_a_fraction_takes_the_plain_mean_over_an_exact_cover():
    values = [[0.0, 1.0, 1.0, 1.0], [1.0, 0.0, 1.0, 1.0],
              [0.0, 0.0, 0.0, 0.0], [0.0, 0.0, 1.0, 0.0]]
    result = _run({"block_x": 2, "block_y": 2,
                   "aggregation": "AREAL_FRACTION_MEAN"}, _field(values))
    assert result["components"]["value"][0] == [[0.5, 1.0], [0.0, 0.25]]


def test_decimal_block_centres_match_the_exact_descriptor_lattice():
    field = _field([[1.0, 2.0, 3.0, 4.0],
                    [5.0, 6.0, 7.0, 8.0]])
    field["x"] = [0.1, 0.2, 0.3, 0.4]
    field["y"] = [1.1, 1.2]
    result = _run({"block_x": 2, "block_y": 2,
                   "aggregation": "AREAL_FRACTION_MEAN"}, field)

    assert result["x"] == [0.15, 0.35]
    assert result["y"] == [1.15]


def test_a_category_takes_the_majority_and_never_an_average():
    values = [[2.0, 2.0, 9.0, 9.0], [2.0, 5.0, 9.0, 4.0],
              [1.0, 1.0, 3.0, 3.0], [1.0, 7.0, 3.0, 3.0]]
    result = _run({"block_x": 2, "block_y": 2,
                   "aggregation": "CATEGORICAL_MAJORITY"}, _field(values))
    aggregated = result["components"]["value"][0]
    assert aggregated == [[2.0, 9.0], [1.0, 3.0]]
    # Every result is a label that actually occurred in its block.
    assert all(value in (1.0, 2.0, 3.0, 9.0) for row in aggregated
               for value in row)


def test_a_majority_tie_resolves_deterministically():
    values = [[4.0, 4.0], [7.0, 7.0]]
    first = _run({"block_x": 2, "block_y": 2,
                  "aggregation": "CATEGORICAL_MAJORITY"}, _field(values))
    second = _run({"block_x": 2, "block_y": 2,
                   "aggregation": "CATEGORICAL_MAJORITY"}, _field(values))
    assert first == second
    assert first["components"]["value"][0] == [[4.0]]


def test_execution_refuses_a_partial_block():
    values = [[1.0, 2.0, 3.0]] * 3
    with pytest.raises(ValueError, match="exactly cover"):
        _run({"block_x": 2, "block_y": 2,
              "aggregation": "EXTREMUM_MAX"}, _field(values))


def test_the_output_axes_are_block_centres():
    values = [[float(index) for index in range(4)] for _ in range(4)]
    result = _run({"block_x": 2, "block_y": 2,
                   "aggregation": "EXTREMUM_MAX"}, _field(values))
    assert result["components"]["value"][0][0] == [1.0, 3.0]
    assert result["x"] == [0.5, 2.5]
    assert result["y"] == [0.5, 2.5]


def test_execution_is_deterministic():
    values = [[float((row * 7 + col) % 5) for col in range(6)]
              for row in range(6)]
    field = _field(values)
    parameters = {"block_x": 3, "block_y": 3,
                  "aggregation": "AREAL_FRACTION_MEAN"}
    assert _run(parameters, field) == _run(parameters, field)


def test_an_unknown_aggregation_is_refused_at_execution():
    with pytest.raises(ValueError, match="unknown block aggregation"):
        _run({"block_x": 2, "block_y": 2, "aggregation": "MEDIAN"},
             _field([[1.0, 2.0], [3.0, 4.0]]))
