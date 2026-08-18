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

from capabilities import BoundInvocation
from partitions import (
    AxisKind,
    CollectionManifest,
    CompletionPolicy,
    PartitionAxis,
    PartitionSetSpec,
    PartitionTaskTemplate,
)
from resolution import (
    DiscoveryCertificate,
    DiscoveryUniverseContract,
    ResolutionStatus,
    WorkflowResolver,
)
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
    bound_invocation: BoundInvocation


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


_SELECTION_CACHE: BoundInvocation | None = None


def resolve_one_selection() -> BoundInvocation:
    """Resolve once and return the exact selected root invocation."""
    global _SELECTION_CACHE
    if _SELECTION_CACHE is not None:
        # The point of Stage 7 is that this resolves *once*; re-solving per
        # partition space would contradict the property being demonstrated.
        return _SELECTION_CACHE
    fixture = make_composition_fixture()
    outcome = WorkflowResolver(
        fixture.catalog, fixture.deployment_snapshot,
        discovery_certificate=DiscoveryCertificate.for_base_catalog(
            fixture.catalog),
        discovery_universe=DiscoveryUniverseContract.declare(
            fixture.catalog.catalog_id),
    ).resolve(fixture.root_uses)
    if (outcome.status is not ResolutionStatus.READY
            or outcome.selection.plan is None):
        raise RuntimeError(
            f"Stage-7 fixture could not resolve a selection: "
            f"{outcome.status.value}")
    chosen = set(outcome.selection.plan.selected_invocation_ids)
    # example-pair is genuinely selected by the resolver *and* consumes no
    # upstream inputs, so a partition of it is executable standalone. That
    # matters: a template whose inputs are unbound could not be run without
    # inventing them, which is exactly what this stage must not do.
    roots = [
        item.invocation for item in outcome.hypergraph.invocation_nodes
        if (item.invocation_id in chosen
            and item.invocation.capability_id == "example-pair")]
    if len(roots) != 1:
        raise RuntimeError(
            "Stage-7 fixture expected exactly one selected example-pair root")
    _SELECTION_CACHE = roots[0]
    return _SELECTION_CACHE


_ALL_SELECTED_CACHE: tuple[BoundInvocation, ...] | None = None


def resolve_all_selected() -> tuple[BoundInvocation, ...]:
    """Every invocation the Stage-3 counterexample selected, sorted by key.

    Tests need two *genuinely different* selections to show that partitioning
    one cannot be confused with partitioning another. Fabricating a second
    invocation would reintroduce exactly the restatement this stage removed.
    """
    global _ALL_SELECTED_CACHE
    if _ALL_SELECTED_CACHE is not None:
        return _ALL_SELECTED_CACHE
    fixture = make_composition_fixture()
    outcome = WorkflowResolver(
        fixture.catalog, fixture.deployment_snapshot,
        discovery_certificate=DiscoveryCertificate.for_base_catalog(
            fixture.catalog),
        discovery_universe=DiscoveryUniverseContract.declare(
            fixture.catalog.catalog_id),
    ).resolve(fixture.root_uses)
    if (outcome.status is not ResolutionStatus.READY
            or outcome.selection.plan is None):
        raise RuntimeError("Stage-7 fixture could not resolve a selection")
    chosen = set(outcome.selection.plan.selected_invocation_ids)
    _ALL_SELECTED_CACHE = tuple(sorted(
        (item.invocation for item in outcome.hypergraph.invocation_nodes
         if item.invocation_id in chosen),
        key=lambda item: item.invocation_key))
    return _ALL_SELECTED_CACHE


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
    invocation = resolve_one_selection()
    template = PartitionTaskTemplate.bind(
        invocation,
        estimated_cost_units=estimated_cost_units,
        retry_safe=retry_safe)
    manifest = CollectionManifest.bind(
        set_id=spec.set_id, template_id=template.template_id,
        expected=spec.total, policy=policy,
        minimum_committed=minimum_committed,
        minimum_fraction=minimum_fraction)
    return Stage7Fixture(
        spec, template, manifest, invocation.invocation_key,
        invocation.capability_id, invocation)


__all__ = [
    "DEMONSTRATION_PARTITIONS",
    "Stage7Fixture",
    "TILE_COUNT",
    "WINDOW_COUNT",
    "make_partition_space",
    "make_stage7_fixture",
    "resolve_all_selected",
    "resolve_one_selection",
]
