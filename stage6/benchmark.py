"""The frozen representative planning benchmark and its Section 9.5 gate.

Stage 3 deferred one thing: a latency target measured on a graph that actually
looks like a composition problem rather than a four-node demo.  This builds
that graph and measures it.

The shape is layered and deliberately awkward for a selector: every level
offers two alternative producers at different costs, consumers share inputs
across branches, and the levels narrow toward a single root.  That combination
is what forces real global selection instead of a greedy walk.

Section 9.5 caps the profile at 1,000 producer/invocation nodes, 5,000
satisfaction/input arcs, and 12 derivation levels, and requires frozen-metadata
graph construction plus selection plus validation at p95 <= 5 s over 30 warm
runs.  ``SECTION_9_5_P95_BUDGET_S`` is that budget, asserted by the tests.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from capabilities import (
    BinderRef,
    BindingParameterization,
    CapabilityCatalog,
    CapabilitySpec,
    DeploymentCapabilitySnapshot,
    DescriptorTemplate,
    ExecutionProfile,
    ImplementationRef,
    InputPortTemplate,
    ParameterField,
    ParameterKind,
    ParameterSchema,
    PlacementRequirements,
    ResourceEnvelope,
    SiteClassCapability,
)
from contracts import (
    ArtifactDescriptor,
    BBoxSupport,
    EvidenceRequirement,
    MissingPolicy,
    Missingness,
    MissingnessStatus,
    OriginClass,
    Requirement,
    RequirementUse,
    SpatialRequirement,
    TemporalKind,
    TemporalRequirement,
    TemporalSupport,
    ValueConstraint,
)
from resolution import (
    MilpSolveOptions,
    PlanningBenchmarkProfile,
    ResolutionOutcome,
    WorkflowResolver,
)

SECTION_9_5_P95_BUDGET_S = 5.0
SECTION_9_5_MAX_INVOCATIONS = 1_000
SECTION_9_5_MAX_ARCS = 5_000
SECTION_9_5_MAX_LEVELS = 12

SCHEMA_VERSION = "bench-scalar-v1"
REPRESENTATION = "application/json"
CRS = "EPSG:4326"
AXES = ("x", "y")
BOUNDS = ("0", "0", "1", "1")


@dataclass(frozen=True)
class RepresentativeGraph:
    catalog: CapabilityCatalog
    deployment_snapshot: DeploymentCapabilitySnapshot
    root_uses: tuple[RequirementUse, ...]
    level_widths: tuple[int, ...]
    capability_count: int


def _descriptor(concept_id: str) -> ArtifactDescriptor:
    return ArtifactDescriptor(
        concept_id=concept_id, schema_version=SCHEMA_VERSION,
        representation=REPRESENTATION, units="1",
        spatial_support=BBoxSupport(CRS, AXES, BOUNDS),
        temporal_support=TemporalSupport(TemporalKind.TIME_INVARIANT),
        vertical_support=None, grid=None, native_resolution=None,
        origin=OriginClass.SYNTHETIC,
        missingness=Missingness(MissingnessStatus.COMPLETE))


def _requirement(concept_id: str) -> Requirement:
    return Requirement(
        concept_id=concept_id, accepted_schema_versions=(SCHEMA_VERSION,),
        representation=ValueConstraint.exact(REPRESENTATION),
        units=ValueConstraint.exact("1"),
        spatial=SpatialRequirement(BBoxSupport(CRS, AXES, BOUNDS)),
        temporal=TemporalRequirement(TemporalKind.TIME_INVARIANT),
        vertical=None, allowed_origins=(OriginClass.SYNTHETIC,),
        max_native_resolution=None, max_effective_resolution=None,
        missing_policy=MissingPolicy(),
        minimum_evidence=EvidenceRequirement(allow_unknown_empirical=True))


def _profile(operation_key: str) -> ExecutionProfile:
    return ExecutionProfile.bind(
        ImplementationRef.from_operation_key(operation_key),
        PlacementRequirements(
            architectures=("x86_64",),
            provider_kinds=("stage1-local-subprocess",),
            resources=ResourceEnvelope(
                min_cpu_cores=1, min_memory_mb=64,
                max_cpu_cores=1, max_memory_mb=256)))


def make_representative_graph(*, base_width: int = 32,
                              levels: int = 6) -> RepresentativeGraph:
    """Build a layered graph with genuine alternatives and shared inputs."""
    if base_width < 2 or levels < 2:
        raise ValueError("a representative graph needs width and depth")
    constant_profile = _profile("synthetic.constant.v1")
    add_profile = _profile("synthetic.add.v1")
    specs: list[CapabilitySpec] = []

    widths: list[int] = []
    width = base_width
    for _ in range(levels):
        widths.append(max(width, 1))
        width = max(width // 2, 1)

    # Level 0: two competing constant sources per slot.
    for slot in range(widths[0]):
        for variant, cost in (("a", 2), ("b", 3)):
            specs.append(CapabilitySpec.bind(
                capability_id=f"bench-source-{slot}-{variant}",
                capability_version="1.0.0",
                implementation=constant_profile.implementation,
                binder=BinderRef.from_key("synthetic.constant.bind.v1"),
                input_ports=(),
                output_ports=(DescriptorTemplate(
                    "result", _descriptor(f"bench.level0.slot{slot}")),),
                parameter_schema=ParameterSchema((
                    ParameterField("value", ParameterKind.NUMBER),)),
                parameterizations=(BindingParameterization(
                    {"value": float(slot)}, {"cost_units": cost}),),
                execution_profile_id=constant_profile.profile_id))

    # Levels 1..n: each slot combines two lower slots, with two cost variants.
    for level in range(1, levels):
        lower = widths[level - 1]
        for slot in range(widths[level]):
            left = _requirement(f"bench.level{level - 1}.slot{(2 * slot) % lower}")
            right = _requirement(
                f"bench.level{level - 1}.slot{(2 * slot + 1) % lower}")
            for variant, cost in (("a", 2), ("b", 3)):
                specs.append(CapabilitySpec.bind(
                    capability_id=f"bench-join-{level}-{slot}-{variant}",
                    capability_version="1.0.0",
                    implementation=add_profile.implementation,
                    binder=BinderRef.from_key("synthetic.add.bind.v1"),
                    input_ports=(InputPortTemplate("left", left),
                                 InputPortTemplate("right", right)),
                    output_ports=(DescriptorTemplate(
                        "result", _descriptor(f"bench.level{level}.slot{slot}")),),
                    parameter_schema=ParameterSchema(()),
                    parameterizations=(BindingParameterization(
                        {}, {"cost_units": cost}),),
                    execution_profile_id=add_profile.profile_id))

    catalog = CapabilityCatalog.freeze(
        specs, (constant_profile, add_profile))
    digests = tuple(sorted({
        constant_profile.implementation.implementation_sha256,
        add_profile.implementation.implementation_sha256}))
    deployment = DeploymentCapabilitySnapshot.freeze(
        "2026-08-15T00:00:00Z",
        (SiteClassCapability(
            site_class_id="private-node-example-cpu", architecture="x86_64",
            provider_kinds=("stage1-local-subprocess",),
            implementation_digests=digests, environment_classes=(),
            network_classes=("none",), credential_classes=(), mount_classes=(),
            policy_classes=(), max_cpu_cores=1, max_memory_mb=256, max_gpus=0),))
    root = RequirementUse(
        "bench-root", "result",
        _requirement(f"bench.level{levels - 1}.slot0"))
    return RepresentativeGraph(
        catalog=catalog, deployment_snapshot=deployment, root_uses=(root,),
        level_widths=tuple(widths), capability_count=len(specs))


def representative_resolver(
        graph: RepresentativeGraph,
        options: MilpSolveOptions | None = None,
) -> Callable[[], ResolutionOutcome]:
    solve_options = options or MilpSolveOptions()

    def resolve() -> ResolutionOutcome:
        return WorkflowResolver(
            graph.catalog, graph.deployment_snapshot,
        ).resolve(graph.root_uses, solve_options=solve_options)
    return resolve


def freeze_benchmark(*, base_width: int = 32, levels: int = 6,
                     measured_runs: int = 30, warmup_runs: int = 2
                     ) -> tuple[PlanningBenchmarkProfile, RepresentativeGraph]:
    """Measure the representative graph and return its frozen profile."""
    graph = make_representative_graph(base_width=base_width, levels=levels)
    options = MilpSolveOptions()
    profile = PlanningBenchmarkProfile.measure(
        representative_resolver(graph, options),
        warmup_runs=warmup_runs, measured_runs=measured_runs,
        solve_options=options,
        run_config={
            "benchmark": "stage6-representative-v1",
            "base_width": base_width,
            "levels": levels,
            "level_widths": list(graph.level_widths),
            "capability_count": graph.capability_count,
        })
    return profile, graph


def section_9_5_report(profile: PlanningBenchmarkProfile) -> dict[str, Any]:
    """Compare a measured profile against the Section 9.5 caps and budget."""
    p95_s = profile.p95_total_ns / 1e9
    return {
        "schema": "stage6-section-9-5-report-v1",
        "benchmark_id": profile.benchmark_id,
        "measured_runs": profile.measured_runs,
        "invocation_count": profile.invocation_count,
        "satisfaction_arc_count": profile.satisfaction_arc_count,
        "selected_derivation_depth": profile.selected_derivation_depth,
        "p95_total_s": p95_s,
        "p95_budget_s": SECTION_9_5_P95_BUDGET_S,
        "within_node_cap": (
            profile.invocation_count <= SECTION_9_5_MAX_INVOCATIONS),
        "within_arc_cap": (
            profile.satisfaction_arc_count <= SECTION_9_5_MAX_ARCS),
        "within_level_cap": (
            profile.selected_derivation_depth <= SECTION_9_5_MAX_LEVELS),
        "within_latency_budget": p95_s <= SECTION_9_5_P95_BUDGET_S,
        "discovery_complete": profile.discovery_complete,
        "globally_optimal": profile.globally_optimal,
        "validation_passed": profile.validation_passed,
    }


__all__ = [
    "SECTION_9_5_MAX_ARCS",
    "SECTION_9_5_MAX_INVOCATIONS",
    "SECTION_9_5_MAX_LEVELS",
    "SECTION_9_5_P95_BUDGET_S",
    "RepresentativeGraph",
    "freeze_benchmark",
    "make_representative_graph",
    "representative_resolver",
    "section_9_5_report",
]
