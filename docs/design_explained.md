# Artifact-first target-driven workflow composition

## One-sentence design

A user declares the scientific result they need; the system searches verified
native artifacts, discovers admissible producers for anything missing, selects
one globally consistent workflow, and makes newly committed native outputs
searchable without copying or converting their payloads.

## Scope

The current path is intentionally narrow:

- generate a workflow from a typed target;
- index complete scientific metadata so the correct native data can be found;
- automatically register a producer output after an authoritative runtime
  commit;
- automatically reconsider durable targets when a new compatible artifact
  arrives.

This path does **not** reproject, resample, transform, ingest into Cube/Zarr,
or invent an adapter for every file format. Those capabilities may exist in
older or experimental modules, but they are not part of this workflow's
scientific authority.

## The central distinction

```text
artifact = immutable scientific product and verified native-byte identity
registry = searchable index over artifact records
catalog  = declared producer/deployment choices used during planning
```

The registry is not a second copy of the data. An artifact record contains a
verified pointer to bytes that remain at their producer-owned location.

An artifact record binds:

- a full `ArtifactDescriptor`;
- native absolute location, media type, size, and content digest;
- producer, invocation, output-port, and runtime lineage;
- availability and commit state;
- intrinsic uncertainty and evidence references represented explicitly rather
  than inferred from a label.

The registry indexes the descriptor fields needed for search: concept,
representation, schema, units, CRS and spatial support, temporal support and
cadence, vertical support, grid/native resolution, origin, missingness,
uncertainty, evidence, producer, and lineage.

## Target-to-plan flow

```text
typed RequirementUse
        |
        v
verify/refresh matching ArtifactRecords
        |
        v
project committed records to ArtifactLeaf candidates
        |
        v
recursive capability discovery
        |
        v
exact minimum-cost selection over the frozen finite graph
        |
        v
independent selected-plan validation
        |
        v
selected producer invocations + exact native artifact pointers
```

Compatibility is strict and transformation-free. Units, representation, CRS,
space, time, cadence, vertical support, origin, missingness, resolution, and
evidence are checked directly. A mismatch is a structured rejection; it is not
silently repaired by an adapter.

The optimizer handles alternatives, AND dependencies, cardinality, sharing,
non-shareability, multi-output co-production, grounding, cycles, cost, and
static deployment feasibility. Its result is replayed by a separate validator
before it is eligible to bind or execute.

Completeness is always scoped to a declared discovery universe. If discovery
hits a bound, the result is incomplete; it cannot be called a global optimum
over producers that were not enumerated.

## Output-to-registry flow

```text
producer writes a native file
        |
        v
closed native-file pointer operation verifies path/digest/size/media type
        |
        v
Stage-1 fenced attempt commits the small pointer receipt
        |
        v
RuntimeArtifactEventBridge replays descriptor + runtime lineage
        |
        v
content-addressed ArtifactEvent
        |
        v
durable PENDING -> REGISTERED -> APPLIED replay
        |
        v
ArtifactRecord indexed; affected durable targets are resolved again
```

Process exit is not publication. A native output becomes discoverable only
after the runtime's authoritative commit. Restart scans terminal commits, so a
crash between commit and event publication converges without rerunning the
scientific task.

Event processing and artifact registration are idempotent by content identity.
The Stage-1 store and artifact/event SQLite stores are separate authorities;
their convergence is replayable, not a distributed atomic transaction.

## What “automatic” means

The implemented automation covers two moments:

1. **A target is requested.** The registry is refreshed and compatible
   committed artifacts are injected into normal global planning.
2. **A native output is committed.** A durable event registers it and triggers
   re-resolution of durable targets that may use it.

It does not mean that arbitrary files appearing anywhere on disk are trusted.
It does not bypass an adapter's responsibility to declare the scientific
descriptor and native output contract.

## Main implementation map

| Responsibility | Code |
|---|---|
| Scientific descriptors and requirements | `NASA_Project/contracts/` |
| Capability and deployment declarations | `NASA_Project/capabilities/` |
| Recursive discovery and exact selection | `NASA_Project/resolution/` |
| Independent proof/plan validation | `NASA_Project/resolution/validator.py` |
| Artifact records, registry, events, and coordinator | `NASA_Project/artifacts/` |
| Durable runtime commits and native pointers | `NASA_Project/engine/runtime/` |
| Bounded demonstrations | `NASA_Project/stage10a/`, `stage10b/`, `stage10c/` |

The retained `engine/`, `drivers/`, `cube/`, and `agentic/` modules contain
legacy and experimental paths still exercised by tests and operational
fixtures. They are not the authority for the artifact-first design and should
not be presented as such.

## Verified bounded behavior

The focused Stage-10 evidence covers:

- exact artifact identity and descriptor search;
- target-time artifact discovery and producer competition;
- durable target and event replay across restart;
- portable identity-checked workflow manifests;
- Stage-1 subprocess commit to artifact-event publication;
- idempotent terminal replay;
- descriptor/media mismatch and native-file mutation refusal;
- unchanged native location and absence of payload copying, transformation, or
  Cube writes in the Stage-10 demonstrations.

The latest recorded focused run before this documentation cleanup was 193
passing tests across the relevant runtime, contract, resolver, Cube-projection,
and Stage-10 slices. That is bounded regression evidence, not a statement that
every repository test or external deployment passed.

## Explicit non-claims

- No WRF-SFIRE workload has run through this v2 artifact path.
- No real Slurm job or real remote data provider was exercised.
- Native locations are same-node regular files; there is no object-store or
  node-loss recovery contract.
- Event-to-target fanout and search are not demonstrated at million-artifact
  scale.
- Graph admission relies on the existing trusted compiler boundary, not a
  cryptographic signer.
- A producer adapter still has to return a scientifically valid native pointer
  and descriptor; the system cannot infer scientific meaning from a filename.
- Reprojection, resampling, transformation, and Cube/Zarr ingestion are outside
  this path.
