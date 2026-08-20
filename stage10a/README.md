# Stage 10A — Automatic Artifact Registration and Discovery

Status: **bounded local implementation**.

Stage 10A makes the artifact, rather than a Cube row, the authoritative object
that joins workflow planning to native scientific files.  Payload bytes remain
where their producer wrote them.  The system stores an immutable scientific
record and a verified pointer; it does not copy, reproject, resample, re-encode,
or write the payload into Zarr.

## Automatic loop

```text
typed RequirementUse target
        |
        v
ArtifactWorkflowResolver snapshots and verifies ArtifactRegistry
        |
        +--> compatible ArtifactRecord -> committed ArtifactLeaf candidate
        |
        +--> missing value -> ordinary declared producer/model candidate
        |
        v
validated globally selected workflow + exact selected native pointers

producer/runtime emits DatasetRef(path, ArtifactDescriptor)
        |
        v
output_arrived() verifies bytes and registers immutable ArtifactRecord
        |
        v
the next target request sees it automatically
```

The registry record binds the full `ArtifactDescriptor`, exact canonical path,
media type, content digest, byte size, producer/version/output port, input
artifact lineage, evidence-profile reference, and immutable supplemental
metadata.  Every planning request rechecks file existence, size, and digest.
Changed or missing bytes become `UNAVAILABLE`; they are not selected from a
stale database row.

`ArtifactRegistry.search(requirement)` uses the same pure `direct_match()`
contract as recursive workflow discovery.  It distinguishes exact compatible
records, compatible records carrying an explicitly allowed caveat,
incompatible records with structured proof reasons, and unavailable records.

`ArtifactWorkflowResolver` is direct-only by default.  It refuses catalogs
containing transformation or acquisition-materialization authority.  Models
and direct data producers remain ordinary alternatives; the service adds no
implicit transformation.

## Runnable acceptance evidence

```bash
workspace=$(mktemp -d /tmp/nasa-stage10a.XXXXXX)
.venv/bin/python scripts/run_stage10a_demo.py --workspace "$workspace"

.venv/bin/python -m pytest -q tests/test_stage10a_artifacts.py
```

The focused suite proves:

- the first target request chooses the ordinary producer derivation;
- a native file output arrives through `DatasetRef` and is registered without
  an array/Zarr write;
- the next identical target request automatically selects that artifact;
- mutation between requests makes it unavailable and restores the producer
  derivation;
- strict direct matching excludes wrong units;
- registration and restart are idempotent;
- co-produced output records publish in one transaction or not at all;
- record/snapshot identity tampering is rejected;
- missing scientific metadata fails before either registry is updated; and
- the legacy dataset index cannot hash one path while cataloguing another.

## Boundaries and nonclaims

- This is a trusted same-node local-file registry backed by SQLite.  It is not
  a cryptographic attestation service or remote object-store inventory.
- Stage 10B adds stat-fingerprint verification receipts and normalized metadata
  indexes. `ALWAYS_REHASH` retains the stronger bounded local mode; neither mode
  is a million-artifact or hostile-writer design.
- A producer must provide a complete typed `ArtifactDescriptor`; the registry
  never infers scientific metadata from a filename or array shape.
- The catalog is a searchable read model.  `ArtifactRecord` and its verified
  manifest-backed `ArtifactLeaf` remain authoritative.
- Stage-1 native binary-file output is not generalized here. The automatic
  output hook accepts typed `DatasetRef` events directly; Stage 10C later adds
  one narrow compiler-authorized native-pointer publication path. That path is
  not a general `ArtifactLeaf` external-input bridge, and the Stage-10A service
  and demo do not require Cube.
- No WRF, MPI, Slurm, network provider, reprojection, transformation, or heavy
  workload was run for this stage.

The durable continuation is [Stage 10B](../stage10b/): persisted targets,
crash-replayable output events, automatic re-resolution, and portable workflow
manifests.
