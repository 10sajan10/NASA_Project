"""Domain-neutral Stage-7 partition fixtures.

The partition space here is 100 spatial tiles x 100 temporal windows = 10,000
partitions, which is the roadmap's demonstration size.  Nothing about the
labels means anything; what matters is that the space is large enough that
materialising it would be visible, and that the same *one* resolved scientific
selection drives every partition in it.

The template's ``invocation_key`` is taken from a real Stage-3 resolution
rather than invented, so "one scientific selection is reused by all compatible
partitions" is a property of the wiring rather than a claim in a docstring.
"""
from __future__ import annotations

from dataclasses import dataclass

from partitions import (
    AxisKind,
    CollectionManifest,
    CompletionPolicy,
    PartitionAxis,
    PartitionSetSpec,
    PartitionTaskTemplate,
)
from resolution import ResolutionStatus, WorkflowResolver
from stage3.fixtures import make_composition_fixture

TILE_COUNT = 100
WINDOW_COUNT = 100
DEMONSTRATION_PARTITIONS = TILE_COUNT * WINDOW_COUNT


@dataclass(frozen=True)
class Stage7Fixture:
    spec: PartitionSetSpec
    template: PartitionTaskTemplate
    manifest: CollectionManifest
    invocation_key: str
    selected_capability_id: str


def make_partition_space(*, tiles: int = TILE_COUNT,
                         windows: int = WINDOW_COUNT) -> PartitionSetSpec:
    """A two-axis space; the temporal axis varies fastest, so fusion groups
    adjacent windows of the same tile rather than scattered work."""
    return PartitionSetSpec.bind((
        PartitionAxis("tile", AxisKind.SPATIAL,
                      tuple(f"t{index:03d}" for index in range(tiles))),
        PartitionAxis("window", AxisKind.TEMPORAL,
                      tuple(f"w{index:03d}" for index in range(windows))),
    ))


_SELECTION_CACHE: tuple[str, str] | None = None


def resolve_one_selection() -> tuple[str, str]:
    """Resolve the Stage-3 counterexample once and return its selection.

    Returns ``(invocation_key, capability_id)``.  This is deliberately a real
    resolver call: the partition layer must consume a selection it did not
    invent.
    """
    global _SELECTION_CACHE
    if _SELECTION_CACHE is not None:
        # The point of Stage 7 is that this resolves *once*; re-solving per
        # partition space would contradict the property being demonstrated.
        return _SELECTION_CACHE
    fixture = make_composition_fixture()
    outcome = WorkflowResolver(
        fixture.catalog, fixture.deployment_snapshot,
    ).resolve(fixture.root_uses)
    if (outcome.status is not ResolutionStatus.READY
            or outcome.selection.plan is None):
        raise RuntimeError(
            f"Stage-7 fixture could not resolve a selection: "
            f"{outcome.status.value}")
    chosen = set(outcome.selection.plan.selected_invocation_ids)
    node = min(
        (item for item in outcome.hypergraph.invocation_nodes
         if item.invocation_id in chosen),
        key=lambda item: item.invocation.capability_id)
    _SELECTION_CACHE = (node.invocation.invocation_key,
                        node.invocation.capability_id)
    return _SELECTION_CACHE


def make_stage7_fixture(
    *,
    tiles: int = TILE_COUNT,
    windows: int = WINDOW_COUNT,
    policy: CompletionPolicy = CompletionPolicy.ALL,
    minimum_committed: int | None = None,
    minimum_fraction: str | None = None,
    estimated_cost_units: int = 1,
    retry_safe: bool = True,
) -> Stage7Fixture:
    spec = make_partition_space(tiles=tiles, windows=windows)
    invocation_key, capability_id = resolve_one_selection()
    template = PartitionTaskTemplate.bind(
        invocation_key=invocation_key,
        operation_key="synthetic.constant.v1",
        input_slot_ids=("slot:example-input",),
        parameters={"value": 1.0},
        estimated_cost_units=estimated_cost_units,
        retry_safe=retry_safe)
    manifest = CollectionManifest.bind(
        set_id=spec.set_id, template_id=template.template_id,
        expected=spec.total, policy=policy,
        minimum_committed=minimum_committed,
        minimum_fraction=minimum_fraction)
    return Stage7Fixture(spec, template, manifest, invocation_key,
                         capability_id)


__all__ = [
    "DEMONSTRATION_PARTITIONS",
    "Stage7Fixture",
    "TILE_COUNT",
    "WINDOW_COUNT",
    "make_partition_space",
    "make_stage7_fixture",
    "resolve_one_selection",
]
