"""Declarative pipeline specification — model-agnostic DAG of work units.

A `Pipeline` is a directed acyclic graph over producer names. Edges encode
ordering constraints: edge `u -> v` means `v` runs after `u` completes.

Three ways to build a pipeline:

    # 1. Chain (sequential, with optional parallel layers as nested seqs)
    Pipeline.chain(["step_a", "step_b", ("step_c1", "step_c2"), "step_d"])

    # 2. Explicit DAG
    Pipeline().add("a").add("b", after="a").add("c", after=["a"])

    # 3. Inferred from a registry (target-driven, like the legacy resolver)
    Pipeline.from_targets(["some_output_var"], registry=reg)

Triggers attach event-driven follow-up steps:

    pipeline.on_complete("some_producer", run="follow_up_producer",
                         when=lambda cube: state_predicate(cube))

The pipeline holds *names*, not producer objects. A `ProducerRegistry`
resolves names to runnable producers at execution time, so the same
pipeline spec is reusable across runs with different wirings.

Nothing in this module knows about specific models or variables. The
engine just orchestrates declared dependencies.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional, Sequence, Union

from .registry import ProducerRegistry, producer_requires


_AfterSpec = Union[str, Sequence[str], None]


def parallel(*names: str) -> tuple[str, ...]:
    """Mark a set of nodes as a parallel layer in a chain. Sugar.

    Equivalent to passing them as a tuple inside `Pipeline.chain([...])`.
    """
    return tuple(names)


@dataclass
class PipelineNode:
    """One step in the DAG."""
    name: str
    after: tuple[str, ...] = ()
    optional: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class Trigger:
    """Conditional follow-up step.

    Fires when `source` completes successfully and `when(cube)` is True.
    The `target` producer is added to the running plan and scheduled
    like any other node, with its dependency set to {source} unless
    declared otherwise.
    """
    source: str
    target: str
    when: Callable[[Any], bool]
    name: str = ""

    def __post_init__(self):
        if not self.name:
            self.name = f"{self.source}->{self.target}"


class Pipeline:
    """Declarative DAG of producer names + optional triggers."""

    def __init__(self, name: str = "pipeline"):
        self.name = name
        self._nodes: dict[str, PipelineNode] = {}
        self._triggers: list[Trigger] = []

    # ---- builders --------------------------------------------------------
    @classmethod
    def chain(cls, steps: Sequence[Union[str, Sequence[str]]],
              name: str = "chain") -> "Pipeline":
        """Build a sequential chain. A nested sequence (tuple/list) is a
        parallel layer that fans out from the previous step and fans back in
        to the next.

        Example:
            Pipeline.chain(["a", ("b", "c"), "d"])
            ->  a -> {b, c (parallel)} -> d
        """
        p = cls(name)
        prev: tuple[str, ...] = ()
        for step in steps:
            if isinstance(step, str):
                p.add(step, after=list(prev))
                prev = (step,)
            else:
                layer = tuple(step)
                for n in layer:
                    p.add(n, after=list(prev))
                prev = layer
        return p

    @classmethod
    def from_targets(cls, targets: Iterable[str],
                     registry: ProducerRegistry,
                     name: str = "from_targets") -> "Pipeline":
        """Walk requires/produces from `targets` to build a DAG of producers.

        Doesn't touch the cube; just builds the graph. Useful for converting
        a target list into an explicit, inspectable pipeline (and as a drop-in
        for the legacy resolver behavior).
        """
        p = cls(name)
        seen: set[str] = set()

        def visit(var: str, stack: list[str]) -> None:
            producer = registry.producer_for(var)
            if producer.name in stack:
                cycle = " -> ".join(stack + [producer.name])
                raise RuntimeError(f"cycle in pipeline graph: {cycle}")
            if producer.name in seen:
                return
            for req in producer_requires(producer):
                if registry.has_variable(req):
                    visit(req, stack + [producer.name])
            after = []
            for req in producer_requires(producer):
                if registry.has_variable(req):
                    upstream = registry.producer_for(req).name
                    if upstream != producer.name:
                        after.append(upstream)
            p.add(producer.name, after=sorted(set(after)))
            seen.add(producer.name)

        for var in targets:
            visit(var, stack=[])
        return p

    # ---- mutators --------------------------------------------------------
    def add(self, step: str, *, after: _AfterSpec = None,
            optional: bool = False,
            metadata: Optional[dict] = None) -> "Pipeline":
        """Add a node to the DAG.

        `after` is one or more upstream node names. Calling `add` for an
        existing step merges the dependency sets (union), so a graph can be
        built incrementally from multiple sources.
        """
        if after is None:
            after_t: tuple[str, ...] = ()
        elif isinstance(after, str):
            after_t = (after,)
        else:
            after_t = tuple(after)

        if step in self._nodes:
            existing = self._nodes[step]
            merged_after = tuple(sorted(set(existing.after) | set(after_t)))
            self._nodes[step] = PipelineNode(
                name=step,
                after=merged_after,
                optional=existing.optional and optional,
                metadata={**existing.metadata, **(metadata or {})},
            )
        else:
            self._nodes[step] = PipelineNode(
                name=step,
                after=after_t,
                optional=optional,
                metadata=dict(metadata or {}),
            )
        return self

    def remove(self, step: str) -> None:
        self._nodes.pop(step, None)
        # also drop any incoming edges to it from siblings
        for n in list(self._nodes.values()):
            if step in n.after:
                self._nodes[n.name] = PipelineNode(
                    name=n.name,
                    after=tuple(s for s in n.after if s != step),
                    optional=n.optional,
                    metadata=n.metadata,
                )

    def on_complete(self, source: str, *, run: str,
                    when: Callable[[Any], bool],
                    name: str = "") -> "Pipeline":
        """Register a trigger.

        When `source` finishes successfully and `when(cube)` returns True,
        the runner adds `run` to the pending nodes (depending on `source`).
        """
        self._triggers.append(Trigger(source=source, target=run,
                                      when=when, name=name))
        return self

    # ---- introspection ---------------------------------------------------
    def nodes(self) -> list[PipelineNode]:
        return list(self._nodes.values())

    def edges(self) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        for n in self._nodes.values():
            for a in n.after:
                out.append((a, n.name))
        return out

    def triggers(self) -> list[Trigger]:
        return list(self._triggers)

    def __contains__(self, name: str) -> bool:
        return name in self._nodes

    def __len__(self) -> int:
        return len(self._nodes)

    # ---- topology --------------------------------------------------------
    def topological_layers(self) -> list[tuple[str, ...]]:
        """Group nodes into layers where every node in layer N depends only
        on nodes in layers <N. Within a layer, nodes are independent and
        may run in parallel.

        Raises RuntimeError on cycles or unknown deps.
        """
        # Validate references first.
        unknown: list[tuple[str, str]] = []
        for n in self._nodes.values():
            for dep in n.after:
                if dep not in self._nodes:
                    unknown.append((n.name, dep))
        if unknown:
            details = ", ".join(f"{a}<-{b}" for a, b in unknown)
            raise RuntimeError(
                f"pipeline references unknown nodes: {details}")

        remaining = {name: set(node.after) for name, node in self._nodes.items()}
        layers: list[tuple[str, ...]] = []
        while remaining:
            ready = sorted([n for n, deps in remaining.items() if not deps])
            if not ready:
                cycle = ", ".join(sorted(remaining.keys()))
                raise RuntimeError(f"cycle in pipeline DAG: {cycle}")
            layers.append(tuple(ready))
            for n in ready:
                remaining.pop(n)
            for deps in remaining.values():
                deps.difference_update(ready)
        return layers

    def explain(self) -> str:
        """Human-readable plan summary. Mirrors the resolver's explain_plan."""
        layers = self.topological_layers()
        lines = [
            f"pipeline {self.name!r}: {len(self._nodes)} nodes, "
            f"{len(layers)} layer(s)"
        ]
        for i, layer in enumerate(layers):
            tag = "parallel" if len(layer) > 1 else "serial"
            lines.append(f"  L{i} [{tag}]: {', '.join(layer)}")
        if self._triggers:
            lines.append("triggers:")
            for t in self._triggers:
                lines.append(f"  on_complete({t.source!r}) -> {t.target!r}")
        return "\n".join(lines)

    def to_dot(self) -> str:
        """Emit Graphviz DOT for visualization. Triggers shown as dashed edges."""
        lines = [f'digraph {self.name.replace("-", "_")} {{',
                 '  rankdir=LR;',
                 '  node [shape=box, style=rounded];']
        for n in self._nodes.values():
            lines.append(f'  "{n.name}";')
        for src, dst in self.edges():
            lines.append(f'  "{src}" -> "{dst}";')
        for t in self._triggers:
            lines.append(
                f'  "{t.source}" -> "{t.target}" '
                f'[style=dashed, label="trigger"];')
        lines.append("}")
        return "\n".join(lines)
