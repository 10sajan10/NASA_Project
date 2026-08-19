# Stage 10B — Durable Artifact Events and Target Planning

Status: **bounded local implementation**.

Stage 10B turns Stage 10A's request-time artifact lookup into a durable loop:

```text
typed target request -> persisted request -> selected producer workflow
                                            |
native output file -> PENDING output event  |
       -> verified ArtifactRecord           |
       -> REGISTERED                        |
       -> every durable target re-resolves -+
       -> APPLIED
```

The two SQLite authorities intentionally do not pretend to be one distributed
transaction. Instead, each transition is idempotent. A crash after publishing
the artifact but before updating the event replays the same content-addressed
record. A crash after `REGISTERED` but before target refresh re-runs target
planning. Only after every target is refreshed does the event become `APPLIED`.

`WorkflowManifest` is standalone canonical JSON containing the complete root
`RequirementUse` values, selected bound invocations, exact satisfaction
bindings, selected native artifact records, discovery certificate/universe,
plan identity, and objective cost. It is a planning manifest, not permission
to execute and not a copy of the data.

Artifact metadata now also has normalized SQLite indexes for concept, schema,
representation, units, spatial/time/vertical summaries, grid CRS, native
resolution, origin, component names, producer/version, and output port.
Scientific acceptance remains `direct_match`; the index only narrows search.
Snapshot verification retains a durable stat fingerprint and content digest.
The default re-hashes when device/inode/size/mtime changes; callers may request
`ALWAYS_REHASH` for stronger local verification.

## Acceptance

```bash
workspace=$(mktemp -d /tmp/nasa-stage10b.XXXXXX)
.venv/bin/python scripts/run_stage10b_demo.py --workspace "$workspace"
.venv/bin/python -m pytest -q tests/test_stage10b_automation.py
```

The demo deliberately stops after artifact registration while the event still
says `PENDING`, reconstructs every service from disk, replays the event once,
and proves that the same durable target changes from a cost-3 producer plan to
a cost-0 native artifact pointer.

## Boundaries

- Same-node SQLite and stable regular files only; no remote object inventory,
  signatures, Byzantine writers, or cross-node transaction claim.
- Output producers must emit a full typed `DatasetRef`/`ArtifactDescriptor`.
  Scientific metadata is never inferred from a filename or array extent.
- [Stage 10C](../stage10c/) now connects an authoritative Stage-1 native-file
  pointer commit to this outbox. Scientific adapters still must create their
  native file and provide the complete descriptor.
- Target refresh currently scans all durable targets after each output event.
  Metadata lookup is indexed, but event-to-target fanout is a bounded MVP.
- A stat fingerprint can miss hostile same-metadata mutation. Use
  `ALWAYS_REHASH` where that threat is in scope.
- No payload is copied, transformed, reprojected, resampled, or written to
  Cube. No WRF, MPI, Slurm, network provider, or heavy workload was run.
