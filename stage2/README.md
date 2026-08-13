# Stage 2 — Scientific contracts and exact small-graph oracle

Status: **implemented and acceptance-tested** on 2026-08-13.

Stage 2 introduces the scientific composition language that was deliberately
absent from the Stage 1 runtime. It can express what an artifact means, what a
consumer requires, what a capability can produce, what evidence supports a
claim, and how an explicit finite set of alternatives composes into a valid
plan.

This is not yet recursive catalog discovery or the production-scale resolver.
Stage 3 will build the derivation hypergraph and global selector, using this
stage's exhaustive enumerator as an independent correctness oracle.

No WRF-SFIRE, MPI, SLURM, remote source, or heavy model was run in this stage.

## What was built

- [`contracts/`](../contracts/) contains immutable artifact descriptors,
  requirement values, distinct requirement uses, spatial/temporal/vertical
  support, missingness policy, empirical evidence, applicability, metric
  evaluators, and complete direct-compatibility proofs.
- [`capabilities/`](../capabilities/) replaces the one-producer legacy registry
  for new composition work. It supports multiple producers per concept,
  multi-output invocations, finite parameter binding, immutable artifact
  declarations with separate commit trust, exact implementation identity, and
  static deployment proofs.
- [`plans/`](../plans/) separates candidate scientific derivations, exact
  result-affecting implementation/artifact bindings, and replaceable
  `DeploymentPlan` placement/resource choices. Changing node class or request
  resources changes deployment identity without changing scientific-plan
  identity.
- [`composition/oracle.py`](../composition/oracle.py) exhaustively enumerates an
  explicit bounded candidate graph. It handles alternatives, co-production,
  sharing, cardinality, defaults/omission, distinctness, deployment, grounding,
  cycle rejection, deterministic tie-breaking, and complete blocker trees.
- [`composition/compiler.py`](../composition/compiler.py) independently checks
  both the bound scientific plan and its targeted deployment plan, then emits
  one Stage 1 task per invocation. Its
  compilation record retains the canonical
  `(invocation_id, port_id, descriptor_id)` binding for every lowered output.
- [`wind_evidence_pack_v1.json`](wind_evidence_pack_v1.json) honestly records
  that no reviewed immutable held-out reference currently exists. It supplies
  the deterministic evidence state needed for a later policy layer to return
  `CHOICE_REQUIRED`; Stage 2 itself does not yet implement that request-policy
  response. Grid spacing is never presented as effective resolution.

The legacy `ProducerV2`, `VarSpec`, cube catalog, and `DataAdapter` are not
promoted into this layer. Their metadata is too incomplete to prove units,
representation, CRS, vertical support, origin, missingness, or empirical
quality, and some legacy adapters hide interpolation/resampling. A later bridge
must be explicit and audited.

## Direct matching boundary

`contracts.direct_match()` is pure and deterministic. It cannot fetch data,
rank sources, recurse, convert units, reproject, interpolate, fill gaps, or
resample. A mismatch returns ordered structured checks and transformation
hints; a future transformation may satisfy it only by appearing as its own
capability invocation.

The matcher distinguishes:

- grid spacing from native information scale;
- native scale from empirically supported effective resolution;
- factual metadata from empirical evidence;
- unknown evidence from failed evidence;
- 10 m AGL from 10 m MSL, pressure, or model levels;
- instantaneous values from means or accumulations;
- distinct consumer ports from equal normalized requirement values.

## Runnable proof

The conformance graph contains a left-value artifact declaration, separate
left and right producers, one cheaper two-output producer, and an add consumer:

```text
declared left artifact --\
left producer -----------+--> add --> requested sum
right producer ----------+
pair producer --left-----+
              \-right----+
```

The exact oracle selects `pair + add` with total cost 3. The compiler emits two
tasks—not three—and the pair task publishes both ports. Running those tasks
through the Stage 1 controller produces 42 with two attempts.

The artifact is only a declaration, not trusted-store evidence, so the typed
artifact factory marks it uncommitted and the oracle correctly leaves it
ineligible. A future artifact-index attestation can make such a candidate
eligible without pretending its caller-supplied manifest proves presence.

```bash
runtime_root=$(mktemp -d /tmp/nasa-stage2-demo.XXXXXX)
.venv/bin/python scripts/run_stage2_demo.py \
  --runtime-root "$runtime_root"
```

## Acceptance evidence

```bash
.venv/bin/python -m pytest \
  tests/test_stage2_matching.py \
  tests/test_stage2_capabilities.py \
  tests/test_stage2_oracle.py \
  tests/test_stage2_evidence_pack.py \
  tests/test_stage2_integration.py -q
```

The suite covers scientific matching, identity/tamper checks, structured
deployment rejection, alternative producers, co-production, optional/default
and cardinality semantics, sharing/distinctness, cycles, incomplete enumeration,
blocker completeness, compilation boundaries, and a real local subprocess run.
It also proves that changing placement/resources changes the `DeploymentPlan`
and compiled Stage-1 graph identities while the `BoundDerivationPlan` identity
remains stable.

## Deliberate limits

- The exhaustive oracle accepts an explicit small candidate universe; it does
  not recursively discover producers. That is Stage 3.
- Selection currently minimizes declared integer cost after hard compatibility
  constraints. General quality optimization and Pareto enumeration are later.
- The typed `BoundInvocation` adapter derives that integer cost from its frozen
  `cost:declared-v1` metric record and derives source grounding from its actual
  input-use set; callers cannot override either selection fact.
- Selected compatibility records accept only an authenticated, standardized
  wrapper around a typed `contracts.CompatibilityProof`; arbitrary JSON cannot
  be promoted into a satisfaction edge.
- A root bound entirely to a caller-declared artifact returns
  `ARTIFACT_COMMIT_UNVERIFIED` and no task. Descriptor, manifest, and content
  hashes supplied by a caller do not prove that bytes are committed in a
  trusted artifact store. An artifact feeding a task returns
  `BRIDGE_EXTERNAL_LEAF_UNSUPPORTED` until Stage 1 gains an exact committed
  external-input binding; it is never disguised as a constant producer.
- Executable output lowering currently accepts only an exact
  `application/json` representation, which is the representation enforced by
  Stage 1's `finite_json` validator. Other representations fail closed instead
  of being silently relabeled.
- Evidence-bound compatibility proofs fail closed at the compiler boundary
  until the compiler accepts and authenticates the exact typed evidence
  profile, snapshot, subject, and requested regime inputs needed to replay
  `direct_match()`.
- Stage 1 currently derives logical task and artifact-recipe IDs from the whole
  executable graph, including resource requests. Consequently, a new
  `DeploymentPlan` currently creates new Stage-1 task/recipe IDs even when the
  bound scientific plan is unchanged. Stable cross-deployment output reuse
  needs the later `LogicalTaskKey`/deployment-binding split in the runtime.
- The only executable binders are closed scalar conformance operations
  (`constant`, `pair`, and `add`). No arbitrary callable or import path exists.
- Recursive resolution, global MILP selection, explicit transformations, real
  source acquisition, partitions, and cluster providers remain later stages.
- The wind JSON fixture is an evidence-gap ledger. An `AVAILABLE` real-data
  pack still needs a reviewed loader into the typed `EvidenceSnapshot`, exact
  invocation/output applicability, uncertainty intervals, and held-out data.
