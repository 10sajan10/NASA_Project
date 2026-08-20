# Codex handoff

Last updated: 2026-08-20

## Repository state

- Code repository: `/uufs/chpc.utah.edu/common/home/parashar-vdc/sajan/NASA_Project`
- Branch: `v2`
- Last pushed implementation commit: `1ca262e` — `Add durable artifact automation and native publication`
- Remote: `git@github.com:sci-ndp/NASA_Project.git`
- Documentation repository: `/uufs/chpc.utah.edu/common/home/parashar-vdc/sajan/nasa_project_docs`
- Documentation branch/commit before this cleanup: `main` at `b898efc`
- The configured documentation remote
  `git@github.com:10sajan10/nasa_project_docs.git` did not exist or was not
  accessible to the authenticated `10sajan10` account, so that commit was local.

The current working trees contain a documentation cleanup on top of those
commits. Do not discard it. It removes duplicate Cube-authority documents and
replaces this handoff and the external roadmap with concise current sources of
truth.

## Current product objective and automation boundary

Given a typed scientific target:

1. search verified native artifacts using complete scientific metadata;
2. inject compatible committed artifacts into recursive workflow discovery;
3. globally select and independently validate one derivation;
4. execute missing producers through the durable Stage-1 runtime; and
5. automatically register a producer-owned native file after its authoritative
   commit, then reconsider durable targets.

The Stage-10D end-to-end pointer path does not copy, transform, reproject,
resample, or ingest native payloads into Cube/Zarr.

Those five steps now form one bounded local path for plans made entirely from
closed supported operations. `TargetExecutionService` accepts a durable target,
not a caller-authored graph or portable manifest; requires a fresh complete and
independently validated result; freezes binding, deployment, compiler, registry,
and discovery inputs; and creates one deterministic Stage-1 run. A
`WorkflowManifest` remains a planning record, not execution authority.

## Current implemented slices

### Stage 10A

`artifacts/records.py`, `registry.py`, `service.py`, and `coordinator.py`
provide content-addressed artifact records, indexed metadata search,
target-time refresh, `ArtifactLeaf` projection, and competition between an
existing artifact and declared producers.

### Stage 10B

`artifacts/manifest.py` and the coordinator persist target requests, artifact
events, replay states, and portable workflow manifests. Registration and target
re-resolution are idempotent across restart. Separate SQLite transactions
converge by replay rather than distributed atomicity.

### Stage 10C

`engine/runtime/native.py` defines a verified `NativeFilePointer`. The closed
runtime operation emits only that pointer, Stage 1 commits it under normal
fencing/validation, and `artifacts/runtime.py` replays the exact scientific
descriptor and runtime lineage before emitting the Stage-10B event. Terminal
run rescan repairs a crash after commit but before event creation.

The repaired compiler boundary now mints process-local compilation authority
only after replaying the exact plan and compiled graph. The Stage-10C native
pointer path is deliberately narrow: a closed identity operation can publish
an exact native pointer, and authoritative Stage-1 input slots are mapped to
their Stage-10 artifact identities for lineage. Stage 10C alone does not
provide the general `ArtifactLeaf` compiler bridge. A restarted observer must
be supplied the exact compiler-issued authority again; it is not reconstructed
from caller-authored graph labels, and absence fails closed.

### Stage 10D

`stage10d/service.py` supplies the durable target-to-execution application
boundary. `composition/compiler.py` and the runtime lower exact committed
registry records to typed registered-input receipts, re-hash bytes at run and
attempt boundaries, and keep Stage-1 and Stage-10 lineage namespaces distinct.
The context and terminal receipt replay every authority coordinate; service
storage is durably bound to one canonical runtime root, target coordinator, and
artifact registry. Terminal success, failure, and cancellation are typed.

`artifacts/query.py` adds immutable snapshot-bound typed metadata queries and
replayable results. `artifacts/publication.py` verifies a complete
adapter-authored native declaration and produces a privately minted proposal,
but that proposal cannot register anything and is not the runtime publication
authority.

The integration fixture uses the closed native-pointer identity producer. It
selects one exact compatible native input in the presence of an incompatible
same-concept record, runs it once, applies the output event, satisfies a waiting
target, queries the exact output, and resumes after restart without a duplicate
attempt. This establishes the application seam; it is not a domain-model run.

## Repairs in the current uncommitted tree

- Contract matching now checks known metric units; objective alternatives bind
  the exact evidence profile and subject; comparability also requires common
  confidence levels and uncertainty methods.
- Generated discovery catalogs are replayed as an exact predecessor chain and
  multiple layers of the same kind map by expansion identity, not by kind.
- WRF georeferencing preserves native `south_north` row direction, replays the
  returned affine against `XLONG`/`XLAT`, and placement refuses axis reversal
  and unauthorized integer coarsening.
- Artifact planning snapshots use stable no-follow reads and full rehashing by
  default. Transient output-event failures have bounded retry; terminal events
  require operator requeue after exact native identity re-verification.
- Partition templates bind exact deployment/resource requests; active packet
  leases cannot be re-offered; the live scheduler checks logical ledgers
  against physical cpuset/memory/GPU capacity and records real local duration
  and peak RSS.
- Cube scientific entry identity is separated from immutable locator receipts,
  so a missing old path can move only after exact byte verification; two valid
  simultaneous paths or changed discovery metadata fail closed.
- Cube projection authority is bound to the complete canonical projection
  (plan, recipe, invocation, descriptor, artifact, and lineage), so verified
  bytes cannot authorize a self-consistent but scientifically relabelled
  projection. `output_arrived()` likewise refuses to report success unless its
  durable event actually reaches `APPLIED`.
- `setup.sh` selects a supported Python 3.12+ interpreter instead of accepting
  the host's obsolete default.
- Stage 10D adds the exact registry-input compiler/runtime bridge,
  target-to-execution application service, adapter proposal surface, and
  snapshot-bound full-metadata query receipts described above.

These are working-tree facts, not a release claim. Preserve unrelated user
edits and deletions and review the diff before any commit.

## Latest bounded evidence

The complete local run after the final Stage-10D provenance and native-pointer
hardening was:

```text
1122 passed, 1 skipped, 7 xfailed in 178.07 s
```

The seven strict expected failures remain visible work: four legacy Stage-0
runtime/tile/fencing defects, two quarantined WRF configuration-policy
decisions, and the Stage-6 planning latency gate (7.06 s p95 against 5 s).
The one skip is conditional environment evidence, not a silently passing
deployment claim.

The final focused Stage-10D application, external-input, publication, and query
suites reported `24 passed in 7.79s`. The complete Stage-10A–D selection reported
`55 passed in 14.84s`; the wider provenance-affected selection reported
`183 passed`. The complete-tree result above was then run on the same final
working tree.

The Stage-10C demo showed Stage-1 `SUCCEEDED`, target cost changing from `1`
to `0` after artifact publication, unchanged native location, two immutable
records with Stage-10-native lineage, and no payload copy, transformation,
reprojection, or Cube write.

No WRF-SFIRE simulation, MPI job, real Slurm job, network provider, or heavy
workload ran. The suite may inspect retained local WRF output fixtures; that is
metadata/georeference evidence, not a model execution or site certification.

## Load-bearing invariants

- `ArtifactDescriptor` states scientific meaning; filenames and array shapes do
  not.
- `ArtifactRecord` binds exact native bytes, descriptor, availability, and
  lineage.
- `ArtifactRegistry` is a searchable index, not the scientific authority.
- Direct matching performs no hidden transformation.
- Discovery completeness is bound to an exact declared finite universe.
- A global optimum claim is forbidden when that universe is incomplete.
- Solver output is independently validated.
- Process exit is not publication; authoritative validation/commit is required.
- Automatic artifact events originate only from committed Stage-1 outputs.
- A durable target launches only after exact resolver, validator, binder,
  deployment, compiler, registry, coordinator, and runtime identities agree.
- Execution graphs admitted through the repaired compiler path carry exact
  compiler-minted authority; a caller-authored identity-valid graph cannot use
  that authority to publish a scientific native artifact.
- Native-file mutation, symlink substitution, descriptor mismatch, and media
  mismatch fail closed.
- Cross-store transitions are replayable and idempotent, not atomic across
  databases.

## Honest boundaries

- Native pointers cover same-node regular files only.
- An adapter must provide the scientific descriptor; meaning is not inferred
  from a file.
- Compilation authority is an in-process, private-minted capability, not a
  cryptographic signature or a cross-service trust protocol.
- A restarted publication observer must retain or deterministically recompile
  the authoritative plan inputs and be supplied the exact authority again.
- Stage-10D execution is execute-once per content-addressed target ID. A changed
  plan is refused instead of silently becoming another run revision.
- `TargetExecutionService.execute()` is an explicit application call. Output
  events re-plan durable targets automatically, but no background dispatcher
  launches every newly planned workflow.
- Deterministic crash/restart replay is tested; clean convergence of
  simultaneous callers is not claimed.
- The execution database is permanently bound to canonical runtime,
  coordinator, and registry paths. Relocation/rebinding is not implemented.
- A runtime-terminal/application-nonterminal recovery must still recompile and
  observe with its frozen selected inputs live. A completed terminal receipt
  can be read after those inputs are archived.
- Arbitrary producer side-effect files are not auto-discovered; publication
  requires a declared closed output through authoritative Stage-1 commit.
- General registered-input delivery supports strict JSON value decoding; the
  no-materialization claim applies to the closed native-pointer delivery path.
- The adapter proposal retains producer family/version and invocation identity
  separately, but `ArtifactRecord` still has one `producer_id`; runtime output
  records currently use the bound invocation key there. A closed
  `scientific_provenance` metadata object makes compiler-bound capability and
  invocation separately queryable, but they are not normalized first-class
  record fields or indexes.
- Legacy scientific bindings without the closed provenance fields fail closed
  and require recompilation; legacy records do not match capability/invocation
  filters unless they contain that exact provenance object.
- Discovery replay currently proves one linear predecessor chain; merging
  independent discovery branches is not implemented.
- Packet resource identity contains an exact deployment binding, but no
  cross-service credential proves who issued that binding from a particular
  frozen deployment snapshot.
- The metadata/event path is not benchmarked at million-artifact scale.
- Cube locator search does not continuously rehash every returned file;
  registration/relocation is verified, while Stage-10 planning snapshots use
  the stronger full-rehash policy.
- The retained `engine/`, `drivers/`, `cube/`, and `agentic/` paths still have
  tests and operational uses, but they are not the Stage-10 authority model.
- WRF-SFIRE has not run through the v2 artifact path.
- Stage 9A is fake-scheduler protocol evidence, not real-site certification.
- Remote acquisition completeness is relative to a durable connector
  transcript, not an open provider's global catalog.

## Next stage

Stage 10E should be **domain producer adoption and durable dispatch**, not a new
data store.

Required next slice:

1. onboard one lightweight real data/model adapter that creates a native output
   through its normal path rather than republishing an input with the identity
   operation;
2. converge the proposal and compiler-authorized publication contracts so there
   is one adapter path and no declaration can bypass Stage-1 commit;
3. promote the closed capability/invocation provenance metadata into distinct
   immutable first-class artifact fields and indexes, with an explicit
   migration;
4. add an idempotent durable dispatcher that can move an approved newly planned
   target to `TargetExecutionService.execute()` without manual glue;
5. expose the typed target and snapshot-query surfaces through a small
   domain-facing API; and
6. prove submit→dispatch→run→publish→query→dependent-target behavior
   across restart, still on a lightweight local fixture.

Do not start with WRF or remote/HPC deployment. First remove the identity
producer and programmatic-call limitations without weakening the Stage-10D
authorities.

## Useful commands

```bash
cd /uufs/chpc.utah.edu/common/home/parashar-vdc/sajan/NASA_Project

.venv/bin/python scripts/run_stage10a_demo.py \
  --workspace "$(mktemp -d /tmp/nasa-stage10a.XXXXXX)"
.venv/bin/python scripts/run_stage10b_demo.py \
  --workspace "$(mktemp -d /tmp/nasa-stage10b.XXXXXX)"
.venv/bin/python scripts/run_stage10c_demo.py \
  --workspace "$(mktemp -d /tmp/nasa-stage10c.XXXXXX)"

.venv/bin/python -m pytest -q \
  tests/test_stage10a_artifacts.py \
  tests/test_stage10b_automation.py \
  tests/test_stage10c_runtime_artifacts.py \
  tests/test_stage10d_application.py \
  tests/test_stage10d_external_bridge.py \
  tests/test_stage10d_artifact_surface.py
```

Use a fresh local `/tmp` runtime root for controller tests; the verified
SQLite/WAL durability boundary is local POSIX, not NFS/Lustre.

## Documentation policy

The maintained design sources are:

- `docs/design_explained.md`
- `docs/scientific_workflow_composition_plan.md`
- `docs/nasa_technical_report.md`
- `docs/agentic_plan.md`
- this handoff for operational continuity

The separate `nasa_project_docs` repository mirrors these sources for
documentation-only distribution. The copies under `docs/` travel with branch
`v2` and should be updated in the same change when either repository changes.

Stage READMEs are bounded implementation evidence. Git history contains the
removed slide decks, generated assets, Cube-authority diagrams, and old roadmap;
they must not be cited as current architecture.
