# Stage 9A — Conditional SLURM provider (9A-Core)

Status: **implemented against a simulated scheduler; not validated against a
real one.** No job was ever submitted to any cluster during this work, and this
work records no authorized site/allocation on which to certify it. Every
behavioural test drives a fake Slurm service through the provider's command
seam. Installed client binaries, when present, are consulted only for `--help`
compatibility and do not establish scheduler reachability or authorization.
Treat this as a provider ready for site validation, not one that has passed it.

The stage is explicitly conditional in the roadmap: it is required only when a
chosen execution profile cannot run in a verified fixed allocation, or when
queued/multi-node portability is an explicit requirement. Nothing in the
composition stack depends on it.

## The problem this exists to solve

A queued provider's hard part is the window between `sbatch` returning a job ID
and that ID reaching durable storage. A controller that dies inside that window
and then resubmits has silently run the science twice — and on a scheduler,
"twice" can mean two multi-hour jobs and two conflicting artifacts.

So identity does not depend on our bookkeeping surviving. Every job carries its
**attempt token in the SLURM job name**:

```text
nasa-attempt-<attempt_token>
```

and submission always asks the cluster first: *is there already a job for this
attempt?* Only if the answer is a confident "no" does `sbatch` run.

## What the answer can be

| Cluster says | Provider does |
|---|---|
| `squeue` has the job | reuse it, `recovered=true` |
| `squeue` empty, `sacct` has it | reuse it — a finished job leaves the queue but stays in accounting |
| both reachable, neither has it | submit |
| **neither reachable** | raise `SlurmUnavailable` — never submit |

That last row is the point. An unanswerable cluster is a human's problem; a
duplicate submission is a corrupted experiment. The same reasoning governs
reconciliation: a job ID unknown to both `squeue` and `sacct` becomes
`SUBMISSION_UNKNOWN`, not `FAILED`, because absence of evidence is not evidence
the attempt never ran.

`sbatch` reporting failure is also not treated as proof the job did not land —
a dropped client connection looks identical to a rejected submission, so the
provider re-queries before concluding.

## Other properties held

- **Placement never enters identity.** Node lists and partitions are operator
  metadata only; the attempt token derives from nothing SLURM chooses, so the
  same attempt is findable wherever it ran.
- **Completion requires a published result.** SLURM reporting `COMPLETED` with
  no `result.json` is a failure, not a success — exit zero is the scheduler's
  opinion, not the worker's.
- **Reconciliation is batched.** One `squeue` (and at most one `sacct`) covers
  a whole batch rather than one query per attempt; polling a shared scheduler
  per task is how a controller becomes a bad cluster citizen.
- **Orphan detection** lists our jobs by name prefix and reports those no live
  attempt claims. Jobs belonging to anyone else are never reported.
- **Unmapped SLURM states** resolve to `SUBMISSION_UNKNOWN` rather than being
  optimistically treated as success.

## Test coverage

`tests/test_stage9a_slurm.py` exercises the fake scheduler across the
crash window after `sbatch`, recovery of an already-finished job, `sbatch`
failing after the job landed, a wholly unreachable cluster, a partially
reachable one, every terminal SLURM state, completion without a result
manifest, unknown and unmapped states, batching, cancellation, and orphans.

One test reads `--help` from the installed client tools to guard against
inventing flags they do not accept. It is skipped when they are absent and
queues no work when they are present.

## What this does not do

- **It has never talked to a real scheduler.** No real `sbatch` has been run by
  the acceptance path. Every assertion here is about behaviour against a fake,
  so real-cluster quirks —
  accounting lag, `sacct` purge windows, partition policy, QOS rejection,
  federation job-ID suffixes — are unverified.
- **Provider choice is not automatic.** `WorkflowController` can execute and
  commit through a caller-supplied `SlurmProvider`, and that path is exercised
  against the fake scheduler. The target/deployment service does not yet choose
  and configure this provider from an execution profile.
- **No MPI gang task.** The roadmap asks that an MPI invocation be represented
  as one SLURM-managed gang task. `SlurmSubmitOptions` carries `nodes` and
  `ntasks`, but no MPI fixture exists and none has been run.
- **Job scripts are deliberately narrow.** The provider writes a deterministic
  one-node, one-task closed-worker script from the immutable `AttemptSpec`. It
  does not generate arbitrary user scripts, MPI launchers, or site modules.
- **No 9A-Scale array support** — stable index manifests, `MaxArraySize`
  chunking, batched element reconciliation, failed-element-only retry. The
  roadmap puts that after Stage 7 and a measured need.
- **Cluster control-state deployment is unaddressed.** The 9A-Core build list
  opens with selecting a transactional service or a documented
  single-controller journal fallback with explicit failover limits. This
  provider assumes the existing single-controller SQLite store.
