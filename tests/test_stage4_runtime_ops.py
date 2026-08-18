"""Closed Stage-4 transformation operation tests.

The tiny ``field-json-v2`` values here are execution fixtures.  They are not a
replacement for production array storage and they deliberately contain no
domain-specific wind semantics.
"""
from __future__ import annotations

import math

import pytest

from capabilities.binders import binder_keys, binder_rule
from engine.runtime.operations import (
    VECTOR_CALM_DIRECTION_CONVENTION,
    VECTOR_DIRECTION_CONVENTION,
    bind_reprojection_parameters,
    execute_component,
    operation_component,
    operation_keys,
)


def _run(key: str, parameters: dict, inputs: dict) -> dict:
    return execute_component(operation_component(key), parameters, inputs)


def _field(*, components: dict | None = None,
           x: list[float] | None = None,
           y: list[float] | None = None,
           time: list[str] | None = None,
           crs: str = "EPSG:4326") -> dict:
    return {
        "schema": "field-json-v2",
        "crs": crs,
        "axis_order": ["longitude", "latitude"],
        "x": [0.0, 1.0, 2.0] if x is None else x,
        "y": [10.0, 11.0, 12.0] if y is None else y,
        "time": (["2026-01-01T00:00:00Z", "2026-01-01T01:00:00Z"]
                 if time is None else time),
        "components": components if components is not None else {
            "value": [
                [[0, 1, 2], [10, 11, 12], [20, 21, 22]],
                [[100, 101, 102], [110, 111, 112], [120, 121, 122]],
            ],
        },
    }


def test_all_stage4_operations_have_closed_exact_binders() -> None:
    expected = {
        "transform.unit_affine.v1": (
            "transform.unit_affine.bind.v1", ("source",), ("result",),
            ("factor", "offset")),
        "transform.spatial_subset.v1": (
            "transform.spatial_subset.bind.v1", ("source",), ("result",),
            ("x_start", "x_stop", "y_start", "y_stop")),
        "transform.temporal_subset.v1": (
            "transform.temporal_subset.bind.v1", ("source",), ("result",),
            ("start", "stop")),
        "transform.temporal_align.v1": (
            "transform.temporal_align.bind.v1", ("source",), ("result",),
            ("count", "start_index", "step")),
        "transform.regrid_bilinear.v1": (
            "transform.regrid_bilinear.bind.v1", ("source",), ("result",),
            ("target_x", "target_y")),
        "transform.reproject_bilinear.v1": (
            "transform.reproject_bilinear.bind.v1", ("source",), ("result",),
            ("pipeline_projjson", "source_axis_order", "source_crs",
             "target_axis_order", "target_crs", "target_x", "target_y")),
        "transform.vector_rotate.v1": (
            "transform.vector_rotate.bind.v1", ("source",), ("result",),
            ("angle_degrees",)),
        "transform.vector_uv_to_speed_direction.v1": (
            "transform.vector_uv_to_speed_direction.bind.v1", ("source",),
            ("speed", "direction"), ()),
        "transform.spatial_block_aggregate.v1": (
            "transform.spatial_block_aggregate.bind.v1", ("source",),
            ("result",), ("aggregation", "block_x", "block_y")),
    }
    assert set(expected).issubset(operation_keys())
    # The binder registry is pinned to an exact tuple, so adding one is a
    # visible change. Pin the transform half of the operation registry the
    # same way: a subset check lets a new operation land unreviewed.
    assert {key for key in operation_keys()
            if key.startswith("transform.")} == set(expected)
    assert {value[0] for value in expected.values()}.issubset(binder_keys())
    for operation_key, (binder_key, inputs, outputs, parameters) in expected.items():
        rule = binder_rule(binder_key)
        assert rule.operation_key == operation_key
        assert rule.input_ports == inputs
        assert rule.output_ports == outputs
        assert rule.parameter_names == parameters


def test_unit_affine_handles_scalar_and_field_deterministically() -> None:
    # factor/offset are runtime execution data only.  TransformSpec's closed
    # unit-pair registry, tested separately, is the scientific authority.
    parameters = {"factor": 1.8, "offset": 32.0}
    assert _run("transform.unit_affine.v1", parameters,
                {"source": 10}) == {"result": 50.0}

    source = _field(time=["TIME_INVARIANT"], x=[0], y=[1],
                    components={"value": [[[10]]]})
    first = _run("transform.unit_affine.v1", parameters, {"source": source})
    second = _run("transform.unit_affine.v1", parameters, {"source": source})
    assert first == second
    assert first["result"]["time"] == ["TIME_INVARIANT"]
    assert first["result"]["components"] == {"value": [[[50.0]]]}


@pytest.mark.parametrize(
    ("parameters", "inputs", "error"),
    [
        ({"factor": float("nan"), "offset": 0}, {"source": 1}, ValueError),
        ({"factor": 1, "offset": 0, "units": "invented"}, {"source": 1},
         ValueError),
        ({"factor": 0, "offset": 1}, {"source": 1}, ValueError),
        ({"factor": 1, "offset": 0}, {"source": True}, TypeError),
        ({"factor": 1, "offset": 0}, {"source": 1, "other": 2}, ValueError),
    ],
)
def test_unit_affine_rejects_unsafe_or_ambiguous_calls(
        parameters: dict, inputs: dict, error: type[Exception]) -> None:
    with pytest.raises(error):
        _run("transform.unit_affine.v1", parameters, inputs)


def test_spatial_and_temporal_selection_are_explicit_half_open_indexes() -> None:
    spatial = _run(
        "transform.spatial_subset.v1",
        {"x_start": 1, "x_stop": 3, "y_start": 0, "y_stop": 2},
        {"source": _field()},
    )["result"]
    assert spatial["x"] == [1.0, 2.0]
    assert spatial["y"] == [10.0, 11.0]
    assert spatial["components"]["value"] == [
        [[1.0, 2.0], [11.0, 12.0]],
        [[101.0, 102.0], [111.0, 112.0]],
    ]

    temporal = _run(
        "transform.temporal_subset.v1", {"start": 1, "stop": 2},
        {"source": _field()},
    )["result"]
    assert temporal["time"] == ["2026-01-01T01:00:00Z"]
    assert temporal["components"]["value"][0][0] == [100.0, 101.0, 102.0]

    aligned_source = _field(
        time=[f"2026-01-01T0{index}:00:00Z" for index in range(5)],
        components={
            "value": [
                [[index, index, index], [index, index, index],
                 [index, index, index]]
                for index in range(5)
            ],
        },
    )
    aligned = _run(
        "transform.temporal_align.v1",
        {"start_index": 0, "step": 2, "count": 3},
        {"source": aligned_source},
    )["result"]
    assert aligned["time"] == [aligned_source["time"][index]
                               for index in (0, 2, 4)]
    assert [plane[0][0] for plane in aligned["components"]["value"]] == [0, 2, 4]


@pytest.mark.parametrize(
    ("key", "parameters"),
    [
        ("transform.spatial_subset.v1",
         {"x_start": 2, "x_stop": 2, "y_start": 0, "y_stop": 1}),
        ("transform.temporal_subset.v1", {"start": 0, "stop": 3}),
        ("transform.temporal_align.v1",
         {"start_index": 1, "step": 2, "count": 2}),
    ],
)
def test_selection_rejects_empty_or_out_of_range_indexes(
        key: str, parameters: dict) -> None:
    with pytest.raises(ValueError):
        _run(key, parameters, {"source": _field()})


def test_continuous_bilinear_regrid() -> None:
    source = _field(
        x=[0, 1, 2], y=[0, 1, 2], time=["TIME_INVARIANT"],
        components={
            "value": [[
                [0, 1, 2],
                [2, 3, 4],
                [4, 5, 6],
            ]],
        },
    )
    result = _run(
        "transform.regrid_bilinear.v1",
        {"target_x": [0.5, 1.5], "target_y": [0.25, 1.25]},
        {"source": source},
    )["result"]
    assert result["x"] == [0.5, 1.5]
    assert result["y"] == [0.25, 1.25]
    for actual_row, expected_row in zip(
            result["components"]["value"][0],
            [[1.0, 2.0], [3.0, 4.0]]):
        assert actual_row == pytest.approx(expected_row)


def test_reprojection_inverse_maps_target_grid_before_bilinear_sampling() -> None:
    pyproj = pytest.importorskip("pyproj")
    source = _field(
        x=[0, 1, 2], y=[0, 1, 2], time=["TIME_INVARIANT"],
        components={
            "value": [[
                [0, 1, 2],
                [2, 3, 4],
                [4, 5, 6],
            ]],
        },
    )
    forward = pyproj.Transformer.from_crs(
        "EPSG:4326", "EPSG:3857", always_xy=True)
    target_x = [forward.transform(value, 0)[0] for value in (0.5, 1.5)]
    target_y = [forward.transform(0, value)[1] for value in (0.5, 1.5)]
    parameters = bind_reprojection_parameters(
        {
            "source_crs": "EPSG:4326",
            "target_crs": "EPSG:3857",
            "target_x": target_x,
            "target_y": target_y,
        },
        source_axis_order=("longitude", "latitude"),
        target_axis_order=("easting", "northing"),
    )
    result = _run(
        "transform.reproject_bilinear.v1", parameters,
        {"source": source},
    )["result"]
    assert result["crs"] == "EPSG:3857"
    assert result["axis_order"] == ["easting", "northing"]
    assert result["x"] == target_x
    assert result["y"] == target_y
    for actual_row, expected_row in zip(
            result["components"]["value"][0],
            [[1.5, 2.5], [3.5, 4.5]]):
        assert actual_row == pytest.approx(expected_row, abs=1e-9)


def test_reprojection_component_identity_binds_pyproj_and_proj_versions(
        monkeypatch) -> None:
    import pyproj

    original = operation_component("transform.reproject_bilinear.v1")
    monkeypatch.setattr(pyproj, "proj_version_str", "different-test-PROJ")
    changed = operation_component("transform.reproject_bilinear.v1")
    assert changed.implementation_digest != original.implementation_digest


def test_vector_rotation_consumes_one_coherent_vector_artifact() -> None:
    source = _field(
        x=[0], y=[0], time=["TIME_INVARIANT"],
        components={"u": [[[1]]], "v": [[[0]]]},
    )
    result = _run(
        "transform.vector_rotate.v1", {"angle_degrees": 90},
        {"source": source},
    )["result"]
    assert result["components"]["u"][0][0][0] == pytest.approx(0, abs=1e-12)
    assert result["components"]["v"][0][0][0] == pytest.approx(1)

    # Separate u/v inputs are intentionally impossible: a selector must bind a
    # single vector realization and cannot mix two independently produced runs.
    with pytest.raises(ValueError, match="invalid vector-rotation inputs"):
        _run("transform.vector_rotate.v1", {"angle_degrees": 0},
             {"u": source, "v": source})


def test_vector_decomposition_uses_documented_mathematical_direction() -> None:
    assert VECTOR_DIRECTION_CONVENTION == (
        "mathematical_counterclockwise_degrees_from_positive_x_[0,360)")
    assert VECTOR_CALM_DIRECTION_CONVENTION == (
        "calm_vector_direction_is_zero_degrees-v1")
    source = _field(
        x=[0, 1, 2], y=[0], time=["TIME_INVARIANT"],
        components={"u": [[[3, 0, 0]]], "v": [[[4, -1, 0]]]},
    )
    outputs = _run(
        "transform.vector_uv_to_speed_direction.v1", {}, {"source": source})
    assert outputs["speed"]["components"] == {
        "speed": [[[5.0, 1.0, 0.0]]]}
    directions = outputs["direction"]["components"]["direction"][0][0]
    assert directions[0] == pytest.approx(math.degrees(math.atan2(4, 3)))
    assert directions[1] == pytest.approx(270.0)
    assert directions[2] == 0.0


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.update(schema="field-json-v1"),
        lambda value: value.update(x=[0.0, 2.0, 1.0]),
        lambda value: value["components"]["value"][0][0].append(3),
        lambda value: value["components"]["value"][0][0].__setitem__(0, float("inf")),
        lambda value: value.update(unexpected=True),
    ],
)
def test_field_operations_reject_malformed_or_non_finite_payloads(mutation) -> None:
    source = _field()
    mutation(source)
    with pytest.raises((TypeError, ValueError)):
        _run("transform.spatial_subset.v1",
             {"x_start": 0, "x_stop": 1, "y_start": 0, "y_stop": 1},
             {"source": source})


def test_existing_closed_synthetic_operation_still_executes() -> None:
    assert _run("synthetic.constant.v1", {"value": 7}, {}) == {"result": 7}
