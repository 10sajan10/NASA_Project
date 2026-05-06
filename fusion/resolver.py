"""Dependency resolver for cube variables.

The resolver turns "I need these variables" into a producer execution graph.
Models no longer need to know which upstream model or data adapter creates an
input; they ask the resolver/cube for the variable, and producers write every
intermediate result back to central storage.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

from cube.store import Cube
from .producers import Producer, ProducerRegistry, VariableRequest


@dataclass(frozen=True)
class PlanStep:
    producer: str
    produces: tuple[str, ...]
    requires: tuple[str, ...]
    kind: str
    can_run_parallel: bool


class DependencyResolver:
    def __init__(self, registry: ProducerRegistry):
        self.registry = registry
        self._ran: set[tuple[str, Optional[object], Optional[object]]] = set()

    def reset_run_cache(self) -> None:
        self._ran.clear()

    def ensure(self, cube: Cube, variables: Iterable[str], *,
               t_start=None, t_end=None, force: bool = False,
               context: Optional[dict] = None) -> list[str]:
        request = VariableRequest(t_start=t_start, t_end=t_end, force=force,
                                  context=dict(context or {}))
        produced: list[str] = []
        for variable in variables:
            produced.extend(self._ensure_one(cube, variable, request, stack=[]))
        return produced

    def plan(self, cube: Cube, variables: Iterable[str], *,
             t_start=None, t_end=None, force: bool = False,
             context: Optional[dict] = None) -> list[PlanStep]:
        request = VariableRequest(t_start=t_start, t_end=t_end, force=force,
                                  context=dict(context or {}))
        steps: list[PlanStep] = []
        seen: set[str] = set()
        for variable in variables:
            self._collect_plan(cube, variable, request, steps, seen, stack=[])
        return steps

    def explain_plan(self, cube: Cube, variables: Iterable[str], *,
                     t_start=None, t_end=None, force: bool = False,
                     context: Optional[dict] = None) -> str:
        steps = self.plan(cube, variables, t_start=t_start, t_end=t_end,
                          force=force, context=context)
        if not steps:
            return "all requested variables are already present in the cube"
        lines = []
        for idx, step in enumerate(steps, start=1):
            req = ", ".join(step.requires) if step.requires else "none"
            prod = ", ".join(step.produces)
            lines.append(
                f"{idx}. {step.producer} [{step.kind}] -> {prod}; "
                f"requires: {req}")
        return "\n".join(lines)

    def _ensure_one(self, cube: Cube, variable: str, request: VariableRequest,
                    stack: list[str]) -> list[str]:
        producer = self.registry.producer_for(variable)
        if producer.is_satisfied(cube, variable, request):
            return []
        if producer.name in stack:
            cycle = " -> ".join(stack + [producer.name])
            raise RuntimeError(f"producer dependency cycle: {cycle}")

        produced: list[str] = []
        for required in producer.requires:
            produced.extend(
                self._ensure_one(cube, required, request,
                                 stack=stack + [producer.name]))

        key = (producer.name, request.t_start, request.t_end)
        if key not in self._ran or request.force:
            print(f"[resolver] {producer.name}: producing "
                  f"{', '.join(producer.produces)}")
            produced_now = producer.run(cube, request)
            produced.extend(produced_now)
            self._ran.add(key)

        if not producer.is_satisfied(cube, variable, request):
            raise RuntimeError(
                f"{producer.name!r} ran but {variable!r} is still missing")
        return produced

    def _collect_plan(self, cube: Cube, variable: str, request: VariableRequest,
                      steps: list[PlanStep], seen: set[str],
                      stack: list[str]) -> None:
        producer = self.registry.producer_for(variable)
        if producer.is_satisfied(cube, variable, request):
            return
        if producer.name in stack:
            cycle = " -> ".join(stack + [producer.name])
            raise RuntimeError(f"producer dependency cycle: {cycle}")
        for required in producer.requires:
            self._collect_plan(cube, required, request, steps, seen,
                               stack=stack + [producer.name])
        if producer.name in seen:
            return
        seen.add(producer.name)
        steps.append(PlanStep(
            producer=producer.name,
            produces=tuple(producer.produces),
            requires=tuple(producer.requires),
            kind=getattr(producer, "kind", "model"),
            can_run_parallel=getattr(producer, "can_run_parallel", False),
        ))
