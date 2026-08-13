# SLURM capability requirements

SLURM is a required deployment capability of the target system. It is not
available to the current process, and Stage 0 does not fake or bypass it.

The provider boundary must keep scientific selection independent from cluster
placement:

```text
Bound scientific task
        |
        v
Deployment/AttemptSpec --submit--> SlurmExecutionProvider --> sbatch
        ^                                  |
        +------ state events <--- reconcile(squeue, sacct)
```

## Core conformance gate

Before any WRF workload depends on it, the provider must demonstrate:

1. nonblocking submission of an immutable `AttemptSpec`;
2. a stable attempt token in queryable SLURM metadata (`--comment` and/or job
   name) before `sbatch` is called;
3. recovery from the crash window after successful `sbatch` but before the job
   ID is persisted by querying both live and accounting records by that token;
4. persisted external job ID and batched reconciliation;
5. cancel, timeout, lost/orphan, preemption, and controller-restart behavior;
6. one MPI gang job with environment, rank, filesystem, and artifact commit
   checks;
7. no claim that this scheduler chooses batch nodes, queue order, or backfill—
   those decisions belong to SLURM;
8. output staging, validation, fencing, and project-owned authoritative commit.

The Stage-0 build-vs-buy decision must be honored: use Dask/Parsl SLURM support
only for semantics it actually passes; implement a direct adapter for missing
identity/reconciliation behavior. Do not build a custom networked worker system.

SLURM arrays are post-MVP. They require stable partition-key/index manifests,
site-limit chunking, batched element reconciliation, and retry of failed
elements without replaying committed siblings.
