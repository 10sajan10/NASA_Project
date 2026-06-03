# Predicting the Cascading Effects of an Asteroid Impact
### A pluggable, infrastructure-independent multi-model system
*4-slide deck · diagrams + speaking notes · audience: NASA*

---
---

## SLIDE 1 — Why: one impact, a cascade of consequences

```
                          ☄  ASTEROID IMPACT
                                  │
              ┌───────────────────┼───────────────────┐
              ▼                   ▼                   ▼
        thermal pulse        blast / seismic      ejecta / dust
              │                   │                   │
              ▼                   ▼                   ▼
        ┌──────────┐        ┌──────────┐        ┌──────────┐
        │ WILDFIRE │ ─────► │  SMOKE / │ ─────► │   AIR     │
        │          │        │ AEROSOLS │        │  QUALITY  │
        └────┬─────┘        └──────────┘        └──────────┘
             │                                        │
             ▼                                        ▼
        ┌──────────┐                            ┌──────────┐
        │ FLOODING │                            │  HEALTH / │
        │ (burn    │                            │ EXPOSURE  │
        │  scars)  │                            └──────────┘
        └──────────┘
        each arrow = one model's output is the next model's input
```

**The point**
- A single impact triggers **interacting** physical processes — fire feeds
  smoke feeds air quality; burn scars change flooding.
- The science lives in **separate, mature models** (WRF-SFIRE, chem
  transport, hydrology) built by different communities, in different
  languages, for different machines.
- No one tool captures the cascade. Stitching them by hand is fragile,
  one-off, and unreproducible.

**What we built**
> A system where **any model plugs in**, **shares one data catalog**, and
> **automatically pulls in whatever upstream models it depends on** — so
> the cascade runs end-to-end and reproducibly.

### 🎤 Speaking notes
"An asteroid airburst doesn't cause one effect — it sets off a chain.
The thermal pulse ignites fires; fires loft smoke; smoke degrades air
quality; burn scars later drive flooding. Each link is a serious model
in its own right, maintained by a different community. Today, coupling
them is heroic, manual, and not repeatable. Our goal was to make the
**cascade itself** a first-class, reproducible object — plug models in,
let the system wire them together. I'll show the abstraction that makes
that possible, then walk it through with a fire model and a smoke model."

---
---

## SLIDE 2 — The abstraction: everything is a *producer*

```
   ┌──────────────────────────────────────────────────────────────┐
   │  ONE CONTRACT FOR EVERYTHING                                  │
   │                                                              │
   │     produces : the variables I can make                      │
   │     requires : the variables I need first                    │
   │     run()    : go make them                                  │
   └──────────────────────────────────────────────────────────────┘

      data driver                         model
   (external bytes → var)          (vars → var, by computing)
   ┌───────────────────┐           ┌───────────────────────┐
   │ requires: (none)  │           │ requires: fuel, wind, │
   │ produces: wind    │           │           ignition    │
   └───────────────────┘           │ produces: fire_arrival│
            same shape  ◄────────►  └───────────────────────┘
                   the engine cannot tell them apart


   ┌───────────────────── THE ENGINE (model-agnostic) ───────────────────┐
   │                                                                     │
   │   ask for a target variable                                         │
   │        │                                                            │
   │        ▼                                                            │
   │   resolve(): is it in the DATA CATALOG, fresh & fine enough?        │
   │        ├─ yes ─────────────────────────────► use it                 │
   │        └─ no  ─► find the producer of it ─► run it ─► re-check       │
   │                       (driver OR model — identical handling)        │
   │                                                                     │
   │   → recursively builds the dependency DAG and executes it           │
   └─────────────────────────────────────────────────────────────────────┘
                 │                                   │
        ┌─────────▼─────────┐               ┌─────────▼──────────┐
        │   DATA CATALOG    │               │  INFRASTRUCTURE     │
        │  single source    │               │  auto-detect HPC,   │
        │  of truth; every  │               │  same code runs on  │
        │  var at native    │               │  any cluster        │
        │  resolution +     │               │  (laptop→supercomp) │
        │  lineage          │               └─────────────────────┘
        └───────────────────┘
```

**Three abstractions, nothing model-specific**
1. **Producer** — uniform contract (`produces` / `requires` / `run`).
   Data sources and models are the *same kind of thing*.
2. **Data catalog** — one place all producers read/write; the system's
   memory and reproducibility anchor.
3. **Resolver** — given a goal, finds and runs whatever satisfies it,
   recursively. It never knows about fire, smoke, or floods.

### 🎤 Speaking notes
"Here's the whole idea on one slide. We force *everything* — every data
source and every model — through one tiny contract: declare what you
produce, what you require, and how to run. The engine only reads that
contract; it has zero domain knowledge. When you ask for a result, the
resolver checks the shared data catalog: if the data is already there,
fresh, and at sufficient resolution, it's reused; otherwise it finds the
producer that makes it and runs it — and that producer might be a data
download *or* another model. Same handling either way. That symmetry is
the trick: it's what lets a model transparently trigger another model.
And because models never name a machine, the same run executes on a
laptop or a supercomputer — the infrastructure layer picks the launcher."

---
---

## SLIDE 3 — Walkthrough: WRF-SFIRE (model 1) → smoke_model (model 2)

```
  GOAL the user asks for:   smoke_concentration
  ───────────────────────────────────────────────────────────────────────

  the engine walks requires → produces, backwards, and DISCOVERS the chain:

     ┌──────────────┐   ignition  ┐
     │ thermal pulse│────────────┐│
     │  (from KML)  │            ││
     └──────────────┘            ▼▼
     ┌──────────────┐ fuel   ┌─────────────────┐ fire_arrival ┌────────────────┐
     │  fuel map    │───────►│   WRF-SFIRE     │─────────────►│  smoke_model   │──► smoke_
     └──────────────┘        │   (MODEL 1)     │ burned_area  │  (MODEL 2,     │  concentration
     ┌──────────────┐ wind   │  fire spread +  │─────────────►│  hypothetical) │
     │  ERA5 wind   │───────►│  atmosphere     │              │  plume + chem  │
     └──────────────┘   ▲    └─────────────────┘      ▲       └────────────────┘
     ┌──────────────┐   │                             │
     │  terrain DEM │───┘                  wind ──────┘  (reused from catalog,
     └──────────────┘                                     already computed!)

  EXECUTION ORDER the engine derives (topological):
     1) thermal, fuel, wind, dem      (independent data drivers, in parallel)
     2) WRF-SFIRE                      (consumes 1)
     3) smoke_model                   (consumes WRF-SFIRE's output + wind)
```

**What the user did vs. what the system did**

| User | System (automatic) |
|------|--------------------|
| `--target smoke_concentration` | discovered smoke_model needs `fire_arrival` |
| (nothing else) | discovered WRF-SFIRE produces `fire_arrival` |
| | ran data drivers → WRF-SFIRE → smoke_model, in order |
| | **reused** `wind` for smoke_model (already in catalog) |

**Plugging in model 2 — the entire integration:**
```
   1. declare its contract:   requires = {fire_arrival, wind}
                              produces = {smoke_concentration}
   2. register it:            catalog.add_model("smoke", …)      ← one line
   → the engine wires the cascade. No engine code changes.
```

### 🎤 Speaking notes
"Let's make it concrete. The user asks for *smoke concentration* —
nothing else. The engine reads smoke_model's contract, sees it needs
fire arrival time, finds that WRF-SFIRE produces it, sees WRF-SFIRE needs
ignition, fuel, wind, terrain — and pulls those from data drivers. It
then runs everything in dependency order: data first, then the fire
model, then the smoke model. Notice the wind field: WRF-SFIRE already
used it, so when smoke_model also needs it, the catalog just hands back
the cached copy — no recompute. To add the smoke model, a scientist
writes its three-line contract and registers it with one line. They
never touch the engine, the catalog, or the fire model. That's the
'plug a model' promise — and it's exactly how the third, fourth, tenth
model in the cascade will go in."

---
---

## SLIDE 4 — The hard part & where we're going

```
  THE CENTRAL CHALLENGE:  "is it already in the system?"
  ──────────────────────────────────────────────────────────────
  Before running an expensive model, decide: REUSE or RECOMPUTE?

        request ──►  ┌───────────────────────────────────────┐
                     │  catalog holds the variable …          │
                     │     • for THIS time window?     ───────┼─► time-aware
                     │     • at FINE-ENOUGH resolution? ──────┼─► resolution-aware
                     │     • from CURRENT inputs (not stale)? ┼─► dirty-tracking
                     │     • provenance matches config?  ─────┼─► lineage / hash
                     └───────────────────────────────────────┘
                          all yes → REUSE        any no → RECOMPUTE
                                                  (and cascade the
                                                   invalidation downstream)

   Get this WRONG one way → silently use stale/coarse data (bad science)
   Get this WRONG the other → recompute a 50,000-core run needlessly
```

**Why this is the real research problem**
- The abstraction is easy to state, hard to make *trustworthy*.
- "Satisfaction" is **multi-dimensional**: time coverage × spatial
  resolution × input freshness × provenance — not just "does the key
  exist?"
- Changing one upstream input must **invalidate exactly** the affected
  downstream products — no more, no less.

**Design stance (abstractions over implementations)**
- Producers declare *requirements* (e.g. "≤ 100 m"), not procedures.
- The catalog records *native resolution + lineage* per variable.
- The resolver decides reuse vs. recompute from declared metadata —
  so the policy lives in **one place**, not smeared across every model.

**Roadmap**
```
  now ──────────────────────────────────────────────────────────────►
  ✔ uniform producer contract        ◻ richer "satisfaction" calculus
  ✔ recursive cascade resolution     ◻ partial / tiled reuse across nests
  ✔ data catalog + lineage           ◻ more models: smoke, flood, exposure
  ✔ infrastructure independence      ◻ cross-model uncertainty propagation
  ✔ WRF-SFIRE = model 1, validated
```

### 🎤 Speaking notes
"Finally, the honest part — the hard problem. The abstraction is simple
to describe; making it *trustworthy* is the research. The crux is
deciding, before launching an expensive model, whether the data we need
is *already in the system*. That's not 'does the key exist' — it's:
do we have it for this time window, at fine-enough resolution, derived
from the current inputs, with matching provenance? Err one way and you
silently run on stale or too-coarse data — bad science. Err the other
and you burn a fifty-thousand-core run you didn't need. Our stance is to
keep this as an *abstraction*: producers declare requirements, the
catalog records resolution and lineage, and one resolver makes the
reuse-versus-recompute decision from that metadata — so the policy lives
in a single place, not scattered through every model. We've got the
contract, the recursive cascade, the catalog, and infrastructure
independence working, with WRF-SFIRE as the validated first model. Next
is a richer satisfaction calculus, partial reuse across nested grids,
and more models in the chain. Thank you — happy to go deeper on any
layer."
```
```
