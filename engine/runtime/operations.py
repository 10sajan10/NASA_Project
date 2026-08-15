"""Closed Stage-1 executable operation registry.

Only keys declared here can run in the local worker.  There is intentionally no
generic import/callable escape hatch, and no WRF, MPI, or scheduler operation.
"""
from __future__ import annotations

import hashlib
import math
import re
from bisect import bisect_right
from pathlib import Path
from typing import Any, Callable

from .types import ExecutableComponent


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


# Stage 4 intentionally uses one small, strict interchange value for local
# gridded-field transformations.  Component tensors are indexed [time][y][x].
# This is an execution format, not a claim that JSON is an appropriate storage
# format for production-sized scientific arrays.
_FIELD_SCHEMA = "field-json-v1"
_FIELD_KEYS = frozenset({"schema", "crs", "x", "y", "time", "components"})
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
    if any(right <= left for left, right in zip(result, result[1:])):
        raise ValueError(f"{context} coordinates must be strictly increasing")
    return result


def _validate_field(value: Any, context: str = "field") -> dict[str, Any]:
    """Validate and normalize a canonical finite ``field-json-v1`` value."""
    if not isinstance(value, dict):
        raise TypeError(f"{context} must be an object")
    _exact_keys(value, _FIELD_KEYS, context)
    if value["schema"] != _FIELD_SCHEMA:
        raise ValueError(f"{context}.schema must be {_FIELD_SCHEMA!r}")
    crs = value["crs"]
    if not isinstance(crs, str) or not crs.strip():
        raise ValueError(f"{context}.crs must be a non-empty string")
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
                components: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "schema": _FIELD_SCHEMA,
        "crs": field["crs"] if crs is None else crs,
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
        raise TypeError("unit-affine source must be a number or field-json-v1")
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
    if coordinate < axis[0] or coordinate > axis[-1]:
        raise ValueError(f"{context} coordinate {coordinate} is outside source grid")
    if coordinate == axis[-1]:
        return len(axis) - 2, 1.0
    lower = bisect_right(axis, coordinate) - 1
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


def _reproject_bilinear(parameters: dict[str, Any],
                        inputs: dict[str, Any]) -> dict[str, Any]:
    _exact_keys(parameters,
                {"source_crs", "target_crs", "target_x", "target_y"},
                "bilinear-reprojection parameters")
    _exact_keys(inputs, {"source"}, "bilinear-reprojection inputs")
    field = _validate_field(inputs["source"], "bilinear-reprojection source")
    if len(field["x"]) < 2 or len(field["y"]) < 2:
        raise ValueError("bilinear reprojection requires at least a 2x2 source grid")
    source_crs = _authority_crs(parameters["source_crs"], "source_crs")
    target_crs = _authority_crs(parameters["target_crs"], "target_crs")
    if field["crs"] != source_crs:
        raise ValueError("source_crs does not equal the source field CRS")
    target_x = _axis(parameters["target_x"], "target_x")
    target_y = _axis(parameters["target_y"], "target_y")
    try:
        from pyproj import Transformer
        transformer = Transformer.from_crs(
            target_crs, source_crs, always_xy=True)
    except Exception as exc:  # pragma: no cover - dependency/site diagnostics
        raise ValueError("invalid or unavailable closed CRS transformation") from exc
    sample_points: list[list[tuple[float, float]]] = []
    for target_y_value in target_y:
        row: list[tuple[float, float]] = []
        for target_x_value in target_x:
            source_x, source_y = transformer.transform(
                target_x_value, target_y_value, errcheck=True)
            row.append((
                _finite_number(source_x, "transformed source x"),
                _finite_number(source_y, "transformed source y"),
            ))
        sample_points.append(row)
    components = {
        name: _bilinear_tensor(tensor, field["x"], field["y"],
                               sample_points, "transformed target")
        for name, tensor in field["components"].items()
    }
    return {"result": _field_with(
        field, x=target_x, y=target_y, crs=target_crs,
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


_OPERATIONS: dict[str, tuple[str, Operation, bool]] = {
    "synthetic.constant.v1": ("1.0.0", _constant, True),
    "synthetic.add.v1": ("1.0.0", _add, True),
    "synthetic.pair.v1": ("1.0.0", _pair, True),
    "synthetic.scale.v1": ("1.0.0", _scale, True),
    "synthetic.sleep.v1": ("1.0.0", _sleep, True),
    "synthetic.fail.v1": ("1.0.0", _fail, True),
    "synthetic.fail_once.v1": ("1.0.0", _fail_once, True),
    "synthetic.identity.v1": ("1.0.0", _identity, True),
    "transform.unit_affine.v1": ("1.0.0", _unit_affine, True),
    "transform.spatial_subset.v1": ("1.0.0", _spatial_subset, True),
    "transform.temporal_subset.v1": ("1.0.0", _temporal_subset, True),
    "transform.temporal_align.v1": ("1.0.0", _temporal_align, True),
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
    except (ImportError, AttributeError) as exc:
        raise RuntimeError(
            "the closed reprojection component requires identifiable pyproj/PROJ"
        ) from exc
    if not pyproj_version or not proj_version:
        raise RuntimeError("pyproj/PROJ dependency versions cannot be empty")
    return (f"pyproj={pyproj_version}\0PROJ={proj_version}"
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
