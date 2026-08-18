# Codex Project Handoff

Last updated: 2026-08-18 (Stage 8R remediation and bounded same-node
operational closeout implemented; real-site evidence external; Stage 9B blocked)

This is the durable handoff for a new AI session. Treat the repository,
tests, and roadmap as authoritative; the old chat transcript is supporting
context only.

## 2026-08-18 current handoff — supersedes older status claims below

Stage 8R is no longer merely planned. Its adversarial matrix has fifteen
passing local cases and one explicitly external-blocked case, including
discovery-universe replay, transformation and
acquisition authority, exact contested arcs, field/grid/component agreement,
packet replay and restart reservations, partial scheduler visibility, and
authoritative Cube projection.  The implementation is currently a large
**uncommitted working-tree change on `v2` at base commit `45aaee1`**.  Do not
discard or broadly stage it.

The correct headline is **“bounded soundness and same-node operational gates
implemented; real-site evidence remains external,” not “universally complete.”**

| Area | Current status |
|---|---|
| Planning/discovery | PASS for an explicitly declared, certificate-covered universe. Every layer is independently replayed; copied/truncated content cannot silently claim the same universe. Stage-6 alternatives and the final chosen/fallback re-solve must retain the exact planning context. |
| Transform authority | PASS. Reserved operations replay the full transformation contract, rule digests, exact parameters/descriptors, component behavior, information-loss class, and uncertainty policy. |
| Acquisition science/content | PASS for the bounded local transcript. Session scope, queries/cursors/candidates, coverage, source schema, descriptor, ordered blobs, receipt, and materialization are replayed. Provider-global truth is not claimed. |
| Acquisition quota recovery | PASS for the same-node boundary. A non-expiring OS manifest lock excludes a live concurrent fetcher; durable per-asset attempt/checkpoint rows reuse completed blobs, and every retry or post-crash reopen consumes another call/byte reservation before provider access. |
| Grid/field contract | PASS for axis-aligned rectilinear `field-json-v2`. Component names are mandatory; signed sample-centre affine coordinates and Decimal block centres agree across contract, runtime, and commit. WRF CRS/origin establishment and hashes of external PROJ grid-shift bytes remain non-claims. |
| Packet/runtime | PASS for bounded same-node safety and reconciliation. Packet completion is derived from the exact RuntimeStore run, plan, tasks, validated artifacts, and scientific input receipts. An expired zero-launch run is cancelled under the exclusive controller lock and released without consuming retry sequence; an exact terminal result remains recoverable after expiry. Any run with uncertain launch evidence remains fenced. |
| Queued provider | PASS in the hermetic fake-Slurm controller→worker→artifact path; real deployment is **PARTIAL/untested**. No real Slurm, MPI, GPU, WRF, or network operation ran. |
| Cube | PASS for the bounded local RuntimeStore/DuckDB projection pair. Projection is authoritative and rebuildable; cross-database convergence is replayable/idempotent rather than atomic. |

Verification was intentionally bounded and split by subsystem.  Current-tree
evidence includes: planning/acquisition focused slices up to 211 passing;
coordinate slice 59 passing; runtime/partition/Cube slice 212 passing and one
skip; fake-Slurm slice 43 passing and one skip; and Stage-6 objective/context
45 passing.  A combined 669-test Stage/Cube collection reached 86% with no
failure before stalling in a slow provider test and was stopped; two attempts
at the repository-wide legacy suite also stalled after 20 tests in the old
Cube/critic path on this NFS workspace.  Do not invent a unified green total.

Operational-closeout verification added one current combined run of **206
passing** across Stage-5 session/binding/manifest/search/integration, Stage-7
admission/partitions, Stage-8R gates/partition inputs, and Stage-1
runtime/provider, followed by the receipt-ack crash regression; the affected
Stage-5/Stage-7 files now pass **70 tests** together. The new
crash/live-owner/late-terminal cases also pass in the machine-readable matrix.

The next stage is **Composition MVP release review**, in this order:

1. review the large uncommitted Stage-8R working tree and commit only project
   changes while preserving the user-owned WRF/config files;
2. ~~decide the missed Stage-6 representative latency/evidence gate~~ **DONE.**
   Re-measured and re-frozen on the same 126-invocation/250-arc graph: the
   latency gate improved 15.12 s -> 7.06 s (3.02x -> 1.41x over the 5 s
   budget) and remains a recorded strict-xfail miss. Doing so surfaced a
   larger limit: past ~254 invocations the 30 s solve budget expires before
   optimality is proven, so exact global selection is unavailable at about a
   quarter of Section 9.5's own 1,000-node cap. The **evidence** half is also
   decided: Stage 6 makes no empirical quality claim and the wind evidence pack
   stays `UNAVAILABLE`, with new tests binding its declared decision policy to
   the running system so the file and the behaviour cannot drift apart. See
   `stage6/README.md`;
3. gather real-site deployment evidence only when a provisioned private/Slurm
   site is available; and
4. choose R1/R2 reference-science work or authorized provider validation. Do
   not begin Stage 9B research work first.

   **R1 is started but structurally blocked.** `scripts/run_cascade.py` now
   refuses at launch when a target cannot be placed onto the cube grid, so the
   recorded 48.6-hour publication failure becomes an immediate refusal. The
   blocker beyond that is `SimulationGrid.crs_epsg: int`: WRF's domain-centred
   Lambert has no EPSG code, so the legacy cube cannot represent WRF's native
   grid at all. `crs_epsg` has 25 non-test uses, one of them
   `models/wrf_sfire_adapter.py:783` (`int(grid.crs_epsg)`), which is
   user-owned. Publishing WRF output on its native grid therefore needs either
   the user's approval to change that adapter, or publication through the
   Cube v3 entries path, which already carries a per-entry grid. Ask before
   touching the adapter.

   **The entries route is blocked by a second wall.** Cube v3 entries are only
   publishable through an authoritative RuntimeStore artifact, and
   `engine/runtime/artifacts.py` accepts `application/json` only. A 2130x2130
   fire mesh as JSON is roughly 4.5M numbers parsed and validated in full, so
   there is no viable binary payload path today. Either route therefore needs
   a deliberate extension: a general CRS on `SimulationGrid`, or a binary
   artifact media type.

   What is unblocked and done: producers can now declare a native grid from
   configuration (`models.wrf_georeference.native_grid_from_scenario`), which
   is what the launch gate consumes. That declaration is deliberately *not*
   authoritative -- WPS snaps the domain centre, so a requested centre of
   (-96.81, 32.78) produced a run at (-96.808891, 32.779987), 103.8 m out in
   longitude, more than one 90 m fire cell. Preflight with it; never publish
   with it.

## First instructions for the next instance

1. Read this file completely.
2. Work in `/uufs/chpc.utah.edu/common/home/parashar-vdc/sajan/NASA_Project`.
3. Verify branch `v2` at base commit `45aaee1`, then inspect the entire dirty
   Stage-8R working tree before changing anything (see above).
4. Inspect `git status` before edits. Preserve the user-owned dirty files listed
   below and never stage them accidentally.
5. Read the master roadmap's **Stage 8R** section and `stage8r/README.md` first.
   Older stage READMEs and the historical audit below contain useful history
   but are not the current status authority.
6. Run only the bounded Stage 0-9A tests initially. Do not run WRF-SFIRE, MPI,
   Slurm, real remote providers, or other heavy workloads. The Stage-5
   connectors are in-process; nothing in the suite touches a network.
7. **Do not trust a stage label or an aggregate green suite as a cross-layer
   proof.** Use the adversarial matrix and its exact tests.
8. **Do not start Stage 9B.** Perform the Composition MVP release review above.

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
- Stage-7 implementation commit: `59ffc8f`
- Stage-8 implementation commit: `538eb65`
- Audit remediation and runtime bridge: `18a5131` through `2d4abca`
- Stage-9A simulated provider: `1f82229`
- WRF placement/georeference/resampling work: `49841bd` through `995dedf`
- Cube v3 immutable-entry sidecar: `45aaee1` (current `v2` head)
- External roadmap:
  `/uufs/chpc.utah.edu/common/home/parashar-vdc/sajan/nasa_project_docs/scientific_workflow_composition_plan.md`
- Original poster:
  `/uufs/chpc.utah.edu/common/home/parashar-vdc/sajan/nasa_project_docs/diagrams/2026_SCposter_SajanNeupane.pdf`

The roadmap is tracked in the separate `nasa_project_docs` repository on branch
`main`; before this update its head was `a930997`. It is updated through the
Stage-9B blocked status and now contains the detailed Stage-8R remediation
plan. That repository has no configured upstream branch on this node, so verify
its remote and push state independently rather than assuming this handoff was
published. Its dirty README/diagram changes belong to the user and must be
preserved.

## Files owned by the user: preserve them

At handoff time, these tracked files have unstaged user changes:

- `configs/asteroid_burntest.yaml`
- `configs/asteroid_impact_7day.yaml`
- `models/wrf_config.py`
- `models/wrf_sfire_adapter.py`

The following user file is untracked:

- `.burncheck_logpath`

The separate `nasa_project_docs` repository also has user-owned changes in
`README.md`, `diagrams/make_diagrams.py`, and untracked diagram/PDF/cache files.
The Stage-8R edit intentionally changes only
`scientific_workflow_composition_plan.md` there.

Do not overwrite, revert, delete, or stage these files. Do not use broad
commands such as `git add .`, `git reset --hard`, or `git checkout -- <file>`.

## Historical pre-Stage-8R audit snapshot

This section is retained to explain why Stage 8R was created.  Its blocker
list describes the 2026-08-17 base tree and is **not** the current status; use
the 2026-08-18 table above for the implemented working tree.

The full hermetic suite was independently rerun from the current branch in the
real host context:

```text
852 passed, 1 skipped, 7 xfailed, 140 warnings in 88.60 s
```

Stage 4--8 bounded demos pass. Twenty-two WRF georeference tests also pass
against retained output metadata. **No WRF-SFIRE simulation, network transfer,
MPI job, or SLURM submission was executed.** These results prove regression and
bounded conformance, not the missing cross-layer guarantees.

| Boundary | Honest current status |
|---|---|
| Stages 0--3 | Built finite-graph foundation, contracts, exact selector, and local durable kernel |
| Stage 4 | Runnable finite-transform prototype; completeness and semantic authority are bypassable at the lowering boundary |
| Stage 5 | Bounded acquisition prototype; coverage/materialization and exact fetched-content identity are not yet one contract |
| Stage 6 | Evidence/decision prototype; exact contested-use forcing is missing, the latency gate fails, and real held-out evidence is absent |
| Stage 7 | 10^4 control-plane lifecycle plus an eight-partition executable slice; general partition inputs and replay idempotency remain open |
| Stage 8 | Policy plus real local concurrency bridge; restart reservation recovery and full-graph live priority remain open |
| Stage 9A | Fake-scheduler prototype only; partial observability can still duplicate submission |
| Stage 9B | Blocked and not started |
| Cube v3 | Tested immutable-entry/lineage sidecar; no production writer uses it and it is not authoritative publication |

### Reproduced release blockers

1. A truncated Stage-4 closure reports a false complete/global optimum when its
   bare augmented catalog is passed to the resolver without optional upstream
   flags.
2. A caller-built raw `CapabilitySpec` can claim a reserved transform and use a
   forged factor (the reproduced metre-to-kilometre example used 666) because
   transform kind/rule/assumptions are dropped during lowering.
3. Stage-5 lowering accepts caller-invented coverage; planned coverage admits
   mosaics the x-only runtime cannot materialize; fetched blob digests are only
   in a mutable post-fetch receipt rather than executable task identity.
4. Reapplying the same failed Stage-7 `PacketResult` increments the attempt
   count and writes another attempt row. `NOT_ATTEMPTED` can also consume retry
   budget.
5. A restarted controller rebuilds an empty Stage-8 ledger while a durable
   attempt is still active, so it can dispatch beyond capacity. Live rank is
   computed from the ready subset rather than the full dependency graph.
6. If a completed job is gone from `squeue` and `sacct` is unavailable, the
   Stage-9A provider can submit another job for the same token.
7. Grid semantics disagree across transform contract, runtime, and commit:
   block aggregation derives different axes, valid north-up negative-y grids
   fail commit validation, and a WRF-specific Lambert display token is not an
   executable CRS definition.
8. Stage-6 "source alternatives" constrain producer presence, not the exact
   contested `RequirementUse`/output arc.

These are the inputs to **Stage 8R -- Cross-layer Soundness and Integration**
in the master roadmap. Do not paraphrase them as already fixed.

### Cube v3: what Claude built and what it did not build

Commit `45aaee1` adds schema-v3 `entries` and `entry_inputs`, immutable entry
identity over content and derivation, deterministic `MOST_DERIVED`, `BASELINE`,
and `MOST_RECENT` policies, content-based staleness, per-entry grids, lineage,
and the important self/downstream exclusion for a model that consumes and
produces the same concept. Its 26 focused tests are valuable and pass.

The layer is not load-bearing. `write_static` still writes legacy mutable
tables; no producer publishes an entry through `ArtifactCommitter`; the Cube
catalog trusts a caller digest/free-text producer; entry and input-edge writes
are not an authoritative artifact transaction; and catalog export does not make
this a durable scientific provenance record. The migration should be additive:
retain compatibility reads for the roughly 15 read-mostly application
consumers, make the artifact transaction authoritative, and project verified
artifacts into Cube entries through an idempotent outbox.

Claude's suggestion that grid/preflight alone is enough to launch WRF is too
strong. It removes one predictable publication failure, but Stage 9B still has
no promoted R1/R2 fixture or certified provider, and the grid contract itself
is inconsistent. Build/compile work may continue independently, but do not run
WRF-SFIRE under this plan.

## Implemented components (scope-qualified)

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

**Current qualification:** the typed transformation layer and scalar demo are
real, but the lowering path drops the transform certificate and the resolver's
completeness input is an optional caller convention. The "correctness fix"
below was first-pass plumbing, not a system invariant; Stage 8R-A replaces it.

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

**Current qualification:** the API order and local demo are real. Claims below
about exact lowering, durable restart, quota, frozen sessions, quarantine, and
snapshot completeness are not release guarantees: adversarial tests found the
descriptor, receipt, coverage/materializer, and restart gaps summarized above.
Stage 8R-B owns their correction.

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

**Current qualification:** pairwise comparability and frozen-evidence lookup
were repaired, but an alternative re-solve constrains producer presence rather
than the exact contested use/arc. The latency gate remains an expected failure
and the evidence is synthetic.

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

### Stage 7 — lazy partitions, bounded admission, and collection completeness

**Current qualification:** the 10^4 run is a control-plane lifecycle. An
eight-member input-free packet executes through Stage 1, but per-partition
input binding is absent. The later replay audit also shows that applying the
same failed result creates another attempt; the retry/idempotency bullets below
describe the intended design and first-pass implementation, not a passed
Stage-8R gate.

- Added `partitions/`. `PartitionSetSpec` is the ordered Cartesian product of
  declared axes represented as a **mixed-radix number system**: partition *n* is
  decoded from its index, `total` is computed by multiplication, and there is
  deliberately no method that returns every key. `iter_keys` takes an offset and
  a limit, so a caller cannot ask for all of them.
- `PartitionTaskTemplate` holds the *single* resolved invocation. Every
  partition derives its logical task key from it, so partitions cannot drift
  onto different producers. The demo takes that invocation from a real Stage-3
  resolution rather than inventing one.
- Logical task identity is Section 8.7's: invocation hash + operation + ordered
  prospective input **slot** IDs + template ID + partition key. Deployment
  binding and attempt number are excluded, so revising one task's resources
  does not rename every unaffected logical task.
- **Admission is one transaction**, which is the whole correctness story. The
  idempotent `INSERT OR IGNORE` upserts and the guarded cursor advance share a
  single `BEGIN IMMEDIATE`, with `UNIQUE(collection, partition_index)` as a
  third defence so even a key collision cannot multiply a partition. A `fault`
  seam fires at three named points *inside* the transaction; tests crash at all
  of them, repeatedly, while draining the space, and assert nothing is skipped
  and nothing is multiplied.
- Added batched state writes (`record_outcomes`). One fsync per partition made
  a 10^4 collection commit-bound; batching cut a 1,600-partition drain to about
  0.1 s with identical semantics.
- Bounded memory is measured, not asserted. Holding tiles fixed and growing the
  temporal axis: 1,000 partitions peak at ~440 KB and 10,000 at ~617 KB, so
  **10x the partitions costs 1.4x the memory**. The 100 -> 1,000 step is window
  saturation and is documented as not an apples-to-apples comparison.
- Fusion bundles adjacent partitions into a `WorkPacket` bounded by member count
  *and* target cost, without merging identity: each member keeps its logical
  key, `PacketAttempt` reports members independently, committed members are
  never recomputed because a sibling failed, and a non-retry-safe template
  retries nothing automatically.
- A committed partition is never un-committed, so a duplicate or late packet
  result cannot destroy landed work. `CompletionPolicy.ALL` requires every
  partition committed *and zero failures*; `FRACTION` rounds its requirement up.

See `partitions/`, `stage7/`, and `stage7/README.md`.

### Stage 8 — resource-aware local scheduling

**Current qualification:** the policy is now wired to a real local-concurrency
bridge, contrary to the older "not wired" text retained later in this file.
However, reservations are in memory and disappear on controller restart, and
live priority does not use the whole graph. The simulation and local bridge are
useful evidence, not a restart-safe scheduler claim.

- Added `scheduling/`. Ready work is ranked by remaining **critical path**
  (computed once in reverse topological order, O(V+E)) combined with **aging**,
  so the task unblocking the longest tail runs first while bounded aging can
  reduce delay. Capped aging is not a general starvation-freedom proof.
  Aging is capped by `starvation_ceiling_s`; without a ceiling a long-waiting
  trivial task eventually outranks everything and the policy degenerates to
  FIFO with extra steps.
- On the imbalanced fixture — six short tasks that sort *before* a three-link
  chain, two cores — the layer runner and FIFO both take 39.0 s and
  event-driven takes **30.0 s**, which is the critical-path lower bound, so no
  ordering could do better. The chain runs back-to-back with no gaps, which is
  the roadmap's stated demonstration.
- `ReservationLedger` tracks CPU, memory, GPU, and scratch and refuses any
  reservation exceeding capacity on any dimension, **naming the dimension that
  blocked**. A refused reservation leaves nothing behind. The simulator routes
  every start through the same ledger, so `oversubscribed` is measured rather
  than assumed.
- Placement is best fit with environment affinity and deterministic tie-break.
- `detect_capacity()` prefers the cgroup quota and affinity mask over
  `os.cpu_count()`; `thread_environment()` caps `OMP_NUM_THREADS` and four
  siblings to reserved cores, without which the ledger's arithmetic is fiction.
- `ObservationHistory` returns the **declared** estimate until it has enough
  successful samples, and every `Estimate` carries its `source`, so a guess is
  never mistaken for a measurement. Median duration, worst-case memory, failed
  attempts excluded from duration but counted in failure rate.
- An underestimate produces a `DeploymentRevision` — an identified record with
  declared value, observed peak, proposal, and reason. It is a **proposal**;
  the declared envelope is never mutated, and a test asserts that.

See `scheduling/`, `stage8/`, and `stage8/README.md`.

## Historical August-16 audit remediation

This section records fixes that landed before the independent August-17
cross-layer review. It is historical evidence, not a claim that every boundary
is now sound; the current status and Stage-8R blockers above supersede its
headlines.

An independent audit in August 2026 reviewed Stages 4-8 and found that several
claims ran ahead of the implementation. Its verdict on status was accepted:
Stage 6 was a synthetic prototype, Stage 7 a control plane rather than
partition execution, and Stage 8 a policy simulator rather than the runtime
scheduler. Every finding below was independently reproduced before being fixed,
and each fix carries a regression test.

**Defects in the unsafe direction** (a wrong answer, not a missing feature):

- *Stage 6 could falsely declare evidence separation.* Interval separation
  asked whether all intervals shared one common intersection, which is not the
  question. With A=[0,2], B=[1,3], C=[4,5] it reported separation while A and B
  plainly overlap. Now a pairwise sweep, with touching endpoints counted as
  overlapping.
- *Stage 6 ignored the evidence snapshot it was given*, trusting a
  caller-supplied profile dictionary instead; an empty unrelated snapshot still
  produced two comparable alternatives. Readings are now resolved by deriving
  each producer's evidence subject and matching it against the frozen snapshot.
  The dictionary was deleted, not deprecated. The snapshot is part of report
  identity.
- *Stage 7 did not reuse the resolved operation.* The fixture resolved
  `example-add`/`synthetic.add.v1` and then built the template from
  `synthetic.constant.v1` with unrelated inputs, so "one scientific selection
  reused by 10,000 partitions" reused only the invocation hash. The template now
  carries a verified `BoundInvocation`. The old test passed because it checked
  the label rather than the operation -- worth remembering.
- *Stage 7 admission accepted a foreign spec or template* while advancing the
  cursor, so the wrong task became permanent. The collection's registered
  definitions are now authoritative.
- *Stage 5 coverage was assessed as independent spatial and temporal
  projections*, so two assets whose projections looked complete could hide a
  hole in their Cartesian product. Now assessed over the product, with a
  `SPATIOTEMPORAL_GAP` code, and a requested halo is mandatory input coverage
  rather than a selection hint.
- *Stage 5 binding did not freeze its query payload*, so a caller could mutate
  it after binding and relabel the same manifest root as another concept.
- *Stage 8 accepted non-finite durations.* NaN compares false against every
  bound, so a bare `< 0` check let it through and then poisoned the simulator.
- *Stage 8 could not see physical oversubscription.* Two eight-core logical
  sites on one eight-core host accepted two eight-core tasks. Sites may now
  declare a host and the ledger enforces its budget too.

**Claimed-but-absent behaviour:**

- *Stage 7 retry was impossible.* Failed members were marked FAILED while packet
  generation selected only ADMITTED rows, and there was no requeue API at all.
  `record_packet_result` now performs the transition and writes a durable
  `partition_attempts` row; the ceiling and non-retry-safe cases are terminal.
- *Stage 8 discarded its most informative evidence.* An OOM-like failed attempt
  produced no revision because all failures were excluded. Observations are now
  typed, a resource-exhausted attempt is a censored lower bound, and revisions
  cover all four dimensions rather than memory alone.
- *`PacketAttempt` mixed identity with outcome*, so a serialized FAILED could be
  edited to COMMITTED and still deserialize. Attempt identity is now minted
  before submission from packet, number, and fence token; `PacketResult` is
  separately identified over its outcomes and verifies that on load.

**Overstated claims, corrected in words rather than code:**

- "Low-priority work cannot starve" is only true for a rank gap within
  `ceiling x weight`. The cap is still wanted -- without it the policy
  degenerates to FIFO -- so the honest property is *bounded delay*, and a test
  now pins both sides.
- Stage-8 makespans of 39/39/30 remain a **simulation**. The wall-clock
  numbers are the bridge ones below.

## The runtime bridge (built)

Stages 7 and 8 had built layers *around* a deliberately serial controller. That
is now connected. `max_inflight` above 1 is permitted **only** against a
`ReservationLedger` -- raising it alone would be the oversubscription the policy
exists to prevent. The dispatch loop orders ready work with the Stage-8
critical-path and aging policy, reserves capacity before dispatch, skips a task
that does not fit rather than blocking behind it, and releases when a task
leaves an active state rather than on process exit. Fencing, leases, and the
atomic commit path are untouched: concurrency is across distinct tasks, each
keeping its own fence. The default is still 1, so every prior Stage-1 guarantee
holds unless a caller opts in.

Measured on real subprocesses: eight 0.4s tasks take about **6.9s serially and
1.9s four-wide**, each committing exactly one attempt. A two-core ledger caps
real concurrency at two even when eight are permitted. Observations now come
from real attempts; peak usage records the *reserved envelope* rather than a
measurement, because nothing samples memory yet and reporting a reservation as
an observed peak would feed the revision machinery invented numbers.

See `tests/test_stage9_runtime_bridge.py`.

## Stage 9A-Core — conditional SLURM provider (simulated only)

**Current qualification:** a later adversarial test found an unsafe partial
observability window: when the token is absent from `squeue` and `sacct` is
unavailable, submission can proceed and duplicate a completed job. Stage 8R-E
must make absence tri-state and require authoritative confirmed absence. Do not
describe this provider as ready for real-site validation until that passes.

`engine/runtime/slurm.py` implements most of the queued-provider boundary:
nonblocking submit, batched reconcile through `squeue` then `sacct`, cancel,
persisted external handles, and orphan detection.

**It has never talked to a real scheduler.** No `sbatch` was run. This node is
not a SLURM submit environment, and every behavioural test drives a fake
scheduler through the provider's command seam. Treat it as ready for Stage-8R-E
repair tests, not ready for real-site validation.

The design centre is the crash window between `sbatch` returning a job ID and
that ID reaching disk. Identity does not depend on our bookkeeping surviving:
each job carries its attempt token in its SLURM job name
(`nasa-attempt-<token>`), and submission asks the cluster whether that attempt
already has a job before running `sbatch`. A finished job has left `squeue` but
remains in `sacct`, so both are consulted.

When **neither** `squeue` nor `sacct` can answer, the provider raises rather
than submitting, and an unknown job ID reconciles to `SUBMISSION_UNKNOWN`
rather than `FAILED`. Absence of evidence is not evidence the attempt never
ran, and a duplicate submission on a scheduler can mean two multi-hour jobs and
two conflicting artifacts. `sbatch` reporting failure is likewise not treated
as proof the job did not land: a dropped client connection looks identical to a
rejection, so the provider re-queries before concluding.

Also held: placement never enters identity (node lists are operator metadata);
`COMPLETED` without a published `result.json` is a failure, because exit zero
is the scheduler's opinion rather than the worker's; reconciliation is batched
so a shared scheduler is polled once per batch; unmapped states resolve to
`SUBMISSION_UNKNOWN` rather than optimistic success.

Not built: controller integration (the runtime still uses the local subprocess
provider), job-script generation from a `TaskTemplate`, the MPI gang task, all
of 9A-Scale arrays, and the cluster control-state deployment decision that
opens the 9A-Core build list. See `stage9a/README.md`.

## Stage 9B — blocked; its 253/1001 blocker is now typed

**Stage 9B has not started.** Its roadmap gates all fail: no R1 golden fixture
is promoted (`stage0/wrf_interface_audit.md`: *"no run is promoted as a trusted
golden scientific fixture in Stage 0"*), no R2/R2A real-data lane exists, and
no provider is certified — Stage 9A is simulated only. Starting 9B anyway would
mean using WRF as the first runtime correctness test, which the roadmap
explicitly forbids.

What was built is the one prerequisite that needs no cluster, no WRF binary,
and no invented fixture: the placement contract for the recurrent 253/1001
defect that the Stage-0 audit calls *"a blocker for WRF output publication."*

The recorded failure, from
`logs/20260708_005323_cascade_20190904T120000_targets.json`: `arrival_s: array
shape (253, 253) != grid (1001, 1001)`, `dead_letter: true`, after
`elapsed_s: 174849.76` — **48.6 hours of compute lost to a shape comparison**,
across three separate logs.

Two faults, and only one is about shapes.

*Wrong time.* `cube/store.py` compares shapes at write time, which is the last
thing a run does. Placement depends only on the two grid **descriptors**, so it
is decidable before any core-hour is spent. `contracts/placement.py` is pure
and allocates nothing; `cube/preflight.py` runs it over a whole publication
plan up front and reports every blocking variable at once, so a doomed run is
not rediscovered one relaunch at a time.

*Wrong question.* Shape answers placement in neither direction: a 100 m fire
mesh and a 900 m cube differ in shape and place exactly (900/100 = 9), while
two 1001×1001 grids in different CRSs share a shape and do not place at all.
`assess_placement` therefore returns a typed verdict naming the relationship —
`EXACT_MATCH`, `INTEGER_REFINEMENT`, `INTEGER_COARSENING`,
`REQUIRES_DECLARED_RESAMPLING`, `UNDEFINED_NO_GEOREFERENCE`, `CRS_MISMATCH`,
`AXIS_ORDER_MISMATCH`, `ROTATED_OR_SKEWED`, `DEGENERATE_GRID`,
`OUTSIDE_TARGET_EXTENT`. Only the first three are `placeable`;
`REQUIRES_DECLARED_RESAMPLING` deliberately is not, because that data is usable
only through an explicit Stage-4 transformation with its own cost and
assumptions. Treating it as placeable is how a silent regrid gets back in.

The recorded case is `UNDEFINED_NO_GEOREFERENCE`. The old error text naming two
shapes and nothing else *is* the evidence: two shapes were all the failure site
had, because the producer declared no georeference. The real geometry shows why
no reshape would have been correct — `outputs.pixel_m: 900`,
`domain.resolutions_m` ending at a 1000 m nest, `domain.fire_mesh_ratio: 10`
giving 100 m fire cells. 1000 m against 900 m is not an integer ratio either
way, and the 253 km nest covers a fraction of the 900.9 km cube. Code that had
"successfully" reshaped (253,253) into (1001,1001) would have published a
silently wrong answer, which is worse than the dead-letter.

Exactness is enforced, not assumed: an aligned 9× refinement is only exact when
the source spans whole 9-cell blocks. 253 is 28 blocks plus one cell, so the
edge target cell is partly covered and that case is refused as well.

### The producer half, against real WRF output

`models/wrf_georeference.py` derives a **verified** `GridDescriptor` from a
wrfout file's own metadata. It was written against the real WRF-SFIRE output on
disk (`wrf-sfire-stack/WRF-SFIRE/test/em_real/wrfout_d0?_2019-09-04_12:00:00`,
gitignored at 131 MB each), so the following are measurements, not assumptions.

**The blocker is the projection, not the shape.** WRF writes a domain-centred
Lambert Conformal on a 6,370 km sphere with no EPSG code; the analysis cube is
UTM. Real d03 output placed against a UTM cube returns `CRS_MISMATCH`. No
reshape reconciles that, and the old shape check could never have said so.
Declared in WRF's own CRS, the 90 m fire mesh places onto a 900 m cube as an
exact, aligned, whole-blocked 10x `INTEGER_REFINEMENT` — so the geometry was
never the problem; the missing thing was a declared reprojection.

**The origin cannot be computed from `CEN_LAT`/`CEN_LON`** — that puts the
domain 1.7 degrees of latitude out. It is taken from the file's own
`XLONG`/`XLAT` corner and then verified against the whole coordinate field;
a derivation missing WRF's coordinates by over 30 m is refused rather than
returned. Measured agreement is 2.7–3.4 m across all three nests, a fraction of
one 90 m fire cell.

**The fire subgrid is padded and its arrays disagree about where they end.**
`west_east` 213, `west_east_stag` 214, `west_east_subgrid` 2140 = 214 x 10. On
that single allocated array, `TIGN_G`/`LFN` hold data to 2130 (the true
213 x 10 extent), `FXLONG` runs one halo column further to 2131, and
`NFUEL_CAT` fills all 2140 because it is an ingested input rather than fire
state. Three arrays, one subgrid, three answers — which is precisely why an
array's extent cannot establish its grid. Block-reducing the full 2140 folds
ten columns of padded fuel into the result silently.

### What a declared regrid would have to guarantee

`transformations/resampling.py` closes the other half of the sentence
"placement requires a declared transformation" by saying, per variable, what
that transformation must preserve. The declarations come from WRF-SFIRE's own
`Registry/registry.fire`, not from variable names: `TIGN_G` is "ignition time
on ground" (s) so `arrival_s` takes the **earliest** value in a block;
`FIRE_AREA` is "fraction of cell area on fire" (units 1) and `FUEL_FRAC` is
"fuel remaining" (1), so both are areal fractions; `ROS` feeds `ros_max`, which
is already an extremum; `NFUEL_CAT` is categorical.

This does **not** overturn `models/wrf_sfire_adapter.py`, which already uses
`min` for `arrival_s` and `mean` for the fractions, and whose `[:H, :W]`
truncation does correctly drop the padding block. What it adds is the
precondition those choices silently depend on:

> A plain mean of per-cell fractions equals the area-weighted mean **only when
> the cells in a block are equal-area.** That holds for an aligned integer
> refinement inside one CRS, and stops holding across a reprojection, because
> Lambert Conformal preserves angles rather than areas.

An order statistic or a mode is indifferent to cell area; a mean is not. So
under `REQUIRES_DECLARED_RESAMPLING` every target is refused — nothing is
declared yet — but for two *different* reasons, each naming what would settle
it: an area-weighted regrid for `fire_area`/`fuel_consumed`/`fire_intensity`,
an order-preserving regrid for `arrival_s`/`ros_max`, nearest-or-majority for
`nfuel_cat`. Integer *coarsening* is refused outright everywhere: it is
replication, not aggregation, and would claim every 90 m subcell ignited at the
same instant.

### The transformation that now exists

`ValueSemantics`' own docstring named the gap: *"Categorical and extensive
fields are outside the bilinear MVP instead of being silently interpolated."*
The bilinear kinds admit only `SCALAR_CONTINUOUS_INTENSIVE`, so of the six
declared variables exactly one (`fire_intensity`) had any admissible
transformation. An arrival time, an extremum, an areal fraction and a category
label are none of them interpolatable — but all four are exactly *aggregatable*
over a block partition.

`TransformationKind.SPATIAL_BLOCK_AGGREGATE` admits that case, with operation
`transform.spatial_block_aggregate.v1` and binder
`transform.spatial_block_aggregate.bind.v1`. Four `ValueSemantics` members were
added for the classes the docstring excluded: `FIRST_OCCURRENCE_TIME`,
`SCALAR_EXTREMUM`, `AREAL_FRACTION`, `CATEGORICAL_LABEL`.

The value class determines the aggregation as a bijection; the `aggregation`
parameter may only restate what the semantics already imply. That is what stops
a mean being applied to an arrival time by writing a different string in the
parameters. It refuses a CRS change, a partial trailing block, cell sizes that
disagree with the block factors, and an offset lattice.

**Correction to the previous note in this file.** Adding the operation and
binder was predicted to invalidate every existing digest. That was wrong: the
digests are computed from source at call time and no fixture persists one, so
the whole change cost exactly one test edit — the pinned binder tuple in
`tests/test_stage2_capabilities.py`, which is what that test exists to catch.

It did expose a real asymmetry. The binder registry is pinned to an exact
tuple, but the operation registry was only `issubset`-checked, so the new
operation landed without any test noticing. `tests/test_stage4_runtime_ops.py`
now pins the `transform.*` operations exactly as well.

Not done: `SPATIAL_BLOCK_AGGREGATE` is same-CRS by construction, so the
WRF-Lambert→UTM transformation still does not exist and WRF output still cannot
be published to a UTM cube. No area-weighted regrid exists — the plain mean is
exact only because the block cover is same-CRS and equal-area. Nothing is wired
end to end: no capability in the catalog emits one of these transformations,
and the WRF adapter does not produce `field-json-v1`.
`cube/store.py` is not wired to the preflight (its
write-time check remains the last line of defence); the reader is not wired to
`models/wrf_sfire_adapter.py`, which is user-owned and untouched, so nothing in
the running pipeline consumes the declaration yet; and **no Stage-4
reprojection WRF→cube is declared** — knowing the CRSs disagree is not the same
as having a costed transformation between them. Vertical and temporal placement
are out of scope, and Stage 9B remains blocked on R1 and provider
certification. See `stage9b/README.md`.

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

The **entire** repository suite passes in the independently verified host
context:

```text
852 passed, 1 skipped, 7 xfailed, 140 warnings in 88.60 s
```

Progression: Stage 4 `501/1/6`, Stage 5 `574/1/6`, Stage 6 `617/1/7`,
Stage 7 `662/1/7`, Stage 8 `691/1/7`, audit remediation `718/1/7`,
Stage 9A-Core `744/1/7`, placement contract `772/1/7`,
WRF georeference reader `794/1/7`, declared resampling rules `811/1/7`,
block-aggregate transformation `829/1/7`, Cube v3 entries `852/1/7`.

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

The seven xfails are strict and deliberate: the measured Stage-6 latency miss,
two quarantined WRF configuration decisions, and four frozen legacy-runtime
defects (see `stage0/`). A strict xfail that starts passing fails the suite and
forces a decision.

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

The executable Stage-7 proof completed:

- 10,000 partitions admitted, packetised, and committed in 625 packets, with
  peak in-flight exactly at the 512 high watermark
- 10x the partitions cost 1.4x peak memory (saturated comparison)
- a restart resumed at persisted cursor 512 and re-admitted nothing
- a crash injected inside admission left the cursor unchanged, with no
  duplicated and no skipped partitions
- a partial 8-member packet kept its 4 committed members and offered 4 for
  retry (0 when the template is not retry-safe)
- a partially committed collection did **not** satisfy the `ALL` policy

The executable Stage-8 proof completed:

- layer runner 39.0 s, FIFO 39.0 s, **event-driven 30.0 s** on the imbalanced
  graph — a 23.1% improvement that also equals the critical-path lower bound
- the chain ran back-to-back (0→10→20→30) with no wait on unrelated work
- peak usage 2 of 2 cores; never oversubscribed under any policy
- a starving task started at 40.0 s without aging and 10.0 s with it, at
  identical makespan
- a reservation was refused naming `cpu_cores`; affinity routed work to the
  `gdal` site; thread caps matched reserved cores
- estimates went DECLARED → DECLARED → OBSERVED as samples accumulated, and an
  underestimate produced a recorded proposal with the declared envelope intact

Stage 8's demo needs no runtime root: `.venv/bin/python scripts/run_stage8_demo.py`.

Run any of the others only with a fresh node-local temporary directory:

```bash
runtime_root=$(mktemp -d /tmp/nasa-stage3-demo.XXXXXX)
.venv/bin/python scripts/run_stage3_demo.py --runtime-root "$runtime_root"

runtime_root=$(mktemp -d /tmp/nasa-stage4-demo.XXXXXX)
.venv/bin/python scripts/run_stage4_demo.py --runtime-root "$runtime_root"

runtime_root=$(mktemp -d /tmp/nasa-stage5-demo.XXXXXX)
.venv/bin/python scripts/run_stage5_demo.py --runtime-root "$runtime_root"

runtime_root=$(mktemp -d /tmp/nasa-stage6-demo.XXXXXX)
.venv/bin/python scripts/run_stage6_demo.py --runtime-root "$runtime_root"

runtime_root=$(mktemp -d /tmp/nasa-stage7-demo.XXXXXX)
.venv/bin/python scripts/run_stage7_demo.py --runtime-root "$runtime_root"
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
  it reads is checked against per-blob SHA-256 values in a post-fetch receipt,
  but that receipt/content root is not part of the pre-fetch executable task
  identity. Stage 8R-B must add a post-fetch content-binding phase; do not call
  the current operation pure or reproducible from its task parameters alone.
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
- Transformation loss/uncertainty is not a complete typed contract and the
  semantic-rule authority is dropped during ordinary capability lowering. A
  lowered transformation carries `evidence:unknown`, but that does not repair
  the missing authenticated rule.
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
- **Stage 9A is simulated only.** The SLURM provider exists and is tested
  against a fake scheduler; it has never submitted a job. This node is not a
  SLURM submit environment, so real-cluster behaviour -- accounting lag,
  `sacct` purge windows, QOS rejection, federation job-ID suffixes -- is
  entirely unverified.
- **The 253/1001 work diagnoses but does not yet enforce launch safety.** WRF's
  retained output is Lambert while the legacy cube request is UTM. Placement
  and georeference tools exist, but `run_cascade.py` does not call a complete
  launch preflight, the grid contract disagrees with artifact validation, and
  the user-owned adapter is not wired to authoritative publication. Do not say
  it now fails before the run; that is a Stage-8R-C acceptance gate.
- **Stage-6 planning latency misses the Section 9.5 target by roughly 3x** on a
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
- **The Stage-8 policy is wired into a real local-concurrency bridge**, but
  reservations are not durable/reconstructed after controller restart and live
  critical-path rank is computed from only currently ready nodes.
- **Stage-8 makespans come from a discrete-event simulation**, not wall-clock
  runs of real subprocesses. Durations are declared or measured; the simulator
  answers whether a policy orders work better, not how long a real run takes.
  Resource feasibility inside it is real — every start goes through the ledger.
- Real local attempts now emit duration and sampled peak-memory observations;
  this remains single-node conformance evidence rather than portable resource
  enforcement.
- **Stage-5 acquisition throttling and network-site feasibility are not
  integrated** into Stage-8 admission, though the Build section asks for it.
  `ExecutionSite` filters on network classes, but the per-provider quota ledger
  stays separate. GPUs are counted, not pinned to IDs.
- Stage 7 executes an eight-member input-free packet through Stage 1. The
  10,000-member result remains a control-plane lifecycle, and general
  partition-key-to-input binding is absent.
- Packet result replay is not idempotent: repeating one failed result creates
  another attempt, and `NOT_ATTEMPTED` can consume budget. This is a Stage-8R-D
  correctness blocker, not merely an external-provider feature.
- Stage-7 fusion overhead is **not** measured against Section 8.7's 5% target,
  because there is no representative useful work to measure it against yet.
- The legacy eager `list(tile_iter)` in `engine/tiled.py` is untouched. The new
  scalable path is lazy by construction and nothing in Stage 7 routes through
  the frozen Stage-0 baseline.
- The largest partition space exercised is 10^4. Million-partition scale
  remains a Stage-10 question.
- WRF-SFIRE is not a current test workload.

## Stage-4 bounded demo evidence (cross-layer gates remain)

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

## Stage-5 bounded demo evidence (cross-layer gates remain)

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

## Stage-6 candidate evidence (release gate not met)

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

- **Warm end-to-end planning does not meet the Section 9.5 target.** The latest
  frozen representative graph has 126 invocations, 250 arcs, and 6 levels --
  all inside the 1,000/5,000/12 caps -- and records p95
  **15.123187728 s against a 5 s budget**. Commit `6a2f310` enabled guarded
  HiGHS presolve and `201d3d7` re-froze this result; the miss remains about 3x.
  The profile also self-labels `STAGE3_CONFORMANCE_MICROBENCHMARK` and
  `NOT_A_STAGE6_REPRESENTATIVE_SLO`, so it is useful performance evidence but
  cannot by itself satisfy the Stage-6 release gate.

  The assertion lives as a **strict xfail** in `tests/test_stage6_benchmark.py`.
  If solver work ever makes it pass, the suite fails and forces a re-freeze and
  an updated claim. `test_the_frozen_profile_is_a_real_graph_not_a_toy` asserts
  a floor on graph size so the gate cannot be met by shrinking the problem.

One design decision worth not re-litigating: the decision record binds into
`bound_plan_id` through a `PlanSnapshotRef`, not into `CandidateDerivationPlan`
identity. The roadmap says "candidate-plan identity"; the bound plan is what
executes, and reaching it this way avoided changing Stage-2 core identity.

## Stage-7 bounded evidence (partial; Stage-8R replay gate open)

Five bounded properties and one executable slice are demonstrated, but packet
replay prevents an exit claim:

- **One scientific selection is reused by all compatible partitions.** The
  template carries a single resolved invocation taken from a real Stage-3
  resolution; 10,000 distinct logical keys derive from that one invocation.
- **Controller restart resumes the persisted partition cursor.** A fresh store
  object over the same file resumed at index 512 and re-admitted nothing.
- **Cursor advancement and task insertion are atomic.** A crash injected at any
  of three points inside the transaction rolls back both halves. Draining the
  whole space while crashing once per window yields exactly `range(total)` —
  nothing skipped, nothing multiplied.
- **Partial partitions cannot satisfy a complete collection.** `ALL` requires
  every partition committed *and* zero failures; `FRACTION` rounds up.
- **A partially failed packet preserves committed members** on the first
  application and offers retry-safe failures. Reapplying the same result,
  however, creates another attempt; replay idempotency is not established.
- **Memory stays bounded by the window, not the partition count.** Peak
  allocation grows 1.4x for a 10x larger space once the window is saturated.

**The scope boundary:** an eight-member input-free packet executes through
Stage 1 and publishes artifacts. The 10^4 demo measures admission,
packetisation, and collection state, not 10^4 subprocesses; partition-specific
input manifests and the five-percent fusion-overhead measurement are absent.

## Stage-8 bounded evidence (partial; Stage-8R recovery gate open)

The original four policy fixtures pass, but later integration checks prevent an
exit claim:

- **CPU and memory are not oversubscribed.** The ledger refuses on any
  dimension and names the one that blocked; the simulator routes every start
  through it, so `oversubscribed` is measured. Peak usage was 2 of 2 cores.
- **Aging reduces delay on the fixture.** A task nothing depends on started at
  40.0 s without aging and 10.0 s with it, at identical makespan. This is not
  starvation freedom: a rank gap above the capped bonus can still dominate.
- **Event-driven execution improves makespan over the layer runner.** 39.0 s →
  30.0 s on the imbalanced graph, a 23.1% improvement that also equals the
  critical-path lower bound, with the chain running back-to-back.
- **Resource underestimation is never an unrecorded mutation.** It produces an
  identified `DeploymentRevision` carrying declared value, observed peak,
  proposal, and reason; the declared envelope is left untouched.

**Current bridge and blocker:** the policy now drives concurrent local
subprocesses and samples peak memory. The 39/39/30 comparison remains a
simulation, while 6.9/1.9 is the real bridge fixture. Controller restart loses
the in-memory ledger while active attempts remain durable, and the live ranker
sees only ready nodes; Stage 8 therefore has not passed its recovery/priority
exit gate.

## Next: Stage 8R -- Cross-layer Soundness and Integration

Do not start another ordinary feature stage. The detailed release-blocking plan
is in the master roadmap. Its implementation order is:

1. **Freeze the adversarial matrix.** Turn every reproduced counterexample in
   the current-status section into a named regression before changing its
   implementation.
2. **Planning authority.** Make discovery completeness a mandatory certificate,
   carry authenticated transform semantics through selection/compile/commit,
   and constrain Stage-6 alternatives to the exact contested satisfaction arc.
3. **Exact data and grids.** Derive descriptors from source metadata, align
   coverage with executable mosaics, bind exact fetched bytes before payload
   execution, freeze discovery sessions, and establish one coordinate/affine/
   CRS contract across descriptor, runtime, and commit.
4. **Authoritative publication and Cube promotion.** Keep `ArtifactCommitter`
   authoritative and project validated artifacts into Cube v3 entries through
   an idempotent outbox. Migrate read-mostly consumers additively; do not route
   producers directly through unauthenticated `commit_entry`.
5. **Runtime recovery.** Make packet-result replay idempotent, preserve retry
   budget for `NOT_ATTEMPTED`, reconstruct/fence reservations before restart
   dispatch, rank from the full bound graph, and bind distinct partition input
   manifests.
6. **Queued-provider uncertainty.** Treat `squeue` and `sacct` as independent
   tri-state evidence; partial observability never authorizes resubmission.
7. **Reconcile release evidence.** Full suite plus bounded demos, one current
   status table across README/stage docs/roadmap/handoff, and explicit non-runs.

The Stage-6 planning latency miss and absent real evidence pack remain separate
MVP release decisions even after Stage 8R. Stage 8R also does not authorize a
WRF-SFIRE run: Stage 9B still lacks R1/R2 fixtures and a certified provider.
The WRF publication preflight is a lightweight Stage-8R-C conformance test; it
must reject the recorded mismatch without launching the model.

## Later stages, briefly

- **Stage 9A:** the fake-scheduler provider remains a prototype. After Stage
  8R-E, validate it only when an eligible, authorized workload/site requires
  managed batch execution.
- **Stage 9B:** WRF integration through a proven execution provider, using
  reference fixtures; never use WRF as the first runtime correctness test.
  **Blocked** — see the Stage 9B section above. The 253/1001 mismatch is
  diagnosed, not yet enforced as a launch-time system invariant; R1 promotion
  and provider certification are also absent.
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
Read codex_handoff.md and the master roadmap's Stage 8R section completely.
Verify branch v2 at 45aaee1, inspect git status, and preserve every listed
user-owned dirty file. The separate nasa_project_docs repository also has dirty
README/diagram work; preserve it.

The last independently verified full suite result is 1034 passed, 1 skipped,
7 xfailed (Stage-8R working tree, including the Cube projection-authority
fix). Initially run only bounded tests; do not run WRF-SFIRE, MPI, a real
Slurm command, or a real remote provider.

Do not begin Stage 9B or another feature stage. Implement Stage 8R in its stated
order, starting by freezing the adversarial reproductions. A green ordinary
suite is not proof of the missing boundaries. In particular: discovery
completeness must be certificate-bound; transforms need authenticated semantic
rules; fetched bytes need a post-fetch content binding; packet replay and
resource restart must be idempotent; partial Slurm observability must never
resubmit; and Cube v3 must remain a projection behind ArtifactCommitter.

Cube v3 at 45aaee1 is tested but not load-bearing. Do not wire producers
directly to Catalog.commit_entry or claim the new entries replace artifact
publication. Preserve compatibility reads and migrate through an idempotent
projection/outbox.

The Stage-6 latency target still fails and its real evidence pack is absent.
Stage 9B also lacks R1/R2 and a certified provider. WRF is an example model and
must not be used as the integration test.
```
