# Agentic plan for verified scientific workflow composition

Last updated: 2026-08-19

## 1. Research objective

The research opportunity is not to replace the workflow resolver with a swarm
of language models. It is to build a heterogeneous multi-agent discovery and
learning layer around an exact, independently verified scientific workflow
kernel.

The central thesis is:

> Agents explore, interpret, retrieve, predict, challenge, and propose;
> deterministic services verify, select, authorize, execute, and commit.

A suitable research name is **proof-carrying heterogeneous multi-agent
scientific workflow composition**. “Proof-carrying” applies only to properties
that a machine can replay, such as schema conformance, exact compatibility,
identity, graph grounding, resource feasibility, and artifact content. Claims
that still require scientific judgment are **evidence-carrying proposals**, not
proofs.

This separation is the project’s main advantage. The current system already
has strict scientific contracts, immutable artifact and capability identities,
recursive hypergraph discovery, exact finite-graph selection, independent plan
validation, fenced execution, and authoritative artifact commits. Agents can
increase discovery coverage and reduce human effort without weakening those
boundaries.

## 2. What counts as an agent

An agent is any bounded policy that observes state, chooses actions, records its
identity, and can be evaluated. It does not have to be an LLM.

The proposed system uses several kinds of agents:

| Agent family | Appropriate role | Examples of methods |
|---|---|---|
| Deterministic symbolic agents | Type checking, graph construction, exact matching, optimization, replay | rule engines, graph search, MILP, CP-SAT |
| LLM agents | Ambiguous language, documentation, proposal drafting, explanation, criticism | tool-using LLMs, code models, vision-language models |
| Retrieval agents | Find likely artifacts, concepts, documentation, or producers | SQL/full-text retrieval, embeddings, cross-encoders |
| Supervised ML agents | Predict duration, memory, failure, source health, or useful priority | gradient boosting, quantile models, survival models, GNNs |
| Contextual-bandit agents | Select among already-safe models, tools, providers, or policies | LinUCB, Thompson sampling, conservative bandits |
| Bayesian optimization agents | Select expensive parameter trials or fidelity levels | Gaussian-process BO, safe BO, multi-fidelity BO |
| Reinforcement-learning agents | Later scheduling and admission policy research in simulation/shadow mode | PPO, graph RL |
| Multi-agent RL agents | Much later decentralized multi-site coordination | centralized training with decentralized proposals |
| Scientific surrogate agents | Produce approximate scientific artifacts under a separate validity contract | Gaussian processes, neural operators, PINNs |
| Human agents | Resolve scientific value judgments and authorize consequential actions | reviewed approval receipts |

The important distinction is authority, not model type. A deterministic search
agent can be unsafe if it is allowed to assert completeness over an unstated
universe. An LLM can be useful and safe if it can only submit a typed proposal
that a separate verifier checks.

## 3. Current system boundary

The maintained artifact-first path is:

```text
typed scientific target
        |
        v
verified native-artifact search
        |
        v
recursive capability discovery
        |
        v
frozen finite derivation hypergraph
        |
        v
exact minimum-cost selection
        |
        v
independent selected-plan validation
        |
        v
fenced runtime and authoritative artifact commit
        |
        v
searchable immutable artifact record and target reconsideration
```

The main authoritative implementation seams are:

| Responsibility | Current code |
|---|---|
| Scientific target contracts | `NASA_Project/contracts/requirements.py` |
| Exact direct compatibility | `NASA_Project/contracts/matching.py` |
| Artifact records and search | `NASA_Project/artifacts/records.py`, `registry.py` |
| Durable target and output-event coordination | `NASA_Project/artifacts/coordinator.py` |
| Target-time artifact reuse | `NASA_Project/artifacts/service.py` |
| Closed capability catalog and binding | `NASA_Project/capabilities/` |
| Discovery universe and certificates | `NASA_Project/resolution/upstream.py` |
| Recursive hypergraph construction | `NASA_Project/resolution/hypergraph.py` |
| Exact selection | `NASA_Project/resolution/milp.py` |
| Independent plan validation | `NASA_Project/resolution/validator.py` |
| Compilation and runtime authority | `NASA_Project/composition/`, `engine/runtime/` |
| Objective choices requiring human judgment | `NASA_Project/objectives/` |

Agents must sit around this path. They must not become an alternative path.

### 3.1 Legacy `agentic/` package

The existing `NASA_Project/agentic/` package is a legacy experimental stack,
not an adapter to the current authority model. It uses a caller-mutable
metacatalog, string-level variables, a heuristic greedy planner, and a direct
script execution path. It does not carry the current `ArtifactDescriptor`,
`RequirementUse`, discovery-universe, compatibility-proof, exact-MILP,
independent-validation, or bound-plan identities.

It must therefore be quarantined as legacy/demo code. Its conversational UX,
read-only tool pattern, ontology descriptions, and MCP ergonomics can inform
new work, but its planner, trust scores, critic, and executor must not be reused
as scientific authority.

## 4. Recommended architecture

Use a **typed append-only proposal blackboard** with a broker inspired by the
classical blackboard and Contract Net patterns. The blackboard is separate
from authoritative target, artifact, capability, plan, and runtime stores.

```text
User or upstream target
        |
        v
Controlled target-intent proposals
        |
        v
+---------------- Agent proposal plane ----------------+
| durable typed blackboard                              |
|                                                       |
|  domain scouts       provider scouts                  |
|  catalog retrievers  capability-onboarding agents     |
|  evidence agents     scientific/security critics      |
|  ranking models      cost/resource predictors         |
+--------------------------+----------------------------+
                           |
                           v
              deterministic proposal verifier
                           |
                           v
              accepted content-addressed fragments
                           |
                           v
+--------------- Existing authority plane --------------+
| freeze universe -> build hypergraph -> exact MILP      |
| -> independent validation -> bind/compile -> runtime   |
| -> validated commit -> artifact registry/event replay |
+--------------------------------------------------------+
                           |
                           v
               observations and evaluation data
                           |
                           v
+------------------ Learning plane ----------------------+
| offline training, calibration, routing, shadow policy  |
+--------------------------------------------------------+
```

The broker publishes bounded tasks keyed by canonical requirement, artifact,
capability, resolution, or run IDs. Agents lease work, submit proposals, and
may challenge proposals. Only a deterministic verifier can emit an accepted
fragment. The resolver always rebuilds a fresh frozen graph from accepted
records; agents never mutate a graph while it is being solved.

Useful blackboard events are:

- `FrontierOpened`;
- `WorkOffered` and `WorkLeased`;
- `ProposalSubmitted`;
- `ChallengeSubmitted`;
- `VerificationRecorded`;
- `FragmentAccepted` or `ProposalRejected`;
- `FrontierClosed`; and
- `DiscoveryEpochFrozen`.

Content-derived IDs, leases, canonical sorting, idempotent replay, and a frozen
epoch make accepted results independent of agent completion order.

## 5. Multi-agent coordination pattern

### 5.1 Blackboard plus bounded contract-net allocation

The blackboard holds typed work and proposal records. A broker sends a call for
proposals to eligible agent specifications. Agents may bid with expected cost,
latency, supported scope, and current load. The broker assigns one or more
bounded leases, with deadlines and budgets.

This borrows the useful parts of the
[FIPA Contract Net protocol](https://www.fipa.org/specs/fipa00029/SC00029H.html):
calls for proposals, explicit refusal, deadlines, acceptance/rejection, and
conversation identity. It does not adopt FIPA’s full wire format, and an
accepted bid is only permission to work—not permission to alter scientific
state.

### 5.2 When to use multiple agents

Multiple agents are useful when work is naturally separable:

- different scientific domains or provider catalogs;
- independent first-pass interpretations of an ambiguous target;
- heterogeneous critics with different defect rubrics;
- retrieval across different source types;
- parallel exploration of alternative derivation frontiers; or
- redundancy required to estimate uncertainty or detect correlated failure.

Multiple agents are not useful merely to vote on deterministic facts. Units,
CRS, exact identities, graph cycles, resource capacity, and artifact hashes
should be checked once by code. Agreement among agents is not compatibility,
evidence, completeness, or authorization.

### 5.3 Debate policy

Debate can be evaluated as a critic method, not installed as a truth mechanism.
The first proposals should be generated independently and blindly. Critics get
different rubrics and do not see other agents’ confidence until after their
initial review. Disputes resolve through deterministic checks or explicit human
review.

Early multi-agent debate results are promising in some settings, but controlled
comparisons show that gains can depend on task, models, prompts, and budgets.
The system must compare debate against independent sampling and self-consistency
at equal token and tool-call cost, rather than assuming that conversation helps.

## 6. Agent roles and exact insertion points

### 6.1 Target interpretation agent

Input: user text, a frozen target-template registry, and allowed objective
policies.

Output: a `TargetIntentDraft` containing a controlled template ID, user-supplied
space/time/vertical values, explicit ambiguities, and unresolved fields.

The first version must not generate a raw `RequirementUse`. A deterministic
`TargetTemplateRegistry.instantiate()` should own schema, representation,
units, origin, resolution, missingness, evidence, and default-quality policy.
Expert-authored raw requirements can be a later, explicitly approved path.

This is a strong LLM use case: natural language is ambiguous, while success can
be checked field by field. Tool-using patterns such as
[ReAct](https://arxiv.org/abs/2210.03629) show how language models can combine
reasoning with bounded external lookup, but the lookup result remains data, not
instructions or authority.

### 6.2 Artifact retrieval agent

Input: a typed target draft and immutable artifact-registry snapshot.

Output: query suggestions, likely `ArtifactRecord` IDs, and explanations.

The agent may use embeddings or semantic expansion to improve recall, but the
registry’s typed search and `direct_match` make the final decision. It cannot
register bytes, change metadata, relabel an artifact, or declare a record
available.

### 6.3 Provider and source discovery agents

Give each agent one predeclared provider, repository, documentation set, or
geographic domain. It returns candidate source IDs, bounded metadata-query
drafts, negative results, activated limits, and exact source references.

Acquisition remains under the existing deterministic scope, quota, connector,
coverage, fetch-authority, receipt, and content-binding controls. Agents never
receive credential values or a raw payload-open operation.

### 6.4 Capability-onboarding agent

Input: reviewed model/driver documentation, code, test fixtures, and a target
contract.

Output: a draft finite capability specification, parameter schema, input/output
contracts, execution-profile proposal, test plan, assumptions, and unresolved
issues.

It cannot activate a capability or supply executable callables. Source review
must establish the closed implementation, binder, execution profile, evidence,
native output contract, and artifact lineage. This role is especially valuable
for migrating real NASA drivers/models out of the legacy catalog.

### 6.5 Recursive frontier discovery agents

Input: one or more canonical `RequirementUse` IDs plus frozen catalog, artifact,
source, and evidence snapshot IDs.

Output: `DiscoveryFragmentProposal` values referencing existing artifact and
capability IDs, or draft acquisition/onboarding work.

The deterministic merger resolves every ID, recomputes compatibility, records
rejections, and freezes the discovery universe. Agents cannot mint proofs,
insert graph edges, set `complete=True`, suppress omitted frontiers, or select
a plan.

### 6.6 Evidence agent

Input: a precise evidence question and approved primary-source collections.

Output: claim records bound to exact source URI, retrieval time, content digest,
page/section, applicability, assumptions, and uncertainty.

The evidence service verifies that a claim applies to the exact component,
configuration, output, regime, time, space, and metric definition. A citation
is not automatically empirical evidence.

### 6.7 Scientific critic society

Use separate critics for:

- units and dimensional consistency;
- CRS, axis, grid, and spatial support;
- temporal coverage, cadence, calendars, and accumulation semantics;
- vertical reference and support;
- native versus effective resolution;
- missingness and intrinsic/estimate uncertainty;
- evidence applicability and frozen identity;
- provenance, licensing, and source mutation;
- transformation loss or unsupported hidden conversion;
- deployment/runtime feasibility; and
- security, prompt injection, secret exposure, and excessive cost.

Each critic emits a `CritiqueProposal` referencing exact proposal, resolution,
proof, rejection, or blocker IDs. It can recommend typed revisions. It cannot
accept or reject the graph.

External verifier feedback is more reliable than unconstrained self-reflection.
[Reflexion](https://arxiv.org/abs/2303.11366) motivates episodic feedback, while
the current system should ground that feedback in actual rejection codes such
as `CRS_MISMATCH`, `TEMPORAL_GAP`, `EVIDENCE_NOT_APPLICABLE`, or
`IMPLEMENTATION_STALE`.

### 6.8 Resolver explainer and human-choice facilitator

This agent reads a completed `ResolutionOutcome`, blocker tree, rejected
alternatives, and `ChoiceRequiredReport`. It explains what was selected, why
alternatives failed, what is unknown, and which human decision is needed.

It cannot rewrite a plan or mint a `ChoiceRecord`. Before exposing a choice
tool, the project needs a human-approval receipt with authenticated principal,
channel, report ID, timestamp/nonce, and integrity protection. The current
choice object does not prove that a human authored it.

### 6.9 Runtime diagnosis agent

Input: exact run, task, attempt, fence/state version, logs, observations, and
artifact-validation results.

Output: a bounded `RecoveryProposal`: retry, do not retry, collect more
diagnostics, revise a future resource envelope, or request human intervention.

The runtime controller alone checks current state, retry safety, reservations,
capacity, idempotence, and fence authority. The agent cannot transition state,
retry work, mutate a declared envelope, or commit an artifact.

### 6.10 Plan and artifact explanation agent

After validation and commit, a small model can explain the selected derivation,
native artifact pointers, provenance, alternatives, and non-claims. It must
describe immutable records; its prose is not added back as scientific metadata.

## 7. Where ML, GNNs, bandits, BO, RL, and surrogates fit

The following decision matrix separates methods that are useful now from those
that require more data or runtime maturity:

| Method | Best project role | Earliest use | Required safety boundary |
|---|---|---|---|
| MILP/CP-SAT/search | exact small-instance oracle; constrained scheduling baseline | now | independently revalidate every selected start |
| HTN/symbolic planning | experiment with explicit goal decomposition | after operation templates | every leaf resolves to a closed capability |
| HEFT/list scheduling | strong deterministic heterogeneous-DAG baseline | now/simulator | resource ledger remains authoritative |
| Supervised regression/survival | duration, memory, failure, transfer estimates | after durable observations | proposal, calibration, OOD abstention |
| GNN | graph-aware estimates, candidate ranking, solver hints | after labeled graphs | cannot prune or assert feasibility |
| Contextual bandit | route among safe models/tools/policies | after propensity logging | finite prevalidated action set |
| Bayesian optimization | select costly bounded trials | after cheap pilot path | quota, schema, safety, and approval gates |
| Active learning | choose informative partitions/evidence runs | after uncertainty model | no scientific equivalence inference |
| RL | ready-task ordering and bounded parallelism | replay/shadow first | hard action masks and fallback |
| MARL | genuinely decentralized multi-site proposals | much later | centralized authority and site-level shields |
| Anomaly/failure model | warn about abnormal attempts or sources | after structured telemetry | alert/propose only |
| Scientific surrogate | approximate producer with explicit applicability | separate research track | distinct capability/artifact/evidence identity |

### 7.0 Deterministic planning and scheduling agents

Not every research agent should learn. CP-SAT, MILP, branch-and-bound, and
short-horizon search can produce exact or bounded schedules under precedence,
CPU, memory, GPU, scratch, site, deadline, and exclusivity constraints. They
are valuable as small-instance oracles and deterministic baselines. Solver
timeout must be reported as a feasible incumbent plus gap or as incomplete,
never silently re-labelled optimal. The [OR-Tools CP-SAT
documentation](https://developers.google.com/optimization/cp) is a practical
reference for a bounded prototype.

Hierarchical Task Network planning can be studied for high-level scientific
goal decomposition. A method may decompose “produce exposure estimate” into
acquisition, model, validation, and publication steps, but each leaf must still
resolve to a closed current capability. Incomplete methods restrict the
reachable plan space, so HTN cannot establish open-world completeness. The
[SHOP2 paper](https://arxiv.org/abs/1106.4869) is a useful primary reference.

For runtime scheduling, implement HEFT/critical-path list scheduling before RL.
These baselines reveal whether graph topology and heterogeneous estimates add
value without training complexity. They also provide interpretable failure
cases and deterministic fallback behavior.

### 7.1 Supervised duration, resource, and failure models

This is the highest-value learning work after durable instrumentation exists.
Predict:

- runtime quantiles rather than only a mean;
- peak memory, scratch, transfer, CPU, and GPU needs;
- probability and type of failure;
- queue or provider delay; and
- confidence and out-of-distribution status.

Features must bind implementation digest, scientific parameters, input scale,
site snapshot, resource allocation, and software/environment identity. A model
emits a versioned estimate proposal. Declared envelopes remain immutable, and
admission uses conservative bounds or abstains.

Useful baselines include gradient-boosted trees, quantile regression, survival
models for censored runs, and simple per-operation empirical models. Complex
models must beat these on held-out graph families and software versions.

### 7.2 GNN agents for graphs and MILP hints

GNNs can encode dependency graphs for duration/slack prediction, ready-task
priority, or MILP warm starts. Work on
[learning branch-and-bound policies](https://arxiv.org/abs/1906.01629) shows
that a variable-constraint bipartite graph can guide exact optimization.

The safe use is advisory:

- rank candidates before deterministic enumeration;
- propose an incumbent or warm start;
- prioritize frontier work; or
- guide solver branching.

The exact formulation still decides feasibility and optimality. A learned
model may not prune a candidate unless the omission is independently justified
and completeness remains honest.

### 7.3 Contextual-bandit routing agent

A contextual bandit is appropriate when the action has an immediate,
attributable outcome. Possible actions include:

- small versus large LLM;
- retrieval-only versus LLM-assisted extraction;
- which safe provider/query strategy to try first;
- which deterministic scheduler policy to use; or
- which conservative resource proposal to test.

Linear contextual-bandit methods provide a clear baseline and regret framework
([Chu et al.](https://proceedings.mlr.press/v15/chu11a.html)). Start with a
static router, then shadow mode, then a conservative bandit restricted to
prevalidated actions. Log context, available actions, selection propensity,
chosen action, delayed outcome, and baseline action. Without propensities,
offline comparison is easily confounded.

### 7.4 Bayesian optimization and active-learning agents

Use BO when an evaluation is expensive and the parameter domain is bounded:

- partition or packet size;
- CPU/memory envelope for recurring lightweight tasks;
- safe concurrency or fusion thresholds;
- model parameter experiments;
- simulation fidelity; or
- which evidence-generating run to perform next.

[Bayesian optimization](https://papers.nips.cc/paper_files/paper/2012/hash/05311655a15b75fab86956663e1819cd-Abstract.html)
can account for variable-duration experiments, and
[multi-fidelity BO](https://proceedings.mlr.press/v70/kandasamy17a.html) can
combine cheap approximations with expensive evaluations. Safety-aware methods
such as [SafeOpt](https://proceedings.mlr.press/v37/sui15.html) are research
references, not an automatic guarantee: their assumptions and implementation
must be validated for the actual system.

BO proposes trials. Quotas, parameter schemas, capability validity, admission,
artifact checks, and human approval still apply. Do not use expensive WRF-SFIRE
runs as the first exploratory workload.

### 7.5 RL scheduling agent

RL becomes relevant only after there is a high-fidelity replay environment,
durable observations, actual stochastic arrivals, and an action shield.

Potential actions are ordering already-ready tasks, bounded parallelism, and
selection among feasible sites or scheduling policies. Readiness, dependencies,
resource capacity, retry safety, quotas, and artifact state are hard action
masks. A deterministic policy is the fallback on model failure, uncertainty,
or state-version mismatch.

[Decima](https://web.mit.edu/decima/index.html) demonstrates that RL with
graph-structured job representations can improve Spark scheduling on its
evaluated workloads. That is motivation for an experiment, not evidence that
the policy transfers to scientific workflows. Evaluate against FIFO, layered,
critical-path/event-driven, HEFT, and exact small-instance schedules.

### 7.6 MARL

Multi-agent RL is not justified by one controller on one private node. It
becomes a research option only when independently controlled sites, providers,
or schedulers genuinely have local observations and decisions.

A later design could use centralized training with decentralized proposals.
Every site agent proposes placements to a global coordinator, while the global
ledger and deployment verifier protect capacity and policy. MAPPO is a useful
cooperative baseline ([Yu et al.](https://arxiv.org/abs/2103.01955)), but MARL
adds non-stationarity, reward design, simulator gap, and hard-to-explain
failures. It should be the last learning stage, not the first multi-agent demo.

### 7.7 Anomaly and failure-diagnosis agents

Anomaly agents can identify unusual resource traces, logs, source metadata,
artifact-validation patterns, or provider behavior. Start with simple rules and
supervised failure classifiers; later compare sequence models. Useful outputs
are an anomaly score, calibrated failure probability, likely class, lead time,
and exact supporting observations.

They remain advisory. A score alone cannot cancel, quarantine, retry, widen a
resource envelope, or relabel an artifact. Measure AUCPR for rare failures,
recall at a fixed false-positive rate, calibration, warning lead time, avoided
wasted compute, and false-intervention cost. Structured failure labels and
software/hardware versions are prerequisites; raw log text alone is not a
stable training contract.

### 7.8 Active-learning and task-sampling agents

Active learning is useful when a small number of probe tasks can improve a
runtime/resource model or decide which expensive scientific observation to
collect. The agent proposes informative partitions, configurations, or
evidence-generating runs under a strict probe budget. Every probe uses normal
admission and artifact authority.

Evaluation should report model error or decision regret versus probe count,
core-hours, coverage, and calibration. Similar runtime behavior does not imply
scientific equivalence, and a sampled subset cannot establish full-domain
accuracy without an explicit statistical argument.

### 7.9 Scientific surrogate agents

Surrogates such as Gaussian processes, neural operators, or
[physics-informed neural networks](https://arxiv.org/abs/1711.10561) are
scientific producers, not planning authorities.

Each surrogate must be a distinct closed capability with:

- training-data, model, and code digests;
- exact input/output descriptors;
- a validity/applicability domain;
- uncertainty and out-of-domain behavior;
- evidence against trusted held-out outputs; and
- an artifact type that cannot be confused with the full-physics model.

Surrogates may support screening, sensitivity analysis, experiment selection,
or multi-fidelity optimization. They must never silently replace a requested
full-model artifact.

## 8. Typed agent protocol

### 8.1 `AgentDescriptor`

```text
AgentDescriptor
  agent_spec_id
  schema_version
  role
  implementation_kind
  model_family_and_exact_version_or_checkpoint
  policy_or_prompt_digest
  tool_contract_digests
  input_schema_ids
  output_schema_ids
  permission_tier
  allowed_source_scopes
  default_token_time_tool_and_cost_budgets
  owner_and_review_status
```

### 8.2 `AgentTask`

```text
AgentTask
  task_id
  task_kind
  subject_ids
  frozen_snapshot_refs
  state_version
  requested_output_schema
  allowed_tools_and_sources
  deadline
  recursion_and_fanout_limits
  token_tool_time_and_cost_budgets
  parent_task_ids
```

### 8.3 `AgentProposal`

```text
AgentProposal
  proposal_id                 # canonical-content identity
  task_id
  agent_spec_id
  agent_run_receipt_id
  parent_proposal_ids
  typed_payload
  claims[]
  source_and_evidence_refs[]
  assumptions[]
  unknowns[]
  bounded_scope
  requested_machine_checks[]
  observed_limits[]
```

### 8.4 `AgentRunReceipt`

```text
AgentRunReceipt
  run_id
  agent_spec_id
  exact_model_or_policy_identity
  prompt_template_and_tool_digests
  input_snapshot_refs
  started_and_finished_times
  tool_calls_and_result_digests
  token_and_cost_accounting
  termination_reason
```

### 8.5 `VerificationReceipt`

```text
VerificationReceipt
  receipt_id
  proposal_id
  verifier_schema_and_build_digest
  snapshot_refs
  exact_checks[]
  status = ACCEPTED | REJECTED | STALE | INCOMPLETE
  rejection_or_caveat_codes[]
  accepted_fragment_id_or_null
```

The public record stores concise claims, assumptions, citations, tool actions,
and machine-checkable witnesses. It should not require or persist a model’s
private chain-of-thought.

### 8.6 Performatives

Use a small closed set:

```text
REQUEST  PROPOSE  CHALLENGE  REVISE  REFUSE
ACCEPT_FOR_VERIFICATION  REJECT  EXPIRE  CANCEL  INFORM_RESULT
```

“Accept” means accepted for deterministic verification or assigned work. It
never means that the scientific claim is true.

## 9. Permission and authority model

| Tier | Agent ability | Examples |
|---|---|---|
| P0 | Read and explain immutable records | plan explainer |
| P1 | Submit typed proposals | target draft, critique, query draft |
| P2 | Query bounded external metadata sources | provider scout |
| P3 | Run sandboxed, quota-bounded conformance or pilot tasks | adapter/test agent |
| P4 | Request an expensive or consequential action for human/policy approval | experiment designer |

No tier permits an agent to:

- mint a compatibility proof or evidence snapshot;
- assert discovery completeness or optimality;
- activate arbitrary code or capability callables;
- alter an artifact descriptor after bytes are bound;
- bypass independent selected-plan validation;
- impersonate a human quality decision;
- transition runtime state, weaken retry fencing, or overbook resources;
- commit or relabel an artifact; or
- access raw credentials or unrestricted shell/network tools.

## 10. Recursive scaling design

The resolver is a hypergraph, not a tree. Scaling must preserve shared
subrequirements, multi-output co-production, alternatives, cardinality,
non-shareability, and cycles.

### 10.1 Sharding

Shard proposal work by canonical `RequirementUse` ID, scientific domain,
provider/source snapshot, or evidence question. A requirement may have many
agent tasks, but accepted candidate fragments are deduplicated by their exact
content identity.

### 10.2 Memoization and leases

The blackboard records opened, leased, completed, rejected, expired, and
omitted frontier work. Leases prevent accidental duplication; deterministic
identity makes duplicated results harmless. Work stealing is allowed only for
expired leases and preserves the same task ID and budgets.

### 10.3 Completeness

Agents never close discovery merely because no proposal was found. A separate
frontier ledger closes a bounded scope only when every required source/rule is
exhausted or every omission has an exact limit reason. `NO_CANDIDATE` and
`INCOMPLETE_DISCOVERY` remain distinct.

### 10.4 Selection

Agent heuristics can determine exploration order and provide warm starts, but
the exact global selector operates on the frozen accepted graph. If a time
limit leaves only an incumbent, the result remains feasible-not-proven-optimal.

### 10.5 Scale experiments

Measure:

- accepted candidates per second and per token;
- duplicate proposals and work amplification;
- broker and verifier contention;
- speedup efficiency by agent count;
- time to first valid incumbent and time to proven optimum;
- graph coverage and omitted frontiers;
- order/restart invariance; and
- communication bytes and cost per accepted fragment.

## 11. Memory design

Keep four forms of memory separate:

| Memory | Contents | Authority |
|---|---|---|
| Working | Current task and frozen snapshots | ephemeral context only |
| Episodic | Agent attempts, tool calls, verifier rejections | immutable audit data |
| Semantic | Reviewed documentation, accepted contracts, committed artifacts | references authoritative IDs |
| Procedural | Versioned prompts, tools, policies, agent descriptors | configuration identity |

Free-form summaries and reflections never become scientific truth. Retrieval
memory returns references and provenance. Every durable memory item is
origin-bound, snapshot-bound, supersedable, and filtered by permission.

[W3C PROV-O](https://www.w3.org/TR/prov-o/) provides a useful vocabulary for
entities, activities, agents, derivation, and attribution, but the project’s
canonical JSON identities remain the executable source of truth.

## 12. Security and failure model

External pages, PDFs, API results, logs, model output, and other agents’ text
are untrusted data. They are not instructions.

Required defenses include:

- deterministic reference monitor outside all models;
- typed allow-listed tools with narrow parameters and result schemas;
- least-privilege source, filesystem, network, and credential access;
- immutable task/snapshot/state identities on every action;
- token, call, wall-time, provider, fan-out, and dollar budgets;
- sandboxed code/test execution;
- no shell or unrestricted network access by default;
- source-content digests and citation-to-claim bindings;
- prompt/data channel separation and output taint tracking;
- stale-state, replay, duplicate, and confused-deputy checks;
- human approval for expensive, external, or scientifically ambiguous action;
- independent artifact verification and authoritative commit; and
- deterministic fallback when models are unavailable or uncertain.

[AgentDojo](https://arxiv.org/abs/2406.13352) demonstrates that indirect prompt
injection is a concrete risk for tool-using agents. The project should create
its own attack suite for malicious provider metadata, poisoned documentation,
cross-agent instruction injection, secret exfiltration, tool-parameter
smuggling, denial-of-wallet, and colluding false proposals.

The verifier is not omniscient. It can only enforce properties represented in
the contracts. Hidden scientific assumptions require expert review and
adversarial test cases.

## 13. Prerequisites before autonomous execution

The code audit found deterministic integration gaps that agents cannot solve by
reasoning around them.

### 13.1 One production application facade

There is no single production service joining target submission, resolution,
binding, compilation, execution, commit, and target reconsideration. Stage
demos currently assemble those boundaries. Build one explicit facade before
adding multi-agent fan-out.

### 13.2 Controlled target-template registry

The rich target contract is currently assembled mainly in fixtures. Build
reviewed templates so an LLM chooses a template and fills user facts instead of
inventing scientific semantics.

### 13.3 Resolution-to-execution service

`WorkflowManifest` is a portable planning record, not an execution graph. Add a
deterministic service such as:

```text
bind_and_compile(
  resolution_id,
  expected_artifact_snapshot_id,
  deployment_snapshot_id
) -> verified executable-plan receipt
```

It must reload the full frozen inputs, revalidate the outcome, bind deployment,
compile, and return a run proposal. Agents may request this transition; they
cannot synthesize a bound graph.

### 13.4 Native artifact input bridge

Stage 1 can bind artifacts already committed in its runtime store, but the
scientific compiler still refuses a registry `ArtifactLeaf` feeding an
invocation. Add an exact receipt bridge from the Stage-10 native artifact
record into Stage-1 external input authority. This is required before a
target-generated workflow can consume arbitrary cataloged native files.

### 13.5 Real producer onboarding

The current closed authoritative runtime does not yet contain WRF/MPI or most
real NASA model operations. Onboard producers individually with strict
descriptors, finite parameter schemas, closed implementation/provider bindings,
resource envelopes, evidence, and native artifact output contracts. An agent
can draft this work but cannot make a legacy card executable.

### 13.6 Deployment identity through runtime

Deployment class is checked during planning/compilation but is not fully
preserved and reverified at dispatch. Site, environment, network, GPU identity,
and scratch constraints must reach runtime before placement ML/RL can be more
than a shadow experiment.

### 13.7 Durable observations

Persist feature, action, propensity, outcome, resource peak, failure, and model
snapshot records. Current in-memory observations are insufficient for
reproducible supervised learning, bandits, or RL.

## 14. Evaluation program

Scientific-agent benchmarks support a conservative approach. For example,
[ScienceAgentBench](https://arxiv.org/abs/2410.05080) evaluates 102 expert-
validated workflow tasks and reports that its best tested agent solved only a
minority independently. The correct unit of evaluation is therefore each
workflow function before an end-to-end autonomy claim.

### 14.1 Baselines

Compare:

1. deterministic system only;
2. one general LLM;
3. one LLM plus deterministic verifier feedback;
4. homogeneous independently sampled agents;
5. heterogeneous specialist agents;
6. heterogeneous agents plus critics;
7. learned routing versus fixed routing; and
8. oracle/exhaustive results on small frozen graphs.

Every comparison uses equal token, tool-call, wall-time, or dollar budgets.

### 14.2 Target interpretation benchmark

Measure exact field accuracy, missing ambiguity detection, clarification recall,
invented constraints, template-selection accuracy, latency, cost, and
run-to-run consistency.

### 14.3 Discovery benchmark

Use frozen hypergraphs with alternatives, shared requirements, co-production,
cycles, unavailable artifacts, similar-but-incompatible descriptors, bounded
providers, and incomplete frontiers.

Measure valid-candidate recall/precision, unsafe proposals, false acceptance,
time to first valid plan, time to optimum, duplicate work, completeness
accuracy, and cost per accepted fragment.

### 14.4 Critic benchmark

Seed unit, CRS, grid, temporal, vertical, resolution, missingness, uncertainty,
evidence, provenance, mutation, implementation, resource, and security defects.
Compare no critic, self-critique, same-model critic, heterogeneous critic, and
deterministic validator. Primary metrics are defect recall and unsafe-survival
rate. False acceptance must remain zero at the authority boundary.

### 14.5 Coordination topology benchmark

At equal budgets compare a central star, chain, tree, group chat, independent
ensemble, and typed blackboard. Measure correctness, duplicate work,
communication, coordinator contention, invalid-claim propagation, latency, and
crash recovery.

### 14.6 Scheduling/learning benchmark

Use replay and synthetic arrival streams before live canaries. Compare FIFO,
layered, current event-driven priority, critical-path/list scheduling, HEFT,
exact small-instance schedules, supervised/GNN priority, contextual bandit,
and RL.

Report mean/p95 completion time, makespan, slowdown, utilization, fairness,
constraint violations, scheduler latency, inference overhead, calibration, and
out-of-distribution behavior.

### 14.7 Security benchmark

Include prompt injection in source metadata, corrupted citations, stale
snapshots, replayed proposals, duplicate IDs, forged human choices, malicious
tool output, secret requests, poisoned memory, colluding agents, and recursive
fan-out/cost attacks.

### 14.8 Reproducibility rules

- Freeze targets, artifacts, capabilities, evidence, providers, and agent specs.
- Bind model/checkpoint, prompt/policy, tool, and source identities.
- Repeat stochastic trials and report confidence intervals.
- Report all rejected/incomplete runs, not only best-of-k.
- Do not use an LLM as the sole judge.
- Validate execution outputs and artifact identities.
- Test event-order and crash/restart invariance.
- Split evaluation by graph family, source, model, software version, and time.

## 15. Staged implementation roadmap

This roadmap follows Stage 10D. Its numbering is independent of the historical
Stage 0–10 implementation sequence.

### Agentic A0 — deterministic prerequisites and benchmark corpus

Build:

- production application facade;
- target-template registry;
- resolution-to-binding/compiler service;
- native artifact input receipt bridge;
- authenticated human-decision receipt;
- durable runtime observation schema;
- frozen target/discovery/critic/security benchmark corpus.

Exit gate:

- one non-agent target travels from template to validated plan and executable
  artifact path without a stage-demo assembly;
- a discovered native artifact can feed an invocation through exact receipts;
- every later agent action has a proposal-only insertion point.

### Agentic A1 — proposal protocol and durable blackboard

Build typed descriptors, tasks, proposals, challenges, run receipts,
verification receipts, leases, budgets, replay, and epoch freezing. Use scripted
deterministic agents first.

Exit gate:

- invalid and stale proposals cannot enter authoritative state;
- duplicate/out-of-order delivery is idempotent;
- accepted fragment identity is independent of agent completion order;
- incomplete frontier work cannot become a completeness claim.

### Agentic A2 — single read-only target and explanation agents

Add one target interpreter over controlled templates and one plan/blocker
explainer. No external writes or runtime actions.

Exit gate:

- held-out target-field accuracy beats deterministic templates alone;
- all invented/ambiguous values are refused or clarified;
- the deterministic plan is unchanged by explanation;
- model outage falls back cleanly.

### Agentic A3 — parallel bounded discovery agents

Add domain, provider, artifact-retrieval, and onboarding scouts with frozen
scopes, leases, budgets, deterministic merge, and exact frontier accounting.

Exit gate:

- higher valid-candidate recall or lower discovery latency than one agent at a
  matched budget;
- zero verifier-bypassing candidates;
- graph, plan, and completeness identities replay after restart and shuffled
  event order;
- speedup and duplicate-work curves are reported across agent counts.

### Agentic A4 — critic society and evidence agents

Add independent rubric-specific critics, evidence retrieval, and verifier-
feedback revision loops.

Exit gate:

- materially higher seeded-defect recall than self-critique and same-model
  sampling at equal cost;
- no increase in false acceptance;
- every evidence claim binds exact source and applicability;
- consensus is never used as authority.

### Agentic A5 — supervised prediction and model routing

Train runtime/resource/failure models and a cost-aware router from durable
traces. Deploy in shadow mode before any influence.

Exit gate:

- calibrated held-out interval coverage and failure probabilities;
- OOD abstention and deterministic fallback;
- routing preserves accepted-proposal recall while reducing cost/latency;
- no declared envelope or runtime state is mutated by a prediction.

### Agentic A6 — bandit, BO, and active-learning experiments

Restrict actions to prevalidated finite choices and cheap pilot workloads.

Exit gate:

- propensities and delayed rewards are complete and replayable;
- conservative baseline performance and safety constraints are preserved;
- trial cost and regret beat fixed policies on held-out workloads;
- expensive/unsafe trials still require policy or human approval.

### Agentic A7 — RL scheduling in replay and canary mode

Add graph-state encoding, hard action masks, simulator/replay training,
deterministic fallback, and small safe canaries. MARL is a separate optional
experiment only after real multi-site control exists.

Exit gate:

- no readiness, resource, state, retry, quota, or artifact violation;
- statistically supported improvement over strong deterministic baselines;
- distribution-shift and simulator-gap results are reported;
- policy timeout/outage changes only performance, never correctness.

### Agentic A8 — publication evaluation

Run fixed-budget ablations, topology comparisons, attack tests, restart tests,
and at least one ordinary real producer path.

Exit gate:

- research claims name exact deployment, workloads, models, snapshots, costs,
  confidence intervals, and non-claims;
- agent order does not change accepted scientific results;
- end-to-end execution still passes deterministic validation and artifact
  authority without an agent shortcut.

## 16. Proposed code organization

Do not extend the legacy `agentic/` package in place. Create a new package with
an explicit authority-neutral name, for example:

```text
NASA_Project/intelligence/
  types.py             # descriptors, tasks, proposals, receipts
  store.py             # append-only SQLite proposal blackboard
  broker.py            # leases, deadlines, budgets, routing
  protocol.py          # closed state machine and performatives
  verifier.py          # schema/identity/staleness checks and adapters
  adapters/
    target.py
    artifact_search.py
    discovery.py
    evidence.py
    critic.py
    explanation.py
    scheduling.py
  learning/
    features.py
    estimates.py
    routing.py
    replay.py
  security.py
  evaluation.py
```

Current authoritative packages remain unchanged in responsibility. Integration
occurs through public typed APIs and separately verified receipts, not private
builder state or shared mutable dictionaries.

## 17. Publishable research questions

The following are defensible hypotheses, not current claims:

1. Do parallel domain-specialist agents improve valid derivation recall or time
   to first valid plan over a single general agent at equal compute?
2. Do typed evidence-carrying proposals plus deterministic verification improve
   discovery recall without increasing unsafe acceptance?
3. Does a content-addressed blackboard reduce duplicate work and error
   propagation compared with free-form group chat?
4. Do heterogeneous rubric-specific critics detect more scientific defects
   than self-critique or homogeneous debate?
5. Can learned routing reduce LLM/tool cost while preserving accepted-fragment
   recall under source and target distribution shift?
6. Can supervised or GNN estimates improve scheduling while hard action shields
   preserve all runtime invariants?
7. When does a contextual bandit outperform a fixed policy without unsafe
   exploration?
8. Can BO or active learning reduce the number of expensive evidence-generating
   runs while preserving declared safety and uncertainty?
9. Are accepted graph and plan identities invariant to asynchronous agent order,
   crash, retry, and model substitution?
10. Which coordination topology gives the best valid-scientific-work-per-cost,
    rather than the most fluent conversation?

The likely paper contribution is not a new chat framework. It is the measured
combination of heterogeneous agent discovery, typed scientific contracts,
proof/evidence-carrying proposals, frozen recursive hypergraphs, exact global
selection, independent validation, and native-artifact provenance.

## 18. Explicit non-claims

- LLM reasoning, reflection, debate, or agreement does not establish scientific
  correctness.
- Schema-valid JSON is not a compatibility proof.
- Retrieval confidence is not evidence applicability.
- A learned ranker or warm start does not establish optimality.
- An agent’s failure to find a producer does not establish completeness.
- Simulated RL improvement is not a live-cluster result.
- A surrogate is not interchangeable with a full-physics producer.
- Existing agent-science papers do not validate this project’s domains or
  deployments.
- The current repository does not yet provide an authoritative WRF/MPI agent
  execution path.
- No multi-agent layer should be described as autonomous science until its
  individual tasks, authority boundaries, attacks, costs, and failure modes are
  measured.

## 19. Research basis

The plan is informed by the following primary sources and standards:

### Language and scientific agents

- Yao et al., [ReAct: Synergizing Reasoning and Acting in Language
  Models](https://arxiv.org/abs/2210.03629).
- Shinn et al., [Reflexion: Language Agents with Verbal Reinforcement
  Learning](https://arxiv.org/abs/2303.11366).
- Yao et al., [Tree of Thoughts: Deliberate Problem Solving with Large
  Language Models](https://arxiv.org/abs/2305.10601).
- Zhou et al., [Language Agent Tree Search Unifies Reasoning, Acting, and
  Planning in Language Models](https://proceedings.mlr.press/v235/zhou24r.html).
- Wu et al., [AutoGen: Enabling Next-Gen LLM Applications via Multi-Agent
  Conversation](https://arxiv.org/abs/2308.08155).
- Du et al., [Improving Factuality and Reasoning in Language Models through
  Multiagent Debate](https://arxiv.org/abs/2305.14325).
- Bran et al., [Augmenting large language models with chemistry
  tools](https://www.nature.com/articles/s42256-024-00832-8).
- Boiko et al., [Autonomous chemical research with large language
  models](https://www.nature.com/articles/s41586-023-06792-0).
- Chen et al., [ScienceAgentBench](https://arxiv.org/abs/2410.05080).

### Multi-agent coordination, provenance, and security

- FIPA, [Contract Net Interaction Protocol
  Specification](https://www.fipa.org/specs/fipa00029/SC00029H.html).
- Hayes-Roth, [A blackboard architecture for
  control](https://www.sciencedirect.com/science/article/pii/0004370285900633).
- W3C, [PROV-O: The PROV Ontology](https://www.w3.org/TR/prov-o/).
- Debenedetti et al., [AgentDojo: A Dynamic Environment to Evaluate Prompt
  Injection Attacks and Defenses for LLM
  Agents](https://arxiv.org/abs/2406.13352).

### Optimization and learning

- Google OR-Tools, [CP-SAT solver
  documentation](https://developers.google.com/optimization/cp).
- Nau et al., [SHOP2: An HTN Planning
  System](https://arxiv.org/abs/1106.4869).
- Topcuoglu et al., [Performance-effective and low-complexity task scheduling
  for heterogeneous
  computing](https://doi.org/10.1109/71.993206).
- Gasse et al., [Exact Combinatorial Optimization with Graph Convolutional
  Neural Networks](https://arxiv.org/abs/1906.01629).
- Mao et al., [Learning Scheduling Algorithms for Data Processing Clusters
  (Decima)](https://web.mit.edu/decima/index.html).
- Chu et al., [Contextual Bandits with Linear Payoff
  Functions](https://proceedings.mlr.press/v15/chu11a.html).
- Snoek et al., [Practical Bayesian Optimization of Machine Learning
  Algorithms](https://papers.nips.cc/paper_files/paper/2012/hash/05311655a15b75fab86956663e1819cd-Abstract.html).
- Sui et al., [Safe Exploration for Optimization with Gaussian
  Processes](https://proceedings.mlr.press/v37/sui15.html).
- Kandasamy et al., [Multi-fidelity Bayesian Optimisation with Continuous
  Approximations](https://proceedings.mlr.press/v70/kandasamy17a.html).
- Yu et al., [The Surprising Effectiveness of PPO in Cooperative, Multi-Agent
  Games](https://arxiv.org/abs/2103.01955).
- Du et al., [DeepLog: Anomaly Detection and Diagnosis from System
  Logs](https://users.cs.utah.edu/~lifeifei/papers/deeplog.pdf).
- Raissi et al., [Physics Informed Deep Learning, Part
  I](https://arxiv.org/abs/1711.10561).
- Li et al., [Fourier Neural Operator for Parametric Partial Differential
  Equations](https://arxiv.org/abs/2010.08895).
- Lu et al., [Learning nonlinear operators via
  DeepONet](https://arxiv.org/abs/1910.03193).

These sources motivate candidate methods. None is treated as evidence that a
method works for this project until it passes the staged evaluation above.
