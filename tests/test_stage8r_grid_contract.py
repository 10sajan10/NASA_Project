"""Stage 8R-C: one grid convention from declaration through execution."""
from __future__ import annotations

import copy
import dataclasses
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from acquisition import AssemblyMode, SourceSchema
from contracts import (
    ArtifactDescriptor,
    BBoxSupport,
    GRID_AFFINE_CONVENTION,
    GridDescriptor,
    Missingness,
    MissingnessStatus,
    OriginClass,
    SampleSemantics,
    ScaleBasis,
    SpatialScale,
    TemporalKind,
    TemporalSupport,
)
from engine.runtime.operations import execute_component, operation_component
from composition.compiler import _output_validation
from stage4.fixtures import _profile
from transformations import (
    TransformationKind,
    TransformationPort,
    TransformationSpec,
    ValueSemantics,
)


def _grid(
        x: list[float], y: list[float], *, crs: str = "EPSG:32614",
        axes: tuple[str, str] = ("easting", "northing"),
) -> GridDescriptor:
    assert len(x) >= 2 and len(y) >= 2
    dx, dy = x[1] - x[0], y[1] - y[0]
    assert all(value - x[index - 1] == dx
               for index, value in enumerate(x[2:], start=2))
    assert all(value - y[index - 1] == dy
               for index, value in enumerate(y[2:], start=2))
    angular = crs == "EPSG:4326"
    return GridDescriptor(
        crs, axes, (len(y), len(x)),
        (repr(dx), "0", repr(x[0]), "0", repr(dy), repr(y[0])),
        SpatialScale(
            repr(abs(dx)), repr(abs(dy)),
            "degree" if angular else "m",
            ScaleBasis.ANGULAR if angular else ScaleBasis.LINEAR,
        ),
    )


def _descriptor(
        grid: GridDescriptor, *, origin: OriginClass = OriginClass.SYNTHETIC,
) -> ArtifactDescriptor:
    return ArtifactDescriptor(
        concept_id="example.scalar.field",
        schema_version="field-json-v2",
        representation="application/json",
        units="K",
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


def _field(grid: GridDescriptor) -> dict:
    x = [float(value) for value in grid.centre_axis("x")]
    y = [float(value) for value in grid.centre_axis("y")]
    return {
        "schema": "field-json-v2",
        "crs": grid.crs,
        "axis_order": list(grid.axis_order),
        "x": x,
        "y": y,
        "time": ["TIME_INVARIANT"],
        "components": {
            "value": [[
                [float(row * len(x) + column) for column in range(len(x))]
                for row in range(len(y))
            ]],
        },
    }


def _spec(kind, source, result, parameters, operation):
    return TransformationSpec.bind(
        transformation_id=f"stage8r-grid-{kind.value.lower()}",
        transformation_version="1.0.0",
        kind=kind,
        execution_profile=_profile(operation),
        input_ports=(TransformationPort(
            "source", source,
            ValueSemantics.SCALAR_CONTINUOUS_INTENSIVE),),
        output_ports=(TransformationPort(
            "result", result,
            ValueSemantics.SCALAR_CONTINUOUS_INTENSIVE),),
        parameters=parameters,
        cost_units=1,
    )


def test_descriptor_names_the_sample_centres_signed_steps_and_outer_edges():
    grid = _grid([0, 1, 2], [10, 9, 8])
    assert GRID_AFFINE_CONVENTION == "sample-centres-axis-aligned-v1"
    assert grid.shape == (3, 3)  # row-major y, x
    assert grid.axis_order == ("easting", "northing")
    assert grid.centre_axis("x") == ("0", "1", "2")
    assert grid.centre_axis("y") == ("10", "9", "8")
    assert grid.support_bounds == ("-0.5", "7.5", "2.5", "10.5")
    grid.require_support(BBoxSupport(
        grid.crs, grid.axis_order, grid.support_bounds))


def test_compiler_refuses_legacy_or_gridless_executable_field_contracts():
    descriptor = _descriptor(_grid([0, 1], [1, 0]))
    legacy = dataclasses.replace(descriptor, schema_version="field-json-v1")
    with pytest.raises(ValueError, match="legacy field-json-v1"):
        _output_validation(
            SimpleNamespace(), SimpleNamespace(descriptor=legacy))

    gridless = dataclasses.replace(descriptor, grid=None)
    with pytest.raises(ValueError, match="requires an exact grid"):
        _output_validation(
            SimpleNamespace(), SimpleNamespace(descriptor=gridless))


def test_field_v2_source_cannot_publish_metadata_without_an_exact_grid():
    with pytest.raises(ValueError, match="requires an exact grid"):
        SourceSchema(
            source_id="source", concept_id="example.scalar.field",
            schema_version="field-json-v2",
            representation="application/json", units="K",
            spatial_crs="EPSG:4326",
            spatial_axis_order=("longitude", "latitude"),
            temporal_kind=TemporalKind.TIME_INVARIANT,
            sample_semantics=SampleSemantics.INSTANTANEOUS,
            origin=OriginClass.OBSERVATION,
            assembly_mode=AssemblyMode.SINGLE_ASSET,
        )


def test_subset_planning_and_runtime_use_the_same_signed_affine():
    source_grid = _grid([0, 1, 2], [10, 9, 8])
    result_grid = _grid([1, 2], [9, 8])
    spec = _spec(
        TransformationKind.SPATIAL_SUBSET,
        _descriptor(source_grid),
        _descriptor(result_grid, origin=OriginClass.DERIVED),
        {"x_start": 1, "x_stop": 3, "y_start": 1, "y_stop": 3},
        "transform.spatial_subset.v1",
    )
    output = execute_component(
        operation_component("transform.spatial_subset.v1"),
        spec.parameters, {"source": _field(source_grid)})["result"]
    assert output["x"] == [1.0, 2.0]
    assert output["y"] == [9.0, 8.0]
    assert result_grid.support_bounds == ("0.5", "7.5", "2.5", "9.5")


def test_block_aggregation_centres_and_descriptor_are_identical():
    source_grid = _grid([0, 1, 2, 3], [10, 9, 8, 7])
    result_grid = _grid([0.5, 2.5], [9.5, 7.5])
    spec = _spec(
        TransformationKind.SPATIAL_BLOCK_AGGREGATE,
        _descriptor(source_grid),
        _descriptor(result_grid, origin=OriginClass.DERIVED),
        {"block_x": 2, "block_y": 2,
         "aggregation": "INTENSIVE_AREA_WEIGHTED_MEAN"},
        "transform.spatial_block_aggregate.v1",
    )
    output = execute_component(
        operation_component("transform.spatial_block_aggregate.v1"),
        spec.parameters, {"source": _field(source_grid)})["result"]
    assert output["x"] == [0.5, 2.5]
    assert output["y"] == [9.5, 7.5]
    assert tuple(str(value) for value in output["x"]) \
        == result_grid.centre_axis("x")
    assert tuple(str(value) for value in output["y"]) \
        == result_grid.centre_axis("y")
    assert source_grid.support_bounds == result_grid.support_bounds


def test_regrid_refuses_a_sample_outside_centres_even_inside_outer_edges():
    source_grid = _grid([0, 1, 2], [2, 1, 0])
    valid_grid = _grid([0.5, 1.5], [1.5, 0.5])
    _spec(
        TransformationKind.REGRID_BILINEAR,
        _descriptor(source_grid),
        _descriptor(valid_grid, origin=OriginClass.DERIVED),
        {"target_x": [0.5, 1.5], "target_y": [1.5, 0.5]},
        "transform.regrid_bilinear.v1",
    )

    # -0.25 lies inside the source's outer -0.5 cell edge, but bilinear
    # interpolation has no centre to its left and must not extrapolate.
    outside_grid = _grid([-0.25, 0.25], [1.5, 0.5])
    with pytest.raises(ValueError, match="sample-centre interpolation support"):
        _spec(
            TransformationKind.REGRID_BILINEAR,
            _descriptor(source_grid),
            _descriptor(outside_grid, origin=OriginClass.DERIVED),
            {"target_x": [-0.25, 0.25], "target_y": [1.5, 0.5]},
            "transform.regrid_bilinear.v1",
        )


def _projected(values_x, values_y):
    pyproj = pytest.importorskip("pyproj")
    forward = pyproj.Transformer.from_crs(
        "EPSG:4326", "EPSG:3857", always_xy=True)
    return (
        [forward.transform(value, 0)[0] for value in values_x],
        [forward.transform(0, value)[1] for value in values_y],
    )


def test_reprojection_freezes_exact_pipeline_and_preflights_every_sample():
    source_grid = _grid(
        [0, 1, 2], [2, 1, 0], crs="EPSG:4326",
        axes=("longitude", "latitude"))
    target_x, target_y = _projected([0.5, 1.5], [1.5, 0.5])
    result_grid = _grid(
        target_x, target_y, crs="EPSG:3857",
        axes=("easting", "northing"))
    spec = _spec(
        TransformationKind.REPROJECT_BILINEAR,
        _descriptor(source_grid),
        _descriptor(result_grid, origin=OriginClass.DERIVED),
        {"source_crs": "EPSG:4326", "target_crs": "EPSG:3857",
         "target_x": target_x, "target_y": target_y},
        "transform.reproject_bilinear.v1",
    )
    assert spec.parameters["pipeline_projjson"]["type"]
    assert spec.parameters["source_axis_order"] == ("longitude", "latitude")
    assert spec.parameters["target_axis_order"] == ("easting", "northing")
    assert TransformationSpec.from_dict(spec.to_dict()) == spec

    output = execute_component(
        operation_component("transform.reproject_bilinear.v1"),
        spec.parameters, {"source": _field(source_grid)})["result"]
    assert output["axis_order"] == ["easting", "northing"]
    assert output["x"] == target_x
    assert output["y"] == target_y

    forged = copy.deepcopy(spec.to_dict())
    forged["parameters"]["pipeline_projjson"]["name"] = "forged pipeline"
    with pytest.raises(ValueError, match="pipeline"):
        TransformationSpec.from_dict(forged)


def test_reprojection_refuses_one_target_sample_outside_source_support():
    source_grid = _grid(
        [0, 1, 2], [2, 1, 0], crs="EPSG:4326",
        axes=("longitude", "latitude"))
    target_x, target_y = _projected([0.5, 2.25], [1.5, 0.5])
    result_grid = _grid(
        target_x, target_y, crs="EPSG:3857",
        axes=("easting", "northing"))
    with pytest.raises(ValueError, match=r"sample \[.*\].*outside"):
        _spec(
            TransformationKind.REPROJECT_BILINEAR,
            _descriptor(source_grid),
            _descriptor(result_grid, origin=OriginClass.DERIVED),
            {"source_crs": "EPSG:4326", "target_crs": "EPSG:3857",
             "target_x": target_x, "target_y": target_y},
            "transform.reproject_bilinear.v1",
        )


def test_temporal_align_last_selected_index_is_the_bound_not_count_times_step():
    utc = timezone.utc
    start = datetime(2026, 1, 1, tzinfo=utc)
    source_temporal = TemporalSupport(
        TemporalKind.SERIES, start, start + timedelta(hours=5), "3600",
        max_gap_s="0")
    result_temporal = TemporalSupport(
        TemporalKind.SERIES, start, start + timedelta(hours=6), "7200",
        anchor=start, max_gap_s="0")
    support = BBoxSupport("EPSG:4326", ("x", "y"), ("0", "0", "1", "1"))
    source = ArtifactDescriptor(
        "example.scalar.series", "field-json-v2", "application/json", "K",
        support, source_temporal, None, None, None, OriginClass.SYNTHETIC,
        Missingness(MissingnessStatus.COMPLETE),
        component_names=("value",))
    result = dataclasses.replace(
        source, temporal_support=result_temporal, origin=OriginClass.DERIVED)

    spec = _spec(
        TransformationKind.TEMPORAL_ALIGN, source, result,
        {"start_index": 0, "step": 2, "count": 3},
        "transform.temporal_align.v1",
    )
    assert spec.parameters == {"start_index": 0, "step": 2, "count": 3}
