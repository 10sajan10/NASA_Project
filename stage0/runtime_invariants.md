# Stage-0 runtime and identity invariants

These invariants apply immediately. Later stages may replace their types but
must preserve their meaning.

## Identity

1. A mutable `Pipeline` is never the execution identity. Execution uses a
   `BoundPipeline` with a content-derived `plan_id`.
2. Every node binds one exact producer object before work starts. Registry
   mutation after binding cannot rewire a plan.
3. A component identity includes its declared name, implementation source hash,
   configuration hash, declared inputs/outputs, version, determinism/idempotency
   declaration, and coarse resource envelope.
4. An explicit `component_version` or `version` is authoritative. Otherwise the
   version is derived from source content. A mutable version such as `latest` is
   not sufficient for a later scientific plan.
5. Component mutation after binding is rejected. A changed component requires a
   new bound plan and plan ID.
6. Configuration is hashed, not copied into the plan, to avoid leaking secrets.
   Later stages must separately record non-secret canonical configuration.

## Execution

1. Runtime graph expansion is disabled by default. A branch must be part of the
   plan identity. The temporary `allow_runtime_triggers=True` flag is legacy
   compatibility, not an accepted composed-runtime mechanism.
2. The Stage-0 runner is at-most-one submission per node only in a live process.
   It makes no exactly-once or restart claim.
3. Determinism and idempotency are declarations, never inferred. `unspecified`
   is a meaningful unsafe state.
4. Resource envelopes are preflight metadata in Stage 0, not reservations.
   Aggregate CPU/memory/GPU admission starts in the resource-aware scheduler.
5. An output is scientifically usable only after validation. The current cube
   violates stronger atomic-publication semantics for tiled output; a strict
   xfail preserves that defect until Stage 1 implements staging and commit.

## Artifact states for the next stage

The durable runtime must implement this minimum progression:

```text
ABSENT -> STAGED -> VALIDATED -> COMMITTED
                   |             |
                   v             v
                 REJECTED      RETIRED
```

Only `COMMITTED` satisfies a downstream dependency. A versioned immutable
manifest conditional-create is the authoritative commit point; catalog rows are
rebuildable indexes. A late or duplicate attempt needs a fencing token and may
not overwrite a committed artifact.

## Minimum provenance

Every Stage-0 `RunResult` records:

- run ID, start/end time, status;
- plan ID and pipeline name;
- Git revision and dirty flag;
- hash and file list for the binding/runner implementation;
- request window, non-secret context keys, and context hash;
- exact component identities;
- per-step status, elapsed time, attempts, and declared produced versions.

Stage 1 must extend this with logical task ID, attempt ID, fencing token,
external handle, state-transition events, input artifact roots, validation, and
authoritative commit record.
