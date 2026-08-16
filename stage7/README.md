# Stage 7 — Lazy partitions, bounded admission, and collection completeness

Status: **control plane, now with real partition execution** — implemented and
acceptance-tested on 2026-08-15, revised 2026-08-16 after an external audit.
Partitions compile into Stage-1 tasks and run: a committed partition means a
subprocess executed and an artifact was published, not a row being set. The
10^4-partition figures below still measure the *control plane*; the executed
slice is smaller and is reported separately. No WRF-SFIRE, MPI, Slurm, or real
remote provider was run.

**Audit corrections (2026-08-16).** The template restated an operation as loose
strings: the fixture resolved `example-add`/`synthetic.add.v1` and then built
the template from `synthetic.constant.v1` with unrelated inputs, so "one
scientific selection reused by 10,000 partitions" reused only the invocation
hash. `PartitionTaskTemplate` now carries a verified `BoundInvocation`, so
operation, parameters, implementation digest, and output contract travel
together. Retry was claimed but impossible — failed members were marked FAILED
while packet generation selected only ADMITTED rows — and is now a real
transition backed by a durable `partition_attempts` table. Admission accepted a
foreign partition set or template while advancing the cursor; the collection's
registered definitions are now authoritative. `PacketAttempt` is split from
`PacketResult` so attempt identity is minted before submission and a serialized
outcome cannot be relabelled.

Stage 7 is where "resolve once, execute many" stops being a slogan. One
scientific selection drives 10,000 partitions, and the controller never holds
more than its admission window.

## The partition space is arithmetic, not a list

`PartitionSetSpec` is the ordered Cartesian product of declared axes — spatial
tiles, temporal windows, scenarios, ensemble members — represented as a
**mixed-radix number system**. Partition *n* is decoded from its index on
demand:

```python
spec.total          # 10_000, computed by multiplication, not by counting
spec.key_at(9_999)  # tile=t099/window=w099
spec.iter_keys(9_990, 20)   # a bounded window, clamped at the end
```

There is deliberately no method that returns every key. `iter_keys` takes an
offset and a limit, so a caller cannot accidentally ask for all of them. This
is the structural half of "never materialize all partitions through
`list(...)`" — the other half is the admission window below.

The last axis varies fastest, so adjacent indices are neighbours in the
innermost dimension. That is what makes fusion group adjacent windows of the
same tile rather than scattered work.

## One scientific selection, reused by every partition

`PartitionTaskTemplate` holds the *single* bound invocation the resolver
selected. Every partition derives its logical task key from that same
invocation, so partitions cannot drift onto different producers — there is only
one to drift from. The demo takes that invocation from a real Stage-3
resolution rather than inventing it.

Logical task identity follows Section 8.7 exactly: bound invocation hash +
operation + ordered prospective input **slot** IDs + template ID + partition
key. Slots rather than content digests, because generated inputs have no digest
at compile time; commit later binds slots to digests without renaming any task.

Deployment binding and attempt number are deliberately absent, so revising one
task's resource envelope does not rename every unaffected logical task.

## Admission is one transaction, and that is the whole correctness story

From Section 8.7: *"Cursor advancement is never a separate write that can skip
a window."* So the upserts and the cursor advance share one transaction:

```sql
BEGIN IMMEDIATE
  SELECT next_index, version FROM cursors ...
  INSERT OR IGNORE INTO logical_tasks ...   -- idempotent, stable key
  UPDATE cursors SET next_index=?, version=? WHERE ... AND version=?
COMMIT
```

Three defences, not one:

- **Atomicity** — a crash anywhere inside the window rolls back both halves.
- **Idempotence** — `INSERT OR IGNORE` on the stable logical key absorbs a
  repeated window.
- **A uniqueness constraint** on `(collection, partition_index)`, so even a key
  collision cannot multiply a logical partition.

The optimistic `WHERE version = ?` guard means a concurrent admission cannot
advance the cursor twice over the same window.

`admit_window` takes a `fault` callback that fires at named points *inside* the
transaction, so tests inject a crash exactly where it would do most damage.
`tests/test_stage7_admission.py` crashes at all three points, repeatedly, while
draining the whole space, and asserts the two properties that matter:

```python
assert len(indices) == spec.total                 # nothing multiplied
assert list(indices) == list(range(spec.total))   # nothing skipped
```

## Bounded memory, measured

`AdmissionPolicy` carries the only knobs that decide controller memory:
`window_size`, `low_watermark`, `high_watermark`. The controller admits only
when in-flight falls to the low watermark and tops up to the high watermark one
bounded window at a time.

Measured peak Python allocation while driving the whole space to completion,
holding tiles fixed at 100 and growing only the temporal axis:

| Partitions | Peak bytes |
|---|---|
| 100 | 54,110 |
| 1,000 | 440,365 |
| 10,000 | 616,945 |

The 100 → 1,000 step is **window saturation**, not scaling: below the high
watermark (512) the window never fills, so it is not an apples-to-apples
comparison. The saturated 1,000 → 10,000 step is: **10× the partitions costs
1.4× the memory**. Peak allocation tracks the admission window and the declared
axis labels, not the partition count.

## Batched state writes

Per-partition durability is correct but one `fsync` per partition made a
10^4-partition collection dominated by commit overhead. `record_outcomes()`
commits a whole batch in one transaction with identical semantics — committed
is still final, and the batch is atomic. This is the roadmap's "batched state
writes".

## Fusion bundles work without merging identity

`fuse_members` groups adjacent partitions into a `WorkPacket` bounded twice
over: by `max_packet_members` and by a target packet cost. Work that is already
large enough is not bundled at all.

What fusion must never do is merge identity. A packet owns one provider handle
and an **ordered set of members**; each member keeps its own logical task key;
and a `PacketResult` reports every member independently. On partial failure:

```python
result.committed_keys()                  # kept, never recomputed
result.retryable_keys(retry_safe=True)   # only the uncommitted ones
result.retryable_keys(retry_safe=False)  # () — a human decides
```

Those retryable keys are now *actionable*. `record_packet_result` writes a
durable attempt row and returns a retry-safe failure below the attempt ceiling
to `ADMITTED`, which is what makes it eligible for a new packet. Reaching the
ceiling, or a template that is not retry-safe, is terminal.

A committed partition is never un-committed, so a duplicate or late packet
result cannot destroy work that already landed.

## A partial result is not a whole one

`CompletionPolicy.ALL` requires every expected partition committed **and zero
failures** — 9,999 of 10,000 is not complete, and neither is 9,999 committed
with one failed. `AT_LEAST` and `FRACTION` exist for declared shortfalls, and
`FRACTION` rounds its requirement *up*: 95.5 partitions required means 96.

## Executable evidence

```bash
runtime_root=$(mktemp -d /tmp/nasa-stage7-demo.XXXXXX)
.venv/bin/python scripts/run_stage7_demo.py --runtime-root "$runtime_root"
```

| Result | Value |
|---|---|
| partitions driven | 10,000, committed 10,000 |
| packets | 625 (16 partitions each) |
| peak in-flight | 512 = the high watermark |
| memory, 10× partitions | 1.4× |
| restart | resumed at persisted cursor 512; nothing re-admitted |
| crash injected mid-admission | cursor unchanged; no duplicates; no skips |
| partial packet (8 members) | 4 committed kept, 4 retryable, 0 when not retry-safe |
| committed member vs duplicate failure | survives |
| partial collection under `ALL` | not complete |

## Test coverage

- `tests/test_stage7_partitions.py` — mixed-radix decoding, window clamping,
  axis-order identity, logical-key stability against deployment churn, fusion
  without identity merge, partial-packet retry semantics, and every completion
  policy including the fraction round-up.
- `tests/test_stage7_admission.py` — crash injection at all three transaction
  points, repeated-crash draining, idempotent re-admission, restart resumption,
  watermark behaviour, committed-is-final, and the 10^4 demonstration.

## What this does not do

- **Partitions execute, but they do not yet differ.** `compile_packet` turns a
  packet into one Stage-1 task per partition and `execute_packet` runs it,
  feeding real per-member outcomes back into the store. What is missing is
  per-partition *input binding*: every partition of a template runs the same
  resolved invocation, so this executes real science identically across the
  space rather than tiling a dataset across it. That binding is the
  acquisition bridge and is not built.
- **The 10^4 figures measure the control plane, not 10^4 executions.** Running
  ten thousand subprocesses is not what those numbers show; the executed slice
  in the demo is eight partitions.
- **A template whose invocation has unbound inputs refuses to execute.** That
  is deliberate — inventing inputs is exactly what this stage must not do — but
  it means only input-free invocations are partitionable today.
- **The legacy eager tiling is untouched.** The roadmap says to remove eager
  `list(tile_iter)` behaviour "from the scalable path". The scalable path here
  is new and lazy by construction; `engine/tiled.py` remains the frozen Stage-0
  baseline with its original behaviour, and nothing in Stage 7 routes through
  it.
- **Fusion overhead is not measured against the 5% target.** Section 8.7 asks
  that scheduling overhead stay below 5% of representative useful work. There
  is no representative useful work yet (see the first bullet), so that ratio is
  unmeasured rather than met.
- Single-node, single-writer SQLite, as with every other durable store in this
  project. No distributed cursor and no multi-controller coordination beyond
  the optimistic version guard.
- Tiny-partition fusion is by count and declared cost only. There is no
  measured duration history feeding it; that is Stage 8's observation work.
- The largest space exercised is 10^4. Million-partition scale remains a
  Stage 10 question.
