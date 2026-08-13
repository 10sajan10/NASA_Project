"""Closed Stage-1 executable operation registry.

Only keys declared here can run in the local worker.  There is intentionally no
generic import/callable escape hatch, and no WRF, MPI, or scheduler operation.
"""
from __future__ import annotations

import hashlib
import math
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


_OPERATIONS: dict[str, tuple[str, Operation, bool]] = {
    "synthetic.constant.v1": ("1.0.0", _constant, True),
    "synthetic.add.v1": ("1.0.0", _add, True),
    "synthetic.pair.v1": ("1.0.0", _pair, True),
    "synthetic.scale.v1": ("1.0.0", _scale, True),
    "synthetic.sleep.v1": ("1.0.0", _sleep, True),
    "synthetic.fail.v1": ("1.0.0", _fail, True),
    "synthetic.fail_once.v1": ("1.0.0", _fail_once, True),
    "synthetic.identity.v1": ("1.0.0", _identity, True),
}


def operation_keys() -> tuple[str, ...]:
    return tuple(sorted(_OPERATIONS))


def operation_component(key: str) -> ExecutableComponent:
    try:
        version, _operation, retry_safe = _OPERATIONS[key]
    except KeyError as exc:
        raise KeyError(f"operation is not in the closed Stage-1 registry: {key}") from exc
    source = Path(__file__).read_bytes()
    digest = hashlib.sha256(key.encode("utf-8") + b"\0" + source).hexdigest()
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
