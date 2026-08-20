# Target-Driven Composition and Orchestration for Scalable Asteroid Cascade-Hazard Workflows

**Technical report for NASA — August 2026**<br>
**Status:** bounded research prototype and forward plan

## Executive summary

Asteroid impacts and airbursts are not isolated events. A complete assessment
may connect uncertain impact conditions to blast and thermal effects, ignition,
terrain and fuels, weather, wildfire spread, smoke, infrastructure disruption,
population exposure, and economic loss. These components are maintained by
different communities, consume different data, use different spatial and
temporal conventions, and may run on resources ranging from a workstation to a
supercomputer. The practical bottleneck is therefore not only running a model;
it is finding and connecting the right data and models into a scientifically
defensible, reproducible cascade.

This project investigates a target-driven workflow composer. A domain expert
specifies the desired scientific outcome and its space, time, resolution, and
admissibility constraints. The system searches verified native artifacts,
discovers declared producers for missing inputs, constructs the possible
derivations, selects one consistent workflow over the complete bounded search
space, independently validates that selection, and records execution and output
provenance. Validated outputs become searchable and can trigger re-planning
without requiring every file to be copied, reprojected, or converted into a
central data cube. A bounded local application service now carries one durable
target through exact binding, compilation, authorization, execution, native
publication, and feedback when the selected workflow uses closed supported
operations.

The motivating application is a hypothetical asteroid-airburst cascade near
Dallas, with WRF-SFIRE as the atmosphere–wildfire component. The present system
is a bounded local prototype: its contract, planning, artifact, and orchestration
mechanisms have conformance evidence, but a WRF-SFIRE run, a live NASA data
provider, and a real HPC deployment have not yet been completed through the
current path.

## 1. The problem: composing a cascade, not a single model

NASA’s Center for Near-Earth Object Studies (CNEOS) computes orbit and impact
risk information and exposes continually updated results through web services
and APIs [1]. NASA’s Asteroid Threat Assessment Project (ATAP) combines
probabilistic assessment with high-fidelity simulations for atmospheric entry,
blast, thermal radiation, tsunami, and global effects [2]. This project addresses
a complementary question: how can an evolving impact scenario be extended
reliably into downstream, location-specific cascading consequences?

Four barriers make that extension difficult:

1. **Fragmented resources.** Relevant observations, terrain, atmosphere, land
   surface, fuels, exposure data, and models are distributed across providers
   and institutions. NASA’s Common Metadata Repository (CMR) demonstrates the
   value of unified, standards-based spatial, temporal, and faceted discovery
   across large Earth-science holdings [3], but each workflow must still bind
   the selected records to the models that can consume them.
2. **Scientific incompatibility.** Two products with the same informal name may
   differ in units, reference system, grid, time support, vertical reference,
   native resolution, origin, missingness, uncertainty, or evidence. A filename
   or array shape is not a sufficient interface contract.
3. **Global workflow decisions.** Multiple models may satisfy one requirement;
   several downstream models may share an input; and one expensive simulation
   may produce several useful outputs. Selecting each input independently can
   miss the best consistent workflow.
4. **Operational complexity.** Long-running, partitioned, or ensemble workflows
   require resource admission, dependency-aware scheduling, restart and retry,
   validation, and provenance that survives failures.

> The core systems problem is to connect scientific intent to distributed data,
> models, and computing resources without hiding incompatibility or losing
> accountability.

## 2. High-level system concept

The design uses five abstractions:

- A **scientific target** states the requested result and its admissibility
  constraints.
- An **artifact** binds scientific meaning and lineage to verified native bytes.
- A **capability** declares a reviewed way to produce a missing artifact, such
  as a data source or model.
- A **workflow plan** selects one consistent set of artifacts and producer
  invocations.
- An **authoritative commit** marks the point at which a validated output can be
  reused safely.

The implementation separates these abstractions into layers with narrow
authority boundaries. No single layer is allowed to interpret a filename,
declare scientific compatibility, choose a workflow, run arbitrary code, and
publish a result by itself.

| Layer | Representative implementation objects | Primary responsibility |
|---|---|---|
| Scientific contracts | `ArtifactDescriptor`, `Requirement`, `RequirementUse`, `CompatibilityProof` | State scientific meaning and prove direct admissibility |
| Artifact control plane | `ArtifactRecord`, `ArtifactRegistry`, immutable snapshots | Verify native bytes; index metadata, identity, availability, and lineage |
| Producer and discovery plane | `CapabilitySpec`, `CapabilityCatalog`, discovery universe and certificates | Declare reviewed producers and the finite universe that was searched |
| Resolution and planning | feasible derivation hypergraph, `WorkflowResolver`, candidate/bound/deployment plans | Discover alternatives, select a derivation, and validate it independently |
| Application authority | `TargetExecutionContext`, `TargetExecutionService`, terminal execution receipt | Freeze an eligible target context and launch one deterministic run |
| Execution authority | compiled execution graph, `WorkflowController`, `ArtifactCommitter` | Enforce state, attempts, resources, output validation, and atomic publication |
| Feedback control plane | runtime artifact bridge and target coordinator | Register committed outputs and re-evaluate durable targets |

```text
Domain expert's target
          |
          v
Verified artifact and resource discovery
    | available              | missing
    v                        v
native data reuse       declared producers
    \                        /
     v                      v
 recursive workflow composition
          |
          v
global selection over the bounded graph
          |
          v
independent validation and orchestration
          |
          v
native outputs + metadata + provenance
          |
          +----> searchable reuse and target re-planning
```

The system begins with the result, rather than a prewritten execution script.
Its registry searches scientific metadata including concept, units,
representation, coordinate reference system, space and time support, cadence,
resolution, uncertainty, evidence, producer, and lineage. A compatible existing
artifact can satisfy the target in place. Missing requirements open a recursive
search through declared producers.

The resulting structure is a hypergraph rather than a simple pipeline: inputs
can be shared, alternatives can compete, and a producer can emit several
outputs. The planner selects a minimum-declared-cost derivation over the frozen,
finite discovery universe, and a separate validator rechecks the selected
edges, grounding, deployment facts, and constraints. If discovery reaches a
limit, the result is reported as incomplete rather than being called a global
optimum over unknown producers.

Outputs remain in producer-owned native files. The prototype records a verified
pointer, content identity, scientific descriptor, and lineage. Only after a
fenced runtime attempt validates and commits an output does a durable event make
it searchable and reconsider waiting targets. This metadata-first approach
reduces unnecessary movement of large scientific files and avoids requiring a
universal file adapter.

Stage 10D now turns that diagram into one bounded explicit service call. It
accepts a persisted target, rejects the portable planning manifest as launch
authority, re-resolves against current snapshots, persists the exact planning
and compiler inputs, and launches a deterministic Stage-1 run. Its conformance
producer is a closed native-pointer identity operation; a real domain producer,
background dispatch policy, remote data provider, and HPC provider remain
integration milestones.

## 3. Technical architecture

### 3.1 Scientific contracts and content identity

The `contracts/` layer defines the scientific type system. An
`ArtifactDescriptor` records a concept, schema, representation, units, spatial,
temporal and vertical support, grid geometry, native resolution, origin,
missing-data policy, uncertainty, and component names. A `Requirement` states
which values are acceptable for those dimensions. `RequirementUse` adds the
consumer-local semantics that are lost in a simple type signature: input port,
cardinality, optional/default behavior, sharing, and distinctness constraints.

The pure `direct_match` operation compares an exact descriptor with an exact
requirement across every dimension and returns a content-identified
`CompatibilityProof`. It reports the full set of reasons instead of
short-circuiting on the first mismatch. It does not fetch data, rank candidates,
or silently insert a unit conversion, interpolation, or reprojection. A
transformation is eligible only when it is represented as an explicit reviewed
capability with its own inputs, outputs, cost, assumptions, and provenance.

Most durable objects use strict, canonical serialization and SHA-256 identities.
Consequently, changing a unit, grid, input lineage, selected edge, deployment,
or validation record changes the corresponding identity. These identities make
replay, deduplication, and parallel proposal merging possible without treating
an agent's confidence or a database row label as scientific authority.

### 3.2 Native artifacts, producers, and discovery scope

The `artifacts/` layer is a searchable control plane, not a central payload
store. An `ArtifactRecord` binds the full descriptor to a content digest and
size, producer/version/output port, exact upstream artifact lineage, evidence,
media type, and native location. Registration verifies a stable regular file,
refuses symbolic-link substitution, and publishes co-produced records together.
Snapshot construction rechecks location and stat identity and uses full
rehashing by default for planning snapshots; stat-fingerprint caching is a
weaker explicit opt-in. Missing or changed bytes become explicitly unavailable
rather than remaining a trusted pointer.
The SQLite index supports candidate narrowing, while the Stage-10D read surface
evaluates strict typed predicates against one immutable registry snapshot. A
query can bind content and descriptor identities, concept, schema,
representation, units, full spatial/grid/time/vertical metadata, native
resolution, origin, missingness, uncertainty, components, ensemble, evidence,
producer/version/port, format, native location, availability, metadata, and
exact direct-lineage pairs. Its receipt binds the query, snapshot, ordered
matches, digests, and availability and must replay from those same immutable
inputs; the query does not open or reinterpret payload data.

An adapter can also prepare a `NativePublicationDeclaration` containing the
complete descriptor, native path, producer/version, exact invocation, output
port, evidence, and complete input-port lineage. Stable no-follow hashing mints
a content-verified proposal, but the proposal is not publication authority and
cannot write the registry. Producer-family and invocation identity are separate
in this proposal; the current `ArtifactRecord` schema still has one
`producer_id`, which the runtime bridge uses for the bound invocation key. The
bridge adds a closed, identity-bound `scientific_provenance` metadata object,
and snapshot queries expose its capability and invocation coordinates. Moving
those values into normalized first-class record fields and indexes is explicit
follow-on work.

Missing artifacts are connected to reviewed `CapabilitySpec` declarations. A
capability is a finite multi-input/multi-output relation with closed
implementation and binder identifiers, parameter schemas or frozen
parameterizations, output descriptor templates, execution profile, evidence,
and declared planning cost. Binding produces a `BoundInvocation`; reserved
acquisition and transformation operations additionally carry replayable
authority records so that a generic capability cannot relabel fetched bytes or
invent a conversion.

Completeness is scoped explicitly. A discovery-universe contract identifies the
catalog, acquisition, and transformation layers that are required, while
certificates record the exact limits, snapshots, results, and omitted frontier.
Thus “optimal” means optimal over a declared, frozen, completely searched
universe. It does not mean optimal over every model or dataset that might exist
on the Internet.

### 3.3 Recursive hypergraph resolution and independent validation

For each root `RequirementUse`, the resolver recursively constructs a feasible
derivation hypergraph containing requirement nodes, distinct consumer-use nodes,
bound producer invocations, committed artifact leaves, and satisfaction arcs.
Each arc retains the compatibility proof for one exact producer output or
artifact. Equal scientific requirements can share discovery work, while their
uses remain distinct so that cardinality, non-sharing, and “different producer”
rules are not lost. Multi-output invocations are represented once, and cycles,
back-references, typed rejections, deployment infeasibility, and activated
discovery limits remain visible in the graph.

The graph is projected into a mixed-integer selection problem. The current
solver minimizes declared integer cost while accounting for shared inputs,
co-products, optional/default inputs, cardinality, distinctness, non-sharing,
grounding, acyclicity, committed-artifact status, inclusion/exclusion policy,
budget, and static deployment feasibility. Primary cost is separated from the
deterministic tie break. Solver output is not trusted: an independent validator
replays identities, compatibility and evidence proofs, artifact availability,
selected arcs, cost, sharing, cycles, grounding, and deployment facts before a
plan becomes eligible for binding.

The result vocabulary preserves uncertainty in the planning process itself.
`READY` means a validated solution in a complete declared universe;
`FEASIBLE_NOT_PROVEN_OPTIMAL` preserves a valid incumbent; `INCOMPLETE` records
an omitted frontier; and unsatisfiable or invalid results retain structured
blockers. In particular, a search-limit result is not mislabeled as proof that
no workflow exists.

### 3.4 From scientific plan to durable execution

Three plan identities are intentionally separated. A `CandidateDerivationPlan`
contains the selected scientific producers, exact satisfaction edges and
proofs, snapshots, and declared cost. A `BoundDerivationPlan` fixes
result-affecting components, parameters, and content bindings. A
`DeploymentPlan` fixes replaceable site-class and resource choices. This allows
deployment to change without silently changing the scientific derivation.

The compiler replays those records, closed implementations, authorities,
descriptors, and resource envelopes before emitting an immutable Stage-1
execution graph and a private process-local compilation authority. This is a
fail-closed in-process capability, not a cryptographic signature or distributed
authorization service. A restarted publication observer must be supplied that
exact authority again; it is not inferred from stored graph labels. The
`WorkflowController` then persists graph, task,
attempt, retry, deadline, reservation, and event state in SQLite. Every attempt
receives a stable token and fence, writes only to attempt-scoped staging, and is
reconciled after controller restart. Workers can invoke only registered closed
operations; arbitrary commands are not accepted as scientific tasks.

Process success is not publication. `ArtifactCommitter` checks the exact output
tree and port, runs the declared payload validator, hashes bytes into immutable
object identity, records validation and manifest metadata, and performs a
fenced all-output commit before downstream dependencies are released. Recovery
is idempotent across controller/process crashes on the same node. This is a
local POSIX and SQLite durability boundary, not node-loss or distributed-storage
durability.

Stage 10D closes the registry-input seam for closed supported operations. A
selected `ArtifactLeaf` must resolve to exactly one committed record in the
frozen snapshot; descriptor, manifest root, content digest, and bound-plan
identity must agree. The compiler emits a distinct registered-artifact binding,
and the controller re-verifies the no-symlink native file at run creation and
again before an attempt. Strict JSON inputs may be decoded as values. Native
pointer delivery is restricted to the reviewed pointer-identity operation and
does not materialize the raw payload. Runtime lineage records whether each
input came from Stage-1 commit or a Stage-10 record so those identifier
namespaces cannot be confused.

`TargetExecutionService` persists the target, manifest, artifact/capability/
deployment/evidence snapshots, selected invocations, bound and deployment
plans, compilation identities, graph, and canonical runtime/coordinator/
registry paths. Before first launch it repeats resolution and requires byte-for-
byte context equality. After restart it recompiles those frozen inputs to
reconstruct private authority and resumes the deterministic run ID. Each target
ID has one execution context and terminal receipt; a changed plan is refused
rather than silently becoming a new run.

### 3.5 Commit-to-discovery feedback and the automation boundary

After a scientific output is authoritatively committed, the runtime artifact
bridge re-reads its scientific binding, verifies the native pointer, digest and
size, reconstructs input lineage from authoritative runtime slots, and enqueues
a content-addressed event. The target coordinator advances each event through
`PENDING`, `REGISTERED`, and `APPLIED`: first the artifact becomes searchable,
then durable targets are resolved again against the new snapshot. The registry
and coordinator use separate SQLite transactions, so correctness comes from
idempotent replay rather than a claimed distributed transaction.

The joined bounded path now supports **explicit execute → commit →
registration → target re-resolution**. The application wires the observer
automatically, and a restart after runtime commit but before event handling can
reconstruct compiler authority, apply the event, and finish the same receipt
without another task attempt. The native-pointer integration fixture retains
the original native path and digest.

The automation boundary is still deliberate. A target is launched only when an
application calls `TargetExecutionService.execute()`; output events re-plan all
durable targets but do not dispatch every newly planned workflow. The next
stage needs an idempotent approval/dispatch state machine with transactional
ownership under simultaneous callers rather than making re-planning itself
permission to spend resources. The current service is also bound to canonical
local runtime/coordinator/registry paths; relocation and node-loss recovery are
not claimed.

## 4. WRF-SFIRE as the integration case

A representative target is wildfire arrival time and burned area following a
hypothetical asteroid airburst over Dallas. A candidate workflow may require:

- an impact or airburst scenario and thermal-ignition estimate;
- terrain, fuel category, and fuel-moisture products;
- atmospheric initial and boundary conditions;
- coupled WRF-SFIRE execution; and
- optional downstream smoke, exposure, infrastructure, or loss models.

WRF-SFIRE illustrates why composition must understand science rather than names.
The model couples fire and atmosphere at every time step: surface wind drives
fire spread, while the fire returns heat fluxes that alter the atmosphere [4].
Its atmospheric and fire meshes, projection, variable extents, and temporal
semantics may differ from both upstream data and downstream consequence grids.
Those relationships must be declared before execution; a generic interpolation
or a late array-shape comparison is not scientifically adequate.

One retained legacy run spent approximately 48.6 hours before publication
failed when a 253 × 253 fire field was compared with a 1001 × 1001 target grid.
That failure motivated typed placement and georeference preflight checks that
can distinguish exact placement, aligned refinement, required resampling, and
incompatible geometry. These checks now have bounded tests, but they are not yet
wired into a completed WRF-SFIRE run through the current artifact-first path.
The example is therefore an integration target and a source of concrete system
requirements—not yet validated cascade-hazard science.

The current technical study separates two gates. Before launch, a scenario
preflight derives expected atmospheric and fire dimensions and spacing, but
does not claim placement when CRS or origin is unknown. After a native WRF
output exists, georeference logic derives its Lambert Conformal grid from WRF
metadata and `XLONG`/`XLAT`, and refuses the reconstruction when coordinate
residual exceeds 30 m; retained nests reconstruct within approximately
2.7–3.4 m. In one retained domain, the atmosphere is 213 × 213 at 900 m and the
valid fire grid is 2130 × 2130 at 90 m, although allocated arrays extend to
2140 × 2140. Different variables populate different extents, demonstrating why
array shape or nonzero extent cannot establish a scientific grid.

Descriptor-level placement returns typed outcomes: exact match, aligned integer
refinement or coarsening, declared resampling required, CRS or axis mismatch,
missing georeference, rotated/skewed grid, degenerate grid, or outside extent.
It checks signed sample-centre affine coordinates, lattice alignment, complete
blocks, and containment; it performs no interpolation. The native fire and
atmospheric grids can be recognized as an aligned 10× relation, while the WRF
Lambert grid and a UTM consequence grid are correctly reported as incompatible
without a declared reprojection. No WRF-to-UTM reprojection is currently
implemented.

Resampling also depends on variable semantics. Arrival time requires a
first-arrival or minimum rule, extrema require max/min preservation, fractions
require area weighting, and categorical fuel labels require a categorical rule;
generic bilinear interpolation is not valid for all of them. These contracts
are implemented as preflight and transformation studies, while the retained
legacy WRF adapter is not yet connected to the authoritative artifact runtime.

NASA data services provide a natural discovery boundary. CNEOS can supply the
scenario context; CMR/Earthdata can identify candidate atmosphere, terrain, and
land products; and registered external providers can contribute other required
inputs. The composer must still prove that each exact product satisfies the
chosen model contract. Provider metadata is discovery evidence, not permission
to silently reinterpret the data.

## 5. Scalability, orchestration, and expert use

Scalability has two dimensions. **Composition scale** requires searching a
growing ecosystem without duplicating shared requirements or losing
multi-output opportunities. Canonical identities allow discovery work to be
partitioned by requirement, provider, or domain while preserving one global
selection problem. **Execution scale** requires bounded admission, durable task
and attempt state, dependency-aware scheduling, recovery, and resource
accounting. The design separates the scientific plan from its deployment so the
same derivation can eventually target local resources or an approved HPC
provider without changing its scientific identity.

The prototype currently demonstrates these ideas on one node: lazy partition
identities, bounded admission, retries, resource-aware local concurrency,
authoritative commits, and replayable artifact registration. The 10,000-item
result is a control-plane admission test, not 10,000 executed simulations.
Real remote-provider operation, real Slurm/MPI execution, multi-node recovery,
and million-scale discovery remain evaluation goals.

Partition sets use mixed-radix integer addressing, so a Cartesian ensemble does
not need every key materialized in memory. A durable cursor admits only a window
between configured low and high watermarks; cursor advancement and task-row
insertion are atomic. Packet fusion can reduce submission overhead while
retaining every member's identity and partial outcome. The executable packet
bridge has run a bounded local subprocess slice, but the larger result measures
control-plane behavior rather than large-scale scientific throughput.

Within the live local controller, scheduling is event-driven: a downstream task
can become ready as soon as its last dependency commits. A priority policy can
combine remaining critical-path rank with capped aging, while a reservation
ledger models CPU, memory, GPU, and scratch capacity and reconstructs active
reservations after restart. The live controller currently propagates CPU,
memory, and GPU envelopes; scratch propagation remains unimplemented. These are
bounds on declared reservations, not operating-system enforcement; GPU
identifiers are not pinned, MPI is refused, and the current Slurm work is a
fake-scheduler protocol demonstration rather than site-certified HPC execution.

The intended expert-facing interaction is outcome-oriented. A domain expert
should choose a reviewed target template, provide scenario facts and bounds,
and receive:

- the selected workflow and viable alternatives;
- reasons each artifact or producer is admissible or rejected;
- unresolved evidence or quality choices;
- estimated resources and cost;
- a reproducible workflow manifest; and
- exact output locations and lineage.

Automation does not remove the domain expert. It shifts effort from manually
wiring software toward declaring scientific intent, reviewing consequential
choices, and interpreting results. The authoritative target, execution, and
snapshot-query surfaces are currently Python APIs; a reviewed domain-facing
service is the next usability milestone.

## 6. Prospective agentic direction

A prospective agentic layer can improve usability and resource discovery
without replacing the verified kernel. Language-model agents can translate a
domain question into a controlled target proposal, explain plans, and draft
capability metadata from documentation. Retrieval agents can search provider
catalogs in parallel. Scientific and security critics can challenge assumptions.
Supervised models can estimate duration, memory, source reliability, or failure
risk; later bandit or reinforcement-learning policies can advise routing and
scheduling.

The state-of-the-art opportunity is a **proof-carrying heterogeneous
multi-agent workflow system**:

> Agents propose, retrieve, predict, and critique; deterministic services verify
> compatibility, establish discovery scope, select plans, authorize execution,
> and commit artifacts.

This boundary is important because current scientific-agent evaluations still
show substantial task-level failure [5]. The agentic layer is therefore a
research plan, not a delivered autonomous system.

## 7. Status and next steps

| Area | Current bounded evidence | Next validation |
|---|---|---|
| Contracts and composition | Typed compatibility, recursive discovery, exact bounded selection, independent validation | Larger and more diverse producer ecosystem |
| Artifact lifecycle | Local native-file verification, snapshot-bound full-metadata queries, durable events, automatic target reconsideration | Normalize producer/invocation provenance; provider-fed population and domain-facing API |
| Orchestration | Durable local controller plus exact target-to-execution service, registered inputs, fenced commit, and restart replay | Durable approval/dispatch, non-identity producer, real provider/HPC certification, scale tests |
| WRF-SFIRE | Adapter studies, georeference analysis, placement/preflight contracts | Reference fixture and current-path native execution |
| Agentic system | Architecture and evaluation roadmap | Typed proposal blackboard and read-only advisory agents |

The recommended progression is:

1. normalize producer-family and bound-invocation provenance, then converge the
   adapter proposal with compiler-authorized Stage-1 publication;
2. onboard one lightweight non-identity producer and add an idempotent durable
   approval/dispatch service plus a domain-facing target/query API;
3. add verified NASA CMR/provider connectors using the same snapshot and
   completeness contracts;
4. integrate WRF-SFIRE with a reference fixture, preflight, and certified local
   or HPC provider;
5. evaluate composition and orchestration at representative graph, data, and
   ensemble scales; and
6. introduce agent-assisted interpretation, discovery, and criticism through
   typed proposals while retaining deterministic authority.

The resulting contribution is a reusable control plane for cascading-hazard
science: it connects expert intent to distributed data and models while keeping
scientific compatibility, provenance, uncertainty, and execution state
explicit.

## References

1. NASA JPL, [Center for Near-Earth Object Studies](https://cneos.jpl.nasa.gov/about/cneos.html).
2. NASA Ames, [Asteroid Threat Assessment Project](https://nas.nasa.gov/areas/atap.html).
3. NASA Earthdata, [Common Metadata Repository](https://www.earthdata.nasa.gov/about/esdis/eosdis/cmr).
4. Mandel, Beezley, and Kochanski, [Coupled atmosphere-wildland fire modeling with WRF 3.3 and SFIRE](https://gmd.copernicus.org/articles/4/591/2011/index.html), 2011.
5. Chen et al., [ScienceAgentBench: Toward Rigorous Assessment of Language Agents for Data-Driven Scientific Discovery](https://arxiv.org/abs/2410.05080), 2024.
