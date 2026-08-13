# Stage 1 — Durable local execution kernel

Status: **implemented and acceptance-tested** on 2026-08-13.

Stage 1 executes a finite, already-bound graph on the current private node. It
does not choose scientific producers or data sources; Stages 2–6 compile those
decisions into this runtime boundary.

No WRF-SFIRE, MPI, SLURM, remote data source, or arbitrary external command was
run while implementing or testing this stage.

## What was built

- Strict, immutable `BoundExecutionGraph`, task, deployment, attempt, fence,
  artifact-recipe, and artifact identities.
- A closed operation registry. Workers cannot import a caller-provided
  `module:function` or execute an arbitrary command.
- One single-writer `WorkflowController` with a SQLite/WAL state/event journal,
  durable task/attempt state transitions, leases, and wake conditions.
- A supervised local-subprocess provider with stable submission tokens,
  PID/process-start identity, process groups, nonblocking reconciliation, and
  cancellation. Losing the controller's in-memory `Popen` does not lose the
  attempt identity.
- Conservative fixed-allocation behavior: discover the current process CPU,
  memory, GPU, and lifetime envelope; preflight every attempt; reject MPI; and
  admit at most one attempt at a time.
- Attempt-private staging and independent controller validation.
- Immutable content-addressed objects/manifests and one fenced, all-output SQL
  commit that unlocks dependents. A worker's exit code or a file's existence is
  never publication.
- Persisted dependency, retry-timer, deadline, and local-reconciliation wake
  conditions.
- Idempotent catalog projection from the authoritative commit outbox.

The fixed vertical slice is intentionally consequence-free:

```text
synthetic.constant(21) -> synthetic.scale(2) -> validated artifact 42
```

## Runtime storage

The control database must live on verified node-local POSIX storage. The NFS
repository is rejected as a live SQLite/WAL location.

- Use `/tmp` only for disposable tests and same-node controller-restart demos.
- Use an explicitly provisioned directory under `/scratch/local` for retained
  development runs on this node.

This stage proves process/controller-crash recovery on the same node. It does
not claim survival after node loss or reboot. Cluster-durable control state is
a later deployment concern.

## Run the fixed fixture

```bash
runtime_root=$(mktemp -d /tmp/nasa-stage1-demo.XXXXXX)
.venv/bin/python scripts/run_stage1_fixture.py \
  --runtime-root "$runtime_root"
```

Expected result: `state` is `SUCCEEDED` and `result` is `42`.

## Acceptance evidence

```bash
.venv/bin/python -m pytest \
  tests/test_stage1_provider.py \
  tests/test_stage1_runtime.py -q
```

The suite covers:

- producer commit before consumer release;
- controller restart while a real subprocess is running;
- no rerun after a crash immediately after authoritative commit;
- retry-timer recovery;
- corrupt/partial staged output remaining invisible;
- identical duplicate completion versus conflicting bytes;
- cancellation and fencing;
- MPI rejection before spawn;
- local-filesystem and single-controller guards;
- component-binding, path-confinement, and committed-input validation.

## Deferred by design

- Scientific requirement/capability/producer contracts and exact producer
  matching: Stage 2.
- Recursive derivation hypergraph and global producer selection: Stage 3.
- Data-source descriptors, coverage semantics, acquisition manifests, and
  connectors: Stages 4–5.
- WRF-SFIRE: example only; never a routine Stage-1 fixture.
- MPI certification and SLURM provider: conditional later provider tracks.
- Parallel resource packing and priority scheduling: Stage 8.
- Partition-scale execution and distributed workers: post-MVP stages.
