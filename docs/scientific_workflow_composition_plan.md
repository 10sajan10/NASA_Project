# Current scientific workflow composition plan

Last updated: 2026-08-20

## 1. Objective

Build the smallest honest system that can:

1. accept a typed scientific target;
2. find compatible native artifacts from complete searchable metadata;
3. generate a globally consistent producer workflow for anything missing;
4. automatically register a producer's native output after authoritative
   runtime commit; and
5. re-plan durable targets when that output becomes available.

The active path does not require payload conversion, reprojection, resampling,
Cube/Zarr storage, or a universal file adapter.

Items 1–5 now form one bounded local path for plans made entirely from closed
supported operations. The Stage-10D application service accepts a durable
target, replays a complete validated planning context, lowers exact committed
registry artifacts into Stage-1 inputs, launches one deterministic run, and
routes committed native-pointer outputs back through registration and target
reconsideration. This is an explicit service call, not a background dispatcher
or evidence that a real domain model has been adopted.

## 2. Authority model

The following objects have distinct jobs:

| Object | Authority |
|---|---|
| `ArtifactDescriptor` | what a scientific product means |
| `ArtifactRecord` | exact native bytes, descriptor, availability, and lineage |
| `ArtifactRegistry` | searchable index of artifact records |
| `CapabilityCatalog` | declared ways to produce missing requirements |
| discovery certificate/universe | what finite producer universe was searched |
| selected and bound plans | chosen scientific derivation and deployment |
| `CompilationAuthority` | exact compiler-replayed plan-to-Stage-1 lowering for the current process |
| `TargetExecutionContext` / receipt | exact durable application inputs and one terminal run outcome |
| Stage-1 artifact commit | authoritative completion of an executable output |
| `ArtifactEvent` | replayable bridge from committed output to registration |
| snapshot query receipt | exact metadata search result over one immutable registry snapshot |

No catalog row, process exit, filename, trust label, or array shape is allowed
to stand in for these authorities.

An adapter-facing `NativePublicationProposal` is intentionally absent from the
authority table. It proves that one complete declaration matched stable native
bytes; it cannot register an artifact or substitute for compiler authority and
Stage-1 commit.

## 3. Current implemented state

### Stage 10A — artifact registry and target-time discovery

Implemented on the bounded local-file path:

- verify a native `DatasetRef` and create an immutable `ArtifactRecord`;
- index full descriptor and lineage metadata;
- refresh records when a target is requested;
- project compatible committed records to ordinary `ArtifactLeaf` candidates;
- let an existing artifact compete with declared producers in global planning;
- return exact native pointers without copying or converting payloads.

### Stage 10B — durable targets and artifact events

Implemented on same-node SQLite/local files:

- content-addressed durable target requests;
- content-addressed artifact-output events;
- crash-safe `PENDING -> REGISTERED -> APPLIED` replay;
- automatic target re-resolution after registration;
- portable, identity-checked workflow manifests;
- normalized metadata indexes; and
- full file re-verification by default for planning snapshots, with
  stat-fingerprint caching available only as an explicit weaker opt-in.

The event and registry transactions converge by idempotent replay; they are not
a distributed transaction.

### Stage 10C — Stage-1 commit bridge

Implemented on the bounded local subprocess path:

- closed native-file pointer operation;
- exact path, digest, size, media-type, and no-symlink validation;
- ordinary fenced Stage-1 output commit;
- replay of the exact scientific output descriptor and runtime input lineage;
- automatic artifact-event emission only after authoritative commit;
- terminal-run rescan after restart without scientific re-execution when the
  exact compiler-issued authority is supplied;
- compiler-minted authority after exact plan/graph replay; and
- a durable mapping from authoritative Stage-1 input slots to Stage-10 artifact
  identities for the narrow native-pointer path.

The native payload remains at the producer-owned path.

### Stage 10D — target-to-execution integration

Implemented on the bounded local closed-operation path:

- accept only a durable target ID or the exact persisted `TargetRequest`;
- require a fresh `READY`, independently validated, globally optimal result in
  a complete declared discovery universe;
- persist the exact manifest, snapshots, invocations, bound/deployment plans,
  compiled graph, compiler identities, and canonical service paths;
- lower a uniquely selected committed `ArtifactRecord` into a typed Stage-1
  registered-input receipt and re-verify its native bytes at run creation and
  attempt start;
- reconstruct private compiler authority after process restart and resume the
  deterministic run without creating another attempt;
- route authoritative native-pointer output commits through the Stage-10C
  observer automatically;
- expose strict typed metadata queries and replayable results bound to one
  immutable registry snapshot; and
- retain a verified adapter publication proposal as a non-authoritative API.

The end-to-end fixture uses the closed native-pointer identity producer, not a
domain model. It delivers the pointer without materializing, copying, or
transforming the raw native payload. General JSON-value delivery deliberately
verifies and decodes JSON for operations that consume values.

## 4. Current acceptance evidence

The complete local run after the final Stage-10D provenance and native-pointer
hardening reported `1122 passed, 1 skipped, 7 xfailed` in 178.07 seconds. The
focused Stage-10D application, external-input, publication, and query suites
reported `24 passed in 7.79s`; the complete Stage-10A–D selection reported
`55 passed in 14.84s`. Expected failures remain explicit backlog, not completed
gates.

The Stage-10D end-to-end fixture establishes that:

- a durable target selects and launches the closed pointer-identity invocation;
- the invocation receives one exact compatible committed artifact while an
  incompatible same-concept artifact is present;
- Stage-1 reaches `SUCCEEDED` with one deterministic run and attempt;
- the committed output event reaches `APPLIED` and a waiting target resolves to
  the exact new derived record;
- a snapshot receipt can find that output through every relevant non-null typed
  field and exact direct lineage;
- restart after runtime commit reconstructs compiler authority, observes the
  output, and does not re-execute the scientific task; and
- the native path, digest, and bytes are unchanged and no transformation,
  reprojection, or Cube write occurs.

Additional tests refuse a stale manifest, a portable manifest used as launch
authority, ambiguous or changed registry inputs, wrong receipt namespaces,
symlinks, service reuse with another runtime/coordinator/registry, and
self-consistent receipt relabelling. Cancellation is a typed terminal result.

Operationally, launch still requires an explicit `execute()` call; clean
simultaneous-caller convergence is not claimed; persisted service authorities
cannot be relocated; and recovery between runtime terminal state and
application observation requires the frozen selected inputs to remain live.
Arbitrary producer side-effect files are not discovered as outputs.

These are bounded local conformance results. They are not real WRF, real Slurm,
remote-provider, cluster-recovery, or production-scale evidence.

## 5. Next stage — 10E domain producer and durable dispatch

The next practical gap is replacing the identity integration fixture and manual
application call without weakening Stage 10D. This is still not a data-store
stage.

### Build

1. Normalize immutable provenance into distinct producer-family/version and
   exact bound-invocation fields across `ArtifactRecord`, registry indexes,
   query receipts, events, and migrations. Today the adapter proposal separates
   them, while runtime records use their single `producer_id` for the bound
   invocation key and expose the capability/invocation coordinate from closed
   `scientific_provenance` metadata.
2. Converge the adapter proposal and compiler-authorized output path into one
   contract. A declaration may describe and verify bytes, but only the exact
   compiled invocation and authoritative Stage-1 commit may publish them.
3. Onboard one lightweight real data/model adapter that creates a native output
   through its normal execution path. It must consume any selected registry
   inputs through Stage-10D receipts and must not merely republish the input
   through the pointer-identity operation.
4. Add a durable, idempotent dispatch state machine for approved targets. It
   should react to submission and re-planning, call
   `TargetExecutionService.execute()` at most once for an exact target context,
   claim dispatch transactionally under simultaneous callers, and retain
   explicit policy states for targets that require human approval or have no
   executable workflow.
5. Expose a small domain-facing API for typed target submission, status,
   workflow inspection, approval/dispatch, snapshot-bound artifact query, and
   terminal output receipt retrieval.
6. Retain the execute-once service invariant; introduce an explicit target
   revision identity rather than silently reusing a target ID if repeat runs are
   needed.

### Exit gate

Stage 10E is complete only when one lightweight non-identity producer:

1. creates new native bytes and a complete descriptor through its normal
   adapter path;
2. is discovered, selected, approved, dispatched, compiled, and launched from a
   durable target without a test calling the service directly;
3. receives exact native registry inputs and publishes only after Stage-1
   commit;
4. records both producer-family and exact invocation provenance as separately
   searchable fields;
5. becomes discoverable through the domain-facing snapshot query API;
6. causes one dependent durable target to advance automatically; and
7. replays correctly across crashes before dispatch, after run creation, after
   commit, and after registry registration without duplicate execution or
   publication, including concurrent attempts to dispatch the same target.

### Non-goals

- WRF-SFIRE execution;
- reprojection, resampling, or unit conversion;
- Cube/Zarr ingestion;
- remote provider pagination or global-provider completeness;
- Slurm deployment;
- million-artifact scale.

## 6. Later work, only when demanded by evidence

### Heterogeneous agent research

The research roadmap in [agentic_plan.md](agentic_plan.md) places LLM,
retrieval, symbolic,
supervised-ML, GNN, bandit, Bayesian-optimization, RL/MARL, critic, surrogate,
and human agents around the verified kernel. It begins only with typed
proposal/advisory roles; compatibility, completeness, selection, authorization,
execution state, and artifact commit remain deterministic authorities.

### Real model adoption

Stage 10E onboards the first non-identity adapter. After that, migrate model
adapters one at a time; each must declare its native output descriptor and
lineage. WRF-SFIRE remains blocked on its own scientific
reference/provider/georeference gates and must not be used as the first
integration test.

### Distributed catalog/event service

Add only when same-node SQLite is inadequate. Requirements include a durable
transactional service, immutable snapshot identities, event-to-target fanout,
retention, authorization, and node-loss recovery.

### Transformations and gridded projections

Keep optional and explicit. If a consumer truly needs a transformed product,
it is a new derived artifact with a declared transformation invocation,
uncertainty/loss policy, exact output descriptor, and separate native bytes.
It must never overwrite or relabel the source artifact.

### Real deployment provider

Certify only when a target requires queued or multi-node execution. Fake-Slurm
tests are protocol conformance, not site certification.

## 7. Permanent correctness rules

1. Direct matching inserts no hidden transformation.
2. Unknown evidence, missingness, uncertainty, or resolution stays unknown.
3. A finite discovery result is globally optimal only over its exact complete
   declared universe.
4. Solver output is independently validated.
5. Process success is not artifact success; validation and authoritative commit
   are mandatory.
6. Artifact identity includes exact native bytes and scientific descriptor.
7. A registry is an index, not the scientific authority.
8. Automatic registration begins only from an authoritative runtime commit.
9. Every claim names its bounded test/deployment scope.
10. Historical code paths may remain for regression or operational reasons but
    must not be documented as the current architecture.

## 8. Maintained documentation policy

This plan and `design_explained.md` are the maintained design sources. Stage
READMEs in the code repository are implementation evidence for their bounded
slices, not a competing current roadmap. Removed slide decks, Cube-authority
diagrams, generated office files, and old plans remain recoverable from Git
history but should not be cited as current system behavior.
