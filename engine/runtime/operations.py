"""Closed Stage-1 executable operation registry.

Only keys declared here can run in the local worker.  There is intentionally no
generic import/callable escape hatch, and no WRF, MPI, or scheduler operation.
"""
from __future__ import annotations

import hashlib
import math
import os
import re
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

from grid_convention import FIELD_JSON_SCHEMA

from .identity import strict_hash, strict_json_loads
from .types import ExecutableComponent

ASSET_STORE_ENVIRONMENT = "NASA_STAGE5_ASSET_STORE"


Operation = Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]


def _constant(parameters: dict[str, Any],
              inputs: dict[str, Any]) -> dict[str, Any]:
    return {"result": parameters["value"]}


def _add(parameters: dict[str, Any],
         inputs: dict[str, Any]) -> dict[str, Any]:
    return {"result": inputs["left"] + inputs["right"]}


def _pair(parameters: dict[str, Any],
          inputs: dict[str, Any]) -> dict[str, Any]:
    """Return two independently addressable outputs from one invocation.

    This consequence-free operation exists to prove that Stage 2 preserves
    co-production: the resolver may select and cost one invocation while two
    downstream requirement uses bind to different output ports.
    """
    return {"left": parameters["left"], "right": parameters["right"]}


def _scale(parameters: dict[str, Any],
           inputs: dict[str, Any]) -> dict[str, Any]:
    return {"result": inputs["value"] * parameters["factor"]}


def _sleep(parameters: dict[str, Any],
           inputs: dict[str, Any]) -> dict[str, Any]:
    import time
    marker = parameters.get("started_marker")
    if marker:
        marker_path = Path(marker).resolve()
        if not str(marker_path).startswith("/tmp/"):
            raise ValueError("synthetic marker must be under /tmp")
        marker_path.touch()
    time.sleep(float(parameters.get("seconds", 0.1)))
    value = parameters.get("value", inputs.get("value", 1))
    return {"result": value}


def _fail(parameters: dict[str, Any],
          inputs: dict[str, Any]) -> dict[str, Any]:
    raise RuntimeError(str(parameters.get("message", "synthetic failure")))


def _fail_once(parameters: dict[str, Any],
               inputs: dict[str, Any]) -> dict[str, Any]:
    marker = Path(parameters["marker"]).resolve()
    if not str(marker).startswith("/tmp/"):
        raise ValueError("fail-once marker must be under /tmp")
    try:
        fd = marker.open("x")
    except FileExistsError:
        return {"result": parameters.get("value", 1)}
    else:
        fd.close()
        raise RuntimeError("synthetic first-attempt failure")


def _identity(parameters: dict[str, Any],
              inputs: dict[str, Any]) -> dict[str, Any]:
    return {"result": inputs.get("value", parameters.get("value"))}


def _native_file_pointer(parameters: dict[str, Any],
                         inputs: dict[str, Any]) -> dict[str, Any]:
    """Publish an exact producer-owned file reference, never its payload."""
    _exact_keys(parameters, {"pointer"}, "native file pointer parameters")
    _exact_keys(inputs, set(), "native file pointer inputs")
    from .native import NativeFilePointer
    pointer = NativeFilePointer.from_dict(parameters["pointer"])
    pointer.verify_file()
    return {"result": pointer.to_dict()}


def _native_file_pointer_identity(
        parameters: dict[str, Any], inputs: dict[str, Any],
) -> dict[str, Any]:
    """Forward exactly one verified native pointer without relabelling it."""
    _exact_keys(parameters, set(), "native file pointer identity parameters")
    _exact_keys(inputs, {"source"}, "native file pointer identity inputs")
    from .native import NativeFilePointer
    pointer = NativeFilePointer.from_dict(inputs["source"])
    pointer.verify_file()
    return {"result": pointer.to_dict()}


# Stage 4 intentionally uses one small, strict interchange value for local
# gridded-field transformations.  Component tensors are indexed [time][y][x].
# This is an execution format, not a claim that JSON is an appropriate storage
# format for production-sized scientific arrays.
_FIELD_SCHEMA = FIELD_JSON_SCHEMA
_FIELD_KEYS = frozenset({
    "schema", "crs", "axis_order", "x", "y", "time", "components"})
_AUTHORITY_CRS = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]*:[A-Za-z0-9_.-]+$")

# The vector decomposition operation is domain-neutral.  In particular, this
# is not the meteorological "direction wind comes from" convention.
VECTOR_DIRECTION_CONVENTION = (
    "mathematical_counterclockwise_degrees_from_positive_x_[0,360)"
)
VECTOR_CALM_DIRECTION_CONVENTION = "calm_vector_direction_is_zero_degrees-v1"


def _exact_keys(value: dict[str, Any], expected: set[str] | frozenset[str],
                context: str) -> None:
    if not isinstance(value, dict):
        raise TypeError(f"{context} must be an object")
    actual = set(value)
    if actual != set(expected):
        missing = sorted(set(expected) - actual)
        extra = sorted(actual - set(expected))
        details: list[str] = []
        if missing:
            details.append(f"missing={missing}")
        if extra:
            details.append(f"unexpected={extra}")
        raise ValueError(f"invalid {context} fields: " + ", ".join(details))


def _finite_number(value: Any, context: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{context} must be a finite number")
    try:
        result = float(value)
    except OverflowError as exc:
        raise ValueError(f"{context} must be a finite number") from exc
    if not math.isfinite(result):
        raise ValueError(f"{context} must be a finite number")
    return result


def _integer(value: Any, context: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{context} must be an integer")
    if minimum is not None and value < minimum:
        raise ValueError(f"{context} must be at least {minimum}")
    return value


def _axis(value: Any, context: str, *, minimum_length: int = 1) -> list[float]:
    if not isinstance(value, (list, tuple)):
        raise TypeError(f"{context} must be an array")
    result = [_finite_number(item, f"{context}[{index}]")
              for index, item in enumerate(value)]
    if len(result) < minimum_length:
        raise ValueError(
            f"{context} must contain at least {minimum_length} coordinates")
    differences = [right - left for left, right in zip(result, result[1:])]
    if differences and (any(item == 0 for item in differences)
                        or min(differences) < 0 < max(differences)):
        raise ValueError(f"{context} coordinates must be strictly monotonic")
    return result


def _validate_field(value: Any, context: str = "field") -> dict[str, Any]:
    """Validate and normalize a canonical finite ``field-json-v2`` value."""
    if not isinstance(value, dict):
        raise TypeError(f"{context} must be an object")
    _exact_keys(value, _FIELD_KEYS, context)
    if value["schema"] != _FIELD_SCHEMA:
        raise ValueError(f"{context}.schema must be {_FIELD_SCHEMA!r}")
    crs = value["crs"]
    if not isinstance(crs, str) or not crs.strip():
        raise ValueError(f"{context}.crs must be a non-empty string")
    axis_order = value["axis_order"]
    if (not isinstance(axis_order, (list, tuple)) or len(axis_order) != 2
            or any(not isinstance(item, str) or not item
                   for item in axis_order)
            or axis_order[0] == axis_order[1]):
        raise ValueError(
            f"{context}.axis_order must name distinct x/y coordinate axes")
    x = _axis(value["x"], f"{context}.x")
    y = _axis(value["y"], f"{context}.y")
    raw_time = value["time"]
    if not isinstance(raw_time, (list, tuple)) or not raw_time:
        raise ValueError(f"{context}.time must be a non-empty array")
    time: list[str] = []
    for index, item in enumerate(raw_time):
        if not isinstance(item, str) or not item:
            raise ValueError(
                f"{context}.time[{index}] must be a non-empty string")
        time.append(item)
    if len(set(time)) != len(time):
        raise ValueError(f"{context}.time entries must be unique")

    raw_components = value["components"]
    if not isinstance(raw_components, dict) or not raw_components:
        raise ValueError(f"{context}.components must be a non-empty object")
    components: dict[str, list[list[list[float]]]] = {}
    for name in sorted(raw_components):
        if not isinstance(name, str) or not name:
            raise ValueError(f"{context} component names must be non-empty")
        raw_tensor = raw_components[name]
        if not isinstance(raw_tensor, (list, tuple)):
            raise TypeError(f"{context}.components.{name} must be an array")
        if len(raw_tensor) != len(time):
            raise ValueError(
                f"{context}.components.{name} must have {len(time)} time planes")
        tensor: list[list[list[float]]] = []
        for time_index, raw_plane in enumerate(raw_tensor):
            if not isinstance(raw_plane, (list, tuple)):
                raise TypeError(
                    f"{context}.components.{name}[{time_index}] must be an array")
            if len(raw_plane) != len(y):
                raise ValueError(
                    f"{context}.components.{name}[{time_index}] must have "
                    f"{len(y)} rows")
            plane: list[list[float]] = []
            for y_index, raw_row in enumerate(raw_plane):
                if not isinstance(raw_row, (list, tuple)):
                    raise TypeError(
                        f"{context}.components.{name}[{time_index}]"
                        f"[{y_index}] must be an array")
                if len(raw_row) != len(x):
                    raise ValueError(
                        f"{context}.components.{name}[{time_index}]"
                        f"[{y_index}] must have {len(x)} columns")
                plane.append([
                    _finite_number(item,
                                   f"{context}.components.{name}"
                                   f"[{time_index}][{y_index}][{x_index}]")
                    for x_index, item in enumerate(raw_row)
                ])
            tensor.append(plane)
        components[name] = tensor
    return {
        "schema": _FIELD_SCHEMA,
        "crs": crs,
        "axis_order": list(axis_order),
        "x": x,
        "y": y,
        "time": time,
        "components": components,
    }


def _field_with(field: dict[str, Any], *,
                x: list[float] | None = None,
                y: list[float] | None = None,
                time: list[str] | None = None,
                crs: str | None = None,
                axis_order: list[str] | None = None,
                components: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "schema": _FIELD_SCHEMA,
        "crs": field["crs"] if crs is None else crs,
        "axis_order": (field["axis_order"] if axis_order is None
                       else list(axis_order)),
        "x": field["x"] if x is None else x,
        "y": field["y"] if y is None else y,
        "time": field["time"] if time is None else time,
        "components": field["components"] if components is None else components,
    }


def _unit_affine(parameters: dict[str, Any],
                 inputs: dict[str, Any]) -> dict[str, Any]:
    """Execute an already-authorized affine unit conversion.

    ``factor`` and ``offset`` are execution values, not scientific authority.
    Stage 4 TransformSpec construction admits them only through its closed
    source-unit/target-unit conversion registry.
    """
    _exact_keys(parameters, {"factor", "offset"}, "unit-affine parameters")
    _exact_keys(inputs, {"source"}, "unit-affine inputs")
    factor = _finite_number(parameters["factor"], "factor")
    offset = _finite_number(parameters["offset"], "offset")
    if factor == 0:
        raise ValueError("factor cannot be zero")
    source = inputs["source"]
    if isinstance(source, bool) or not isinstance(source, (int, float, dict)):
        raise TypeError("unit-affine source must be a number or field-json-v2")
    if isinstance(source, dict):
        field = _validate_field(source, "unit-affine source")
        components = {
            name: [[[(value * factor) + offset for value in row]
                    for row in plane] for plane in tensor]
            for name, tensor in field["components"].items()
        }
        return {"result": _field_with(field, components=components)}
    return {"result": (_finite_number(source, "source") * factor) + offset}


def _spatial_subset(parameters: dict[str, Any],
                    inputs: dict[str, Any]) -> dict[str, Any]:
    _exact_keys(parameters, {"x_start", "x_stop", "y_start", "y_stop"},
                "spatial-subset parameters")
    _exact_keys(inputs, {"source"}, "spatial-subset inputs")
    field = _validate_field(inputs["source"], "spatial-subset source")
    x_start = _integer(parameters["x_start"], "x_start", minimum=0)
    x_stop = _integer(parameters["x_stop"], "x_stop", minimum=1)
    y_start = _integer(parameters["y_start"], "y_start", minimum=0)
    y_stop = _integer(parameters["y_stop"], "y_stop", minimum=1)
    if not x_start < x_stop <= len(field["x"]):
        raise ValueError("x subset must be a non-empty half-open in-range interval")
    if not y_start < y_stop <= len(field["y"]):
        raise ValueError("y subset must be a non-empty half-open in-range interval")
    components = {
        name: [[row[x_start:x_stop] for row in plane[y_start:y_stop]]
               for plane in tensor]
        for name, tensor in field["components"].items()
    }
    return {"result": _field_with(
        field,
        x=field["x"][x_start:x_stop],
        y=field["y"][y_start:y_stop],
        components=components,
    )}


def _temporal_subset(parameters: dict[str, Any],
                     inputs: dict[str, Any]) -> dict[str, Any]:
    _exact_keys(parameters, {"start", "stop"}, "temporal-subset parameters")
    _exact_keys(inputs, {"source"}, "temporal-subset inputs")
    field = _validate_field(inputs["source"], "temporal-subset source")
    start = _integer(parameters["start"], "start", minimum=0)
    stop = _integer(parameters["stop"], "stop", minimum=1)
    if not start < stop <= len(field["time"]):
        raise ValueError(
            "temporal subset must be a non-empty half-open in-range interval")
    components = {
        name: tensor[start:stop]
        for name, tensor in field["components"].items()
    }
    return {"result": _field_with(
        field, time=field["time"][start:stop], components=components)}


def _temporal_align(parameters: dict[str, Any],
                    inputs: dict[str, Any]) -> dict[str, Any]:
    _exact_keys(parameters, {"start_index", "step", "count"},
                "temporal-align parameters")
    _exact_keys(inputs, {"source"}, "temporal-align inputs")
    field = _validate_field(inputs["source"], "temporal-align source")
    start = _integer(parameters["start_index"], "start_index", minimum=0)
    step = _integer(parameters["step"], "step", minimum=1)
    count = _integer(parameters["count"], "count", minimum=1)
    indexes = [start + index * step for index in range(count)]
    if indexes[-1] >= len(field["time"]):
        raise ValueError("temporal alignment selects outside the source series")
    components = {
        name: [tensor[index] for index in indexes]
        for name, tensor in field["components"].items()
    }
    return {"result": _field_with(
        field,
        time=[field["time"][index] for index in indexes],
        components=components,
    )}


def _bracket(axis: list[float], coordinate: float, context: str) -> tuple[int, float]:
    lower_bound, upper_bound = sorted((axis[0], axis[-1]))
    if coordinate < lower_bound or coordinate > upper_bound:
        raise ValueError(f"{context} coordinate {coordinate} is outside source grid")
    if coordinate == axis[-1]:
        return len(axis) - 2, 1.0
    # The array index, rather than numeric coordinate order, is authoritative.
    # A north-up y axis is decreasing, so use a small direction-neutral binary
    # search and retain a signed denominator for the interpolation fraction.
    increasing = axis[-1] > axis[0]
    left, right = 0, len(axis) - 1
    while right - left > 1:
        middle = (left + right) // 2
        if ((axis[middle] <= coordinate) if increasing
                else (axis[middle] >= coordinate)):
            left = middle
        else:
            right = middle
    lower = left
    fraction = ((coordinate - axis[lower])
                / (axis[lower + 1] - axis[lower]))
    return lower, fraction


def _bilinear_tensor(tensor: list[list[list[float]]],
                     source_x: list[float], source_y: list[float],
                     sample_points: list[list[tuple[float, float]]],
                     context: str) -> list[list[list[float]]]:
    brackets = [[
        (_bracket(source_x, source_x_value, f"{context}.x"),
         _bracket(source_y, source_y_value, f"{context}.y"))
        for source_x_value, source_y_value in row
    ] for row in sample_points]
    result: list[list[list[float]]] = []
    for plane in tensor:
        output_plane: list[list[float]] = []
        for bracket_row in brackets:
            output_row: list[float] = []
            for ((x_index, x_fraction),
                 (y_index, y_fraction)) in bracket_row:
                lower = (plane[y_index][x_index] * (1.0 - x_fraction)
                         + plane[y_index][x_index + 1] * x_fraction)
                upper = (plane[y_index + 1][x_index] * (1.0 - x_fraction)
                         + plane[y_index + 1][x_index + 1] * x_fraction)
                output_row.append(
                    lower * (1.0 - y_fraction) + upper * y_fraction)
            output_plane.append(output_row)
        result.append(output_plane)
    return result


def _aggregate_block(values: list[float], aggregation: str) -> float:
    """Reduce one block. Deterministic, and never invents an absent value."""
    if aggregation == "FIRST_OCCURRENCE_MIN":
        return min(values)
    if aggregation == "EXTREMUM_MAX":
        return max(values)
    if aggregation in ("AREAL_FRACTION_MEAN",
                       "INTENSIVE_AREA_WEIGHTED_MEAN"):
        # The block cover is exact and same-CRS, so every contributing cell has
        # the same area and the area-weighted mean reduces to a plain mean.
        # Stage 4 refuses this kind across a reprojection precisely because
        # that equality stops holding.
        return math.fsum(values) / len(values)
    if aggregation == "CATEGORICAL_MAJORITY":
        counts: dict[float, int] = {}
        for value in values:
            counts[value] = counts.get(value, 0) + 1
        best = max(counts.values())
        # Ties resolve to the smallest label so the result never depends on
        # iteration order.
        return min(label for label, count in counts.items() if count == best)
    raise ValueError(f"unknown block aggregation {aggregation!r}")


def _spatial_block_aggregate(parameters: dict[str, Any],
                             inputs: dict[str, Any]) -> dict[str, Any]:
    """Execute an already-authorized exact block reduction.

    ``aggregation`` is an execution value, not scientific authority: Stage 4
    admits it only when it matches the aggregation the source's declared value
    class requires.
    """
    _exact_keys(parameters, {"block_x", "block_y", "aggregation"},
                "block-aggregate parameters")
    _exact_keys(inputs, {"source"}, "block-aggregate inputs")
    field = _validate_field(inputs["source"], "block-aggregate source")
    block_x = _integer(parameters["block_x"], "block_x", minimum=1)
    block_y = _integer(parameters["block_y"], "block_y", minimum=1)
    aggregation = parameters["aggregation"]
    if not isinstance(aggregation, str):
        raise TypeError("aggregation must be a string")
    width, height = len(field["x"]), len(field["y"])
    if width % block_x or height % block_y:
        raise ValueError(
            "block factors do not exactly cover the source field; a partial "
            "trailing block is not an aggregation")

    components = {}
    for name, tensor in field["components"].items():
        planes = []
        for plane in tensor:
            rows = []
            for out_y in range(height // block_y):
                row = []
                for out_x in range(width // block_x):
                    block = [plane[out_y * block_y + dy][out_x * block_x + dx]
                             for dy in range(block_y)
                             for dx in range(block_x)]
                    row.append(_aggregate_block(block, aggregation))
                rows.append(row)
            planes.append(rows)
        components[name] = planes

    def _centres(axis: list[float], block: int) -> list[float]:
        # Coordinate identity is decimal and exact at the descriptor/commit
        # boundary.  Binary accumulation made an ordinary 0.1/0.2 pair emit
        # 0.15000000000000002, which the authoritative affine validator
        # correctly rejected against the declared 0.15 centre.  Convert each
        # already-validated JSON number through its canonical decimal spelling,
        # perform the exact mean there, then emit the shortest JSON float.
        divisor = Decimal(block)
        return [float(sum(
                    (Decimal(str(value)) for value in
                     axis[index * block:(index + 1) * block]),
                    Decimal(0)) / divisor)
                for index in range(len(axis) // block)]

    return {"result": _field_with(
        field,
        x=_centres(field["x"], block_x),
        y=_centres(field["y"], block_y),
        components=components,
    )}


def _regrid_bilinear(parameters: dict[str, Any],
                     inputs: dict[str, Any]) -> dict[str, Any]:
    _exact_keys(parameters, {"target_x", "target_y"},
                "bilinear-regrid parameters")
    _exact_keys(inputs, {"source"}, "bilinear-regrid inputs")
    field = _validate_field(inputs["source"], "bilinear-regrid source")
    if len(field["x"]) < 2 or len(field["y"]) < 2:
        raise ValueError("bilinear regrid requires at least a 2x2 source grid")
    target_x = _axis(parameters["target_x"], "target_x")
    target_y = _axis(parameters["target_y"], "target_y")
    sample_points = [[(x, y) for x in target_x] for y in target_y]
    components = {
        name: _bilinear_tensor(tensor, field["x"], field["y"],
                               sample_points, "target")
        for name, tensor in field["components"].items()
    }
    return {"result": _field_with(
        field, x=target_x, y=target_y, components=components)}


def _authority_crs(value: Any, context: str) -> str:
    if not isinstance(value, str) or not _AUTHORITY_CRS.fullmatch(value):
        raise ValueError(
            f"{context} must be a closed authority identifier such as EPSG:4326")
    return value


_REPROJECTION_PUBLIC_PARAMETERS = frozenset({
    "source_crs", "target_crs", "target_x", "target_y"})
_REPROJECTION_BOUND_PARAMETERS = frozenset({
    *_REPROJECTION_PUBLIC_PARAMETERS, "pipeline_projjson",
    "source_axis_order", "target_axis_order"})


def bind_reprojection_parameters(
        parameters: dict[str, Any], *,
        source_axis_order: tuple[str, str] | list[str],
        target_axis_order: tuple[str, str] | list[str],
) -> dict[str, Any]:
    """Select and freeze the exact locally available target-to-source pipeline.

    PROJ may choose between operations based on locally installed grid files.
    Planning therefore uses ``TransformerGroup`` and refuses a degraded choice
    when its preferred operation is unavailable.  The chosen PROJJSON is then
    content-addressed with the transformation instead of repeating that choice
    at execution time.
    """
    _exact_keys(parameters, _REPROJECTION_PUBLIC_PARAMETERS,
                "reprojection authority parameters")
    source_crs = _authority_crs(parameters["source_crs"], "source_crs")
    target_crs = _authority_crs(parameters["target_crs"], "target_crs")
    target_x = _axis(parameters["target_x"], "target_x")
    target_y = _axis(parameters["target_y"], "target_y")
    for value, label in ((source_axis_order, "source_axis_order"),
                         (target_axis_order, "target_axis_order")):
        if (not isinstance(value, (tuple, list)) or len(value) != 2
                or any(not isinstance(item, str) or not item for item in value)
                or value[0] == value[1]):
            raise ValueError(f"{label} must name distinct x/y axes")
    try:
        from pyproj.transformer import TransformerGroup
        group = TransformerGroup(target_crs, source_crs, always_xy=True)
    except Exception as exc:  # pragma: no cover - site diagnostics
        raise ValueError("invalid or unavailable closed CRS transformation") from exc
    if not group.best_available:
        unavailable = ", ".join(
            operation.name for operation in group.unavailable_operations[:3])
        detail = f": {unavailable}" if unavailable else ""
        raise ValueError(
            "preferred PROJ operation is unavailable (required local grid "
            f"data may be missing){detail}")
    if not group.transformers:
        raise ValueError("PROJ found no available target-to-source operation")
    transformer = group.transformers[0]
    # Exercise every target sample now.  This resolves deferred pipeline
    # selection and exposes missing grid data before expensive upstream work.
    for y_value in target_y:
        for x_value in target_x:
            try:
                transformer.transform(x_value, y_value, errcheck=True)
            except Exception as exc:
                raise ValueError(
                    "PROJ could not transform every declared target sample") from exc
    try:
        pipeline = transformer.to_json_dict()
    except Exception as exc:  # pragma: no cover - dependency diagnostics
        raise ValueError("PROJ did not expose the selected pipeline identity") from exc
    if not isinstance(pipeline, dict) or not pipeline:
        raise ValueError("PROJ selected pipeline identity is empty")
    return {
        "source_crs": source_crs,
        "target_crs": target_crs,
        "target_x": target_x,
        "target_y": target_y,
        "source_axis_order": list(source_axis_order),
        "target_axis_order": list(target_axis_order),
        "pipeline_projjson": pipeline,
    }


def reprojection_sample_points(
        parameters: dict[str, Any], *, verify_local_selection: bool,
) -> list[list[tuple[float, float]]]:
    """Replay one frozen pipeline and return all target samples in source CRS."""
    _exact_keys(parameters, _REPROJECTION_BOUND_PARAMETERS,
                "bound reprojection parameters")
    source_crs = _authority_crs(parameters["source_crs"], "source_crs")
    target_crs = _authority_crs(parameters["target_crs"], "target_crs")
    target_x = _axis(parameters["target_x"], "target_x")
    target_y = _axis(parameters["target_y"], "target_y")
    pipeline = parameters["pipeline_projjson"]
    if not isinstance(pipeline, dict) or not pipeline:
        raise ValueError("pipeline_projjson must be a non-empty object")
    if verify_local_selection:
        public = {name: parameters[name]
                  for name in _REPROJECTION_PUBLIC_PARAMETERS}
        expected = bind_reprojection_parameters(
            public,
            source_axis_order=parameters["source_axis_order"],
            target_axis_order=parameters["target_axis_order"],
        )
        if strict_hash(expected["pipeline_projjson"]) != strict_hash(pipeline):
            raise ValueError(
                "bound PROJ pipeline is not the locally selected exact pipeline")
    try:
        import json
        from pyproj import Transformer
        transformer = Transformer.from_pipeline(json.dumps(
            pipeline, sort_keys=True, separators=(",", ":")))
    except Exception as exc:  # pragma: no cover - dependency/site diagnostics
        raise ValueError("bound PROJ pipeline is invalid or unavailable") from exc
    sample_points: list[list[tuple[float, float]]] = []
    for target_y_value in target_y:
        row: list[tuple[float, float]] = []
        for target_x_value in target_x:
            try:
                source_x, source_y = transformer.transform(
                    target_x_value, target_y_value, errcheck=True)
            except Exception as exc:
                raise ValueError(
                    "bound PROJ pipeline failed for a declared target sample") \
                    from exc
            row.append((
                _finite_number(source_x, "transformed source x"),
                _finite_number(source_y, "transformed source y"),
            ))
        sample_points.append(row)
    return sample_points


def _reproject_bilinear(parameters: dict[str, Any],
                        inputs: dict[str, Any]) -> dict[str, Any]:
    _exact_keys(parameters, _REPROJECTION_BOUND_PARAMETERS,
                "bilinear-reprojection parameters")
    _exact_keys(inputs, {"source"}, "bilinear-reprojection inputs")
    field = _validate_field(inputs["source"], "bilinear-reprojection source")
    if len(field["x"]) < 2 or len(field["y"]) < 2:
        raise ValueError("bilinear reprojection requires at least a 2x2 source grid")
    source_crs = _authority_crs(parameters["source_crs"], "source_crs")
    target_crs = _authority_crs(parameters["target_crs"], "target_crs")
    if field["crs"] != source_crs:
        raise ValueError("source_crs does not equal the source field CRS")
    if tuple(field["axis_order"]) != tuple(parameters["source_axis_order"]):
        raise ValueError(
            "source_axis_order does not equal the source field axes")
    target_x = _axis(parameters["target_x"], "target_x")
    target_y = _axis(parameters["target_y"], "target_y")
    sample_points = reprojection_sample_points(
        parameters, verify_local_selection=False)
    components = {
        name: _bilinear_tensor(tensor, field["x"], field["y"],
                               sample_points, "transformed target")
        for name, tensor in field["components"].items()
    }
    return {"result": _field_with(
        field, x=target_x, y=target_y, crs=target_crs,
        axis_order=parameters["target_axis_order"],
        components=components)}


def _vector_source(value: Any, context: str) -> dict[str, Any]:
    field = _validate_field(value, context)
    if set(field["components"]) != {"u", "v"}:
        raise ValueError(f"{context} must contain exactly u and v components")
    return field


def _zip_vector_components(
        field: dict[str, Any], operation: Callable[[float, float], tuple[float, float]],
        output_names: tuple[str, str]) -> dict[str, Any]:
    left_output: list[list[list[float]]] = []
    right_output: list[list[list[float]]] = []
    for u_plane, v_plane in zip(field["components"]["u"],
                                field["components"]["v"]):
        left_plane: list[list[float]] = []
        right_plane: list[list[float]] = []
        for u_row, v_row in zip(u_plane, v_plane):
            left_row: list[float] = []
            right_row: list[float] = []
            for u_value, v_value in zip(u_row, v_row):
                left, right = operation(u_value, v_value)
                left_row.append(_finite_number(left, output_names[0]))
                right_row.append(_finite_number(right, output_names[1]))
            left_plane.append(left_row)
            right_plane.append(right_row)
        left_output.append(left_plane)
        right_output.append(right_plane)
    return {output_names[0]: left_output, output_names[1]: right_output}


def _vector_rotate(parameters: dict[str, Any],
                   inputs: dict[str, Any]) -> dict[str, Any]:
    _exact_keys(parameters, {"angle_degrees"}, "vector-rotation parameters")
    _exact_keys(inputs, {"source"}, "vector-rotation inputs")
    field = _vector_source(inputs["source"], "vector-rotation source")
    angle = _finite_number(parameters["angle_degrees"], "angle_degrees") % 360.0
    radians = math.radians(angle)
    cosine = math.cos(radians)
    sine = math.sin(radians)
    components = _zip_vector_components(
        field,
        lambda u, v: (u * cosine - v * sine,
                      u * sine + v * cosine),
        ("u", "v"),
    )
    return {"result": _field_with(field, components=components)}


def _vector_uv_to_speed_direction(parameters: dict[str, Any],
                                  inputs: dict[str, Any]) -> dict[str, Any]:
    """Return magnitude and mathematical direction, never wind-from direction."""
    _exact_keys(parameters, set(), "vector-decomposition parameters")
    _exact_keys(inputs, {"source"}, "vector-decomposition inputs")
    field = _vector_source(inputs["source"], "vector-decomposition source")

    def decompose(u: float, v: float) -> tuple[float, float]:
        speed = math.hypot(u, v)
        # A zero vector has no intrinsic direction.  This explicit, versioned
        # execution convention prevents platform atan2 signed-zero behavior
        # from silently becoming scientific meaning.  Transform provenance
        # records the corresponding closed scientific assumption ID.
        direction = (0.0 if u == 0.0 and v == 0.0
                     else math.degrees(math.atan2(v, u)) % 360.0)
        return speed, direction

    components = _zip_vector_components(
        field, decompose, ("speed", "direction"))
    return {
        "speed": _field_with(
            field, components={"speed": components["speed"]}),
        "direction": _field_with(
            field, components={"direction": components["direction"]}),
    }


def _assemble_tiles(tiles: list[dict[str, Any]]) -> dict[str, Any]:
    """Join single-source tiles along x in bound manifest order.

    This is ordered concatenation of tiles that already agree on CRS, rows, and
    time — not a general mosaic.  Overlaps and holes raise instead of being
    blended, because a silently blended seam is indistinguishable from data.
    """
    if len(tiles) == 1:
        return tiles[0]
    fields = [_validate_field(tile, f"acquired tile[{index}]")
              for index, tile in enumerate(tiles)]
    reference = fields[0]
    for index, field in enumerate(fields[1:], start=1):
        for key in ("crs", "axis_order", "y", "time"):
            if field[key] != reference[key]:
                raise ValueError(
                    f"acquired tile[{index}] disagrees on {key}; Stage 5 joins "
                    "only tiles that share rows, CRS, and time")
        if set(field["components"]) != set(reference["components"]):
            raise ValueError(
                f"acquired tile[{index}] has different components")
    ordered = sorted(fields, key=lambda item: item["x"][0])
    x: list[float] = []
    for index, field in enumerate(ordered):
        if x and field["x"][0] <= x[-1]:
            raise ValueError(
                "acquired tiles overlap or repeat along x; the manifest does "
                "not describe a clean partition")
        x.extend(field["x"])
    components: dict[str, list[list[list[float]]]] = {}
    for name in sorted(reference["components"]):
        planes: list[list[list[float]]] = []
        for time_index in range(len(reference["time"])):
            rows: list[list[float]] = []
            for y_index in range(len(reference["y"])):
                row: list[float] = []
                for field in ordered:
                    row.extend(field["components"][name][time_index][y_index])
                rows.append(row)
            planes.append(rows)
        components[name] = planes
    return _field_with(reference, x=x, components=components)


def _acquisition_materialize(parameters: dict[str, Any],
                             inputs: dict[str, Any]) -> dict[str, Any]:
    """Materialize payload that a Stage-5 manifest binding already fetched.

    This operation never reaches a network.  It reads a local content-addressed
    store whose location is a *site* property (the environment variable below),
    while *what* it reads is pinned by the manifest root and asset list in its
    scientific parameters and by the sha256 of every blob.  Two nodes with
    different store paths therefore still produce identical results, and a
    tampered blob fails closed rather than flowing into a commit.
    """
    _exact_keys(parameters, {"content_binding"},
                "acquisition-materialize parameters")
    _exact_keys(inputs, set(), "acquisition-materialize inputs")
    binding = parameters["content_binding"]
    if not isinstance(binding, dict):
        raise ValueError("content_binding must be an object")
    _exact_keys(binding, {
        "schema", "binding_id", "manifest_root", "source_id", "receipt_id",
        "content_root", "assets", "source_schema_id", "coverage_contract_id",
        "assembly_mode", "descriptor",
    }, "fetched content binding")
    if binding["schema"] != "stage8r-fetched-content-binding-v1":
        raise ValueError("fetched content binding schema is unsupported")
    binding_payload = dict(binding)
    binding_id = binding_payload.pop("binding_id")
    if (not isinstance(binding_id, str)
            or strict_hash(binding_payload) != binding_id):
        raise ValueError("fetched content binding identity does not verify")
    manifest_root = binding["manifest_root"]
    if not isinstance(manifest_root, str) or not re.fullmatch(
            r"[0-9a-f]{64}", manifest_root):
        raise ValueError("manifest_root must be a lowercase sha256 digest")
    source_id = binding["source_id"]
    if not isinstance(source_id, str) or not source_id:
        raise ValueError("fetched content source_id must be text")
    assets = binding["assets"]
    if not isinstance(assets, list) or not assets:
        raise ValueError("fetched content assets must be a non-empty array")
    for asset in assets:
        if not isinstance(asset, dict):
            raise ValueError("fetched content asset must be an object")
        _exact_keys(asset, {"asset_id", "blob_sha256", "byte_size"},
                    "fetched content asset")
        if (not isinstance(asset["asset_id"], str) or not asset["asset_id"]
                or not isinstance(asset["blob_sha256"], str)
                or not re.fullmatch(r"[0-9a-f]{64}", asset["blob_sha256"])
                or isinstance(asset["byte_size"], bool)
                or not isinstance(asset["byte_size"], int)
                or asset["byte_size"] < 0):
            raise ValueError("fetched content asset fields are malformed")
    asset_ids = [item["asset_id"] for item in assets]
    if asset_ids != sorted(set(asset_ids)):
        raise ValueError("fetched content asset order is not canonical")
    expected_content_root = strict_hash({
        "schema": "stage8r-fetched-content-root-v1", "assets": assets})
    if binding["content_root"] != expected_content_root:
        raise ValueError("fetched content root does not verify")

    root = os.environ.get(ASSET_STORE_ENVIRONMENT, "")
    if not root:
        raise RuntimeError(
            f"{ASSET_STORE_ENVIRONMENT} must name the local asset store")
    store = Path(root)
    if not store.is_absolute():
        raise ValueError(f"{ASSET_STORE_ENVIRONMENT} must be an absolute path")

    receipt_path = store / "receipts" / f"{manifest_root}.json"
    if not receipt_path.exists():
        raise RuntimeError(
            f"no fetch receipt for manifest {manifest_root}; payload transfer "
            "must complete before materialization")
    receipt = strict_json_loads(receipt_path.read_text(encoding="utf-8"))
    if not isinstance(receipt, dict):
        raise ValueError("fetch receipt must be an object")
    _exact_keys(receipt, {
        "schema", "receipt_id", "manifest_root", "source_id", "assets",
        "content_root", "total_bytes", "transient_retries",
    }, "fetch receipt")
    if receipt["schema"] != "stage8r-fetch-receipt-v2":
        raise ValueError("fetch receipt schema is not stage8r-fetch-receipt-v2")
    if receipt.get("manifest_root") != manifest_root:
        raise ValueError("fetch receipt does not match the requested manifest")
    if receipt["source_id"] != source_id:
        raise ValueError("fetch receipt source does not match content binding")
    if receipt["assets"] != assets:
        raise ValueError("fetch receipt assets do not exactly match binding")
    if receipt["content_root"] != binding["content_root"]:
        raise ValueError("fetch receipt content root does not match binding")
    receipt_identity = {
        "schema": "stage8r-fetch-receipt-identity-v1",
        "manifest_root": receipt["manifest_root"],
        "source_id": receipt["source_id"],
        "content_root": receipt["content_root"],
        "assets": receipt["assets"],
        "total_bytes": receipt["total_bytes"],
    }
    if strict_hash(receipt_identity) != receipt["receipt_id"]:
        raise ValueError("fetch receipt identity does not verify")
    if receipt["receipt_id"] != binding["receipt_id"]:
        raise ValueError("fetch receipt identity does not match binding")
    if receipt["total_bytes"] != sum(item["byte_size"] for item in assets):
        raise ValueError("fetch receipt total bytes do not verify")

    tiles: list[Any] = []
    for asset in assets:
        asset_id = asset["asset_id"]
        digest = asset["blob_sha256"]
        blob = store / digest[:2] / digest
        if not blob.exists():
            raise RuntimeError(f"payload {digest} is absent from the store")
        payload = blob.read_bytes()
        if hashlib.sha256(payload).hexdigest() != digest:
            raise RuntimeError(
                f"payload {digest} failed its content check; refusing to "
                "materialize tampered bytes")
        if len(payload) != asset["byte_size"]:
            raise RuntimeError(
                f"payload {digest} size disagrees with fetched binding")
        tiles.append(strict_json_loads(payload.decode("utf-8")))

    assembly_mode = binding["assembly_mode"]
    if assembly_mode == "SINGLE_ASSET" and len(tiles) == 1:
        return {"result": tiles[0]}
    if (assembly_mode == "FIELD_JSON_X_TILES_V1"
            and all(isinstance(item, dict) for item in tiles)):
        return {"result": _assemble_tiles(tiles)}
    raise ValueError("fetched content assembly mode cannot materialize payloads")


def _same_grid(left: dict[str, Any], right: dict[str, Any],
               context: str) -> None:
    for key in ("crs", "axis_order", "x", "y", "time"):
        if left[key] != right[key]:
            raise ValueError(f"{context} disagree on {key}")


def _sole_component(field: dict[str, Any], context: str
                    ) -> list[list[list[float]]]:
    components = field["components"]
    if len(components) != 1:
        raise ValueError(f"{context} must carry exactly one component")
    return next(iter(components.values()))


def _reduced_downscale(parameters: dict[str, Any],
                       inputs: dict[str, Any]) -> dict[str, Any]:
    """A deterministic lightweight model: gain on the coarse field plus support.

    This stands in for a downscaling model.  It is deliberately trivial and
    meaningless; what matters for Stage 6 is that it is a *model* producer with
    its own declared evidence and applicability limits, competing against
    direct data, rather than a semantic transformation.
    """
    _exact_keys(parameters, {"gain"}, "reduced-downscale parameters")
    _exact_keys(inputs, {"coarse", "terrain"}, "reduced-downscale inputs")
    gain = _finite_number(parameters["gain"], "gain")
    coarse = _validate_field(inputs["coarse"], "downscale coarse")
    terrain = _validate_field(inputs["terrain"], "downscale terrain")
    _same_grid(coarse, terrain, "downscale coarse and terrain")
    support = _sole_component(terrain, "downscale terrain")
    components = {
        name: [[[value * gain + support[t][y][x]
                 for x, value in enumerate(row)]
                for y, row in enumerate(plane)]
               for t, plane in enumerate(tensor)]
        for name, tensor in coarse["components"].items()
    }
    return {"result": _field_with(coarse, components=components)}


def _reduced_consequence(parameters: dict[str, Any],
                         inputs: dict[str, Any]) -> dict[str, Any]:
    """A four-input reduced consequence model over meaningless quantities.

    Its only job is to be a genuine multi-input producer: the selected flow
    field, two scalars, and a static field must all be resolved, bound, and
    executed before it can run.
    """
    _exact_keys(parameters, {"threshold"}, "reduced-consequence parameters")
    _exact_keys(inputs, {"flow", "fuel", "ignition", "terrain"},
                "reduced-consequence inputs")
    threshold = _finite_number(parameters["threshold"], "threshold")
    fuel = _finite_number(inputs["fuel"], "fuel")
    ignition = _finite_number(inputs["ignition"], "ignition")
    flow = _validate_field(inputs["flow"], "consequence flow")
    terrain = _validate_field(inputs["terrain"], "consequence terrain")
    _same_grid(flow, terrain, "consequence flow and terrain")
    support = _sole_component(terrain, "consequence terrain")
    components = {
        name: [[[min(value * fuel + support[t][y][x] * ignition, threshold)
                 for x, value in enumerate(row)]
                for y, row in enumerate(plane)]
               for t, plane in enumerate(tensor)]
        for name, tensor in flow["components"].items()
    }
    return {"result": _field_with(flow, components=components)}


_OPERATIONS: dict[str, tuple[str, Operation, bool]] = {
    "acquisition.materialize.v1": ("1.0.0", _acquisition_materialize, True),
    "reduced.consequence.v1": ("1.0.0", _reduced_consequence, True),
    "reduced.downscale.v1": ("1.0.0", _reduced_downscale, True),
    "synthetic.constant.v1": ("1.0.0", _constant, True),
    "synthetic.add.v1": ("1.0.0", _add, True),
    "synthetic.pair.v1": ("1.0.0", _pair, True),
    "synthetic.scale.v1": ("1.0.0", _scale, True),
    "synthetic.sleep.v1": ("1.0.0", _sleep, True),
    "synthetic.fail.v1": ("1.0.0", _fail, True),
    "synthetic.fail_once.v1": ("1.0.0", _fail_once, True),
    "synthetic.identity.v1": ("1.0.0", _identity, True),
    "native.file_pointer.v1": ("1.0.0", _native_file_pointer, True),
    "native.file_pointer_identity.v1": (
        "1.0.0", _native_file_pointer_identity, True),
    "transform.unit_affine.v1": ("1.0.0", _unit_affine, True),
    "transform.spatial_subset.v1": ("1.0.0", _spatial_subset, True),
    "transform.temporal_subset.v1": ("1.0.0", _temporal_subset, True),
    "transform.temporal_align.v1": ("1.0.0", _temporal_align, True),
    "transform.spatial_block_aggregate.v1": (
        "1.0.0", _spatial_block_aggregate, True),
    "transform.regrid_bilinear.v1": ("1.0.0", _regrid_bilinear, True),
    "transform.reproject_bilinear.v1": (
        "1.0.0", _reproject_bilinear, True),
    "transform.vector_rotate.v1": ("1.0.0", _vector_rotate, True),
    "transform.vector_uv_to_speed_direction.v1": (
        "1.0.0", _vector_uv_to_speed_direction, True),
}


def operation_keys() -> tuple[str, ...]:
    return tuple(sorted(_OPERATIONS))


def _dependency_identity(key: str) -> bytes:
    """Bind result-affecting native dependency versions into component IDs."""
    if key != "transform.reproject_bilinear.v1":
        return b""
    try:
        import pyproj
        pyproj_version = pyproj.__version__
        proj_version = pyproj.proj_version_str
        epsg_version = pyproj.database.get_database_metadata("EPSG.VERSION")
        proj_database_version = pyproj.database.get_database_metadata(
            "PROJ.VERSION")
    except (ImportError, AttributeError) as exc:
        raise RuntimeError(
            "the closed reprojection component requires identifiable pyproj/PROJ"
        ) from exc
    if not pyproj_version or not proj_version:
        raise RuntimeError("pyproj/PROJ dependency versions cannot be empty")
    return (f"pyproj={pyproj_version}\0PROJ={proj_version}"
            f"\0EPSG={epsg_version}\0PROJ_DB={proj_database_version}"
            .encode("utf-8"))


def operation_component(key: str) -> ExecutableComponent:
    try:
        version, _operation, retry_safe = _OPERATIONS[key]
    except KeyError as exc:
        raise KeyError(f"operation is not in the closed Stage-1 registry: {key}") from exc
    source = Path(__file__).read_bytes()
    digest = hashlib.sha256(
        key.encode("utf-8") + b"\0" + source + b"\0"
        + _dependency_identity(key)
    ).hexdigest()
    return ExecutableComponent(
        component_id=key,
        version=version,
        implementation_digest=digest,
        operation_key=key,
        deterministic=True,
        idempotent=True,
        retry_safe=retry_safe,
    )


def execute_component(component: ExecutableComponent,
                      parameters: dict[str, Any],
                      inputs: dict[str, Any]) -> dict[str, Any]:
    expected = operation_component(component.operation_key)
    if expected != component:
        raise RuntimeError(
            f"component binding mismatch for {component.operation_key!r}")
    operation = _OPERATIONS[component.operation_key][1]
    return operation(parameters, inputs)
