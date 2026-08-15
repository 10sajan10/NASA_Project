# Codex Project Handoff

Last updated: 2026-08-15 (Stage 6 complete; Composition MVP boundary reached)

This is the durable handoff for a new AI session. Treat the repository,
tests, and roadmap as authoritative; the old chat transcript is supporting
context only.

## First instructions for the next instance

1. Read this file completely.
2. Work in `/uufs/chpc.utah.edu/common/home/parashar-vdc/sajan/NASA_Project`.
3. Verify branch `v2` and Stage-6 commit `bf4e360` before changing anything.
4. Inspect `git status` before edits. Preserve the user-owned dirty files listed
   below and never stage them accidentally.
5. Read `stage4/README.md`, `stage5/README.md`, and `stage6/README.md`, plus
   the Composition MVP boundary note in the external roadmap.
6. Run only the bounded Stage 0-6 tests initially. Do not run WRF-SFIRE, MPI,
   Slurm, real remote providers, or other heavy workloads. The Stage-5
   connectors are in-process; nothing in the suite touches a network.
7. **Do not start Stage 7.** Stage 6 reached the Composition MVP release
   boundary, where the roadmap requires a user/scientist review before any
   scale feature is added. Two findings should lead that review: the Section
   9.5 planning-latency gate is measured and **missed**, and no real reference
   observations exist for an evidence pack. Ask the user how they want to
   handle both before writing code.

## User's actual objective

Build a non-agentic scientific workflow-composition system that can:

- accept an open-ended required outcome or scientific artifact contract;
- discover data and model producers recursively;
- decide between direct data and model-produced alternatives;
- explicitly represent transformations instead of hiding them in adapters;
- select one globally consistent workflow while accounting for shared inputs
  and multi-output producers once;
- bind exact data identities before payload execution;
- execute durably on a laptop or a private allocated node;
- later support Slurm as an execution provider without making Slurm a current
  development dependency; and
- eventually scale discovery and partition execution without assuming a
  million eager in-memory tasks.

Agentic planning and the research paper are intentionally deferred. Build and
validate the deterministic system first. WRF-SFIRE is a heavy example model;
do not run it unless the user explicitly changes this instruction.

Wind has appeared frequently in examples because it exercises units, vector
representation, space, time, vertical coordinates, resolution, provenance,
and source-versus-model choice. It is not special-cased by the resolver.

## Repository and roadmap

- Repository: `https://github.com/sci-ndp/NASA_Project.git`
- Working directory:
  `/uufs/chpc.utah.edu/common/home/parashar-vdc/sajan/NASA_Project`
- Active branch: `v2`
- Stage-3 implementation commit:
  `36c46a8fc9b9cd4620d17089709dea780c8739cc`
- Stage-4 implementation commit: `e8b4bfd` (post-rewrite; was `faf19cd`)
- Stage-5 implementation commit: `a6e4a5c`
- Stage-6 implementation commit: `bf4e360`
- External roadmap:
  `/uufs/chpc.utah.edu/common/home/parashar-vdc/sajan/nasa_project_docs/scientific_workflow_composition_plan.md`
- Original poster:
  `/uufs/chpc.utah.edu/common/home/parashar-vdc/sajan/nasa_project_docs/diagrams/2026_SCposter_SajanNeupane.pdf`

The roadmap has been updated through Stage 5, including an execution-status
block on the Stage-5 section. It still marks the representative Stage-6
planning-latency gate as pending. `stage4/README.md` and `stage5/README.md`
remain the authoritative records of what those stages actually delivered.

**Note on the roadmap file.** `scientific_workflow_composition_plan.md` lives in
a *separate* repository (`nasa_project_docs`, remote
`https://github.com/10sajan10/nasa_project_docs.git`) and, as of this handoff,
is still **untracked** there — it has never been committed or pushed. It exists
only on this node's filesystem. Treat backing it up as a standing task.

## Files owned by the user: preserve them

At handoff time, these tracked files have unstaged user changes:

- `configs/asteroid_burntest.yaml`
- `configs/asteroid_impact_7day.yaml`
- `models/wrf_config.py`
- `models/wrf_sfire_adapter.py`

The following user file is untracked:

- `.burncheck_logpath`

Do not overwrite, revert, delete, or stage these files. Do not use broad
commands such as `git add .`, `git reset --hard`, or `git checkout -- <file>`.

## Completed implementation

### Stage 0 — baseline and architectural audit

- Froze the legacy system as a baseline rather than silently evolving it.
- Recorded cluster and software facts without claiming unavailable Slurm.
- Established domain-neutral invariants and an honest missing-evidence record.
- Identified the legacy greedy variable resolver, mutable Cube publication,
  layer barriers, and eager tile materialization as boundaries to retire.

See `stage0/` and commit `20c1488`.

### Stage 0A — runtime build-versus-buy spike

- Compared the thin local controller boundary with Dask/Parsl capabilities.
- Selected a project-owned durable controller plus supervised local subprocess
  provider for the private development node.
- Dask or Parsl may later serve as bounded executors, but neither owns
  scientific identity, authoritative state, fencing, or artifact commit.

See `stage0a/` and commit `20c1488`.

### Stage 1 — durable local execution kernel

- Added immutable bound execution graphs and closed operation keys.
- Added task-versus-attempt state, SQLite/WAL single-controller durability,
  leases/fencing, restart reconciliation, persisted wake conditions, and
  conservative fixed-allocation admission.
- Added attempt-scoped staging, validation, content-addressed objects,
  idempotent all-output commit, and dependency release only after authoritative
  commit.
- Uses at-least-once execution with idempotent publication; it does not claim
  exactly-once execution or node-loss durability.

See `engine/runtime/`, `stage1/`, and commit `2b87bd1`.

### Stage 2 — scientific contracts and producer redesign

- Added strict artifact descriptors, requirements, requirement uses, spatial,
  temporal, vertical, origin, missingness, evidence, and uncertainty records.
- Added pure `direct_match()` with structured proofs and no hidden transforms.
- Replaced the one-producer registry with an immutable multi-producer,
  multi-output capability catalog and closed binders.
- Separated scientific invocation identity from deployment binding.
- Added artifact leaves, candidate/bound/deployment plans, exhaustive
  small-graph oracle, blocker trees, and a verified Stage-1 compiler.
- Evidence is allowed to remain honestly unknown; unavailable real wind
  validation data was not fabricated.

See `contracts/`, `capabilities/`, `plans/`, `composition/`, `stage2/`, and
commit `29f9898`.

### Stage 3 — recursive discovery and exact global selection

- Added recursive finite hypergraph discovery from typed root requirements.
- Equal requirement values share discovery while distinct consumer ports retain
  distinct `RequirementUse` identities.
- Discovery preserves multi-output invocation sharing, candidate cycles,
  artifact availability attestations, evidence binding, deployment feasibility,
  rejections, omitted frontiers, and explicit completeness status.
- Depth-sensitive memoization reopens a shared requirement when a shallower
  path provides additional remaining discovery budget.
- Added a SciPy/HiGHS MILP that globally minimizes declared integer cost while
  enforcing cardinality, optional/default/omit, sharing, co-production,
  distinctness, include/exclude policy, cost budget, artifact commit,
  deployment feasibility, grounding, and acyclicity.
- Primary cost and deterministic tie-breaking are separate phases. Presolve is
  disabled by default because the installed HiGHS build produced a false
  infeasibility on a valid regression; opt-in presolved infeasibility is
  confirmed without presolve.
- Added an independent backend-neutral validator that replays proof, artifact,
  evidence, deployment, identity, sharing, cycle, and grounding semantics.
- Added explicit `INCOMPLETE`, `FEASIBLE_NOT_PROVEN_OPTIMAL`,
  `LIMIT_NO_INCUMBENT`, `UNSATISFIABLE`, and solver-error boundaries.
- Added a content-identified conformance benchmark profile with full-call
  timing and environment/solver metadata.
- Added a domain-neutral pair/add demonstration that compiles through Stage 1.

See `resolution/`, `stage3/`, and commit `36c46a8`.

### Stage 4 — explicit semantic transformations

- Added `TransformationSpec`: an immutable hyperedge between *exact* descriptor
  states carrying kind, closed operation/binder pair, typed ports, parameters,
  cost, semantic rule ID, and explicit scientific assumptions.
- `to_capability_spec()` lowers each edge to an ordinary `CapabilitySpec`, so
  Stage 3 required **no** transform-specific code path. A declared conversion
  competes with direct data inside the same global selector.
- `direct_match()` is unchanged and still inserts nothing.
- Unit coefficients come only from a closed versioned affine registry; an
  unregistered unit pair cannot become a transformation.
- Construction-time semantic guards reject dishonest edges: outputs must
  declare `DERIVED` origin, unit conversion may change only units, regrid may
  not change CRS while reprojection must, and bilinear interpolation is
  admitted only for explicitly typed continuous/intensive fields.
- `expand_transform_catalog()` computes a finite forward-reachability fixed
  point and returns an augmented catalog. A transform whose inputs nothing
  produces is an ordinary unreachable frontier item, not a truncation.
- **Correctness fix.** Transformation closure is discovery, but its
  completeness previously never reached the selector: a closure truncated by
  `MAX_DEPTH` still produced `READY` / `globally_optimal=True` /
  `eligible_for_binding=True`. `WorkflowResolver` now takes
  `upstream_discovery_complete` and `upstream_limit_codes`; the effective flag
  is the conjunction with its own graph expansion and reaches the selector, the
  `UNSATISFIABLE`-versus-`INCOMPLETE` decision, and the independent validator's
  candidate-universe check. Inconsistent flag pairs raise. This channel is
  general — **Stage 5 remote search must feed the same input.**
- Vertical slice: against direct kilometres at cost 9 and metres at cost 4, the
  resolver selects metres plus an explicit cost-1 conversion, validates,
  compiles to two Stage-1 tasks, executes, and commits `1.5` from `1500` m.
  Restricting the request to `SYNTHETIC` origin correctly falls back to the
  direct source instead of transforming silently.

See `transformations/`, `stage4/`, `stage4/README.md`, and commit `e8b4bfd`.

### Stage 5 — progressive acquisition, coverage, and real alternatives

- Acquisition became a planning act with a type-enforced order:
  `search metadata -> assess coverage -> bind an exact manifest -> fetch bytes`.
  `SourceConnector.search_metadata()` cannot return bytes, and
  `open_payload()` requires a `FetchAuthorization` whose only constructor is
  `BoundAssetManifest.authorization()`. The demo measures zero bytes moved
  while the availability snapshot is being built.
- `AssetManifest` is summary-only: shard digests, counts, and a root that
  commits to shard *order*. Assets stream one shard at a time from a
  content-addressed `ManifestShardStore`, so controller memory tracks shard
  count rather than asset count.
- A bound manifest lowers to an ordinary `CapabilitySpec` through
  `lower_manifest_to_capability()`, exactly as Stage 4 lowered a
  transformation. Stage 3 again needed **no** acquisition-specific code path,
  and an acquired artifact competes with conversions and models in one global
  selection. `manifest_root` and `asset_ids` are scientific parameters, so the
  manifest identity reaches the bound derivation ID for free.
- Discovery is a genuine fixed point. A producer's expansion can open a
  second-order data query — the fixture's model needs support data over the
  extent the coarse source *actually* returned, which the overhanging tiles
  make unknowable in round zero — and the snapshot cannot freeze until no new
  query appears. Second-order rules come from a closed registry keyed by rule
  ID; a caller cannot supply a callable.
- **Every Stage-5 truncation feeds the Stage-4 channel, not a new one.**
  `resolution/upstream.py` adds `UpstreamCompleteness`, which folds discovery
  layers by conjunction of completeness and union of typed reasons and emits
  exactly the two keyword arguments `WorkflowResolver` already accepts. It
  refuses to assemble an inconsistent pair, mirroring the resolver's own rule.
- Planning sessions are durable. Cursors, candidates, cooldowns, persisted
  truncation reasons, and a provider quota ledger live in SQLite; a page and
  its cursor advance commit in one transaction. A restart resumes from the
  persisted page and cannot re-spend quota it already spent. Only a *whole*
  search freezes the session; a truncated one stays resumable.
- Snapshot identity covers what was found and whether the search was whole —
  manifest roots, completeness, limit reasons — not the pagination history. A
  resumed search that finds the same assets yields the same snapshot ID.
- Staleness ends a plan rather than patching it. A mutated or missing bound
  asset raises `BINDING_STALE` and yields an `ExclusionChildPlan` that records
  what may not be used again, with no replacement field. A *transient* outage
  is the opposite case: the same binding is retried, the manifest root does not
  change, and no metadata search runs.
- A source with no ETag, version, or checksum is `UNBINDABLE`. It can only be
  bootstrapped through a quarantined `SnapshotIngestionPlan` that
  content-hashes what it observed and commits an `ArtifactLeaf`.
- Secrets are referenced (`CredentialRef`), resolved only at transfer time, and
  asserted absent from every serialized record.

See `acquisition/`, `stage5/`, `stage5/README.md`, and `resolution/upstream.py`.

### Stage 6 — dataset versus model, evidence gating, and the latency gate

- Built the four-input reduced-consequence slice the roadmap draws: a result
  requiring ignition, fuel, terrain, and a contested flow field that a direct
  observed source and a coarse-source-plus-lightweight-model path both offer.
  Terrain feeds both the consequence model and the downscaling model, so the
  shared-input counterexample is executable rather than described.
- **Evidence gating happens at discovery, not at selection.** Move the model's
  `EvidenceApplicability` outside the request and it never enters the graph:
  `direct_match` rejects it, the rejection is recorded, and no constraint or
  objective can resurrect it. The predicate itself was Stage-2 work; Stage 6
  makes it executable and reuses the *same* predicate in the decision report
  rather than a second copy that could drift.
- Added `objectives/`, implementing Section 6.4's named selection policies.
  `MINIMUM_COST` is the only automatic objective. `EMPIRICAL_QUALITY` always
  returns `CHOICE_REQUIRED` with no auto-selected plan.
- Alternatives are enumerated by **re-solving the whole problem once per
  candidate producer**, so every presented option is a globally consistent plan
  with a real cost — necessary because producers share inputs. This is not
  Pareto enumeration and claims no non-dominance.
- `ranking_complete` and `nondominance_claimed` are fixed `False` properties,
  not fields; `from_dict` rejects a payload claiming either.
- Comparability is refused unless every alternative carries a known claim for
  the same metric, from the same evaluator and protocol, against the same
  reference manifest, in the same unit and bound kind, with applicability
  covering the request. All blocking codes are reported together. Overlapping
  confidence intervals set `separation_established=False`; missing intervals
  fall to the safe side.
- The sharpest property: the fixture's evidence *is* comparable and its
  intervals *do not overlap*, so a ranking would look defensible — and the
  system still returns `CHOICE_REQUIRED`.
- A `ChoiceRecord` is bound to the `report_id` it was made from; a stale choice
  raises `StaleChoiceError` instead of being applied to a changed world. The
  accepted decision becomes a `PlanSnapshotRef("objective_decision", …)` and
  therefore enters `bound_plan_id`: the same invocations chosen by a human and
  chosen by cost are different plans.
- **Closed a real compiler gap.** `compile_bound_plan` used to refuse every
  evidence-bound proof outright. It now accepts an `evidence_snapshot` and
  replays them faithfully — checking the profile is really in the frozen
  snapshot, the snapshot identity matches, and deriving the evidence subject
  from the producer rather than reading it from the proof. Without a snapshot
  it still fails closed.
- Added the closed `reduced.downscale.v1` and `reduced.consequence.v1`
  operations and their binders.

See `objectives/`, `stage6/`, and `stage6/README.md`.

## Why the global resolver looks this way

Do not replace Stage 3 with independent per-requirement greedy or local top-k
selection. That approach is not globally correct when components share inputs
or one invocation co-produces multiple outputs. The chosen architecture is:

```text
typed requirements
       |
       v
finite admissible derivation hypergraph
       |
       v
global exact selection over the frozen graph
       |
       v
independent semantic validation
       |
       v
bound scientific plan -> deployment plan -> durable runtime
```

The exhaustive Stage-2 oracle remains the correctness reference for small
graphs. The MILP is the finite-graph production selector, not a claim that
million-candidate discovery is solved.

## Verification evidence at handoff

The **entire** repository suite passed with Stage 6 in the working tree:

```text
617 passed, 1 skipped, 7 xfailed
```

Progression: Stage 4 `501/1/6`, Stage 5 `574/1/6`, Stage 6 `617/1/7`. Stage 6
added 43 tests and changed no existing expectation except the closed
binder-key list.

**The seventh xfail is new and is not a quarantine.** It is the strict-xfail
Section 9.5 latency gate: a real, measured miss (see the Stage-6 exit evidence
below). The other six remain the two quarantined WRF decisions and the four
frozen legacy-runtime defects.

If `stage6/planning_benchmark_v1.json` is ever absent, five benchmark tests
skip instead of running; re-freeze it with
`scripts/freeze_stage6_benchmark.py` (about 13 minutes for 30 runs).

```bash
.venv/bin/python -m pytest tests/ -q
```

The six xfails are strict and deliberate: two quarantined WRF configuration
decisions and four frozen legacy-runtime defects (see `stage0/`). A strict
xfail that starts passing fails the suite and forces a decision.

Note: `tests/test_stage1_runtime.py::
test_controller_restart_reconciles_live_process_without_resubmit` is
timing-sensitive and has been observed to fail once under full-suite load while
passing repeatedly in isolation. Re-run it alone before treating it as a
regression.

The executable Stage-3 proof completed:

- resolution status: `READY`; discovery complete; global optimum proven
- selected capabilities: `example-pair` plus `example-add`, cost `3`
- Stage-1 tasks/attempts `2 / 2`; committed result `42`; `SUCCEEDED`

The executable Stage-4 proof completed:

- resolution status: `READY`, validated, eligible for binding
- transformation closure complete; effective discovery complete
- selected: `example-length-metres` +
  `transform:example-metres-to-kilometres`, cost `5`
- rejected direct alternative at cost `9`
- Stage-1 tasks/attempts `2 / 2`; committed result `1.5`; `SUCCEEDED`

The executable Stage-5 proof completed:

- discovery ran 2 rounds; the second-order query fired before the snapshot froze
- 3 manifests bound; coverage `COMPLETE` from two tiles
- bytes transferred during planning: `0`; after binding: `685`
- binding `FRESH` at transfer time; manifest root present in the bound plan
- resolution `READY`, validated, globally optimal
- selected: `acquire:remote-tiled-archive:…` + `transform:example-mps-to-kmph`,
  cost `5`
- rejected the pinned local alternative at cost `8`
- Stage-1 tasks/attempts `2 / 2`; committed the joined, converted field;
  `SUCCEEDED`

The executable Stage-6 proof completed:

- minimum cost selected the modelled path at cost `11`, rejecting the direct
  path at `13`; 6 tasks / 6 attempts; committed `21.0`; `SUCCEEDED`
- the same request outside the model's evidence applicability resolved to the
  direct source at `13`, with the model **absent from the graph entirely**
- a quality request returned `CHOICE_REQUIRED` with `auto_selected=false`,
  despite comparable evidence with disjoint confidence intervals
- a recorded human choice re-solved to the direct source and changed the bound
  plan identity

Run any of them only with a fresh node-local temporary directory:

```bash
runtime_root=$(mktemp -d /tmp/nasa-stage3-demo.XXXXXX)
.venv/bin/python scripts/run_stage3_demo.py --runtime-root "$runtime_root"

runtime_root=$(mktemp -d /tmp/nasa-stage4-demo.XXXXXX)
.venv/bin/python scripts/run_stage4_demo.py --runtime-root "$runtime_root"

runtime_root=$(mktemp -d /tmp/nasa-stage5-demo.XXXXXX)
.venv/bin/python scripts/run_stage5_demo.py --runtime-root "$runtime_root"

runtime_root=$(mktemp -d /tmp/nasa-stage6-demo.XXXXXX)
.venv/bin/python scripts/run_stage6_demo.py --runtime-root "$runtime_root"
```

Additional evidence:

- A 30-run warm conformance microbenchmark measured approximately 93 ms p95
  around the full resolver call on the development node. This is not the
  pending representative Stage-6 SLO.
- A 1,000-producer/1,000-arc alternative-fanout stress check returned
  `OPTIMAL` in approximately 2.16 seconds and 36 solver calls on this node.
- A read-only audit compared 1,000 generated small frozen graphs with the
  exhaustive oracle: 411 feasible and 589 unsatisfiable, with zero exact
  plan-ID/signature mismatches.

## Current limitations and non-claims

- Stage 3 uses a finite frozen in-memory catalog. Discovery bounds limit the
  retained graph, not necessarily the work of an eager binder.
- **No real network provider has been contacted.** Stage 5's two connectors run
  in process. They implement the full contract — pagination, conditional
  identity, mutation, disappearance, transient outages — but a real HTTP/S3
  connector is not written, and nothing is claimed about live provider
  behaviour, TLS, or authentication flows.
- Stage-5 coverage is single-source and axis-aligned. Cross-provider mosaics,
  coverage atoms, and general polygon unions remain unimplemented; manifest
  sharding is a two-level digest tree, not a general Merkle structure with
  inclusion proofs. The largest manifest exercised is 2,000 assets, so
  million-asset discovery is still not claimed.
- `acquisition.materialize.v1` reads a local content-addressed store whose
  location comes from the `NASA_STAGE5_ASSET_STORE` environment variable. What
  it reads is pinned by manifest root, asset list, and per-blob sha256, so the
  result does not depend on the path — but the operation is not pure over its
  parameters alone. This is a deliberate, documented exception.
- Stage-5 transfer is single-threaded and per-asset: no parallel fetch, range
  requests, or resume mid-asset. Provider quota is one SQLite ledger on one
  node, not a distributed quota. Connector deadlines are checked between pages,
  not as cancellation of an in-flight request.
- `direct_match()` still performs no conversion, reprojection, interpolation,
  regridding, filling, or vector transformation. As of Stage 4 those exist as
  explicit declared capabilities; nothing became implicit.
- Transformation catalogs are finite and in memory. Closure is forward
  reachability over declared edges; it never synthesizes new edges.
- The demonstrated Stage-4 slice converts a scalar. Grid, temporal,
  reprojection, and vector operations are implemented and tested at the
  operation and contract layers, but no multi-hop chain is promoted as a
  scientific fixture.
- Transformation *loss* is declared and visible but is not an optimization
  dimension. A lowered transformation carries `evidence:unknown`; Stage 4 does
  not invent empirical error for a conversion.
- The automatic MVP objective is minimum declared integer cost under hard
  constraints. Quality optimization, latency objectives, Pareto enumeration,
  CP-SAT, beam/A*, and learned estimates are deferred.
- Static deployment feasibility is not scheduling, capacity reservation, node
  choice, queue prediction, or backfilling.
- The Stage-1 compiler cannot yet feed a selected external committed artifact
  directly into an executable task. It fails closed rather than fabricating a
  producer.
- SQLite durability is same-node controller/process recovery only. It is not
  node-loss durability and must not be placed on unverified NFS/Lustre storage.
- Slurm remains a future conditional provider. The development machine is a
  private CHPC node, not a Slurm test environment.
- **Stage-6 planning latency misses the Section 9.5 target by roughly 5x** on a
  representative graph. This is the largest known gap at the MVP boundary.
- Stage-6 evidence is **synthetic fixture data**. Real wind evidence remains
  unavailable and was not invented; `stage2/wind_evidence_pack_v1.json` is
  still frozen at `status: UNAVAILABLE`. Nothing in Stage 6 is a claim about
  ERA5, WRF, or any real wind product.
- The Stage-6 "lightweight model" is meaningless gain-plus-support arithmetic.
  It is a model architecturally -- a producer with declared evidence and
  applicability limits -- not scientifically.
- Comparability is judged on provenance and applicability, not statistical
  power. Overlapping intervals are reported; no hypothesis test runs, and the
  block-bootstrap method is *declared* by the fixture rather than executed.
- Quality alternatives are include-constrained re-solves, one per candidate
  producer. Not Pareto enumeration, no non-dominance claim. Only one contested
  concept per decision report is supported.
- `quality_under_budget`, `minimum_dependency_latency`, and user-defined
  lexicographic policies from Section 6.4 remain deferred.
- WRF-SFIRE is not a current test workload.

## Stage-4 exit evidence (met)

Recorded so the next instance does not re-litigate settled ground. Each of the
ten Stage-4 invariants was checked against the implementation:

- `direct_match()` remains pure; no adapter hides a scientific transformation.
- Every conversion is a declared, versioned, costed capability with typed
  ports, closed operation/binder identity, and explicit assumption IDs.
- Codec/materialization operations stayed separate from semantic transforms.
- Closure terminates over a finite descriptor-state space; every activated
  bound marks discovery incomplete with typed reasons naming what was dropped.
- Truncation can no longer support a global-optimality claim (this was a real
  defect found and fixed, not merely asserted -- see the Stage-4 entry above).
- Transformation nodes, costs, versions, and the closure identity are visible
  in the bound plan and its snapshot references.
- The transformed artifact validates and commits through the Stage-1 runtime.
- Domain-neutral scalar and vector fixtures only; no resolver code depends on
  any concept meaning.

## Stage-5 exit evidence (met)

Each of the ten Stage-5 invariants was checked against the implementation, and
each expected exit criterion is asserted by a test rather than only observed in
the demo:

1. **Metadata search never fetches payload bytes.** Enforced by types:
   `search_metadata()` cannot return bytes, and `open_payload()` requires an
   unforgeable `FetchAuthorization`. Byte counters read zero at snapshot freeze.
2. **No stable conditional identity means `UNBINDABLE`.** Such candidates are
   named, never silently dropped, and only the quarantined
   `SnapshotIngestionPlan` can content-address and commit them.
3. **Truncation flows through the existing channel.** `UpstreamCompleteness`
   folds acquisition and transformation layers into the same two resolver
   arguments Stage 4 added. No second completeness mechanism was built.
4. **A missing or mutated bound asset ends the plan.** `BINDING_STALE` plus an
   `ExclusionChildPlan` with no replacement field; the surviving neighbour is
   never promoted to cover for the excluded one.
5. **A transient outage retries the same binding.** Same manifest root, same
   asset set, zero additional metadata searches.
6. **Planning sessions are durable.** Cursors, candidates, cooldowns, and limits
   survive restart; resumption re-reads only the remaining pages; only a whole
   search freezes the session, and re-freezing at a different snapshot raises.
7. **Quota is system-level.** One provider ledger debited by both search and
   transfer, surviving restart so a resumed session cannot re-spend.
8. **Secrets are referenced, never embedded.** A test asserts the secret value
   is absent from every serialized expansion, manifest, and descriptor.
9. **Controller memory stays bounded.** The manifest holds only shard digests;
   a 2,000-asset manifest streams with at most one shard resident.
10. **Gaps cannot register a complete artifact.** A hole is a typed
    `SPATIAL_GAP`/`TEMPORAL_GAP` that refuses to bind, and lowering rejects a
    descriptor claiming `COMPLETE` missingness over incomplete coverage.

Exit criteria:

- payload transfer begins only after binding — **met** (0 bytes during planning);
- a wind-shaped requirement resolves against at least two real direct
  alternatives — **met** (pinned local at cost 8 versus remote tiles at cost 3
  plus a declared cost-2 conversion, with an admissible model alternative at 8
  losing on cost rather than on feasibility);
- restarting mid-pagination resumes from the persisted cursor — **met**;
- a model whose expansion reveals a second-order data query triggers another
  discovery round before the snapshot freezes — **met** (2 rounds); and
- the bound derivation ID includes the manifest root — **met** (`manifest_root`
  is a scientific parameter of the acquisition capability).

One design decision worth not re-litigating: acquisition lowers to an
executable *capability*, not to an `ArtifactLeaf`. That was deliberate. The
Stage-1 compiler still fails closed on a selected external leaf feeding an
invocation (`BRIDGE_EXTERNAL_LEAF_UNSUPPORTED`), and fixing that bridge is not
Stage-5 scope. Lowering to a capability keeps acquisition inside the ordinary
selector without touching the compiler, and mirrors how Stage 4 lowered
transformations.

## Stage-6 exit evidence (mostly met; one gate MISSED)

Six of the seven Stage-6 exit criteria are met and asserted by tests. The
seventh -- the Section 9.5 planning-latency budget -- was measured and **is not
met on this node**. It is recorded as a real miss rather than engineered around.

Met:

- **Direct and model-produced sources compete in the global selector.** The
  modelled path wins at cost 11 against a real direct alternative at 13.
- **Model output qualifies only where its evidence applies.** Out of scope it
  is rejected during *discovery* and never becomes a candidate, which is
  stronger than losing on cost.
- **`discovery_complete` + `OPTIMAL` really means globally minimum cost** for
  the frozen graph; the shared terrain input is counted once.
- **Every quality request returns `CHOICE_REQUIRED`**, including the case where
  the evidence is comparable and its intervals are disjoint. Comparable metrics
  are decision support attached to a human's recorded choice, never an
  automatic quality optimizer.
- **Full execution follows the selected immutable derivation** -- six tasks,
  six attempts, committed result, `SUCCEEDED`.
- **The result retains inputs, outputs, configuration, evidence snapshot, and
  attempt provenance**, and a recorded choice changes `bound_plan_id`.

Missed:

- **Warm end-to-end planning does not meet the Section 9.5 target.** On the
  frozen representative graph (126 invocations, 250 arcs, 6 levels -- all
  inside the 1,000/5,000/12 caps) the p95 is roughly **23 s against a 5 s
  budget**. The MILP solve is ~83% of planning time and scales sharply: 30
  invocations 1.1 s, 64 invocations 5.5 s, 126 invocations ~23 s. Enabling
  HiGHS presolve helps (4.6 s to 3.1 s solve at 64 invocations) but nowhere
  near enough, and presolve is off by default because Stage 3 found this build
  returning a false infeasibility on a valid regression.

  The assertion lives as a **strict xfail** in `tests/test_stage6_benchmark.py`.
  If solver work ever makes it pass, the suite fails and forces a re-freeze and
  an updated claim. `test_the_frozen_profile_is_a_real_graph_not_a_toy` asserts
  a floor on graph size so the gate cannot be met by shrinking the problem.

One design decision worth not re-litigating: the decision record binds into
`bound_plan_id` through a `PlanSnapshotRef`, not into `CandidateDerivationPlan`
identity. The roadmap says "candidate-plan identity"; the bound plan is what
executes, and reaching it this way avoided changing Stage-2 core identity.

## Next: the Composition MVP release boundary -- not Stage 7

The roadmap stops here deliberately: *"stop here, run a user/scientist review,
and repair correctness or usefulness problems before adding scale features. The
remaining stages are separately justified post-MVP work, not prerequisites for
the first usable system."*

**Do not begin Stage 7 without the user's direction.** Bring these two findings
to the review first:

1. **Planning latency misses its target by roughly 5x** on a representative
   graph. This is a solver-strategy problem, not a graph-size problem. Options
   worth costing: re-examining the two-phase cost/tie-break formulation and its
   solver-call count, the disabled presolve (which needs the Stage-3 HiGHS
   false-infeasibility bug re-checked against a current build), warm starts,
   CP-SAT as an alternative backend, or accepting a larger budget and saying so
   in Section 9.5.
2. **There is still no real reference observation dataset.**
   `stage2/wind_evidence_pack_v1.json` remains frozen at `status: UNAVAILABLE`
   with `NO_REVIEWED_IMMUTABLE_HELD_OUT_REFERENCE`. Stage 6's evidence is
   synthetic fixture data and is labelled as such everywhere. Populating a real
   pack needs held-out observations with a reviewed licence and QC policy --
   a data-acquisition and review task, not a coding task.

## Later stages, briefly

- **Stage 7:** lazy partition/collection runtime with bounded admission and
  `10^4`-partition correctness.
- **Stage 8:** resource-aware local/fixed-allocation scheduling and placement.
- **Stage 9A:** conditional nonblocking Slurm provider, only when an eligible
  workload/site requires it.
- **Stage 9B:** WRF integration through a proven execution provider, using
  reference fixtures; never use WRF as the first runtime correctness test.
- **Stage 10+:** measurement-triggered arrays/pilot/million-scale hardening,
  operations, standards export, and finally the research paper.

## Credentials and machine migration

Credentials are intentionally absent from this file and the repository.

- Authenticate the new Codex/OpenAI installation independently.
- Authenticate GitHub independently with `gh auth login`, a credential manager,
  or an SSH key.
- Never paste OpenAI credentials, GitHub tokens, passwords, or SSH private keys
  into a chat, plan, Git commit, or handoff file.
- Before pushing, verify the remote and staged paths:

```bash
git remote -v
git status --short
git diff --cached --name-only
```

If the remote cannot be used, transport the branch without credentials by
creating a Git bundle:

```bash
git bundle create ../NASA_Project-v2.bundle v2
```

Then copy that bundle to the new machine, clone it, set the GitHub remote, log
in there, and push `v2`.

## Suggested first prompt on the new machine

```text
Read codex_handoff.md completely. Verify branch v2 and Stage-6 commit bf4e360.
Inspect git status and preserve the listed user-owned dirty files. Read
stage4/README.md, stage5/README.md, and stage6/README.md. Run the full test
suite (expect 617 passed, 1 skipped, 7 xfailed) without WRF-SFIRE, MPI, Slurm,
or any real remote provider.

Do NOT start Stage 7. Stage 6 reached the Composition MVP release boundary,
where the roadmap requires a user/scientist review first. Two findings should
lead that review: the Section 9.5 planning-latency gate is measured and missed
(the MILP solve dominates and blows the 5 s p95 budget at ~126 invocations,
well inside the 1,000-node cap), and no real reference observations exist to
populate an evidence pack. Ask the user how they want to handle both before
writing any code. If they direct you to work on latency, treat solver strategy
as the target, not graph shrinking -- the benchmark deliberately asserts a
floor on graph size so the gate cannot be gamed.
```
