# Stage 4 — Explicit semantic transformations

Status: **core implemented and acceptance-tested** on 2026-08-14 for finite,
in-memory transformation catalogs. Remote acquisition, coverage, and mosaics
remain Stage 5. No WRF-SFIRE, MPI, Slurm, or remote source was run.

Stage 4 makes every result-affecting conversion an explicit, versioned,
independently costed node. `direct_match()` is unchanged and still inserts
nothing: a metre descriptor simply does not satisfy a kilometre requirement.
The bridge is a declared transformation that competes with direct data in the
same global selector.

The retained fixture uses meaningless scalar concepts so the domain-neutrality
boundary is executable rather than aspirational.

## What was built

- [`transformations/model.py`](../transformations/model.py) defines
  `TransformationSpec` — an immutable hyperedge between *exact* descriptor
  states, carrying its kind, closed operation/binder pair, typed ports,
  parameters, cost, semantic rule ID, and explicit scientific assumptions.
  `to_capability_spec()` lowers it to an ordinary `CapabilitySpec`, which is
  why Stage 3 needs no transform-specific code path.
- Unit coefficients come only from a closed, versioned affine registry. A
  caller cannot supply its own factor, and an unregistered unit pair cannot
  become a transformation at all.
- Semantic guards reject scientifically dishonest edges at construction:
  outputs must declare `DERIVED` origin; a unit conversion may change only
  units; regrid may not change CRS while reprojection must; bilinear
  interpolation is admitted only for explicitly typed continuous/intensive
  fields, so categorical and extensive data are excluded rather than silently
  interpolated.
- [`transformations/search.py`](../transformations/search.py) computes a finite
  forward-reachability fixed point over descriptor states and returns an
  augmented catalog. Bounds on depth, descriptor states, and transformation
  count are explicit; activating any of them sets `complete=False` with typed
  reasons naming the omitted subjects.
- A transformation whose inputs no producer supplies is an ordinary
  *unreachable frontier item*, not a truncation — completeness is preserved.
- [`stage4/fixtures.py`](fixtures.py) and [`stage4/demo.py`](demo.py) run the
  full vertical slice: closure → recursive discovery → global selection →
  independent validation → Stage-1 compilation → durable execution → validated
  commit.

## Truncated discovery cannot claim a global optimum

Transformation closure is *discovery*. A selection that is optimal over a
catalog which is missing candidates is not globally optimal, so
`WorkflowResolver` now accepts upstream completeness:

```python
WorkflowResolver(
    expansion.augmented_catalog,
    deployment_snapshot,
    upstream_discovery_complete=expansion.complete,
    upstream_limit_codes=(...),        # typed reasons, e.g. MAX_DEPTH
)
```

The effective flag is `graph.discovery_complete and upstream_complete`, and it
reaches the selector, the `UNSATISFIABLE`-versus-`INCOMPLETE` decision, and the
independent validator's candidate-universe check alike. The two flags cannot be
set inconsistently: complete-with-codes and incomplete-without-codes both raise.

With a truncated closure the plan stays structurally valid, but the reported
status becomes `FEASIBLE_NOT_PROVEN_OPTIMAL` and binding is refused unless the
caller explicitly passes `require_proven_optimal=False`. The serialized report
separates `complete` (effective) from `graph_expansion_complete` (this
resolver's own expansion) and lists `upstream_limit_codes`, so a reader can
attribute the truncation without re-deriving it. Upstream completeness is
deliberately *not* reconstructed from those two booleans — a conjunction cannot
be inverted — and the limit codes already name every truncation that fired.

This channel is general, not transform-specific: Stage 5's remote metadata
search is the next thing that will feed it.

## Executable evidence

`scripts/run_stage4_demo.py` resolves one kilometre requirement against a
catalog offering direct kilometres at cost 9 and metres at cost 4:

| Result | Value |
|---|---|
| resolution status | `READY`, validated, eligible for binding |
| selected path | `example-length-metres` + `transform:example-metres-to-kilometres` |
| selected cost | 5 (versus 9 for the direct alternative) |
| committed result | `1.5` from a `1500` m source |
| Stage-1 tasks / attempts | 2 / 2 |
| run state | `SUCCEEDED` |

The transformation is a real compiled task with its own attempt and provenance.
The expansion identity is recorded in the bound plan as a
`transformation_expansion` snapshot reference, so the derivation can be
replayed against the same descriptor-state universe.

Because origin is a consumer constraint, restricting the same request to
`SYNTHETIC` origin correctly falls back to the cost-9 direct source instead of
silently transforming — covered by
`tests/test_stage4_integration.py`.

## Test coverage

- `tests/test_stage4_transformations.py` — contract and closure layers:
  registry authority, origin/metadata forgery guards, vector decomposition
  typing, content-addressed determinism, capability lowering, reachability,
  unreachable-versus-truncated, and every bound marking incompleteness.
- `tests/test_stage4_integration.py` — transformation-versus-direct-source
  competition, transform visibility as a plan node, origin-constrained
  fallback, truncation refusing a global-optimality claim, flag consistency,
  report attribution, and the executed/committed vertical slice.
- `tests/test_stage4_runtime_ops.py` and
  `tests/test_stage4_artifact_validation.py` — closed operation execution
  semantics and scientific field validation at the commit boundary.

## Current limitations and non-claims

- Transformation catalogs are finite and in memory. Closure is forward
  reachability over declared edges; it does not synthesize new edges.
- The demonstrated slice converts a scalar. Grid, temporal, reprojection, and
  vector operations are implemented and unit-tested at the operation and
  contract layers, but no multi-hop chain is promoted as a scientific fixture.
- Cost remains the only automatic objective. Transformation *loss* is declared
  and visible, but is not yet an optimization dimension.
- `evidence_profile_id` on a lowered transformation is `evidence:unknown`.
  Stage 4 does not invent empirical error for a conversion.
- No remote acquisition, coverage algebra, mosaic, or asset manifest — Stage 5.
