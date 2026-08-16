# Stage 8 — Resource-aware local scheduling

Status: **policy plus a working runtime bridge** — implemented and
acceptance-tested on 2026-08-15, revised 2026-08-16 after an external audit.
The policy now drives the durable Stage-1 controller: concurrent attempts run
under a reservation ledger, measured on real subprocesses. The three-policy
makespan comparison below is still a **simulation**. No WRF-SFIRE, MPI, Slurm,
or real remote provider was run.

**Audit corrections (2026-08-16).** Logical sites could share physical hardware
undetected, so two eight-core sites on one eight-core host accepted two
eight-core tasks and reported no oversubscription; sites may now declare a host
and the ledger enforces its budget too. Best-fit weighed only CPU and memory, so
CPU-only work could consume the single GPU node; scarce GPU and scratch capacity
now rank first. Non-finite durations were accepted everywhere because NaN
compares false against every bound. Resource observations are now typed: an
attempt killed by a limit is `RESOURCE_EXHAUSTED` and its peak is a censored
lower bound rather than a measurement, and such attempts drive revisions instead
of being discarded with ordinary failures. Revisions cover all four dimensions.
The "low-priority work cannot starve" claim was too strong for a capped aging
bonus and is now stated as bounded delay.

**The runtime bridge (2026-08-16).** `max_inflight` above 1 is allowed against a
`ReservationLedger`; the controller orders ready work with this policy, reserves
before dispatch, releases when a task leaves an active state, and emits
observations from real attempts. Eight 0.4s tasks take about 6.9s serially and
about 1.9s four-wide, each committing exactly one attempt.

Stage 8 is where the runtime stops running work in the order it happened to
arrive.

## The problem, in one graph

Six short unrelated tasks and one three-link chain, on a two-core node. The
short tasks sort *first* by task ID, so a blind policy grabs both cores with
work that unblocks nothing while the chain — the thing that decides when the
run can possibly end — waits its turn.

| Policy | Makespan |
|---|---|
| `LAYERED` (the Stage-1 shape) | 39.0 s |
| `FIFO` (event-driven but blind) | 39.0 s |
| **`EVENT_DRIVEN`** | **30.0 s** |

30.0 s is the critical-path lower bound, so on this graph the policy is not
merely better — no ordering could do better. That is a 23% improvement over the
layer runner, and the chain runs back-to-back with no gaps:

```text
z-chain-1   0.0 -> 10.0
z-chain-2  10.0 -> 20.0     starts the instant its dependency commits
z-chain-3  20.0 -> 30.0
```

which is exactly the roadmap's demonstration: *"a downstream task starts
immediately after its last dependency and does not wait for unrelated work."*

## Two forces in tension, on purpose

**Critical path** ranks each task by the longest remaining chain below it,
computed once over the whole graph in reverse topological order (O(V+E), not a
search per task). Running the task that unblocks the longest tail first is what
shortens makespan, and FIFO cannot see it.

**Aging** raises the priority of work that has been waiting. Pure critical-path
ranking starves a short off-path branch forever, and a scheduler that never
runs your small job is broken regardless of its makespan. The starvation
fixture makes this concrete — one tiny task nothing depends on, behind a long
chain:

| | starts at |
|---|---|
| without aging | 40.0 s |
| with aging | 10.0 s |

and the makespan is identical (41.0 s) either way, so on this graph
anti-starvation is free.

Aging is **capped** by `starvation_ceiling_s`. Without a ceiling a
long-waiting trivial task eventually outranks everything and the schedule
degenerates into FIFO with extra steps.

## Reservations refuse; they do not warn

`ReservationLedger` tracks CPU, memory, GPU, and scratch per site and refuses
any reservation that would exceed capacity on *any* dimension, naming the
dimension that blocked:

```text
site 'a' cannot grant 2 cpu_cores; 1 available
```

A refused reservation leaves nothing behind, and `invariant_holds()` asserts
that live usage is within declared capacity at every site. The simulator routes
every start through this same ledger, so `oversubscribed` is a measured
property of the run rather than an assumption.

Placement is **best fit** — the feasible site leaving least slack — so a large
task is not blocked by small ones scattered everywhere. Ties break on
environment affinity, then site ID for determinism. A task declaring a needed
environment can only land where that environment exists.

## Capacity is what we were granted, not what the machine has

`detect_capacity()` prefers the cgroup CPU quota and the process affinity mask
over `os.cpu_count()`. On this development node those differ sharply, and
scheduling against the machine total is how a well-behaved job becomes a bad
neighbour on shared hardware.

`thread_environment()` caps `OMP_NUM_THREADS` and its four siblings to the
cores actually reserved. Without it a one-core reservation still starts a
thread per machine core inside NumPy, and the ledger's arithmetic becomes a
polite fiction.

## Estimates say where they came from

`ObservationHistory` returns the **declared** value until it has
`minimum_samples` successful observations, and the returned `Estimate` carries
its `source` so nobody mistakes a guess for a measurement:

```text
0 samples -> DECLARED 10.0
2 samples -> DECLARED 10.0     still below the minimum
3 samples -> OBSERVED  4.2     median, not mean
```

Median rather than mean, so one pathological run does not move the estimate
far. Failed attempts are excluded from *duration* history — a task that failed
fast is not fast — but they count toward `failure_rate`, and a
resource-exhausted attempt still counts toward envelope review. Memory
estimates take the worst observed case rather than the typical one.

## An underestimate is a record, not a silent widening

When observed usage exceeds the declared envelope on *any* dimension,
`review_envelope()` emits a `DeploymentRevision`: an identified record naming
the declared value, the observed peak, the dimensions exceeded, a proposed
envelope, and the reason.

Attempts killed by a resource limit are the most informative evidence here and
are included rather than discarded with ordinary failures. Their peak is a
**censored lower bound** — the attempt was stopped, so the real requirement may
be higher — and the revision says so via `from_censored_evidence`.

Headroom applies only to the continuous dimensions. Cores and GPUs are discrete
counts: a task that used two GPUs is proposed two, not padded to three.

It is a **proposal**. This layer never mutates the declared envelope, because
quietly enlarging it would make the original plan unreproducible and hide a
real modelling error. The test asserts the declared envelope is unchanged after
a revision is emitted.

## Executable evidence

```bash
.venv/bin/python scripts/run_stage8_demo.py
```

| Result | Value |
|---|---|
| layered / FIFO / event-driven makespan | 39.0 / 39.0 / **30.0** s |
| improvement over layer runner | 9.0 s (23.1%) |
| matches critical-path lower bound | yes |
| chain runs back-to-back | yes |
| peak usage vs capacity | 2 of 2 cores; never exceeded |
| oversubscribed | no |
| starving task, without → with aging | 40.0 s → 10.0 s start |
| refused reservation dimension | `cpu_cores` |
| environment affinity | routed to `gdal-node` |
| thread caps | all five variables = reserved cores |
| estimate source progression | DECLARED → DECLARED → OBSERVED |
| underestimate | recorded as a proposal, declared untouched |

## Test coverage

`tests/test_stage8_scheduling.py` — oversubscription refusal on all four
dimensions, release returning capacity, best-fit and affinity placement, thread
capping, allocation-aware capacity, critical-path ranks, cycle refusal, aging
and its ceiling, deterministic ordering, makespan comparison, back-to-back
dependency starts, dependency respect and exactly-once execution under every
policy, loud deadlock on unschedulable work, declared-until-enough-history,
failed attempts excluded, revision-as-proposal, and history feeding back into
the schedule.

## What this does not do

- **The policy is not wired into the live Stage-1 controller.** That controller
  still enforces `max_inflight=1` — it is deliberately serial, and its comment
  says packing and priority belong to Stage 8. Lifting that guard means
  reworking a durable runtime that Stages 1–7 depend on, and it was not done
  here. Stage 8 delivers the *policy*, tested in isolation; making it the
  runtime's scheduler is the first follow-up.
- **Makespans come from a discrete-event simulation**, not from wall-clock runs
  of real subprocesses. Durations are declared estimates or measured history.
  The simulator answers "does this policy order work better" — not "how long
  will this take on your node". Resource feasibility inside it *is* real: every
  start goes through the same ledger.
- **No live observation collection.** `TaskObservation` values are supplied by
  the caller; nothing yet instruments the Stage-1 worker to produce them from
  real attempts. Duration, memory, and transfer history are therefore only as
  real as what is fed in.
- **`peak_memory_mb` is not measured anywhere.** There is no cgroup or
  `resource.getrusage` sampling in this stage; the revision machinery is
  correct but is currently driven by supplied numbers.
- **Stage-5 acquisition throttling and network-site feasibility are not
  integrated** into this admission path. `ExecutionSite` carries
  `network_classes` and placement filters on them, but the per-provider quota
  ledger from Stage 5 remains separate. Unifying them is explicitly listed in
  the Build section and is unfinished.
- No GPU-ID assignment — GPUs are counted, not individually reserved or pinned.
- No locality, fan-out, or learned runtime estimates. The roadmap says to add
  those only after measurement, and the measurement does not exist yet.
- Single node. "Best-fit placement across sites" means multiple logical sites
  on one machine, never cluster-node allocation.
