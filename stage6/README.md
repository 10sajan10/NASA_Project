# Stage 6 — Dataset-versus-model slice, evidence gating, and the latency gate

Status: **prototype** — implemented and acceptance-tested on 2026-08-15,
revised 2026-08-16 after an external audit. Two exit gates do not hold: the
Section 9.5 planning-latency budget is **measured and missed** by roughly 1.4x
(see "The latency gate fails"), and the evidence driving the whole
dataset-versus-model contest is **synthetic fixture data** — no real held-out
reference observations exist. What the stage demonstrates is the decision
machinery, not a scientific result. No WRF-SFIRE, MPI, Slurm, or real remote
provider was run.

**Audit corrections (2026-08-16).** Interval separation was decided by asking
whether all intervals shared one common intersection, which is not the same as
asking whether any pair overlaps; with A=[0,2], B=[1,3], C=[4,5] it reported
separation while A and B plainly overlap. It is now a pairwise sweep. The
decision report also accepted an evidence snapshot and then ignored it, trusting
a caller-supplied profile dictionary; readings are now resolved by deriving each
producer's evidence subject and matching it against the frozen snapshot, and the
snapshot is part of report identity.

Stage 6 is where a model competes with data on the record rather than on
vibes, and where the system is asked to rank scientific quality — and refuses.

## The contest

```text
result
    AND ignition
    AND fuel
    AND terrain
    AND flow
          OR direct observed source            cost 6
          OR coarse source -> lightweight model cost 1 + 3
```

Terrain feeds *both* the consequence model and the downscaling model, so a
correct selector must pay for it once. That shared input is the counterexample
that makes per-requirement greedy selection wrong, and it is wired into the
fixture deliberately rather than described in a comment.

Minimum cost selects the modelled path at 11 against the direct path at 13,
executes six Stage-1 tasks, and commits the field.

## Evidence gating happens at discovery, not at selection

Move the model's `EvidenceApplicability` outside the requested region and it
does not lose on cost — **it never enters the graph at all**. `direct_match`
rejects it, the rejection is recorded, and no constraint or objective can
resurrect it. The same request then resolves to the direct source at cost 13.

That predicate was already built in Stage 2 (`_applicability_contains`). What
Stage 6 adds is a fixture that makes it executable, and the discipline of using
the *same* predicate in the decision report rather than a second copy that
could drift.

## A quality request returns CHOICE_REQUIRED, always

`objectives/` implements Section 6.4's named selection policies. There is
exactly one automatic objective — minimum declared cost under hard constraints
— and `EMPIRICAL_QUALITY` is not a second one.

```python
outcome = resolve_with_objective(resolve, ObjectiveRequest(EMPIRICAL_QUALITY), ...)
assert outcome.status is ObjectiveStatus.CHOICE_REQUIRED
assert outcome.resolution is None            # nothing was auto-selected
```

The report carries real alternatives obtained by **re-solving the whole problem
once per candidate producer**, not by locally swapping one node. Each presented
option is therefore a globally consistent plan with a real cost, which matters
precisely because producers share inputs.

`ranking_complete` and `nondominance_claimed` are properties fixed at `False`,
not fields — no caller can construct a report that claims either, and
`from_dict` rejects a payload that tries.

The sharpest test in the stage: the fixture's evidence *is* comparable and its
confidence intervals *do not overlap*, so a ranking would be easy and would
look defensible. The system still returns `CHOICE_REQUIRED`. An automatic
empirical-quality optimizer is not something this MVP can honestly claim, and
the one case where it would have been tempting is the case that proves it.

## When two numbers may be compared

`objectives/comparability.py` refuses a comparison unless every alternative
carries a *known* claim for the same metric, from the same evaluator and
protocol, against the same reference manifest, in the same unit and bound kind,
with applicability that covers the request. Every blocking condition is
reported, not just the first:

| Blocking code | Meaning |
|---|---|
| `SINGLE_ALTERNATIVE` | nothing to compare against |
| `METRIC_NOT_KNOWN` | an alternative has no known claim |
| `REFERENCE_MANIFEST_DIFFERS` | measured against different reference data |
| `EVALUATOR_DIFFERS` / `PROTOCOL_DIFFERS` | different measuring apparatus |
| `UNIT_DIFFERS` / `BOUND_KIND_DIFFERS` | point estimate versus conservative bound |
| `APPLICABILITY_DOES_NOT_COVER_REQUEST` | valid evidence, wrong place or time |

Even when comparable, overlapping confidence intervals set
`separation_established=False`. A smaller point estimate is not a winner. When
intervals are missing entirely the verdict falls to the safe side and treats
the difference as unresolved.

## The decision is part of the plan

A `ChoiceRecord` is bound to the `report_id` it was made from. Present
different alternatives and an old choice raises `StaleChoiceError` rather than
being applied to a changed world. The accepted decision becomes a
`PlanSnapshotRef("objective_decision", …)`, which enters `bound_plan_id` — so
the same selected invocations chosen *by a human* and chosen *by cost* are
different plans, and the demo asserts exactly that.

A declared `FallbackDecision` records the opposite act — "stop asking about
quality, use minimum cost" — with the same identity treatment, so giving up on
a comparison is attributable rather than a silent default.

## A compiler gap closed

`compile_bound_plan` previously refused any evidence-bound proof outright:
*"evidence-bound proof replay is unsupported without the exact typed evidence
inputs used by direct_match."* Stage 6's slice cannot execute under that rule,
so the compiler now takes an `evidence_snapshot` and replays such proofs
faithfully.

The recorded identifiers are treated as claims to be checked, not lookups to be
trusted: the profile must really be in the frozen snapshot, the snapshot
identity must match, and the evidence subject is **derived from the producer**
rather than read from the proof. A forged evidence reference fails here. With
no snapshot supplied the compiler still fails closed, because replaying an
evidence-bound proof without its evidence would verify a weaker claim than the
one that was selected.

## The latency gate fails

Section 9.5 asks for graph construction + selection + validation at p95 ≤ 5 s
over 30 warm runs, on a graph of at most 1,000 invocation nodes, 5,000 arcs,
and 12 levels. This is the gate the roadmap has carried as pending since
Stage 3. Measured on the frozen representative graph
(`stage6/planning_benchmark_v1.json`):

| | Value |
|---|---|
| invocations / arcs / levels | 126 / 250 / 6 — **all within caps** |
| p95 total (30 warm runs) | **7.06 s** (was 15.1 s before Stage 8R) |
| budget | 5 s |
| within budget | **no** |

The graph is nowhere near the size caps; the **MILP solve is the bottleneck**,
at 83% of planning time and scaling sharply:

| Invocations | Total | Solve | Solver calls |
|---|---|---|---|
| 30 | 1.1 s | 0.7 s | 3 |
| 64 | 5.5 s | 4.6 s | 7 |
| 126 | 7.06 s | — | — |

Presolve is now **enabled** by default. It was disabled because this HiGHS
build returns a false infeasibility on a valid formulation, but the solver path
already re-confirms any presolved infeasibility on the unpresolved model, so
presolve cannot turn a feasible problem into UNSAT — the guard was the fix, and
disabling presolve on top of it was paying twice. That bought 23.3 s → 15.1 s,
a 37% reduction at identical cost and proven optimality.

It is not enough. The remaining cost is structural: solver calls scale with
graph width because deterministic tie-breaking freezes producer bits in
30-bit lexicographic chunks, so a 126-invocation graph needs 11 solves rather
than one. Closing the gate means changing that formulation or accepting a
larger budget — both are decisions for the MVP review, not silent edits.

**This result is recorded, not engineered around.** Shrinking the graph until
the number looked good would have produced a passing gate that meant nothing,
which is why `test_the_frozen_profile_is_a_real_graph_not_a_toy` asserts a
floor on graph size. The latency assertion is a **strict xfail**: it documents
the miss, and if the solver ever becomes fast enough to pass it, the suite
fails and forces someone to re-freeze the profile and update this claim.

Re-freeze with:

```bash
.venv/bin/python scripts/freeze_stage6_benchmark.py \
    --base-width 32 --levels 6 --measured-runs 30
```

## Executable evidence

```bash
runtime_root=$(mktemp -d /tmp/nasa-stage6-demo.XXXXXX)
.venv/bin/python scripts/run_stage6_demo.py --runtime-root "$runtime_root"
```

| Result | Value |
|---|---|
| minimum-cost selection | modelled path, cost 11 (direct path 13) |
| tasks / attempts | 6 / 6 |
| committed result | 21.0 = (2·4 + 1)·2 + 1·3 |
| run state | `SUCCEEDED` |
| out-of-scope request | model absent from graph; direct source at cost 13 |
| quality request | `CHOICE_REQUIRED`, `auto_selected=false` |
| comparability | comparable, intervals disjoint, separation established |
| recorded choice | direct source at cost 13, overriding minimum cost |
| decision changes plan identity | yes |

## Test coverage

- `tests/test_stage6_objectives.py` — comparability across every blocking code,
  interval separation, report immutability against forged ranking claims,
  choice/fallback identity and round trips.
- `tests/test_stage6_integration.py` — the contest, shared-input accounting,
  discovery-level evidence gating, `CHOICE_REQUIRED` under separated evidence,
  constrained re-solve costs, stale-choice refusal, plan-identity change,
  execution, and the compiler's evidence replay.
- `tests/test_stage6_benchmark.py` — representative-graph structure and
  optimality, Section 9.5 caps, correctness gating of the measurement, the
  anti-shrink floor, and the strict-xfail latency gate.

## Current limitations and non-claims

- **The Section 9.5 latency gate is not met.** See above. Planning latency is
  the largest known gap at the MVP boundary.
- The evidence in this fixture is **synthetic**. Real wind evidence remains
  unavailable: `stage2/wind_evidence_pack_v1.json` is still frozen at
  `status: UNAVAILABLE` with `NO_REVIEWED_IMMUTABLE_HELD_OUT_REFERENCE`,
  and Stage 6 did not invent numbers for it. Nothing here is a claim about
  ERA5, WRF, or any real wind product.
- The "lightweight model" is a meaningless gain-plus-support arithmetic
  operation. It is a model in the architectural sense — a producer with
  declared evidence and applicability limits — not in the scientific one.
- Comparability is judged on provenance and applicability, not on statistical
  power. Overlapping intervals are reported; no hypothesis test is run, and the
  block-bootstrap method is *declared* by the fixture rather than executed.
- Alternatives are enumerated by include-constrained re-solve, one per
  candidate producer. This is not Pareto enumeration and makes no
  non-dominance claim; `epsilon`-constraint cuts remain post-MVP.
- Only one contested concept is supported per decision report. A request with
  two independent quality-sensitive inputs would need a report per concept, and
  their interaction is not modelled.
- `quality_under_budget`, `minimum_dependency_latency`, and user-defined
  lexicographic policies from Section 6.4 are still deferred.
- The decision binds into `bound_plan_id` through a snapshot reference, not
  into `CandidateDerivationPlan` identity. The roadmap says "candidate-plan
  identity"; the bound plan is what executes, and reaching it this way avoided
  changing Stage-2 core identity. The difference is deliberate and recorded.

## Composition MVP release boundary

The roadmap stops here: run a user/scientist review and repair correctness or
usefulness problems before adding any scale features. Stages 7+ are separately
justified post-MVP work, not prerequisites.

The two items that should lead that review are the failed latency gate and the
absence of any real reference observations to put in an evidence pack.


## Re-measured after Stage 8R, and a second limit found

Stage 8R made the discovered hypergraph share producers properly instead of
instantiating one invocation node per requirement context. Re-frozen on the
*same* representative graph — 126 invocations, 250 satisfaction arcs, depth 6,
so the anti-gaming floors in `tests/test_stage6_benchmark.py` still hold — the
gate improved from **15.12 s to 7.06 s**, from 3.02x over budget to 1.41x.

It is still a miss, and it is still recorded as a strict xfail rather than
re-scoped until it passes.

Measuring the re-freeze surfaced a **larger limit than latency**. Holding the
solver's default 30 s interactive `time_limit_s`:

| invocations | arcs | wall | status |
|---|---|---|---|
| 30 | 58 | 1.03 s | `READY` |
| 62 | 122 | 1.90 s | `READY` |
| 126 | 250 | 7.02 s | `READY` |
| 254 | 506 | 33.77 s | `FEASIBLE_NOT_PROVEN_OPTIMAL` |
| 510 | 1018 | 37.53 s | `FEASIBLE_NOT_PROVEN_OPTIMAL` |

Beyond roughly 250 invocations the solve budget expires before optimality is
proven, so the resolver stops claiming a global optimum. Section 9.5's node cap
is 1,000, so **exact global selection is unavailable at about a quarter of the
scale the system declares for itself.**

That is a correctness-scope boundary, not a speed complaint, and it matters more
than the 1.41x latency miss: latency degrades a service, but an unproven optimum
degrades the central claim. The system does report it honestly — the status
becomes `FEASIBLE_NOT_PROVEN_OPTIMAL` rather than silently returning a
non-optimal plan labelled optimal — so this is a documented limit rather than a
defect. A larger time limit would not fix it.

### Where the time actually goes, measured

An earlier version of this section (and of the xfail reason) attributed both
the latency miss and this wall to the deterministic tie-break freezing producer
bits in 30-bit chunks. **That was wrong, and instrumenting the solver refutes
it.** Per-call timings:

| graph | solver calls | per-call seconds |
|---|---|---|
| 126 invocations | 11 | `0.10, 4.89, 0.03, 0.02, 0.01 x 7` |
| 254 invocations | 2 | `0.21, 29.68` (limit) |

The nine tie-break chunk calls total about 0.1 s -- roughly 1.4% of solver
time. **One call, the primary cost MILP, is the entire bottleneck**: 4.89 s of
the 5.08 s spent in the solver at 126 invocations, and at 254 it alone exhausts
the 30 s budget, which is why only two calls occur there.

Three candidate causes were tested and each refuted:

| hypothesis | test | result |
|---|---|---|
| big-M / rank domain too loose | rank upper bound and big-M tightened from 253 to 16 | 33.68 s -> 33.68 s, no change |
| permutation symmetry across slots | source-level costs made distinct per slot, optimum preserved | 33.71 s, no change |
| tie-break chunking | per-call instrumentation | ~0.1 s total, not material |

So the cause of the primary solve's scaling is **still unidentified**, and that
is recorded as an open question rather than guessed at again. Symmetry at the
*upper* levels was not tested and remains the most plausible remaining
candidate.


## The evidence gate, decided

The other half of the Stage-6 gate is evidence, and the instruction for it was
to *decide* it rather than relabel conformance evidence as science.

**Decision: Stage 6 makes no empirical quality claim, and
`stage2/wind_evidence_pack_v1.json` stays `UNAVAILABLE`.**

No reviewed, immutable, held-out reference observations exist for wind. None
were manufactured to close the gate, and the synthetic fixtures that drive the
Stage-6 slice are *conformance* data -- they exercise the machinery, they are
not measurements of ERA5, WRF, or any real wind product. The pack records that
directly: `scope.status: UNFROZEN` because no AOI, time window, vertical scope
or validation regime was ever reviewed; `reference_observations.reason_code:
NO_REVIEWED_IMMUTABLE_HELD_OUT_REFERENCE`; every entry in `empirical_claims` at
`status: UNKNOWN` with a null estimate and a null bound; and the legacy
`quality.trust_tier` string quarantined, because a catalog label is not
evidence.

What was missing was not honesty in the file but a **binding between the file
and the system**. The pack declared `quality_sensitive_request:
CHOICE_REQUIRED`, and Stage 6 did behave that way, but nothing connected the
two. Either could have drifted -- the resolver relaxed into auto-selecting on
quality, or the policy edited -- and no test would have disagreed. That is
exactly how conformance evidence becomes science by accident.

`tests/test_stage2_evidence_pack.py` now closes it:

- no empirical claim may carry an estimate, a bound, or a reference manifest,
  so `UNAVAILABLE` cannot quietly acquire a number underneath it;
- the declared `decision_policy` is asserted against the running Stage-6 quality
  path, so the string and the behaviour must agree;
- the scope must stay `UNFROZEN`, so no result can be generalised beyond a
  review that never happened.

The gate is therefore **decided and closed as a recorded non-claim**, not as a
pass. Populating it needs real held-out observations and a reviewed validation
regime; until those exist, quality-sensitive requests return `CHOICE_REQUIRED`
and a human makes the call.
