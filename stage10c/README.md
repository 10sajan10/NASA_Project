# Stage 10C — Runtime Artifact Event Bridge

Status: **bounded local implementation**.

Stage 10C closes the explicit-output-hook gap left by Stage 10B. A Stage-1
task may now return a content-addressed `NativeFilePointer`. The worker and
controller validate the pointer and exact producer-owned file; the ordinary
fenced Stage-1 artifact commit remains the authority for task completion.
Only after that commit does `RuntimeArtifactEventBridge` create the durable
Stage-10B event and automatically re-plan waiting targets.

```text
Stage-1 subprocess
  -> native.file_pointer.v1 verifies producer-owned file
  -> attempt-scoped pointer JSON
  -> Stage-1 validation + fenced authoritative commit
  -> RuntimeArtifactEventBridge
  -> descriptor replay from ScientificArtifactBinding
  -> exact native bytes re-verified
  -> Stage-10B PENDING/REGISTERED/APPLIED event
  -> durable target automatically re-resolved
```

The native file is not copied into Stage-1 object storage. Only the small JSON
pointer/receipt is stored there. Artifact lineage is derived from authoritative
runtime input slots rather than supplied by the output declaration.

## Acceptance

```bash
workspace=$(mktemp -d /tmp/nasa-stage10c.XXXXXX)
.venv/bin/python scripts/run_stage10c_demo.py --workspace "$workspace"
.venv/bin/python -m pytest -q tests/test_stage10c_runtime_artifacts.py
```

Tests prove normal subprocess completion, controller restart after a commit
that had not yet been observed, repeated terminal replay, descriptor/media
tamper refusal, and native-file mutation refusal.

## Boundaries

- Local regular files and the existing same-node Stage-1/SQLite authorities.
- This is a native-file *publication* contract, not a generic command escape;
  only the closed `native.file_pointer.v1` operation can create the fixture.
- An actual scientific adapter still has to produce the file and full
  `ScientificArtifactBinding`; metadata is never inferred from file shape.
- Runtime success and artifact registration converge by replay, not one
  cross-database transaction. A registration failure does not falsify the
  already-committed runtime result; it remains visible as an unapplied event.
- No WRF, MPI, Slurm, network, transformation, reprojection, Cube write, or
  heavy workload was run.
