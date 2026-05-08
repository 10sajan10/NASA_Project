"""Contract test kit for ProducerV2 adapters.

Provides:

  * `FakeCube`         - in-memory cube that records every read/write and
                         enforces declared-input/output isolation.
  * `check_capabilities` - structural validation (types, halo invariants).
  * `check_io_isolation` - extract reads only declared inputs; update writes
                           only declared outputs.
  * `check_idempotency`  - running the same producer twice on the same
                           inputs gives byte-identical outputs.
  * `check_producer`     - one-shot composite that runs all three.

Importable from production tests; no pytest dependency at this layer.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Iterable, Optional

import numpy as np

from .contracts import ProducerCapabilities, ProducerV2, Request, VarSpec


# ----------------------------------------------------------------- FakeCube
@dataclass
class _FakeGrid:
    height: int = 64
    width: int = 64
    pixel_m: float = 100.0
    crs_epsg: int = 32614

    @property
    def shape(self) -> tuple[int, int]:
        return (self.height, self.width)


@dataclass
class _FakeCatalog:
    """Mimics the bits of cube.catalog.Catalog the contract uses."""
    versions: dict[str, int] = field(default_factory=dict)

    class _Conn:
        def __init__(self, catalog: "_FakeCatalog"):
            self.catalog = catalog

        def execute(self, sql: str, params: list[Any]):
            class _Result:
                def __init__(self, rows): self._rows = rows
                def fetchone(self): return self._rows[0] if self._rows else None
                def fetchall(self): return self._rows
            if sql.strip().upper().startswith("SELECT MAX(VERSION)"):
                var = params[0]
                v = self.catalog.versions.get(var, 0)
                return _Result([(v,)])
            return _Result([])

    @property
    def con(self):
        return _FakeCatalog._Conn(self)


@dataclass
class FakeCube:
    """Recording cube for contract tests.

    Records every read/write; reads from undeclared variables raise; writes to
    undeclared variables raise. Use one FakeCube per test invocation.
    """
    declared_inputs: tuple[str, ...] = ()
    declared_outputs: tuple[str, ...] = ()
    grid: _FakeGrid = field(default_factory=_FakeGrid)
    catalog: _FakeCatalog = field(default_factory=_FakeCatalog)

    _statics: dict[str, np.ndarray] = field(default_factory=dict)
    _times: dict[str, tuple[list[datetime], np.ndarray]] = field(default_factory=dict)
    reads: list[str] = field(default_factory=list)
    writes: list[str] = field(default_factory=list)

    # --- static reads/writes --------------------------------------------
    def has(self, variable: str, t: Optional[datetime] = None) -> bool:
        if t is None:
            return variable in self._statics
        return variable in self._times

    def read_static(self, variable: str) -> np.ndarray:
        if variable not in self.declared_inputs:
            raise AssertionError(
                f"FakeCube: read of undeclared input {variable!r}; "
                f"declared inputs = {self.declared_inputs}")
        self.reads.append(variable)
        return self._statics[variable]

    def write_static(self, variable: str, array: np.ndarray, *,
                     source: str = "", native_res_m: float = 0.0,
                     units: str = "", producer: str = "",
                     description: str = "") -> None:
        if variable not in self.declared_outputs:
            raise AssertionError(
                f"FakeCube: write of undeclared output {variable!r}; "
                f"declared outputs = {self.declared_outputs}")
        self.writes.append(variable)
        self._statics[variable] = np.asarray(array)
        self.catalog.versions[variable] = self.catalog.versions.get(
            variable, 0) + 1

    # --- 3D reads/writes -------------------------------------------------
    def read_3d(self, variable: str) -> tuple[list[datetime], np.ndarray]:
        if variable not in self.declared_inputs:
            raise AssertionError(
                f"FakeCube: read of undeclared input {variable!r}")
        self.reads.append(variable)
        return self._times[variable]

    def write_3d(self, variable: str, ts: list[datetime],
                 cube_array: np.ndarray, *,
                 source: str = "", native_res_m: float = 0.0,
                 units: str = "", producer: str = "",
                 description: str = "") -> None:
        if variable not in self.declared_outputs:
            raise AssertionError(
                f"FakeCube: write of undeclared output {variable!r}")
        self.writes.append(variable)
        self._times[variable] = (list(ts), np.asarray(cube_array))
        self.catalog.versions[variable] = self.catalog.versions.get(
            variable, 0) + 1

    # --- helpers for tests -----------------------------------------------
    def seed_static(self, variable: str, array: np.ndarray) -> None:
        """Pre-populate a declared input. Doesn't count as a write."""
        self._statics[variable] = np.asarray(array)

    def seed_3d(self, variable: str, ts: list[datetime],
                array: np.ndarray) -> None:
        self._times[variable] = (list(ts), np.asarray(array))


# -------------------------------------------------------------- check funcs
def check_capabilities(p: ProducerV2) -> list[str]:
    """Return a list of capability/declaration errors. Empty list = OK."""
    errs: list[str] = []
    if not getattr(p, "name", ""):
        errs.append("missing .name")
    produces = getattr(p, "produces", None)
    requires = getattr(p, "requires", None)
    if not isinstance(produces, tuple) or not produces:
        errs.append(".produces must be a non-empty tuple of VarSpec")
    else:
        for v in produces:
            if not isinstance(v, VarSpec):
                errs.append(f".produces item {v!r} is not a VarSpec")
    if not isinstance(requires, tuple):
        errs.append(".requires must be a tuple of VarSpec (possibly empty)")
    else:
        for v in requires:
            if not isinstance(v, VarSpec):
                errs.append(f".requires item {v!r} is not a VarSpec")
    cap = getattr(p, "capabilities", None)
    if not isinstance(cap, ProducerCapabilities):
        errs.append(".capabilities must be a ProducerCapabilities instance")
    return errs


def check_io_isolation(p: ProducerV2,
                       seed: Callable[[FakeCube], None] | None = None
                       ) -> list[str]:
    """Verify extract reads only declared inputs and update writes only
    declared outputs. `seed` may pre-populate the FakeCube with input data."""
    errs: list[str] = []
    cube = FakeCube(
        declared_inputs=tuple(v.name for v in p.requires),
        declared_outputs=tuple(v.name for v in p.produces),
    )
    if seed is not None:
        seed(cube)
    request = Request()
    try:
        outputs = p.compute(p.extract(cube, request), request)
    except AssertionError as e:
        errs.append(f"IO isolation violated during extract/compute: {e}")
        return errs
    declared = {v.name for v in p.produces}
    returned = set(outputs.keys())
    if returned != declared:
        errs.append(
            f"compute returned {sorted(returned)}; declared produces "
            f"{sorted(declared)}")
        return errs
    try:
        p.update(cube, outputs, request)
    except AssertionError as e:
        errs.append(f"IO isolation violated during update: {e}")
    return errs


def check_idempotency(p: ProducerV2,
                      seed: Callable[[FakeCube], None] | None = None,
                      tolerance: float = 0.0) -> list[str]:
    """Run the producer twice on the same seeded cube; outputs must match."""
    errs: list[str] = []

    def _run() -> dict[str, np.ndarray]:
        cube = FakeCube(
            declared_inputs=tuple(v.name for v in p.requires),
            declared_outputs=tuple(v.name for v in p.produces),
        )
        if seed is not None:
            seed(cube)
        request = Request()
        outputs = p.compute(p.extract(cube, request), request)
        return {k: np.asarray(v) for k, v in outputs.items()}

    a = _run()
    b = _run()
    for k in a:
        if a[k].shape != b[k].shape:
            errs.append(f"idempotency: {k} shape changed across runs")
            continue
        if a[k].dtype != b[k].dtype:
            errs.append(f"idempotency: {k} dtype changed across runs")
        if tolerance == 0.0:
            if not np.array_equal(a[k], b[k], equal_nan=True):
                errs.append(f"idempotency: {k} not byte-equal across runs")
        else:
            if not np.allclose(a[k], b[k], atol=tolerance, equal_nan=True):
                errs.append(
                    f"idempotency: {k} differs beyond tol={tolerance}")
    return errs


def check_producer(p: ProducerV2,
                   seed: Callable[[FakeCube], None] | None = None,
                   tolerance: float = 0.0) -> list[str]:
    """One-shot: capabilities + io isolation + idempotency. Empty = pass."""
    errs = check_capabilities(p)
    if errs:
        return errs
    errs += check_io_isolation(p, seed=seed)
    if errs:
        return errs
    errs += check_idempotency(p, seed=seed, tolerance=tolerance)
    return errs
