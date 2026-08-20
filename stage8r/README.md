# Stage 8R — Cross-layer Soundness and Integration

Status: **bounded remediation and same-node operational closeout implemented;
real-site evidence remains external** (2026-08-18).

All eleven original frozen cases and four local operational-closeout cases in
`adversarial_matrix.json` now have ordinary passing regressions. The remaining
matrix item is explicitly `BLOCKED_EXTERNAL`: no authorized real Slurm site is
available on this node. The current honest status is:

| Track | Status | Boundary |
|---|---|---|
| A: planning authority | PASS, bounded | Mandatory declared discovery universe and independently replayed layers; exact contested arcs; final choice re-solves cannot change planning context. |
| B: acquisition identity | PASS, bounded same-node | Scope, transcript, coverage, descriptor, receipt, and blob identities replay. An OS-held manifest owner plus durable per-asset attempts/checkpoints prevents concurrent duplicate transfer and pre-charges every post-crash reopen. |
| C: coordinates | PASS, bounded | `field-json-v2`, exact component names, signed sample-centre grids, and Decimal block centres agree across plan/runtime/commit. WRF CRS establishment and external PROJ-grid byte identity are not claimed. |
| D: runtime recovery | PASS, bounded same-node | Packet results derive from the exact runtime run and commits; restart reservations and input receipts fence duplicates/substitution. An expired run with zero launch evidence is cancelled under the exclusive controller lock and safely released; exact terminal results remain recoverable after expiry. Runs with uncertain launch evidence remain fenced. |
| E: queued provider | PASS in the hermetic fake; deployment evidence PARTIAL | Controller-to-fake-Slurm worker/commit path and visibility/identity failures are tested. No real Slurm site was exercised. |
| F: Cube projection | PASS, bounded local pair | RuntimeStore commits project through a replayable outbox; a fresh Cube catalog rebuilds authority. Cross-database convergence is idempotent, not atomic. |

The latency/evidence decision has since been recorded: Stage 6 remains a
synthetic conformance fixture, and its 7.06 s p95 misses the 5 s budget by
1.41x. The next integration work is the general target-to-execution and
`ArtifactLeaf` external-input bridge, plus real-site evidence only when an
authorized site is available. Continue to describe optimality as relative to
the declared certificate-covered universe and source completeness as relative
to the durable connector transcript.

Stage 8R repairs facts that became optional when moving between otherwise
well-tested layers. It does not add a new model, connector family, scheduler,
or workflow feature. The master plan is
`nasa_project_docs/scientific_workflow_composition_plan.md`; this file records
the implementation evidence in the code repository.

## Safety boundary

- No WRF-SFIRE simulation.
- No real Slurm command, MPI job, or network provider.
- Preserve the user-owned WRF/configuration changes.
- A passing ordinary suite does not close an adversarial gate.
- Report each track independently; never call Stage 8R complete until A--F
  pass and G reconciles the evidence.

## Starting point

- Branch: `v2`
- Audited code head: `45aaee1`
- Historical aggregate counts are not current release evidence; run the full
  repaired tree before recording a new aggregate.
- Cube v3 is a tested immutable-entry sidecar, not authoritative publication.

The machine-readable case inventory is `adversarial_matrix.json`. Every case
must have a stable regression test before its implementation status becomes
`PASS`.

## Track order

1. **8R-0 — freeze failures.** Preserve each adversarial counterexample and
   the exact safe result it should eventually produce.
2. **8R-A — planning authority.** Mandatory discovery certificate,
   authenticated transformation certificate, and exact contested-use arcs.
3. **8R-B/C — data and coordinates.** Exact fetched-content binding plus one
   coverage/grid/CRS contract.
4. **8R-F — artifacts and Cube.** `ArtifactCommitter` remains authoritative;
   Cube entries are idempotent projections through an outbox.
5. **8R-D — runtime recovery.** Packet replay, retry budget, durable resource
   reservations, full-graph rank, and partition input binding.
6. **8R-E — queued-provider uncertainty.** Partial scheduler visibility never
   permits duplicate submission.
7. **8R-G — release evidence.** Reconcile all status documents only after
   Tracks A--F pass.

## Track A acceptance

- A truncated generated catalog without a matching certificate cannot return
  `READY`, global optimality, or execution eligibility.
- A raw capability cannot impersonate a reserved transformation operation;
  the forged metre-to-kilometre factor 666 case is rejected.
- A reported source alternative must satisfy the exact contested
  requirement-use/output arc, not merely appear elsewhere in the plan.
- Certificates are strict, content-addressed, round-trip verified, and included
  in resolution/plan provenance.

## Historical implementation notes

The sections below record how the individual corrections landed.  Where an
early note calls a later track `OPEN`, the status table above and the
machine-readable adversarial matrix supersede it. Missing real held-out
scientific evidence and real-site deployment measurements remain non-claims;
the soundness work does not manufacture either one.

## Track C correction — canonical grid/coordinate contract

Track C is closed for the bounded local field representation. The one named
convention is `sample-centres-axis-aligned-v1`:

- `GridDescriptor.affine` is `(dx, 0, first_x_centre, 0, dy,
  first_y_centre)`, `shape` is row-major `(y, x)`, and `axis_order` names the
  semantic `(x_axis, y_axis)` pair;
- signed non-zero steps are authoritative. A north-first raster has `dy < 0`,
  while a WRF array whose `south_north` index increases northward has `dy > 0`;
  replay must preserve the producer's actual row order;
- spatial-support bounds are the outer cell edges, half a step beyond the
  first and last centres; and
- field payloads carry the exact axis names, while compile-time output
  configuration binds axis names, shape, affine convention, affine, support
  bounds, temporal lattice, and components into authoritative commit
  validation.

This is a load-bearing schema boundary rather than a side validator.
Executable gridded outputs use `field-json-v2`; the compiler refuses legacy
`field-json-v1` and any v2 descriptor without an exact grid. Frozen Stage-5
source schemas must publish that grid, and a fetched tile set describes the
complete materialized lattice (including deterministic tile overhang), not a
caller-authored request box. The real Stage-5 acquisition/conversion and
Stage-6 dataset/model workflows compile their field outputs to
`stage8r.field-json@2` validation and execute through that commit gate.

Subset execution preserves the indexed centres. Exact block aggregation emits
the mean centre of each complete block and its descriptor must name those same
centres; partial trailing blocks are rejected. Bilinear operations accept
either signed orientation and reject extrapolation outside source sample
centres even when a point is inside the outer cell edge. Temporal alignment
now bounds the last selected index, `start + (count - 1) * step`, rather than
the off-by-one `start + count * step`. Series field coordinates use the
half-open descriptor lattice `[start, end)`; the Stage-5/6 executable fixtures
were corrected to stop emitting an extra sample at `end`.

Reprojection planning requires PROJ's preferred operation to be locally
available, transforms every declared target sample during preflight, and
freezes the selected target-to-source PROJJSON in transformation identity.
Runtime replays that exact pipeline. Component identity additionally binds the
pyproj, PROJ, EPSG-database, and PROJ-database versions.

The WRF configuration-only preflight reads declared domain dimensions and fire
refinement without launching WRF. It can reject an impossible publication
shape early, but explicitly reports that shape agreement does **not** establish
a CRS or origin; output georeferencing still requires verified WRF metadata.

Track-C non-claims are precise: the local `field-json-v2` executor supports
axis-aligned rectilinear grids, not rotated/skewed grids; PROJJSON and database
versions are bound, but the bytes of external PROJ grid-shift files are not
independently content-hashed; and no WRF, MPI, Slurm, or network operation was
run. Regression evidence is in `tests/test_stage8r_grid_contract.py`,
`tests/test_stage4_artifact_validation.py`, and
`tests/test_wrf_publication_preflight.py`; the load-bearing Stage-5/6 compiler
assertions are in their respective integration suites.

## Track F correction — authoritative artifact-to-Cube projection

Track F is closed for the bounded local RuntimeStore/Cube pair. Public
`Catalog.commit_entry(CubeEntry)` now refuses caller-authored content digests
and producer strings. The Stage-2 compiler instead binds the complete verified
`ArtifactDescriptor` snapshot and identity, bound-plan ID, invocation ID, and
output port into the Stage-1 output recipe. The authoritative validator copies
that exact binding into its passed validation receipt; it is never inferred
from result bytes.

`CubeProjector` reconstructs each projection solely from the immutable bound
graph, `artifact_commits`, the matching passed validation, and the committed
manifest/object bytes. A private projection authority is minted only after the
manifest is re-read and the object bytes are re-hashed; an internally
self-consistent caller-authored projection cannot publish an entry. Input edges
are reconstructed from the bound graph's
ports and exact upstream RuntimeStore artifact slots, then resolved to their
authoritative Cube projection receipts. Precommitted external inputs retain
the exact source-run, recipe, artifact, and entry identities rather than being
relabelled as products of the consuming run. One DuckDB transaction commits
the entry, all lineage edges, and a content-addressed projection receipt. A
separate SQLite outbox is acknowledged only afterward. Replaying a crash after
either database boundary converges without duplicate entries; contradictory
receipts fail closed. Repeated identical derivations across runs reuse the
immutable entry but retain distinct per-run authoritative receipts.

The load-bearing integration test executes and commits a two-task, non-WRF
metre-to-kilometre chain whose input and output share the same scientific
concept, then projects both entries and verifies the exact lineage. Crash,
replay, conflict, cross-run reuse, compiler-binding, and raw-publication tests
are in `tests/test_cube_projection.py` and
`tests/test_stage8r_open_gates.py::test_cube_authority_gate`.

Track-F non-claims are precise: Cube remains a rebuildable read model rather
than the dependency-release authority; projection is an explicit local
service call rather than an automatically configured controller sink; the
current artifact format is the runtime's validated JSON object format; and no
claim is made for distributed object-store/DuckDB transactions or recovery
after loss of the local durable files. The repaired compilation path uses a
private process-local authority after exact graph replay; it is not a
cryptographic signer or a distributed authorization protocol.

## Early Track D correction

The packet-result idempotency gate is already closed independently of the
remaining runtime work. A durable packet-result receipt makes exact replay a
no-op, contradictory outcomes for one attempt fail closed, and
`NOT_ATTEMPTED` leaves the member admitted without consuming retry budget.
Focused regressions cover the Stage-7 admission and Stage-8R gate files. This
historical subsection does **not** by itself close restart reservation recovery,
full-graph live ranking, or partition-specific scientific input binding.
