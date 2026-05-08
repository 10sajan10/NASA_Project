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
    engine ProducerRegistry.

    Accepts:
      * a fusion.ProducerRegistry (or any object with a `producers()` method
        returning an iterable of producers)
      * a plain iterable of producer-shaped objects

    The producers themselves don't need to change. Engine accepts anything
    with `name`, `produces`, `requires`, and `run(cube, request)`, which is
    exactly the legacy fusion.Producer protocol.
    """
    if hasattr(source, "producers") and callable(source.producers):
        items = source.producers()
    else:
        items = source
    eng = ProducerRegistry()
    for p in items:
        eng.register(p)
    return eng
