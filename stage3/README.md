# Stage 3 — Recursive discovery and exact global selection

Status: **core implemented and acceptance-tested** on 2026-08-13 for finite,
in-memory capability catalogs. The final Stage-3 planning-SLO gate remains
conditional on the representative Stage-6 graph, which does not exist yet.

Stage 3 turns the explicit scientific records from Stage 2 into a working
resolver. A caller supplies one or more typed final `RequirementUse` values;
the resolver discovers every directly compatible capability recursively,
constructs a finite derivation hypergraph, selects one globally consistent
minimum-cost workflow, and independently validates the selected subgraph before
it is eligible for binding.

The resolver contains no wind, fire, terrain, weather, or model-specific rule.
Those are ordinary concepts and capabilities supplied through the same catalog.
The retained fixture uses meaningless scalar concepts so this architectural
boundary is executable rather than aspirational.

No WRF-SFIRE, MPI, Slurm, remote source, or heavy model was run in this stage.

## What was built

- [`resolution/hypergraph.py`](../resolution/hypergraph.py) recursively expands
  a frozen multi-producer catalog. Equal requirement values share discovery,
  while distinct consumer ports retain separate `RequirementUse` identities,
  cardinality, optional/default, distinctness, and sharing rules.
- Artifact declarations are candidates only when a separate content-addressed
  `ArtifactAvailabilitySnapshot` says they are committed. A manifest hash
  supplied by a caller is not treated as proof of storage commit.
- Evidence profiles are bound to exact invocation/configuration/output or
  artifact-manifest subjects. Missing named profiles, relabeled evidence, and
  attestations from a different availability snapshot fail closed.
- Discovery retains multi-output invocations once, projects each compatible
  output to every distinct use, preserves candidate cycles as back-references,
  records scientific and deployment rejections, and enforces explicit bounds
  on requirements, invocations, candidates, arcs, and depth. Any omitted
  frontier sets `discovery_complete=False` with typed reasons.
- [`resolution/milp.py`](../resolution/milp.py) uses SciPy/HiGHS to minimize
  declared integer invocation cost over the complete selected subgraph. It
  handles shared inputs, co-products, optional/default/omit choices,
  cardinality, distinctness, non-shareable outputs, committed artifacts,
  include/exclude policy, a cost budget, static deployment feasibility,
  grounding, and acyclicity.
- Primary cost is solved first and frozen. A second exact lexicographic phase
  reproduces the deterministic Stage-2 oracle ordering without using a weighted
  objective that could alter the scientific optimum.
- Capability and artifact lookups use immutable indexes. Scientific producer
  selection is global; because Stage 3 has no shared-capacity constraints,
  static site choice is a deterministic post-selection feasibility choice,
  not a claim that this stage schedules nodes.
- [`resolution/validator.py`](../resolution/validator.py) treats solver output
  as untrusted. It independently replays compatibility proofs and checks plan
  identity, active uses, producer sets, cost, policy, artifact trust,
  deployment, sharing, distinctness, cycles, and grounding. A failed selection
  produces a deterministic `ALL` blocker tree and cannot be bound.
- [`resolution/service.py`](../resolution/service.py) is the domain-neutral
  orchestration API. `WorkflowResolver.resolve()` reports graph, solve,
  validation, serialization, and total planning time separately.
- Discovery completeness and optimization status are never conflated. An
  optimum over a truncated graph is not globally optimal. A valid solver
  incumbent lacking an optimality proof is
  `FEASIBLE_NOT_PROVEN_OPTIMAL`; callers requiring proof cannot bind it.
  Infeasibility over a truncated graph is reported as `INCOMPLETE`, never as a
  claim that the scientific request is unsatisfiable.
  A 30-second interactive solver budget is the default; a limit without an
  incumbent is `LIMIT_NO_INCUMBENT`, distinct from solver error and UNSAT.

## Resolver flow

```text
typed final RequirementUse(s)
            |
            v
recursive catalog discovery ----> finite FeasibleDerivationHypergraph
            |                              |
            |                              v
            |                    exact global MILP selection
            |                              |
            +------------------------------v
                         independent plan validation
                                      |
                         validated CandidateDerivationPlan
                                      |
                             Stage-2/Stage-1 compiler
```

## Runnable proof

The domain-neutral fixture contains:

- one final `add` capability requiring `left` and `right`;
- individual left and right producers costing 4 each;
- one two-output pair producer costing 2;
- one separately attested committed left artifact; and
- the add invocation costing 1.

Starting only with the final sum requirement, discovery builds 3 normalized
requirements, 3 distinct uses, 4 invocations, and 6 satisfaction arcs. The
global selector chooses `pair + add` for cost 3, counts the pair once, and the
independent exhaustive oracle returns the same plan ID. Compilation produces
two Stage-1 tasks; the pair task publishes both outputs. The durable local
runtime executes two attempts and commits the result `42`.

```bash
runtime_root=$(mktemp -d /tmp/nasa-stage3-demo.XXXXXX)
.venv/bin/python scripts/run_stage3_demo.py --runtime-root "$runtime_root"
```

The demonstration uses a disposable node-local runtime root. It does not prove
node-loss durability or cluster scheduling.

## Why global selection is required

The tests include the roadmap's shared-input counterexample: two consumers can
each choose a locally cheap private source, or reuse one source whose cost is
paid once. Independent per-input choices cost 6; the globally selected shared
source costs 5. A similar test proves that two outputs of one invocation cost
3 once, rather than selecting two cost-2 single-output producers.

## Acceptance evidence

```bash
.venv/bin/python -m pytest -q \
  tests/test_stage3_hypergraph.py \
  tests/test_stage3_milp.py \
  tests/test_stage3_validator.py \
  tests/test_stage3_integration.py
```

The suite covers recursive and memoized discovery, distinct-use projection,
artifact trust, limit propagation, cycles, shared inputs, co-products,
optional/default/cardinality semantics, deterministic ties, include/exclude and
budget constraints, exact deployment-choice validation, forged proof/evidence
rejection, explicit timeout paths, independent cycle/grounding rejection,
exhaustive oracle agreement, an end-to-end local execution, and a 30-run warm
planning p95 regression below 5 seconds for the retained conformance graph.

An additional read-only fuzz audit compared 1,000 generated frozen graphs
(optional/default/omit, cardinality, sharing, co-products, distinctness,
cycles, artifacts, deployment, budgets, and include/exclude policy) against the
independent exhaustive oracle: 411 were feasible, 589 unsatisfiable, with zero
status or exact plan-ID/signature mismatches. A 1,000-producer/1,000-arc broad
alternative stress check completes in roughly 2.4 seconds on the current
development node; this is diagnostic evidence, not the future Stage-6 SLO.

To emit a content-identified conformance-microbenchmark profile with graph
size, selected-plan depth, cache/run configuration, CPU/memory/platform,
Python/NumPy/SciPy/HiGHS versions, full-call and resolver-internal samples, and
p95:

```bash
.venv/bin/python scripts/benchmark_stage3_planning.py --runs 30
```

## Deliberate limits

- Discovery currently operates on a finite, frozen in-memory capability
  catalog. It does not search remote provider APIs, page through assets, fetch
  bytes, mosaic coverage, or persist a planning session. Those are Stage 5 and
  post-MVP acquisition concerns.
- `direct_match()` still inserts no transforms. Unit conversion, reprojection,
  regridding, temporal alignment, and other semantic transformations must be
  explicit capabilities; their bounded path search is Stage 4.
- The only automatic objective is minimum declared integer cost under hard
  constraints. Empirical-quality choice, latency objectives, Pareto
  enumeration, CP-SAT, beam/A*, and learned estimates remain post-MVP.
- Static deployment feasibility means an eligible site class exists in a
  frozen snapshot. It does not reserve CPU/GPU/memory, choose a cluster node,
  predict queue delay, or schedule tasks. Resource-aware local placement and
  cluster providers are later runtime stages.
- SciPy/HiGHS is the only production selector backend. Exhaustive enumeration
  remains an independent correctness oracle only for bounded test graphs.
- Presolve defaults off because the deployed HiGHS build produced a false
  infeasibility on a valid bounded integer fixture. An explicit opt-in is
  permitted, but every presolved infeasibility is re-solved without presolve
  before it can be reported as `UNSATISFIABLE`.
- A timeout can be returned as a validated incumbent only when HiGHS supplies
  one. A timeout before any valid incumbent is `LIMIT_NO_INCUMBENT`, not an
  error or unsatisfiable proof.
- The Stage-1 compiler cannot yet feed an external committed artifact into an
  executable task. Such a selected plan remains a valid scientific derivation
  but fails closed at that later bridge rather than disguising the artifact as
  a producer invocation.
- Discovery limits bound the retained graph, not the work of an eager binder.
  Stage 5 must add lazy/paginated binding, budget propagation, and persisted
  continuation state before accepting very large parameter or asset domains.
- The current p95 gate is a regression measurement for the frozen conformance
  graph on this environment, not a universal latency/scale claim. The roadmap's
  representative graph (up to 1,000 invocations/5,000 arcs/12 levels) must be
  frozen and benchmarked once Stage 6 supplies it.
