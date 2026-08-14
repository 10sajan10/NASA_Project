# Codex Project Handoff

Last updated: 2026-08-14

This is the durable handoff for a new Codex session. Treat the repository,
tests, and roadmap as authoritative; the old chat transcript is supporting
context only.

## First instructions for the next Codex instance

1. Read this file completely.
2. Work in `/uufs/chpc.utah.edu/common/home/parashar-vdc/sajan/NASA_Project`.
3. Verify branch `v2` and Stage-3 commit `36c46a8` before changing anything.
4. Inspect `git status` before edits. Preserve the user-owned dirty files listed
   below and never stage them accidentally.
5. Read `stage3/README.md` and the Stage-4 section of the external roadmap.
6. Run only the bounded Stage 0-3 tests initially. Do not run WRF-SFIRE, MPI,
   Slurm, remote acquisition, or other heavy workloads.
7. Continue with Stage 4 only after reviewing its scientific and termination
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
- External roadmap:
  `/uufs/chpc.utah.edu/common/home/parashar-vdc/sajan/nasa_project_docs/scientific_workflow_composition_plan.md`
- Original poster:
  `/uufs/chpc.utah.edu/common/home/parashar-vdc/sajan/nasa_project_docs/diagrams/2026_SCposter_SajanNeupane.pdf`

The roadmap was updated through Stage 3. It explicitly marks the final
representative Stage-6 planning-latency gate as pending.

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

The complete bounded Stage 0-3 suite passed:

```text
160 passed, 4 expected xfails
```

Command:

```bash
.venv/bin/python -m pytest -q \
  tests/test_stage0_runtime_baseline.py \
  tests/test_stage0a_conformance.py \
  tests/test_stage1_provider.py \
  tests/test_stage1_runtime.py \
  tests/test_stage2_matching.py \
  tests/test_stage2_capabilities.py \
  tests/test_stage2_oracle.py \
  tests/test_stage2_integration.py \
  tests/test_stage2_evidence_pack.py \
  tests/test_stage3_hypergraph.py \
  tests/test_stage3_milp.py \
  tests/test_stage3_validator.py \
  tests/test_stage3_integration.py
```

The executable Stage-3 proof completed:

- resolution status: `READY`
- discovery complete: `true`
- global optimum proven: `true`
- selected capabilities: `example-pair` plus `example-add`
- selected cost: `3`
- Stage-1 tasks/attempts: `2 / 2`
- committed result: `42`
- run state: `SUCCEEDED`

Run it only with a fresh node-local temporary directory:

```bash
runtime_root=$(mktemp -d /tmp/nasa-stage3-demo.XXXXXX)
.venv/bin/python scripts/run_stage3_demo.py --runtime-root "$runtime_root"
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
- `direct_match()` performs no conversion, reprojection, interpolation,
  regridding, filling, or vector transformation. Those must become explicit
  Stage-4 capabilities.
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

## Next implementation stage: Stage 4

Stage 4 introduces explicit, bounded semantic transformation-path discovery.
The roadmap currently uses wind to exercise this layer, but the implementation
must remain applicable to any scientific artifact.

Before editing, critique the Stage-4 design against these invariants:

1. `direct_match()` remains pure and never inserts transformations.
2. Every result-affecting transformation is a declared, versioned capability
   with typed input/output descriptors, parameters, cost, evidence, and
   implementation identity.
3. Codec/materialization operations such as decoding, decompression, NetCDF or
   Zarr serialization, and model staging remain explicit but separate from
   scientific semantic transformations.
4. Search terminates over a finite canonical descriptor-state space or another
   documented well-founded measure.
5. Any depth/state/candidate truncation marks discovery incomplete and cannot
   support a global-optimality claim.
6. Transform chains appear in the candidate/bound plan and provenance; an
   adapter may not silently subset, interpolate, reproject, rotate, convert, or
   fill values.
7. The selected transform output is independently validated against the target
   requirement before commit.
8. Start with bounded transforms required by one vertical slice: unit
   conversion, spatial/temporal subsetting, temporal alignment,
   reprojection/continuous regridding, and vector representation/rotation.
9. Use domain-neutral scalar and vector fixtures for correctness. A small local
   wind artifact may be an integration example, but no resolver code may depend
   on the concept being wind.
10. Do not begin remote acquisition while implementing Stage 4; progressive
    source discovery and exact `AssetManifest` binding belong to Stage 5.

Expected Stage-4 exit evidence:

- no adapter hides a scientific transformation;
- bounded search terminates and exposes incomplete frontiers honestly;
- scalar continuous-field and generic vector tests cover units, space, time,
  grid/CRS, alignment, and representation changes needed by the vertical
  slice;
- transformation nodes, proofs, costs, and versions are visible in the
  selected plan; and
- the transformed artifact validates and commits through the existing runtime.

## Later stages, briefly

- **Stage 5:** progressive acquisition; local plus one remote connector;
  metadata search; exact immutable `AssetManifest`; identity checks; quotas,
  timeouts, and coverage; payload bytes only after binding.
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
Read codex_handoff.md completely. Verify branch v2 and Stage-3 commit 36c46a8.
Inspect git status and preserve the listed user-owned dirty files. Read
stage3/README.md and the Stage-4 roadmap. Run the bounded Stage 0-3 acceptance
suite without WRF-SFIRE, MPI, Slurm, or remote data. Then critique the Stage-4
approach and implement only its first exit-gated domain-neutral slice.
```
