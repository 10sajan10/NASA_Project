# ADR-0001: Stage-1 runtime substrate

- Status: accepted
- Date: 2026-08-12
- Decision: `CUSTOM_LOCAL_ONLY`

## Context

The new composition engine needs durable task/attempt identity, bounded
admission, cancellation, controller restart, opaque external handles, fencing,
validation, and authoritative artifact commit. The existing `Backend`
abstraction supplies only `submit`, `map`, and `shutdown`; it cannot own those
semantics.

This system is being developed on one private CHPC node without a usable SLURM
profile. WRF-SFIRE is a heavy example model and was explicitly excluded from
the spike. The decision must nevertheless preserve a future provider boundary
for external models and cluster schedulers.

## Candidates and method

We ran the same project-owned fixture against:

1. a disposable supervised local-subprocess provider;
2. Dask Distributed 2026.3.0 with two local one-thread workers;
3. Parsl 2026.8.10 with a two-thread local executor.

Dask/Parsl lived in `/tmp/nasa-stage0a-frameworks`; the project environment
remains unchanged. Results are retained in `results/comparison.json`.

The controller, not the framework, assigned task/attempt IDs and fencing tokens,
bounded admission, validated results, and invoked one shared artifact-commit test
double. The fake external handle was serialized but no external scheduler was
contacted.

## Decision

Stage 1 will implement the minimum thin controller plus supervised local
subprocess provider. This is **not** authorization to build a distributed worker
system. It is the smallest implementation that owns the correctness properties
the project cannot delegate.

### Project-owned in Stage 1

- logical task, attempt, lease, and fencing identities;
- single-writer transactional state/event journal;
- dependency readiness, persisted wake conditions, and bounded admission;
- retry classification and total retry budgets;
- process submission intent and durable external handle records;
- validation, attempt-scoped staging, and authoritative artifact commit;
- duplicate/late-result rejection and controller restart reconciliation;
- routing among local and future external providers.

### Thin local provider owns

- launching one immutable `AttemptSpec` in a process group;
- PID plus process-start identity and output/error markers;
- nonblocking state reconciliation;
- timeout and process-group cancellation;
- returning staged result references—never committing scientific artifacts.

### Dask boundary

Dask may later be added as a `FRAMEWORK_EXECUTOR` for stateless partition work.
It does not own scientific task identity, retries, controller state, external
subprocesses, validation, or commit. The spike showed that cancelling the Dask
future did not terminate synchronous worker code and a reconstructed local view
could only report the attempt as `LOST`.

### Parsl boundary

Parsl is not selected. Its running local task could not be cancelled, and its
future could not be reconstructed. It can be reconsidered only if a measured
HPC workload makes its executor/provider integrations valuable while the
project controller retains all authoritative semantics.

### WRF-SFIRE and SLURM

Neither is part of Stage 1 execution on this node. WRF-SFIRE remains an external
example component and must not be launched by routine tests. A future SLURM
provider must implement the frozen `ExecutionProvider` behavior independently;
SLURM absence here does not leak into scientific producer contracts.

## Consequences

Positive:

- Stage 1 begins without adding a large runtime dependency.
- Local external processes can be stopped and reconciled using durable OS-level
  evidence rather than an in-memory `Future`.
- Scientific identity and artifact correctness stay framework-independent.
- Dask remains available later without constraining the controller design.

Costs and risks:

- The project must implement a small state machine, journal, process supervisor,
  and artifact committer.
- The Stage-0A provider is only a conformance prototype. It has no transactional
  submission intent, leases, durable database, atomic artifact publication, or
  node-loss recovery. Those are Stage-1 gates, not inherited claims.
- Same-node PID reconciliation cannot survive loss of this private node.
- MPI and SLURM behavior remain `NOT_TESTED` and may not be inferred from the
  fake handle.

## Rejected alternatives

- `FRAMEWORK_PROVIDER` with Dask: rejected because local scheduler state is not
  durable and running synchronous work was not terminated by future
  cancellation.
- `FRAMEWORK_PROVIDER` with Parsl: rejected because running-task cancellation
  and future reconstruction failed.
- Custom distributed workers: rejected because no current workload justifies
  building transport, membership, or a cluster scheduler.
