"""Producer registry — agnostic to producer implementation.

Accepts anything that quacks like a producer:

  * `name`     : str
  * `produces` : iterable of variable names (strings) or VarSpec objects
  * `requires` : iterable of variable names (strings) or VarSpec objects
  * `run(cube, request)` -> dict[str, int] | list[str] | None

This keeps the orchestration engine model-agnostic. Legacy `fusion.Producer`
instances and new `engine.ProducerV2` instances coexist in the same
registry, and the pipeline runner treats them uniformly.
"""
from __future__ import annotations

from typing import Any, Iterable, Iterator


def _names(items: Iterable) -> tuple[str, ...]:
    """Extract variable names from a list of strings or VarSpec-like objects."""
    out: list[str] = []
    for it in items or ():
        if isinstance(it, str):
            out.append(it)
        elif hasattr(it, "name"):
            out.append(str(it.name))
        else:
            out.append(str(it))
    return tuple(out)


def producer_produces(producer) -> tuple[str, ...]:
    return _names(getattr(producer, "produces", ()))


def producer_requires(producer) -> tuple[str, ...]:
    return _names(getattr(producer, "requires", ()))


class ProducerRegistry:
    """Map producer name -> producer, and variable name -> producing producer."""

    def __init__(self) -> None:
        self._by_name: dict[str, Any] = {}
        self._var_to_producer: dict[str, str] = {}

    # ---- registration ----------------------------------------------------
    def register(self, producer, *, replace: bool = False):
        name = getattr(producer, "name", None)
        if not name:
            raise ValueError("producer must have a non-empty .name attribute")
        produces = producer_produces(producer)
        if not produces:
            raise ValueError(f"producer {name!r}: empty .produces declaration")

        if name in self._by_name and not replace:
            raise ValueError(f"producer already registered: {name!r}")
        for var in produces:
            owner = self._var_to_producer.get(var)
            if owner is not None and owner != name and not replace:
                raise ValueError(
                    f"variable {var!r} already produced by {owner!r}; "
                    f"cannot register {name!r}")

        self._by_name[name] = producer
        for var in produces:
            self._var_to_producer[var] = name
        return producer

    def unregister(self, name: str) -> None:
        producer = self._by_name.pop(name, None)
        if producer is None:
            return
        for var in producer_produces(producer):
            if self._var_to_producer.get(var) == name:
                self._var_to_producer.pop(var, None)

    # ---- lookup ----------------------------------------------------------
    def get(self, name: str):
        try:
            return self._by_name[name]
        except KeyError as exc:
            raise KeyError(f"no producer named {name!r}") from exc

    def producer_for(self, variable: str):
        try:
            return self._by_name[self._var_to_producer[variable]]
        except KeyError as exc:
            raise KeyError(
                f"no producer registered for variable {variable!r}") from exc

    def has(self, name: str) -> bool:
        return name in self._by_name

    def has_variable(self, variable: str) -> bool:
        return variable in self._var_to_producer

    # ---- introspection ---------------------------------------------------
    def names(self) -> list[str]:
        return list(self._by_name.keys())

    def variables(self) -> dict[str, str]:
        return dict(sorted(self._var_to_producer.items()))

    def __contains__(self, name: str) -> bool:
        return name in self._by_name

    def __iter__(self) -> Iterator:
        return iter(self._by_name.values())

    def __len__(self) -> int:
        return len(self._by_name)


def to_engine_registry(source: Any) -> ProducerRegistry:
    """Bridge any registry-like object (or iterable of producers) into an
    engine ProducerRegistry without modifying the producers.

    Accepts:
      * a fusion.ProducerRegistry (or any object with a `producers()` method
        returning an iterable of producers)
      * a plain iterable of producer-shaped objects

    Producers pass through unchanged. Use `to_adapter_registry` if you
    want fusion-shaped producers auto-promoted to engine adapters
    (cube.satisfies-aware skip + merge-policy benefits).
    """
    if hasattr(source, "producers") and callable(source.producers):
        items = source.producers()
    else:
        items = source
    eng = ProducerRegistry()
    for p in items:
        eng.register(p)
    return eng


def _maybe_wrap_legacy(producer) -> Any:
    """Promote a legacy fusion producer to an engine adapter when we can
    recognise the shape; otherwise pass through.

    Detection is duck-typed (we check for `.driver` with `.fetch`, or for
    a callable `.func`) so this works against fusion.DriverProducer /
    fusion.FunctionProducer without importing fusion here.
    """
    # Already an engine-side producer: pass through.
    from .contracts import ProducerV2
    if isinstance(producer, ProducerV2):
        return producer

    # DriverProducer-shaped: wraps a Driver with .fetch.
    driver = getattr(producer, "driver", None)
    if driver is not None and hasattr(driver, "fetch"):
        from .adapters import DataDriverAdapter
        return DataDriverAdapter(
            driver=driver,
            name=getattr(producer, "name", driver.name),
            produces=getattr(producer, "produces", driver.produces),
            requires=getattr(producer, "requires", ()),
            time_end_mode=getattr(producer, "time_end_mode",
                                  "as_requested"),
            time_check_mode=getattr(producer, "time_check_mode",
                                    "as_requested"),
        )

    # FunctionProducer-shaped: holds a callable .func(cube, request).
    func = getattr(producer, "func", None)
    if callable(func):
        from .adapters import ModelFunctionAdapter
        return ModelFunctionAdapter(
            name=producer.name,
            produces=producer.produces,
            requires=getattr(producer, "requires", ()),
            func=func,
        )

    return producer


def to_adapter_registry(source: Any) -> ProducerRegistry:
    """Bridge a fusion registry (or iterable of producers) into an engine
    ProducerRegistry, AUTO-PROMOTING legacy fusion-shaped producers to
    engine adapters.

    A producer wrapped as a ``DataDriverAdapter`` or
    ``ModelFunctionAdapter`` picks up:

      * cube.satisfies-aware skip on re-runs (resolution + time window
        coverage), not just cube.has
      * merge_policy enforcement on writes (when produces declares a
        VarSpec with a non-default policy)
      * the ProducerV2 contract, so future scheduler features
        (checkpointing, halo I/O, version tracking) apply uniformly

    Producers that already look like ProducerV2 — or that can't be
    safely recognised — pass through unchanged.
    """
    if hasattr(source, "producers") and callable(source.producers):
        items = source.producers()
    else:
        items = source
    eng = ProducerRegistry()
    for p in items:
        eng.register(_maybe_wrap_legacy(p))
    return eng
