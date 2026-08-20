# Stage 10D — Target-to-Execution Integration

Status: **bounded local implementation**.

Stage 10D joins the previously separate target-planning and artifact-feedback
paths. `TargetExecutionService` accepts an existing durable `TargetRequest`,
requires a fresh `READY` result that is independently validated and globally
optimal over its complete declared universe, binds deployment, recompiles the
exact plan, launches one deterministic Stage-1 run, and records an
identity-checked terminal receipt.

```text
durable TargetRequest
  -> fresh exact resolve + WorkflowManifest equality
  -> bound scientific and deployment plans
  -> compiler replay + private CompilationAuthority
  -> deterministic Stage-1 run
  -> fenced native-pointer commit
  -> automatic Stage-10 event REGISTERED/APPLIED
  -> waiting durable targets re-resolve
```

The execution context persists the target and manifest; artifact, capability,
deployment, evidence, discovery, plan, compiler, graph, coordinator, registry,
and runtime identities needed to reconstruct authority after process restart.
A portable `WorkflowManifest` remains an inspection record and is explicitly
refused as launch authority.

## Exact external artifacts

The compiler can lower a selected committed `ArtifactLeaf` into a
`RegisteredArtifactInputBinding`. It requires one exact record from the frozen
registry snapshot and replays descriptor, manifest-root, content, availability,
and bound-plan identities. The runtime re-verifies the same no-symlink regular
file at run creation and again before an attempt.

Two closed delivery modes exist:

- `JSON_VALUE` verifies and decodes `application/json` for an operation that
  consumes a value.
- `NATIVE_FILE_POINTER` delivers only a verified pointer envelope and is
  restricted to the reviewed native-pointer identity operation. It does not
  materialize the native payload.

Every runtime input records whether its lineage came from a Stage-1 commit or a
Stage-10 registered artifact, so their identifiers cannot be confused.

## Publication proposals and metadata queries

`NativePublicationDeclaration` is an adapter-facing description of a complete
native output. It separates producer/version metadata from the exact invocation
identity, requires complete direct input-port lineage (or an explicit source
role), and verifies stable canonical bytes without following symlinks.
`prepare_native_publication()` returns a privately minted, content-verified
proposal. The proposal is **not publication authority**, does not write the
registry, and is not yet the contract consumed by the runtime bridge.

`ArtifactSnapshotQuery` provides conjunctive typed filters over an immutable
`ArtifactRegistrySnapshot`. It covers content and descriptor identities,
concept, representation, schema, units, space/CRS/bounds, grid and native
resolution, time/cadence, vertical support, origin, missingness, uncertainty,
components, ensemble, evidence, producer/version/port, media type, native
location, metadata, availability, exact runtime invocation/capability
provenance, and exact direct-lineage pairs. Results bind the query identity,
snapshot identity, ordered records, content identities, and availability and
must replay from the same query and snapshot. Querying never opens or
transforms payload bytes.

## Acceptance

```bash
.venv/bin/python -m pytest -q \
  tests/test_stage10d_application.py \
  tests/test_stage10d_external_bridge.py \
  tests/test_stage10d_artifact_surface.py
```

The final focused local run reported `24 passed in 7.79s`. The
end-to-end fixture includes a compatible native JSON artifact and an
incompatible same-concept artifact. A durable target is forced through the
closed pointer-identity producer, receives the exact compatible artifact,
compiles and succeeds, publishes one derived record with exact input lineage,
and automatically makes a previously unsatisfiable waiting target resolve to
that output. Reopening the service reconstructs authority and returns the same
receipt without another run or attempt. A separate recovery test restarts
after Stage-1 commit but before observation and applies the event without
scientific re-execution.

The native data file stays at its original canonical path with the same digest;
the pointer path neither copies nor transforms its raw bytes.

## Boundaries

- This is same-node local POSIX regular-file and SQLite/WAL evidence. It is not
  cross-node, object-store, distributed-transaction, or node-loss durability.
- Execution is allowed only for operations already supported by the closed
  compiler/runtime registries. A `READY` manifest does not make arbitrary code
  executable.
- A target ID has execute-once semantics in this service: one frozen context,
  deterministic run ID, and one terminal receipt. A changed planning context
  is refused rather than launched as a silent revision.
- The end-to-end producer is the lightweight closed native-pointer identity
  operation. It proves the integration boundary, not a domain model or remote
  acquisition adapter.
- The publication-proposal API and authoritative runtime publication path are
  deliberately separate. `ArtifactRecord` currently has one `producer_id`, so
  the runtime bridge uses the bound invocation key there and stores the exact
  capability, version, bound plan, invocation, and evidence coordinate in a
  closed `scientific_provenance` metadata object. Invocation and capability are
  queryable from that object, but they are not normalized first-class record
  columns. The adapter proposal independently retains producer family/version
  and invocation identity.
- A newly submitted target runs only when the application calls
  `TargetExecutionService.execute()`. Output events automatically re-plan
  durable targets, but there is no background policy that launches every newly
  planned target.
- Clean simultaneous-caller convergence is not claimed. The service provides a
  durable deterministic execute-once result across process crash/restart, not a
  distributed admission protocol.
- Persisted execution authorities are bound to canonical runtime, coordinator,
  and registry paths and cannot be relocated or rebound. Recovery after a
  runtime commit but before application observation still needs the frozen
  selected inputs to be live; a completed terminal receipt remains readable
  after those inputs are archived.
- Arbitrary files created as producer side effects are not discovered. A closed
  operation must return its declared native pointer through the authoritative
  Stage-1 output path.
- Legacy persisted scientific bindings without the required closed provenance
  fields fail closed and must be recompiled. Legacy artifact records do not
  match invocation/capability query filters unless they contain the exact
  provenance structure.
- No WRF-SFIRE, reprojection, resampling, unit conversion, Cube/Zarr write,
  remote provider, MPI, Slurm, or heavy workload ran in this stage.
