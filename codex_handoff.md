# Codex Project Handoff

Last updated: 2026-08-14 (Stage 4 complete)

This is the durable handoff for a new AI session. Treat the repository,
tests, and roadmap as authoritative; the old chat transcript is supporting
context only.

## First instructions for the next instance

1. Read this file completely.
2. Work in `/uufs/chpc.utah.edu/common/home/parashar-vdc/sajan/NASA_Project`.
3. Verify branch `v2` and Stage-4 commit `faf19cd` before changing anything.
4. Inspect `git status` before edits. Preserve the user-owned dirty files listed
   below and never stage them accidentally.
5. Read `stage3/README.md`, `stage4/README.md`, and the Stage-5 section of the
   external roadmap.
6. Run only the bounded Stage 0-4 tests initially. Do not run WRF-SFIRE, MPI,
   Slurm, remote acquisition, or other heavy workloads.
7. Continue with Stage 5 only after reviewing its acquisition and binding
   invariants. Keep the implementation domain-neutral: wind is an example, not
   a privileged concept in the architecture.

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
- Stage-4 implementation commit: `faf19cd`
- External roadmap:
  `/uufs/chpc.utah.edu/common/home/parashar-vdc/sajan/nasa_project_docs/scientific_workflow_composition_plan.md`
- Original poster:
  `/uufs/chpc.utah.edu/common/home/parashar-vdc/sajan/nasa_project_docs/diagrams/2026_SCposter_SajanNeupane.pdf`

The roadmap was updated through Stage 3. It explicitly marks the final
representative Stage-6 planning-latency gate as pending. The roadmap text
itself has **not** been updated for Stage 4; `stage4/README.md` is the
authoritative record of what Stage 4 actually delivered.

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

See `transformations/`, `stage4/`, `stage4/README.md`, and commit `faf19cd`.

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

The **entire** repository suite passed at Stage-4 commit `faf19cd`:

```text
501 passed, 1 skipped, 6 xfailed
```

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

Run either only with a fresh node-local temporary directory:

```bash
runtime_root=$(mktemp -d /tmp/nasa-stage3-demo.XXXXXX)
.venv/bin/python scripts/run_stage3_demo.py --runtime-root "$runtime_root"

runtime_root=$(mktemp -d /tmp/nasa-stage4-demo.XXXXXX)
.venv/bin/python scripts/run_stage4_demo.py --runtime-root "$runtime_root"
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
- There is no remote metadata search, durable planning-session cursor,
  progressive asset binding, payload fetch, generic mosaic, or million-asset
  manifest yet.
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

## Next implementation stage: Stage 5

Stage 5 introduces progressive acquisition: local plus one remote connector,
metadata-only search, coverage alternatives, exact immutable `AssetManifest`
binding, and payload bytes fetched **only after** binding.

Before editing, critique the Stage-5 design against these invariants:

1. Metadata search never fetches payload bytes. Binding precedes transfer.
2. A remote source without stable conditional identity (ETag/version/checksum)
   is `UNBINDABLE` for a scientific run. It may only be bootstrapped through a
   separate quarantined `SnapshotIngestionPlan` whose bytes cannot satisfy a
   scientific requirement until committed as an `ArtifactLeaf`.
3. Discovery truncation -- page limits, asset caps, plan-count caps, timeouts,
   provider cooldowns -- **must** flow into the resolver through the existing
   `upstream_discovery_complete` / `upstream_limit_codes` channel that Stage 4
   added. Do not build a second, parallel completeness mechanism.
4. A missing or mutated bound asset ends the plan with `BINDING_STALE` and a
   child plan recording the exclusion. It is never runtime substitution.
5. A transient outage retries the same binding; that is not replanning.
6. Planning sessions are durable: page cursors, cooldowns, and partial coverage
   state survive restart without bypassing quota or silently changing the
   frozen availability snapshot.
7. Provider quotas are system-level and shared by planning and fetching.
8. Secrets are referenced, never embedded in plans, manifests, or logs.
9. Controller memory stays bounded while manifest shards stream.
10. Gaps cannot register a complete artifact.

Expected Stage-5 exit evidence:

- payload transfer begins only after derivation and manifest binding;
- a wind-shaped requirement (still domain-neutral in code) resolves against at
  least two real direct alternatives;
- restarting mid-pagination resumes from the persisted cursor;
- a model whose expansion reveals a second-order data query triggers another
  discovery round before the availability snapshot freezes; and
- the bound derivation ID includes the manifest root.

## Later stages, briefly

- **Stage 6:** reduced scientific vertical slice and honest evidence-based
  alternatives. This is where the representative planning graph and final
  Stage-3 latency gate can be frozen.
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
Read codex_handoff.md completely. Verify branch v2 and Stage-4 commit faf19cd.
Inspect git status and preserve the listed user-owned dirty files. Read
stage3/README.md, stage4/README.md, and the Stage-5 roadmap. Run the full test
suite (expect 501 passed, 1 skipped, 6 strict xfailed) without WRF-SFIRE, MPI,
Slurm, or remote data. Then critique the Stage-5 acquisition approach against
the ten invariants listed in this file and implement only its first exit-gated
domain-neutral slice. Route any Stage-5 discovery truncation through the
existing upstream_discovery_complete channel rather than adding a second
completeness mechanism.
```
