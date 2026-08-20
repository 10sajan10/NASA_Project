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
from plans import InvocationDeploymentBinding
from partitions import (
    AxisKind,
    CollectionManifest,
    CompletionPolicy,
    PartitionAxis,
    PartitionSetSpec,
    PartitionTaskTemplate,
)
from stage3.demo import Stage3DemoPlan, build_demo_plan

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
    deployment_binding: InvocationDeploymentBinding


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


_STAGE3_PLAN_CACHE: Stage3DemoPlan | None = None


def _stage3_plan() -> Stage3DemoPlan:
    """One validated Stage-3 scientific and deployment selection."""
    global _STAGE3_PLAN_CACHE
    if _STAGE3_PLAN_CACHE is None:
        _STAGE3_PLAN_CACHE = build_demo_plan()
    return _STAGE3_PLAN_CACHE


def resolve_one_selection() -> BoundInvocation:
    """Resolve once and return the exact selected root invocation."""
    # example-pair is genuinely selected by the resolver *and* consumes no
    # upstream inputs, so a partition of it is executable standalone. That
    # matters: a template whose inputs are unbound could not be run without
    # inventing them, which is exactly what this stage must not do.
    roots = [
        invocation for invocation in _stage3_plan().selected_invocations
        if invocation.capability_id == "example-pair"]
    if len(roots) != 1:
        raise RuntimeError(
            "Stage-7 fixture expected exactly one selected example-pair root")
    return roots[0]


def resolve_all_selected() -> tuple[BoundInvocation, ...]:
    """Every invocation the Stage-3 counterexample selected, sorted by key.

    Tests need two *genuinely different* selections to show that partitioning
    one cannot be confused with partitioning another. Fabricating a second
    invocation would reintroduce exactly the restatement this stage removed.
    """
    return _stage3_plan().selected_invocations


def deployment_binding_for(
    invocation: BoundInvocation,
) -> InvocationDeploymentBinding:
    """Return Stage 3's exact placement for one selected invocation.

    This deliberately does not synthesize a resource envelope in Stage 7.
    The invocation and placement are looked up together in the validated
    Stage-3 demo plan, and a caller cannot ask for a binding for an invocation
    outside that frozen selection.
    """
    if not isinstance(invocation, BoundInvocation):
        raise TypeError("deployment binding lookup requires a BoundInvocation")
    plan = _stage3_plan()
    selected = {
        value.invocation_key: value for value in plan.selected_invocations
    }
    if selected.get(invocation.invocation_key) != invocation:
        raise ValueError("invocation is not part of the Stage-3 selection")
    bindings = {
        value.invocation_id: value
        for value in plan.deployment_plan.invocation_bindings
    }
    try:
        return bindings[invocation.invocation_key]
    except KeyError as exc:
        raise RuntimeError(
            "Stage-3 deployment plan omitted a selected invocation") from exc


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
    deployment_binding = deployment_binding_for(invocation)
    template = PartitionTaskTemplate.bind(
        invocation, deployment_binding,
        estimated_cost_units=estimated_cost_units,
        retry_safe=retry_safe)
    manifest = CollectionManifest.bind(
        set_id=spec.set_id, template_id=template.template_id,
        expected=spec.total, policy=policy,
        minimum_committed=minimum_committed,
        minimum_fraction=minimum_fraction)
    return Stage7Fixture(
        spec, template, manifest, invocation.invocation_key,
        invocation.capability_id, invocation, deployment_binding)


__all__ = [
    "DEMONSTRATION_PARTITIONS",
    "Stage7Fixture",
    "TILE_COUNT",
    "WINDOW_COUNT",
    "deployment_binding_for",
    "make_partition_space",
    "make_stage7_fixture",
    "resolve_all_selected",
    "resolve_one_selection",
]
