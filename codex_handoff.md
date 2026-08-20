# Codex handoff

Last updated: 2026-08-19

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

The active Stage-10 path does not copy, transform, reproject, resample, or
ingest native payloads into Cube/Zarr.

Steps 1–3 and the post-commit part of step 5 exist as bounded services. Stage 1
also executes already-compiled graphs durably. They are not yet one general
automatic path: a target result does not itself bind, compile, authorize, and
launch all missing producers, and an ordinary selected `ArtifactLeaf` cannot
yet be lowered as a general external input to an arbitrary downstream
invocation. A `WorkflowManifest` is a planning record, not an execution graph.

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
their Stage-10 artifact identities for lineage. This does not provide the
general `ArtifactLeaf` compiler bridge. A restarted observer must be supplied
the exact compiler-issued authority again; it is not reconstructed from
caller-authored graph labels, and absence fails closed.

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

These are working-tree facts, not a release claim. Preserve unrelated user
edits and deletions and review the diff before any commit.

## Latest bounded evidence

The final complete local run on this exact repaired tree was:

```text
1098 passed, 1 skipped, 7 xfailed in 170.72 s
```

The seven strict expected failures remain visible work: four legacy Stage-0
runtime/tile/fencing defects, two quarantined WRF configuration-policy
decisions, and the Stage-6 planning latency gate (7.06 s p95 against 5 s).
The one skip is conditional environment evidence, not a silently passing
deployment claim.

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

Stage 10D is **target-to-execution integration and producer adoption**, not a
new data store. The plan is maintained in the sibling documentation repository.

Required exit path:

1. one ordinary producer is selected from a typed target;
2. selected registry `ArtifactLeaf` values lower to exact, verified Stage-1
   external-input receipts when needed by downstream tasks;
3. an application service binds, compiles, authorizes, and launches the plan;
4. the producer commits a native pointer with complete descriptor and lineage;
5. the artifact event is emitted and applied automatically;
6. every relevant metadata field can find it from a snapshot-bound query;
7. a waiting durable target resolves to it after restart; and
8. its native bytes remain unmodified and unmoved.

Do not start with WRF. Use a bounded non-heavy producer and include incompatible
same-concept artifacts so query correctness is actually tested.

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
  tests/test_stage10c_runtime_artifacts.py
```

Use a fresh local `/tmp` runtime root for controller tests; the verified
SQLite/WAL durability boundary is local POSIX, not NFS/Lustre.

## Documentation policy

The maintained design sources are:

- `nasa_project_docs/design_explained.md`
- `nasa_project_docs/scientific_workflow_composition_plan.md`
- this handoff for operational continuity

Stage READMEs are bounded implementation evidence. Git history contains the
removed slide decks, generated assets, Cube-authority diagrams, and old roadmap;
they must not be cited as current architecture.
