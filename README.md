# A Pluggable HPC System for Asteroid-Impact Cascade Modeling

**Ask for a scientific outcome. Get an admissible, costed, reproducible workflow — composed automatically.**

An asteroid airburst over a city is not one simulation. It is a cascade: a
thermal pulse ignites fuels, a fire spreads under real weather, smoke loads the
atmosphere, blast damages structures, and losses propagate through exposed
population and assets. Each link needs different data, at different resolutions,
from different providers, and each can be satisfied by a direct dataset *or* by
another model.

Assembling that by hand does not scale, and the hand-assembled version hides the
decisions that matter. This project builds the system that assembles it for you
— and refuses to lie to you about what it assembled.

```
     "economic loss from a 5 Mt airburst over Dallas"
                          │
                          ▼
      ┌─────────────────────────────────────────┐
      │  what would satisfy this, scientifically?│   contracts + capabilities
      └─────────────────────────────────────────┘
                          │
                          ▼
      ┌─────────────────────────────────────────┐
      │  every admissible derivation, recursively│   finite hypergraph
      │  data ─ transform ─ model ─ another model│
      └─────────────────────────────────────────┘
                          │
                          ▼
      ┌─────────────────────────────────────────┐
      │  ONE globally consistent, cheapest plan  │   exact MILP selection
      └─────────────────────────────────────────┘
                          │
                          ▼
      ┌─────────────────────────────────────────┐
      │  independently re-validated, then frozen │   solver output is untrusted
      └─────────────────────────────────────────┘
                          │
                          ▼
      ┌─────────────────────────────────────────┐
      │  durable execution → validated commit    │   laptop or allocated node
      └─────────────────────────────────────────┘
```

Nothing in that pipeline knows what "asteroid", "fire", or "wind" *mean*. The
domain enters entirely through declared contracts and capabilities — which is
precisely why a second application can reuse it.

---

## The test application: the Dallas impact cascade

The driving scenario is a 5 Mt airburst centered on Dallas, TX
(`-96.81, 32.78`). It is a good stress case because its links disagree with each
other about almost everything:

| Link | Needs | Competing ways to satisfy it |
|---|---|---|
| Ignition | thermal fluence → per-cell ignition time | KML isodose rings, scaling laws |
| Fuel | Anderson-13 fuel category @ 30 m | LANDFIRE, local fuel map |
| Terrain | elevation, slope, aspect | USGS 3DEP, Copernicus DEM |
| Weather | wind at the fire's grid and cadence | direct high-res analysis, coarse reanalysis **+ downscaling**, or a full atmospheric model |
| Fire | arrival time, area, intensity | WRF-SFIRE |
| Smoke | PM2.5, tracer loading | WRF-Chem coupled to the fire |
| Damage | overpressure → damage fraction | blast scaling + vulnerability curve |
| Loss | economic loss, population exposure | HAZUS-style model over exposure data |

Wind is the deliberately contested dependency. A 31 km reanalysis field
interpolated onto a 3 km grid **is not** a 3 km observation, and the system is
built so that distinction survives all the way into the plan rather than being
quietly lost inside an adapter.

### What runs today

```bash
# the consequence half of the cascade, end to end, no network, no HPC
.venv/bin/python scripts/run_stage0_baseline.py \
    --workspace /tmp/ws --manifest /tmp/cascade.json
```

```
exposure ───────────────┐
                        ├──▶ econ_loss ──▶ economic_loss_usd
impact_scaling ──▶ blast_damage           population_exposure
```

Four producers resolve, execute, and commit with a manifest recording every
component version, configuration hash, input, output digest, and code revision.
Physical sanity is asserted, not assumed: damage stays in `[0,1]`, center
overpressure exceeds the corner, loss is positive.

**This is an orchestration conformance fixture, not validated science.** The
exposure data is synthetic and the loss formulas are simplified. It proves the
machinery, and the manifest says so in the file itself
(`"scientific_status": "conformance_only_not_validated"`).

---

## Why automatic composition is the hard part

The naive resolver — walk backward from the target, pick the cheapest producer
for each input — is *wrong*, and wrong in a way that is easy to miss.

**Shared inputs.** Two models both need terrain. Locally, each avoids an
expensive DEM. Globally, buying that DEM once is cheaper than two private
substitutes. Per-requirement greedy selection cannot see this.

**Co-production.** One atmospheric invocation yields wind, temperature, and
humidity together. Its cost must be counted **once**, no matter how many
downstream requirements consume its outputs.

So selection is split in two: recursive discovery builds a finite hypergraph of
*everything admissible*, then a single exact optimization picks one globally
consistent subgraph over the whole graph at once. The counterexample is a
regression test, not a footnote — see
[`tests/test_stage3_milp.py`](tests/test_stage3_milp.py).

Three rules keep the result honest:

1. **The solver is not trusted.** An independent validator replays every
   compatibility proof, re-checks grounding, acyclicity, cost, evidence,
   sharing, and deployment feasibility. A plan that fails cannot bind.
2. **Truncated search never claims a global optimum.** Any depth, candidate, or
   state bound that fires downgrades the result to
   `FEASIBLE_NOT_PROVEN_OPTIMAL` and names what was dropped. Infeasibility over
   a truncated universe is reported as `INCOMPLETE`, never as proof that no
   derivation exists.
3. **Unknown stays unknown.** The system never invents a quality number from a
   trust label, and never relabels a model output as an observation.

---

## Transformations are first-class, not adapter magic

A metre descriptor does not satisfy a kilometre requirement. Compatibility
matching stays pure and inserts nothing — instead the conversion is a declared,
versioned, independently costed node that competes with direct data:

```bash
.venv/bin/python scripts/run_stage4_demo.py --runtime-root "$(mktemp -d)"
```

Offered direct kilometres at cost 9 and metres at cost 4, the resolver selects
**metres + an explicit cost-1 conversion (total 5)**, compiles it to two tasks,
runs them, and commits `1.5` from a `1500 m` source. Unit coefficients come only
from a closed versioned registry — a caller cannot supply its own factor.

The same guards refuse scientifically dishonest edges at construction time:
outputs must declare `DERIVED` origin, a unit conversion may change only units,
regridding may not change CRS while reprojection must, and bilinear
interpolation is admitted only for explicitly typed continuous/intensive fields
— so categorical fuel classes are excluded rather than silently interpolated.

Because origin is a consumer constraint, a request restricted to `SYNTHETIC`
origin correctly falls back to the expensive direct source instead of
transforming. That is the whole point: the workflow is automatic, but the
science stays the consumer's decision.

---

## Build status

The system is being built in exit-gated stages. Each stage ends with something
runnable, and no stage may claim capability it has not demonstrated.

| Stage | Delivers | State |
|---|---|---|
| [0](stage0/) | Frozen baseline, cluster/software audit, kernel invariants | done |
| [0A](stage0a/) | Runtime build-vs-buy spike (own controller + local provider) | done |
| [1](stage1/) | Durable local kernel: leases, fencing, restart, atomic commit | done |
| [2](stage2/) | Scientific contracts, multi-producer catalog, exhaustive oracle | done |
| [3](stage3/) | Recursive discovery, exact global MILP selection, validator | done |
| [4](stage4/) | Explicit semantic transformations + finite closure | done |
| [5](stage5/) | Progressive acquisition: in-process connectors, coverage, asset manifests | prototype |
| [6](stage6/) | Dataset-versus-model slice, evidence gating, `CHOICE_REQUIRED` | prototype |
| [7](stage7/) | Lazy partitions, bounded atomic admission, durable retry | control plane |
| [8](stage8/) | Resource-aware scheduling policy, now driving the controller | policy + bridge |
| [9A](stage9a/) | Conditional SLURM provider: token recovery, batched reconcile | simulated only |
| [8R](stage8r/) | Cross-layer authority, replay, recovery, and projection remediation | bounded local gates pass; real-site evidence external |
| [10A](stage10a/) | Automatic native-artifact registration and target-time discovery | bounded local implementation |
| [10B](stage10b/) | Durable targets, output-event replay, and automatic re-planning | bounded local implementation |
| [10C](stage10c/) | Stage-1 commit to durable native-artifact event bridge | bounded local implementation |
| 9B | WRF integration behind a certified provider | |

Stages 5–8 are deliberately labelled below "done". An external audit found
several claims running ahead of the implementation; those defects are fixed and
the labels now match what is demonstrated. The honest reading:

- **Stage 5** contacts no real network provider; both connectors run in process.
- **Stage 6** competes a model against data on synthetic evidence. No real
  held-out reference observations exist, and its Section 9.5 planning-latency
  gate is **measured and missed** by roughly 5x.
- **Stage 7** drives 10^4 partitions through bounded control-plane admission.
  The Stage-8R bridge also executes bounded partitions through Stage 1 with
  exact committed input receipts; it is not yet the million-partition design.
- **Stage 8** is a scheduling policy plus a real bridge into the durable
  controller: concurrent attempts under a reservation ledger, measured on real
  subprocesses. Its three-policy makespan comparison remains a simulation.
- **Stage 9A** has never talked to a real scheduler. Its complete
  controller→fake-`sbatch`→worker→artifact path is hermetic conformance
  evidence, not deployment validation.
- **Stage 8R** closes the frozen cross-layer false-success cases. Same-node
  fetch ownership/checkpoints now preserve quota across crash/concurrency, and
  expired packets can release only after an exclusive zero-launch proof while
  exact terminal runs remain recoverable. Real-site deployment evidence is
  still external and unclaimed.
- **Stage 10A** keeps native files at their producer locations. A complete
  typed `ArtifactRecord` is verified and indexed automatically when a
  `DatasetRef` arrives; each target request refreshes those records and injects
  compatible committed artifacts into the normal global resolver. It does not
  copy, transform, reproject, or store the payload in Cube/Zarr.
- **Stage 10B** persists typed target requests and native-output events. A
  crash-safe `PENDING -> REGISTERED -> APPLIED` replay loop registers each
  content-addressed artifact idempotently, automatically re-resolves durable
  targets, and writes a portable identity-checked workflow manifest. Metadata
  search is indexed and snapshot verification re-hashes on stat change. The
  current bridge is same-node SQLite/local files; Stage-1 binary outputs do not
  emit those records themselves.
- **Stage 10C** connects the event stream to the authoritative Stage-1 commit.
  A closed native-file pointer operation validates producer-owned bytes, the
  controller commits its receipt under the normal fence, and a replayable
  observer binds the exact scientific descriptor and runtime lineage before
  emitting Stage 10B events. The native payload remains at its original path.

```bash
.venv/bin/python -m pytest -q tests/test_stage*.py tests/test_cube*.py
```

Use [`stage8r/adversarial_matrix.json`](stage8r/adversarial_matrix.json) for
the stable cross-layer gates. The complete repository suite currently stalls
in an older Cube/critic test on this NFS workspace, so the handoff reports
split subsystem evidence rather than inventing one aggregate green count.

---

## What this does not claim

Stated plainly, because a workflow system that overstates itself is worse than
none.

- **WRF-SFIRE has not been run through the v2 stack.** It remains a heavy
  example model. A known open blocker is recorded in
  [`stage0/wrf_interface_audit.md`](stage0/wrf_interface_audit.md): fire-domain
  output at `(253,253)` has no defined placement onto a `(1001,1001)` cube grid.
- **Idealized and real-data fire modes are different contracts.** Real-data WRF
  needs a full 3-D meteorological boundary collection; it must never be
  simplified to "10 m wind".
- **No real remote provider was contacted.** Stage 5 implements bounded
  metadata discovery, manifests, coverage, receipts, and payload identity with
  in-process connectors. Completeness is relative to its durable connector
  transcript, not a claim about an open provider's global catalog.
- **Cost is the only automatic objective.** Quality, latency, and Pareto
  ranking are deferred; transformation loss is visible but not optimized.
- **SLURM is unavailable on this development node** and is a future conditional
  provider, not a current dependency. Durability is same-node process recovery,
  not node-loss durability.
- **Agentic/LLM planning is out of scope** for the current architecture. The
  legacy [`agentic/`](agentic/) layer predates it and is parked, not extended.

---

## Setup

```bash
./setup.sh                                              # creates .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests/ -q
```

Run any demo with a **fresh node-local** runtime root (never on NFS/Lustre —
the controller's SQLite/WAL is only verified on local POSIX storage):

```bash
runtime_root=$(mktemp -d /tmp/nasa-demo.XXXXXX)
.venv/bin/python scripts/run_stage3_demo.py --runtime-root "$runtime_root"
.venv/bin/python scripts/run_stage4_demo.py --runtime-root "$runtime_root"
```

## Layout

```
contracts/      artifact descriptors, requirements, evidence, pure matching
capabilities/   immutable multi-producer / multi-output catalog, deployment
transformations/ explicit semantic transforms + finite reachability closure
resolution/     recursive discovery, exact global selector, independent validator
composition/    exhaustive correctness oracle, blocker trees, Stage-1 compiler
plans/          candidate / bound / deployment derivation identities
engine/runtime/ durable controller, attempts, leases, fencing, atomic commit
stage0..stage4/ per-stage evidence, fixtures, and runnable demonstrations

cube/           legacy Zarr + DuckDB gridded artifact backend (retained)
engine/         legacy orchestration substrate (retained as baseline)
drivers/        data-source fetchers (thermal, LANDFIRE, DEM, ERA5)
models/         WRF-SFIRE adapter, namelist generation, consequence models
agentic/        legacy LLM planning layer (parked; out of scope for v2)
```

For the durable engineering handoff — invariants, per-stage detail, and the
next stage's design constraints — see [`codex_handoff.md`](codex_handoff.md).
