# Stage 5 — Progressive acquisition, coverage, and real alternatives

Status: **core implemented and acceptance-tested** on 2026-08-15 for a local
connector and one paginated remote connector, both running in process. No real
network provider, WRF-SFIRE, MPI, or Slurm workload was contacted or run.

Stage 5 makes data acquisition a planning act rather than a side effect. The
system asks providers what exists, decides whether the answers actually cover
the request, freezes an exact `AssetManifest`, and only then moves a byte. The
manifest root travels into the bound derivation, so a plan that reads different
bytes is a different plan.

## The ordering, enforced by types

```text
metadata search  ->  coverage assessment  ->  manifest binding  ->  payload fetch
```

`SourceConnector` has exactly two methods. `search_metadata()` returns
`AssetCandidate` descriptions and has no way to return bytes. `open_payload()`
returns bytes and requires a `FetchAuthorization`, which cannot be constructed
directly — `BoundAssetManifest.authorization()` is its only mint. "Bind before
transfer" is therefore a type property, not a convention, and
`tests/test_stage5_integration.py` asserts the byte counters are still zero
when the availability snapshot freezes.

## What was built

- [`acquisition/manifest.py`](../acquisition/manifest.py) — `AssetRef`,
  `AssetManifest`, and a content-addressed `ManifestShardStore`. The manifest
  retains only shard digests and counters; assets stream one shard at a time
  through `iter_assets()`, so controller memory tracks shard count rather than
  asset count. The root commits to shard *order*, because assembly order is
  scientifically meaningful for a tiled artifact.
- [`acquisition/connector.py`](../acquisition/connector.py) — the provider
  boundary, `CredentialRef` (a reference, never a value), typed transient and
  permanent failures, and the unforgeable `FetchAuthorization`.
- [`acquisition/coverage.py`](../acquisition/coverage.py) — single-source
  containment with halo-aware selection. A hole is a typed `SPATIAL_GAP` or
  `TEMPORAL_GAP` and stops the binding; it is never interpolated across.
  Cross-provider mosaics are out of scope and a CRS mismatch is refused rather
  than reprojected.
- [`acquisition/session.py`](../acquisition/session.py) — durable planning
  sessions in SQLite: page cursors, discovered candidates, provider cooldowns,
  persisted truncation reasons, and a provider quota ledger. Each page and its
  cursor advance commit in one transaction, so a crash cannot leave a cursor
  past rows that were never stored.
- [`acquisition/search.py`](../acquisition/search.py) — the bounded fixed-point
  discovery loop and its closed second-order query registry.
- [`acquisition/binding.py`](../acquisition/binding.py) — `bind_manifest()`,
  `verify_binding()`, and `derive_exclusion_plan()`.
- [`acquisition/fetch.py`](../acquisition/fetch.py) — payload transfer, content
  verification, and the `FetchReceipt` that bridges to execution.
- [`acquisition/quarantine.py`](../acquisition/quarantine.py) — the
  `SnapshotIngestionPlan` path for sources that cannot version their bytes.
- [`acquisition/lowering.py`](../acquisition/lowering.py) — a bound manifest
  becomes an ordinary `CapabilitySpec`.

## Acquisition is not a special case in the resolver

Stage 4 lowered a transformation to a `CapabilitySpec` so that Stage 3 needed
no transform-specific code path. Stage 5 does the same thing for a bound
manifest: `lower_manifest_to_capability()` emits a normal costed capability
whose closed binder is `acquisition.materialize.bind.v1`. The resolver has no
idea acquisition exists. An acquired artifact, a declared conversion, and a
model producer all compete inside one global selection.

The `manifest_root` and `asset_ids` are *scientific parameters* of that
capability, which is how the exit gate "the bound derivation ID includes the
manifest identity" is satisfied without a new mechanism: parameters are already
part of invocation identity, and invocation identity is already part of the
plan.

A lowered manifest cannot be relabelled. The descriptor must agree with the
query that produced it on concept, schema version, units, and representation,
and it cannot declare `COMPLETE` missingness over a gap.

## Discovery is a loop, and it feeds the existing completeness channel

A producer's expansion can reveal a data question that did not exist when
planning started. The fixture's downscaling model needs support data over
*whatever extent the coarse source actually returned* — and the tiles overhang
the request, so that extent is unknowable in round zero. `AcquisitionSearch`
therefore runs rounds until no new query appears, and only then freezes.

Second-order queries come from a closed registry keyed by rule ID, exactly like
a binder or a unit conversion. A caller cannot supply a callable, so this is
not an escape hatch into discovery.

Every bound that can stop the loop early — page caps, asset caps, query caps,
round caps, connector deadlines, provider cooldowns, exhausted quota, a missing
connector — is typed, persisted to the session, and surfaced through
`AcquisitionExpansion.complete` and `.limit_codes`.

Those feed **the same** `upstream_discovery_complete` / `upstream_limit_codes`
inputs Stage 4 added. Stage 5 deliberately did not add a second channel:

```python
upstream = UpstreamCompleteness.merge_all((
    UpstreamCompleteness.from_layer(acquisition.complete, acquisition.limit_codes),
    UpstreamCompleteness.from_layer(closure.complete, closure_codes),
))
WorkflowResolver(catalog, snapshot, **upstream.resolver_kwargs()).resolve(roots)
```

[`resolution/upstream.py`](../resolution/upstream.py) folds layers by
conjunction of completeness and union of reasons, and refuses to assemble an
inconsistent pair — the same rule the resolver already enforces on its own
inputs. A truncated remote search cannot support a global-optimality claim, for
the same reason a truncated transformation closure cannot.

## Staleness ends a plan; it never substitutes

A bound asset that changed or vanished produces `BINDING_STALE` and a
`ExclusionChildPlan` recording which assets may not be used again and why. The
child plan carries no replacement, because choosing one is a planning decision
that belongs to a new session under review — not to a runtime that quietly
swaps its inputs.

A *transient* outage is treated as the opposite case: the provider blinked, so
the fetcher retries **the same binding**. The manifest root does not change and
no metadata search runs. The tests assert both halves of that distinction.

## Restart resumes; it does not repeat

A truncated search leaves its session open with cursors intact. A new process
opening the same database continues from the persisted page, and the provider
quota it already spent is still spent — a restart cannot buy extra budget by
forgetting. Only a *whole* search freezes the session, and re-freezing at a
different snapshot raises rather than silently replacing a frozen world.

Snapshot identity is over *what was found and whether the search was whole* —
manifest roots, completeness, and limit reasons — not over the pagination
history that produced it. Resuming an interrupted search and finding the same
assets yields the same snapshot ID, which is what makes a restart invisible to
science while remaining honest about truncation.

## Executable evidence

`scripts/run_stage5_demo.py` resolves one field requirement in `km.h-1` over a
4×4 region and a three-hour window, against:

| Alternative | Path | Cost |
|---|---|---|
| local pinned artifact | already in `km.h-1`, one asset | 8 |
| **remote archive** | **two coarse `m.s-1` tiles + declared conversion** | **3 + 2 = 5** |
| downscaling model | coarse + second-order support data | 3 + 1 + 4 = 8 |

| Result | Value |
|---|---|
| discovery rounds | 2 (the second-order query ran before freezing) |
| bound manifests | 3 |
| bytes transferred during planning | **0** |
| bytes transferred after binding | 685 |
| coverage | `COMPLETE` from two tiles |
| binding at transfer time | `FRESH` |
| resolution status | `READY`, validated, globally optimal |
| selected | `acquire:remote-tiled-archive:…` + `transform:example-mps-to-kmph` |
| selected cost | 5 (versus 8 for the pinned alternative) |
| manifest root in bound plan | yes |
| Stage-1 tasks / attempts | 2 / 2 |
| committed result | both tiles joined, then converted (10 → 36, 20 → 72 km/h) |
| run state | `SUCCEEDED` |

Run it only with a fresh node-local temporary directory:

```bash
runtime_root=$(mktemp -d /tmp/nasa-stage5-demo.XXXXXX)
.venv/bin/python scripts/run_stage5_demo.py --runtime-root "$runtime_root"
```

## Test coverage

- `tests/test_stage5_manifest.py` — manifest identity and order sensitivity,
  bounded shard streaming at 2,000 assets, content-addressed shard verification,
  single-axis coverage sweeps, spatial/temporal gaps, CRS refusal, halo
  selection.
- `tests/test_stage5_session.py` — cursor and candidate durability, quota shared
  across phases and surviving restart, cooldown persistence, persisted limits,
  freeze idempotence and conflict.
- `tests/test_stage5_binding.py` — `UNBINDABLE` classification, gap refusal,
  unforgeable authorization, staleness detection, exclusion child plans,
  quarantine content-addressing.
- `tests/test_stage5_search.py` — the two-round fixed point, second-order extent,
  restart resumption without re-pagination, every truncation code, the closed
  rule registry, and the upstream-channel fold.
- `tests/test_stage5_integration.py` — the vertical slice, transfer ordering,
  transient retry versus staleness, idempotent transfer, shared quota, secret
  hygiene, and lowering guards.

## Current limitations and non-claims

- **No real network provider was contacted.** Both connectors run in process.
  They implement the full contract — pagination, conditional identity, mutation,
  disappearance, outages — but a real HTTP/S3 connector is not written, and no
  claim is made about real provider behaviour, TLS, or authentication flows.
- Coverage is single-source and axis-aligned: tiles sharing a CRS and axis order
  that sweep one axis. Cross-provider mosaics, coverage atoms, and general
  polygon unions are not implemented.
- Manifest sharding is a two-level digest tree (shard digests → root), not a
  general Merkle structure, and there is no proof-of-inclusion API.
- The largest manifest exercised is 2,000 assets in tests. Million-asset
  discovery is not claimed.
- `acquisition.materialize.v1` reads a local content-addressed store whose
  *location* comes from the `NASA_STAGE5_ASSET_STORE` environment variable —
  a site property. *What* it reads is pinned by the manifest root, the asset
  list, and every blob's sha256, so results do not depend on the store path;
  but the operation is not pure over its parameters alone, and that is a
  deliberate, documented exception.
- Tiles are joined by ordered concatenation along one axis. Overlaps and holes
  raise rather than blend.
- Transfer is single-threaded and per-asset. There is no parallel fetch, range
  request, resume-mid-asset, or partial-object recovery.
- Provider quota is system-level within one SQLite database on one node. It is
  not a distributed or cross-host quota.
- Connector deadlines are wall-clock budgets checked between pages, not
  cancellation of an in-flight request.
- Evidence for an acquired artifact remains `evidence:unknown`. Stage 5 does
  not invent empirical error for a source, and dataset-versus-model evidence
  comparison is Stage 6.
- The Stage-1 external-committed-leaf bridge is still unimplemented. Stage 5
  routes around it by lowering acquisition to an executable capability rather
  than to an `ArtifactLeaf`; a selected external `ArtifactLeaf` feeding an
  invocation still fails closed with `BRIDGE_EXTERNAL_LEAF_UNSUPPORTED`.
- The representative Stage-6 planning-latency gate remains pending. The demo's
  measured planning time is not that SLO.
