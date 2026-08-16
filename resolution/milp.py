"""Exact global selection over a frozen feasible derivation graph.

The recursive discovery layer deliberately does not live here.  It freezes its
result as :class:`composition.oracle.OracleProblem`, the same explicit graph
projection consumed by the independent Stage-2 exhaustive oracle.  This module
then selects a globally minimum-cost derivation with SciPy/HiGHS.

The primary integer-cost objective and deterministic bit-vector ordering are
solved in separate phases.  Tie bits are handled in exact, bounded-width
lexicographic chunks, so they cannot change the primary optimum or overflow a
double's exact-integer range.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Sequence

from composition.oracle import (
    ArtifactLeafNode,
    DistinctBy,
    InvocationNode,
    OracleProblem,
    RequirementUseNode,
    SatisfactionArc,
)
from engine.runtime.identity import freeze_json, strict_copy, strict_hash
from plans import (
    CandidateDerivationPlan,
    CompatibilityProofRecord,
    ProducerKind,
    SatisfactionBinding,
    SatisfactionKind,
)


_MAX_EXACT_FLOAT_INTEGER = (1 << 31) - 1
_FEASIBILITY_TOLERANCE = 1e-6
_LEXICOGRAPHIC_CHUNK_BITS = 30


def _text(value: str, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")


def _nonnegative_integer(value: int, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")


def _finite_positive(value: float, label: str) -> None:
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(float(value)) or float(value) <= 0):
        raise ValueError(f"{label} must be finite and positive")


@dataclass(frozen=True, order=True)
class ProducerSelectionRef:
    """A globally unambiguous producer constraint."""

    producer_kind: ProducerKind
    producer_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.producer_kind, ProducerKind):
            raise TypeError("producer selection requires typed ProducerKind")
        _text(self.producer_id, "producer selection identity")

    def to_dict(self) -> dict[str, str]:
        return {
            "producer_kind": self.producer_kind.value,
            "producer_id": self.producer_id,
        }


@dataclass(frozen=True)
class SelectionConstraints:
    """Request-scoped hard constraints, separate from graph discovery."""

    include: tuple[ProducerSelectionRef, ...] = ()
    exclude: tuple[ProducerSelectionRef, ...] = ()
    maximum_cost_units: int | None = None

    def __post_init__(self) -> None:
        for values, label in ((self.include, "included producers"),
                              (self.exclude, "excluded producers")):
            if (not isinstance(values, tuple)
                    or not all(isinstance(value, ProducerSelectionRef)
                               for value in values)
                    or values != tuple(sorted(values))
                    or len(values) != len(set(values))):
                raise ValueError(f"{label} must be unique and canonically ordered")
        if set(self.include) & set(self.exclude):
            raise ValueError("one producer cannot be both included and excluded")
        if self.maximum_cost_units is not None:
            _nonnegative_integer(self.maximum_cost_units, "maximum cost")
            if self.maximum_cost_units > _MAX_EXACT_FLOAT_INTEGER:
                raise ValueError("maximum cost exceeds exact MILP integer range")

    @classmethod
    def bind(
            cls, *, include: Iterable[ProducerSelectionRef] = (),
            exclude: Iterable[ProducerSelectionRef] = (),
            maximum_cost_units: int | None = None,
    ) -> "SelectionConstraints":
        return cls(
            include=tuple(sorted(include)),
            exclude=tuple(sorted(exclude)),
            maximum_cost_units=maximum_cost_units,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "include": [value.to_dict() for value in self.include],
            "exclude": [value.to_dict() for value in self.exclude],
            "maximum_cost_units": self.maximum_cost_units,
        }


@dataclass(frozen=True, order=True)
class DeploymentOption:
    """One frozen static site/environment class for an invocation.

    Feasibility is a planning-snapshot fact.  It neither reserves resources nor
    predicts queue availability.
    """

    invocation_id: str
    site_class_id: str
    feasible: bool = True
    blocker_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _text(self.invocation_id, "deployment invocation identity")
        _text(self.site_class_id, "deployment site-class identity")
        if type(self.feasible) is not bool:
            raise TypeError("deployment feasibility must be bool")
        if (not isinstance(self.blocker_codes, tuple)
                or any(not isinstance(value, str) or not value
                       for value in self.blocker_codes)
                or self.blocker_codes != tuple(sorted(self.blocker_codes))
                or len(self.blocker_codes) != len(set(self.blocker_codes))):
            raise ValueError("deployment blocker codes must be unique and ordered")

    @property
    def choice_id(self) -> str:
        return strict_hash(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "invocation_id": self.invocation_id,
            "site_class_id": self.site_class_id,
            "feasible": self.feasible,
            "blocker_codes": list(self.blocker_codes),
        }


@dataclass(frozen=True)
class MilpSelectionProblem:
    """Frozen Stage-3 selector input over one explored hypergraph."""

    selection_problem_id: str
    graph: OracleProblem
    discovery_complete: bool
    discovery_limit_codes: tuple[str, ...]
    deployment_options: tuple[DeploymentOption, ...]
    constraints: SelectionConstraints

    def __post_init__(self) -> None:
        _text(self.selection_problem_id, "selection problem identity")
        if not isinstance(self.graph, OracleProblem):
            raise TypeError("selection graph must be OracleProblem")
        if type(self.discovery_complete) is not bool:
            raise TypeError("discovery completeness must be bool")
        if (not isinstance(self.discovery_limit_codes, tuple)
                or any(not isinstance(value, str) or not value
                       for value in self.discovery_limit_codes)
                or self.discovery_limit_codes
                != tuple(sorted(self.discovery_limit_codes))
                or len(self.discovery_limit_codes)
                != len(set(self.discovery_limit_codes))):
            raise ValueError("discovery limit codes must be unique and ordered")
        if (not isinstance(self.deployment_options, tuple)
                or not all(isinstance(value, DeploymentOption)
                           for value in self.deployment_options)
                or self.deployment_options
                != tuple(sorted(self.deployment_options))
                or len({(value.invocation_id, value.site_class_id)
                        for value in self.deployment_options})
                != len(self.deployment_options)):
            raise ValueError("deployment options must be unique and ordered")
        if not isinstance(self.constraints, SelectionConstraints):
            raise TypeError("selection constraints are invalid")
        invocation_ids = {value.invocation_id for value in self.graph.invocations}
        if any(value.invocation_id not in invocation_ids
               for value in self.deployment_options):
            raise ValueError("deployment option references an unknown invocation")
        known = {
            ProducerSelectionRef(ProducerKind.INVOCATION, value.invocation_id)
            for value in self.graph.invocations
        } | {
            ProducerSelectionRef(ProducerKind.ARTIFACT_LEAF, value.leaf_id)
            for value in self.graph.artifact_leaves
        }
        if not set(self.constraints.include).issubset(known):
            raise ValueError("include constraint references an unknown producer")
        if not set(self.constraints.exclude).issubset(known):
            raise ValueError("exclude constraint references an unknown producer")
        if strict_hash(self._identity_payload()) != self.selection_problem_id:
            raise ValueError("selection problem identity does not verify")

    @classmethod
    def bind(
            cls, graph: OracleProblem, *, discovery_complete: bool = True,
            discovery_limit_codes: Iterable[str] = (),
            deployment_options: Iterable[DeploymentOption] | None = None,
            constraints: SelectionConstraints | None = None,
    ) -> "MilpSelectionProblem":
        if not isinstance(graph, OracleProblem):
            raise TypeError("selection graph must be OracleProblem")
        if type(discovery_complete) is not bool:
            raise TypeError("discovery completeness must be bool")
        if deployment_options is None:
            # Exact adapter for the Stage-2 projection.  New discovery code
            # should supply real frozen site classes instead.
            deployment_values = tuple(DeploymentOption(
                invocation_id=value.invocation_id,
                site_class_id=f"stage2-declared:{value.invocation_id}",
                feasible=value.deployable,
                blocker_codes=() if value.deployable else (
                    "DEPLOYMENT_INFEASIBLE",),
            ) for value in graph.invocations)
        else:
            deployment_values = tuple(sorted(deployment_options))
        constraint_values = constraints or SelectionConstraints()
        values = {
            "graph": graph,
            "discovery_complete": discovery_complete,
            "discovery_limit_codes": tuple(sorted(set(
                discovery_limit_codes))),
            "deployment_options": deployment_values,
            "constraints": constraint_values,
        }
        provisional = object.__new__(cls)
        for name, value in values.items():
            object.__setattr__(provisional, name, value)
        return cls(
            selection_problem_id=strict_hash(provisional._identity_payload()),
            **values,
        )

    @classmethod
    def from_hypergraph(
            cls, graph: Any, *,
            constraints: SelectionConstraints | None = None,
    ) -> "MilpSelectionProblem":
        """Losslessly adapt a Stage-3 discovery result and its real sites.

        Imports are local to keep this selector usable as the independent
        Stage-2 oracle backend even when recursive discovery is not loaded.
        """
        from resolution.hypergraph import FeasibleDerivationHypergraph
        from resolution.projection import project_oracle_problem

        if not isinstance(graph, FeasibleDerivationHypergraph):
            raise TypeError("graph must be FeasibleDerivationHypergraph")
        proofs = {value.profile_id: value
                  for value in graph.deployment_proofs}
        options: list[DeploymentOption] = []
        for node in graph.invocation_nodes:
            proof = proofs[node.invocation.execution_profile_id]
            for site in proof.sites:
                options.append(DeploymentOption(
                    invocation_id=node.invocation_id,
                    site_class_id=site.site_class_id,
                    feasible=site.feasible,
                    blocker_codes=tuple(sorted(
                        value.code.value for value in site.rejections)),
                ))
        return cls.bind(
            project_oracle_problem(graph),
            discovery_complete=graph.discovery_complete,
            discovery_limit_codes=(value.code.value
                                   for value in graph.limit_reasons),
            deployment_options=options,
            constraints=constraints,
        )

    def _identity_payload(self) -> dict[str, Any]:
        return {
            "schema": "stage3-milp-selection-problem-v1",
            "graph_problem_id": self.graph.problem_id,
            "discovery_complete": self.discovery_complete,
            "discovery_limit_codes": list(self.discovery_limit_codes),
            "deployment_options": [value.to_dict()
                                   for value in self.deployment_options],
            "constraints": self.constraints.to_dict(),
        }


@dataclass(frozen=True)
class MilpSolveOptions:
    """HiGHS limits; none of these alter graph discovery semantics."""

    # Interactive service default.  Explicit ``None`` remains available for
    # caller-controlled exact/offline runs.
    time_limit_s: float | None = 30.0
    node_limit: int | None = None
    mip_relative_gap: float = 0.0
    # HiGHS presolve has produced a false-infeasible result for a valid small
    # integer formulation in the deployed SciPy/HiGHS build.  That is handled
    # rather than avoided: every presolved infeasibility is re-confirmed on the
    # unpresolved model before it can become a scientific answer, so presolve
    # cannot turn a feasible problem into UNSAT.  With that guard in place the
    # speedup is free -- about 37% off a representative solve -- so it is on by
    # default.  The false-UNSAT regression test still runs with presolve=True.
    presolve: bool = True
    display_solver_output: bool = False

    def __post_init__(self) -> None:
        if self.time_limit_s is not None:
            _finite_positive(self.time_limit_s, "MILP time limit")
        if self.node_limit is not None:
            if (isinstance(self.node_limit, bool)
                    or not isinstance(self.node_limit, int)
                    or self.node_limit < 1):
                raise ValueError("MILP node limit must be a positive integer")
        if (isinstance(self.mip_relative_gap, bool)
                or not isinstance(self.mip_relative_gap, (int, float))
                or not math.isfinite(float(self.mip_relative_gap))
                or not 0 <= float(self.mip_relative_gap) < 1):
            raise ValueError("MILP relative gap must be finite and in [0,1)")
        if (type(self.presolve) is not bool
                or type(self.display_solver_output) is not bool):
            raise TypeError("MILP flags must be bool")


class MilpStatus(str, Enum):
    OPTIMAL = "OPTIMAL"
    FEASIBLE_NOT_PROVEN_OPTIMAL = "FEASIBLE_NOT_PROVEN_OPTIMAL"
    LIMIT_NO_INCUMBENT = "LIMIT_NO_INCUMBENT"
    UNSATISFIABLE = "UNSATISFIABLE"
    ERROR = "ERROR"


@dataclass(frozen=True)
class SelectionBlocker:
    code: str
    subject_id: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _text(self.code, "selection blocker code")
        _text(self.subject_id, "selection blocker subject")
        _text(self.message, "selection blocker message")
        object.__setattr__(self, "details", freeze_json(self.details))
        if not isinstance(self.details, dict):
            raise ValueError("selection blocker details must be an object")

    @property
    def blocker_id(self) -> str:
        return strict_hash(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "subject_id": self.subject_id,
            "message": self.message,
            "details": strict_copy(self.details),
        }


@dataclass(frozen=True)
class MilpSelectionResult:
    selection_problem_id: str
    graph_problem_id: str
    status: MilpStatus
    discovery_complete: bool
    primary_cost_proven: bool
    tie_break_complete: bool
    plan: CandidateDerivationPlan | None
    deployment_choices: tuple[tuple[str, str], ...]
    objective_cost_units: int | None
    solver_status: int | None
    solver_message: str
    mip_gap: float | None
    mip_node_count: int | None
    solver_calls: int
    solve_seconds: float
    blockers: tuple[SelectionBlocker, ...] = ()

    def __post_init__(self) -> None:
        for value, label in (
                (self.selection_problem_id, "selection result identity"),
                (self.graph_problem_id, "selection graph identity"),
                (self.solver_message, "solver message")):
            _text(value, label)
        if not isinstance(self.status, MilpStatus):
            raise TypeError("selection result status is invalid")
        if (type(self.discovery_complete) is not bool
                or type(self.primary_cost_proven) is not bool
                or type(self.tie_break_complete) is not bool):
            raise TypeError("selection result proof flags must be bool")
        if self.plan is not None:
            if not isinstance(self.plan, CandidateDerivationPlan):
                raise TypeError("selection result plan is invalid")
            self.plan.validate_identity()
            if self.plan.discovery_complete is not self.discovery_complete:
                raise ValueError("plan and selector discovery completeness disagree")
            if self.plan.oracle_problem_id != self.graph_problem_id:
                raise ValueError("plan covers a different graph")
        if self.status in (
                MilpStatus.OPTIMAL,
                MilpStatus.FEASIBLE_NOT_PROVEN_OPTIMAL) and self.plan is None:
            raise ValueError("a feasible result requires a validated plan")
        if self.status in (
                MilpStatus.LIMIT_NO_INCUMBENT,
                MilpStatus.UNSATISFIABLE,
                MilpStatus.ERROR):
            if self.plan is not None:
                raise ValueError("an infeasible/error result cannot carry a plan")
        if self.status is MilpStatus.OPTIMAL:
            if not self.primary_cost_proven or not self.tie_break_complete:
                raise ValueError("optimal status requires complete objective proofs")
        if self.objective_cost_units is not None:
            _nonnegative_integer(self.objective_cost_units, "result objective")
            if self.plan is None or (
                    self.plan.total_cost_units != self.objective_cost_units):
                raise ValueError("result objective disagrees with its plan")
        if (not isinstance(self.deployment_choices, tuple)
                or self.deployment_choices
                != tuple(sorted(self.deployment_choices))
                or len({value[0] for value in self.deployment_choices})
                != len(self.deployment_choices)):
            raise ValueError("deployment choices must be unique and ordered")
        if self.plan is not None and (
                {value[0] for value in self.deployment_choices}
                != set(self.plan.selected_invocation_ids)):
            raise ValueError("deployment choices do not cover selected invocations")
        if self.solver_status is not None and (
                isinstance(self.solver_status, bool)
                or not isinstance(self.solver_status, int)):
            raise TypeError("solver status must be an integer")
        if self.mip_gap is not None and (
                not math.isfinite(self.mip_gap) or self.mip_gap < 0):
            raise ValueError("MIP gap must be finite and non-negative")
        if self.mip_node_count is not None:
            _nonnegative_integer(self.mip_node_count, "MIP node count")
        _nonnegative_integer(self.solver_calls, "solver call count")
        if (not isinstance(self.solve_seconds, (int, float))
                or not math.isfinite(float(self.solve_seconds))
                or self.solve_seconds < 0):
            raise ValueError("solve time must be finite and non-negative")
        if (not isinstance(self.blockers, tuple)
                or not all(isinstance(value, SelectionBlocker)
                           for value in self.blockers)
                or self.blockers
                != tuple(sorted(self.blockers,
                                key=lambda value: value.blocker_id))):
            raise ValueError("selection blockers must be canonically ordered")

    @property
    def optimal_over_explored_graph(self) -> bool:
        return self.status is MilpStatus.OPTIMAL

    @property
    def globally_optimal_over_discovery_space(self) -> bool:
        return self.status is MilpStatus.OPTIMAL and self.discovery_complete


@dataclass
class _LinearModel:
    names: list[str] = field(default_factory=list)
    lower: list[float] = field(default_factory=list)
    upper: list[float] = field(default_factory=list)
    integrality: list[int] = field(default_factory=list)
    cost: list[float] = field(default_factory=list)
    rows: list[dict[int, float]] = field(default_factory=list)
    row_lower: list[float] = field(default_factory=list)
    row_upper: list[float] = field(default_factory=list)
    producer_index: dict[tuple[ProducerKind, str], int] = field(
        default_factory=dict)
    use_index: dict[str, int] = field(default_factory=dict)
    arc_index: dict[str, int] = field(default_factory=dict)
    default_index: dict[str, int] = field(default_factory=dict)
    omit_index: dict[str, int] = field(default_factory=dict)
    deployment_index: dict[tuple[str, str], int] = field(default_factory=dict)
    rank_index: dict[str, int] = field(default_factory=dict)
    signature_indices: list[int] = field(default_factory=list)
    producer_signature_indices: list[int] = field(default_factory=list)
    choice_signature_indices: list[int] = field(default_factory=list)
    choice_producer_index: dict[int, int] = field(default_factory=dict)
    deployment_signature_indices: list[int] = field(default_factory=list)

    def variable(
            self, name: str, *, lower: float = 0, upper: float = 1,
            integer: bool = True, cost: float = 0,
    ) -> int:
        index = len(self.names)
        self.names.append(name)
        self.lower.append(float(lower))
        self.upper.append(float(upper))
        self.integrality.append(1 if integer else 0)
        self.cost.append(float(cost))
        return index

    def constraint(
            self, coefficients: dict[int, float], *,
            lower: float = -math.inf, upper: float = math.inf,
    ) -> None:
        combined = {index: float(value)
                    for index, value in coefficients.items() if value != 0}
        self.rows.append(combined)
        self.row_lower.append(float(lower))
        self.row_upper.append(float(upper))


def _coefficients(*items: tuple[int, float]) -> dict[int, float]:
    result: dict[int, float] = {}
    for index, value in items:
        result[index] = result.get(index, 0.0) + value
    return {index: value for index, value in result.items() if value != 0}


def _producer_order(
        problem: OracleProblem,
) -> tuple[tuple[ProducerKind, str], ...]:
    return tuple(sorted(
        [(ProducerKind.INVOCATION, value.invocation_id)
         for value in problem.invocations]
        + [(ProducerKind.ARTIFACT_LEAF, value.leaf_id)
           for value in problem.artifact_leaves],
        key=lambda value: (value[0].value, value[1]),
    ))


def _nonproducer_choice(
        use: RequirementUseNode, kind: SatisfactionKind,
) -> SatisfactionBinding:
    return SatisfactionBinding(
        use_id=use.use_id,
        kind=kind,
        default_id=use.default_id if kind is SatisfactionKind.DEFAULT else None,
    )


def _build_model(request: MilpSelectionProblem) -> _LinearModel:
    graph = request.graph
    model = _LinearModel()
    invocation_by_id = {value.invocation_id: value
                        for value in graph.invocations}
    leaf_by_id = {value.leaf_id: value for value in graph.artifact_leaves}
    use_by_id = {value.use_id: value for value in graph.uses}
    mutable_arcs_by_use: dict[str, list[SatisfactionArc]] = {
        use.use_id: [] for use in graph.uses}
    for arc in graph.satisfaction_arcs:
        mutable_arcs_by_use[arc.use_id].append(arc)
    arcs_by_use = {
        key: tuple(values) for key, values in mutable_arcs_by_use.items()}
    feasible_deployment = {
        invocation.invocation_id: False for invocation in graph.invocations}
    for option in request.deployment_options:
        feasible_deployment[option.invocation_id] |= option.feasible

    maximum_possible_cost = sum(value.cost_units
                                for value in graph.invocations) + sum(
        value.cost_units for value in graph.artifact_leaves)
    if maximum_possible_cost > _MAX_EXACT_FLOAT_INTEGER:
        raise ValueError("candidate costs exceed exact MILP integer range")

    for kind, producer_id in _producer_order(graph):
        producer = (invocation_by_id[producer_id]
                    if kind is ProducerKind.INVOCATION
                    else leaf_by_id[producer_id])
        upper = 1.0
        if kind is ProducerKind.INVOCATION:
            if (not producer.deployable
                    or not feasible_deployment[producer.invocation_id]
                    or (not producer.input_use_ids
                        and not producer.zero_input_source)):
                upper = 0.0
        elif not producer.committed:
            upper = 0.0
        ref = ProducerSelectionRef(kind, producer_id)
        if ref in request.constraints.exclude:
            upper = 0.0
        lower = 1.0 if ref in request.constraints.include else 0.0
        model.producer_index[(kind, producer_id)] = model.variable(
            f"producer:{kind.value}:{producer_id}",
            lower=lower, upper=upper, cost=producer.cost_units)

    for use in graph.uses:
        model.use_index[use.use_id] = model.variable(
            f"active-use:{use.use_id}")
    for arc in graph.satisfaction_arcs:
        model.arc_index[arc.arc_id] = model.variable(
            f"satisfaction:{arc.arc_id}")
    for use in graph.uses:
        if use.default_id is not None:
            binding = _nonproducer_choice(use, SatisfactionKind.DEFAULT)
            model.default_index[use.use_id] = model.variable(
                f"default:{binding.choice_ids[0]}")
        if use.optional:
            binding = _nonproducer_choice(use, SatisfactionKind.OMIT)
            model.omit_index[use.use_id] = model.variable(
                f"omit:{binding.choice_ids[0]}")
    rank_upper = max(0, len(graph.invocations) - 1)
    for invocation in graph.invocations:
        model.rank_index[invocation.invocation_id] = model.variable(
            f"rank:{invocation.invocation_id}", upper=rank_upper)

    # Root and consumer-port activation.
    root_ids = set(graph.root_use_ids)
    for use in graph.uses:
        active = model.use_index[use.use_id]
        if use.use_id in root_ids:
            model.constraint({active: 1}, lower=1, upper=1)
        else:
            owner = model.producer_index[
                (ProducerKind.INVOCATION, use.owner_invocation_id)]
            model.constraint(_coefficients(
                (active, 1), (owner, -1)), lower=0, upper=0)

    # One explicit producer-mode/default/omit choice per active use, with
    # producer cardinality applying only in producer mode.
    for use in graph.uses:
        active = model.use_index[use.use_id]
        arc_variables = [model.arc_index[value.arc_id]
                         for value in arcs_by_use[use.use_id]]
        nonproducer = []
        if use.use_id in model.default_index:
            nonproducer.append(model.default_index[use.use_id])
        if use.use_id in model.omit_index:
            nonproducer.append(model.omit_index[use.use_id])
        model.constraint(_coefficients(
            *((index, 1) for index in nonproducer), (active, -1)), upper=0)
        producer_minimum = max(1, use.cardinality.minimum)
        model.constraint(_coefficients(
            *((index, 1) for index in arc_variables),
            (active, -producer_minimum),
            *((index, producer_minimum) for index in nonproducer)), lower=0)
        model.constraint(_coefficients(
            *((index, 1) for index in arc_variables),
            (active, -use.cardinality.maximum),
            *((index, use.cardinality.maximum) for index in nonproducer)),
            upper=0)

    # A selected edge implies its producer.  Conversely, no producer may be
    # selected merely to satisfy an include bit without contributing an output.
    outgoing: dict[tuple[ProducerKind, str], list[int]] = {
        key: [] for key in model.producer_index
    }
    for arc in graph.satisfaction_arcs:
        z_index = model.arc_index[arc.arc_id]
        key = (arc.producer_kind, arc.producer_id)
        x_index = model.producer_index[key]
        outgoing[key].append(z_index)
        model.constraint(_coefficients(
            (z_index, 1), (x_index, -1)), upper=0)
    for key, x_index in model.producer_index.items():
        model.constraint(_coefficients(
            (x_index, 1), *((index, -1) for index in outgoing[key])),
            upper=0)

    # Static deployment has no cross-invocation coupling in Stage 3.  A
    # producer is admissible only when at least one frozen site is feasible;
    # the canonical site is chosen after scientific selection.  Site binaries
    # would add thousands of redundant variables and misleadingly imply that
    # this stage performs resource scheduling.

    if request.constraints.maximum_cost_units is not None:
        model.constraint({
            index: model.cost[index]
            for index in model.producer_index.values()
            if model.cost[index] != 0
        }, upper=request.constraints.maximum_cost_units)

    # Per-use and cross-use distinctness.
    for use in graph.uses:
        if use.distinct_by is DistinctBy.PRODUCER:
            groups: dict[tuple[ProducerKind, str], list[int]] = {}
            for arc in arcs_by_use[use.use_id]:
                groups.setdefault(
                    (arc.producer_kind, arc.producer_id), []).append(
                        model.arc_index[arc.arc_id])
            for indices in groups.values():
                if len(indices) > 1:
                    model.constraint({index: 1 for index in indices}, upper=1)

    arcs_by_output: dict[tuple[ProducerKind, str, str], list[SatisfactionArc]] = {}
    for arc in graph.satisfaction_arcs:
        arcs_by_output.setdefault(
            (arc.producer_kind, arc.producer_id, arc.output_port_id),
            []).append(arc)
    for arcs in arcs_by_output.values():
        for position, first in enumerate(arcs):
            first_use = use_by_id[first.use_id]
            for second in arcs[position + 1:]:
                second_use = use_by_id[second.use_id]
                if first.use_id != second.use_id and (
                        not first_use.shareable or not second_use.shareable):
                    model.constraint({
                        model.arc_index[first.arc_id]: 1,
                        model.arc_index[second.arc_id]: 1,
                    }, upper=1)

    distinct_groups: dict[str, list[SatisfactionArc]] = {}
    for arc in graph.satisfaction_arcs:
        group = use_by_id[arc.use_id].distinctness_group
        if group is not None:
            distinct_groups.setdefault(group, []).append(arc)
    for arcs in distinct_groups.values():
        distinct_by = use_by_id[arcs[0].use_id].distinct_by
        keyed: dict[tuple[str, ...], list[int]] = {}
        for arc in arcs:
            key = ((arc.producer_kind.value, arc.producer_id)
                   if distinct_by is DistinctBy.PRODUCER else
                   (arc.producer_kind.value, arc.producer_id,
                    arc.output_port_id))
            keyed.setdefault(key, []).append(model.arc_index[arc.arc_id])
        for indices in keyed.values():
            if len(indices) > 1:
                model.constraint({index: 1 for index in indices}, upper=1)

    # Topological ranks make every selected invocation dependency acyclic.
    invocation_count = len(graph.invocations)
    if invocation_count:
        big_m = invocation_count
        for arc in graph.satisfaction_arcs:
            use = use_by_id[arc.use_id]
            if (use.owner_invocation_id is None
                    or arc.producer_kind is not ProducerKind.INVOCATION):
                continue
            producer_rank = model.rank_index[arc.producer_id]
            owner_rank = model.rank_index[use.owner_invocation_id]
            selected_edge = model.arc_index[arc.arc_id]
            model.constraint(_coefficients(
                (producer_rank, 1), (owner_rank, -1),
                (selected_edge, big_m)), upper=big_m - 1)

    # Stage-2's stable ordering is retained, so bounded oracle and MILP
    # solutions can be compared directly.
    model.producer_signature_indices.extend(
        model.producer_index[key] for key in _producer_order(graph))
    model.signature_indices.extend(model.producer_signature_indices)
    choice_variables: dict[str, int] = {
        arc.arc_id: model.arc_index[arc.arc_id]
        for arc in graph.satisfaction_arcs
    }
    for use in graph.uses:
        if use.use_id in model.default_index:
            choice_variables[
                _nonproducer_choice(use, SatisfactionKind.DEFAULT)
                .choice_ids[0]] = model.default_index[use.use_id]
        if use.use_id in model.omit_index:
            choice_variables[
                _nonproducer_choice(use, SatisfactionKind.OMIT)
                .choice_ids[0]] = model.omit_index[use.use_id]
    model.choice_signature_indices.extend(
        choice_variables[key] for key in sorted(choice_variables))
    model.signature_indices.extend(model.choice_signature_indices)
    for arc in graph.satisfaction_arcs:
        model.choice_producer_index[model.arc_index[arc.arc_id]] = (
            model.producer_index[(arc.producer_kind, arc.producer_id)])
    return model


def _solver_arrays(model: _LinearModel, extra_rows: Sequence[
        tuple[dict[int, float], float, float]], objective: Sequence[float]):
    import numpy as np
    from scipy.optimize import Bounds, LinearConstraint
    from scipy.sparse import coo_array

    rows = list(model.rows) + [value[0] for value in extra_rows]
    lower = list(model.row_lower) + [value[1] for value in extra_rows]
    upper = list(model.row_upper) + [value[2] for value in extra_rows]
    row_indices: list[int] = []
    column_indices: list[int] = []
    data: list[float] = []
    for row_index, row in enumerate(rows):
        for column_index, coefficient in row.items():
            row_indices.append(row_index)
            column_indices.append(column_index)
            data.append(coefficient)
    matrix = coo_array(
        (np.asarray(data, dtype=float),
         (np.asarray(row_indices, dtype=np.int32),
          np.asarray(column_indices, dtype=np.int32))),
        shape=(len(rows), len(model.names)),
    ).tocsc()
    constraints = LinearConstraint(
        matrix, np.asarray(lower, dtype=float), np.asarray(upper, dtype=float))
    bounds = Bounds(np.asarray(model.lower), np.asarray(model.upper))
    return (np.asarray(objective, dtype=float),
            np.asarray(model.integrality, dtype=np.int32), bounds, constraints)


def _validated_vector(
        model: _LinearModel, vector: Any, *,
        extra_rows: Sequence[tuple[dict[int, float], float, float]] = (),
) -> list[float] | None:
    if vector is None or len(vector) != len(model.names):
        return None
    values = [float(value) for value in vector]
    if any(not math.isfinite(value) for value in values):
        return None
    for index, value in enumerate(values):
        if (value < model.lower[index] - _FEASIBILITY_TOLERANCE
                or value > model.upper[index] + _FEASIBILITY_TOLERANCE):
            return None
        if (model.integrality[index]
                and abs(value - round(value)) > _FEASIBILITY_TOLERANCE):
            return None
    for row, lower, upper in zip(
            model.rows, model.row_lower, model.row_upper, strict=True):
        value = sum(values[index] * coefficient
                    for index, coefficient in row.items())
        if (value < lower - _FEASIBILITY_TOLERANCE
                or value > upper + _FEASIBILITY_TOLERANCE):
            return None
    for row, lower, upper in extra_rows:
        value = sum(values[index] * coefficient
                    for index, coefficient in row.items())
        if (value < lower - _FEASIBILITY_TOLERANCE
                or value > upper + _FEASIBILITY_TOLERANCE):
            return None
    return values


def _selected(value: float) -> bool:
    return int(round(value)) == 1


def _decode_plan(
        request: MilpSelectionProblem, model: _LinearModel,
        vector: list[float],
) -> tuple[CandidateDerivationPlan, tuple[tuple[str, str], ...]]:
    graph = request.graph
    selected_producers = {
        key for key, index in model.producer_index.items()
        if _selected(vector[index])
    }
    selected_invocations = tuple(sorted(
        producer_id for kind, producer_id in selected_producers
        if kind is ProducerKind.INVOCATION))
    selected_leaves = tuple(sorted(
        producer_id for kind, producer_id in selected_producers
        if kind is ProducerKind.ARTIFACT_LEAF))
    mutable_arcs_by_use: dict[str, list[SatisfactionArc]] = {
        use.use_id: [] for use in graph.uses}
    for arc in graph.satisfaction_arcs:
        mutable_arcs_by_use[arc.use_id].append(arc)
    arcs_by_use = {
        key: tuple(values) for key, values in mutable_arcs_by_use.items()}
    satisfactions: list[SatisfactionBinding] = []
    for use in graph.uses:
        if not _selected(vector[model.use_index[use.use_id]]):
            continue
        outputs = tuple(sorted(
            (arc.output_ref for arc in arcs_by_use[use.use_id]
             if _selected(vector[model.arc_index[arc.arc_id]])),
            key=lambda value: value.choice_id,
        ))
        default_selected = (use.use_id in model.default_index and _selected(
            vector[model.default_index[use.use_id]]))
        omit_selected = (use.use_id in model.omit_index and _selected(
            vector[model.omit_index[use.use_id]]))
        if outputs:
            binding = SatisfactionBinding(
                use_id=use.use_id,
                kind=SatisfactionKind.PRODUCERS,
                outputs=outputs,
            )
        elif default_selected:
            binding = _nonproducer_choice(use, SatisfactionKind.DEFAULT)
        elif omit_selected:
            binding = _nonproducer_choice(use, SatisfactionKind.OMIT)
        else:
            raise ValueError(f"active use {use.use_id!r} has no satisfaction")
        satisfactions.append(binding)

    proof_by_id = {value.proof.proof_id: value.proof
                   for value in graph.satisfaction_arcs}
    proof_ids = sorted({output.proof_id
                        for binding in satisfactions
                        for output in binding.outputs})
    proofs = tuple(proof_by_id[value] for value in proof_ids)
    invocation_by_id = {value.invocation_id: value
                        for value in graph.invocations}
    leaf_by_id = {value.leaf_id: value for value in graph.artifact_leaves}
    total_cost = sum(invocation_by_id[value].cost_units
                     for value in selected_invocations) + sum(
        leaf_by_id[value].cost_units for value in selected_leaves)
    signature = tuple(int(_selected(vector[index]))
                      for index in model.signature_indices)
    plan = CandidateDerivationPlan.bind(
        root_use_ids=graph.root_use_ids,
        root_requirements=tuple(
            (use_id, next(value.requirement_id for value in graph.uses
                          if value.use_id == use_id))
            for use_id in graph.root_use_ids),
        oracle_problem_id=graph.problem_id,
        discovery_complete=request.discovery_complete,
        selected_invocation_ids=selected_invocations,
        selected_artifact_leaf_ids=selected_leaves,
        satisfactions=satisfactions,
        compatibility_proofs=proofs,
        snapshot_refs=graph.snapshot_refs,
        total_cost_units=total_cost,
        selection_signature=signature,
    )
    deployments = _canonical_deployment_choices(
        request, selected_invocations)
    errors = _semantic_errors(request, plan, deployments)
    if errors:
        raise ValueError("decoded MILP incumbent failed validation: "
                         + "; ".join(errors))
    return plan, deployments


def _canonical_deployment_choices(
        request: MilpSelectionProblem,
        selected_invocations: tuple[str, ...],
) -> tuple[tuple[str, str], ...]:
    """Choose one deterministic feasible static site per invocation.

    Stage 3 models only independent feasibility, not shared site capacity or
    scheduling.  Coupled placement belongs to the later resource scheduler.
    """
    options_by_invocation: dict[str, list[DeploymentOption]] = {
        value: [] for value in selected_invocations}
    for option in request.deployment_options:
        if option.invocation_id in options_by_invocation and option.feasible:
            options_by_invocation[option.invocation_id].append(option)
    choices: list[tuple[str, str]] = []
    for invocation_id in selected_invocations:
        options = sorted(
            options_by_invocation[invocation_id],
            key=lambda value: (value.site_class_id, value.choice_id),
        )
        if not options:
            raise ValueError(
                f"selected invocation {invocation_id!r} has no feasible site")
        choices.append((invocation_id, options[0].site_class_id))
    return tuple(choices)


def _semantic_errors(
        request: MilpSelectionProblem, plan: CandidateDerivationPlan,
        deployments: tuple[tuple[str, str], ...],
) -> tuple[str, ...]:
    """Backend-independent safety check for decoded solver incumbents."""
    graph = request.graph
    errors: list[str] = []
    use_by_id = {value.use_id: value for value in graph.uses}
    invocation_by_id = {value.invocation_id: value
                        for value in graph.invocations}
    leaf_by_id = {value.leaf_id: value for value in graph.artifact_leaves}
    arc_keys = {
        (value.use_id, value.producer_kind, value.producer_id,
         value.output_port_id, value.proof.proof_id)
        for value in graph.satisfaction_arcs
    }
    selected = {
        ProducerSelectionRef(ProducerKind.INVOCATION, value)
        for value in plan.selected_invocation_ids
    } | {
        ProducerSelectionRef(ProducerKind.ARTIFACT_LEAF, value)
        for value in plan.selected_artifact_leaf_ids
    }
    if not set(request.constraints.include).issubset(selected):
        errors.append("included producer is absent")
    if set(request.constraints.exclude) & selected:
        errors.append("excluded producer is selected")
    expected_uses = set(graph.root_use_ids)
    for invocation_id in plan.selected_invocation_ids:
        expected_uses.update(invocation_by_id[invocation_id].input_use_ids)
    bindings = {value.use_id: value for value in plan.satisfactions}
    if set(bindings) != expected_uses:
        errors.append("active requirement-use set is incorrect")

    output_uses: dict[tuple[ProducerKind, str, str], list[str]] = {}
    distinct_groups: dict[str, list[tuple[RequirementUseNode, Any]]] = {}
    dependency_edges: dict[str, set[str]] = {}
    for use_id, binding in bindings.items():
        use = use_by_id[use_id]
        if binding.kind is SatisfactionKind.PRODUCERS:
            count = len(binding.outputs)
            if not use.cardinality.minimum <= count <= use.cardinality.maximum:
                errors.append(f"cardinality failed for {use_id}")
            distinct_keys: list[tuple[str, ...]] = []
            for output in binding.outputs:
                if ((use_id, output.producer_kind, output.producer_id,
                     output.output_port_id, output.proof_id) not in arc_keys):
                    errors.append(f"unknown satisfaction edge for {use_id}")
                ref = ProducerSelectionRef(
                    output.producer_kind, output.producer_id)
                if ref not in selected:
                    errors.append(f"edge producer is unselected for {use_id}")
                output_key = (output.producer_kind, output.producer_id,
                              output.output_port_id)
                output_uses.setdefault(output_key, []).append(use_id)
                if use.distinct_by is DistinctBy.PRODUCER:
                    distinct_keys.append((output.producer_kind.value,
                                          output.producer_id))
                elif use.distinct_by is DistinctBy.OUTPUT:
                    distinct_keys.append((output.producer_kind.value,
                                          output.producer_id,
                                          output.output_port_id))
                if use.distinctness_group is not None:
                    distinct_groups.setdefault(
                        use.distinctness_group, []).append((use, output))
                if (use.owner_invocation_id is not None
                        and output.producer_kind is ProducerKind.INVOCATION):
                    dependency_edges.setdefault(
                        output.producer_id, set()).add(
                            use.owner_invocation_id)
            if (use.distinct_by is not DistinctBy.NONE
                    and len(distinct_keys) != len(set(distinct_keys))):
                errors.append(f"within-use distinctness failed for {use_id}")
        elif binding.kind is SatisfactionKind.DEFAULT:
            if use.default_id != binding.default_id:
                errors.append(f"undeclared default for {use_id}")
        elif not use.optional:
            errors.append(f"required use {use_id} was omitted")

    for _output, use_ids in output_uses.items():
        if len(set(use_ids)) > 1 and any(
                not use_by_id[value].shareable for value in use_ids):
            errors.append("non-shareable output was reused")
    for group, values in distinct_groups.items():
        kind = values[0][0].distinct_by
        keys = [
            ((output.producer_kind.value, output.producer_id)
             if kind is DistinctBy.PRODUCER else
             (output.producer_kind.value, output.producer_id,
              output.output_port_id))
            for _use, output in values
        ]
        if len(keys) != len(set(keys)):
            errors.append(f"distinctness group {group} failed")

    option_by_key = {
        (value.invocation_id, value.site_class_id): value
        for value in request.deployment_options
    }
    if {value[0] for value in deployments} != set(
            plan.selected_invocation_ids):
        errors.append("deployment choices do not cover invocations")
    for key in deployments:
        option = option_by_key.get(key)
        if option is None or not option.feasible:
            errors.append(f"invalid deployment choice {key!r}")
    for invocation_id in plan.selected_invocation_ids:
        invocation = invocation_by_id[invocation_id]
        if not invocation.deployable:
            errors.append(f"undeployable invocation {invocation_id}")
        if not invocation.input_use_ids and not invocation.zero_input_source:
            errors.append(f"ungrounded invocation {invocation_id}")
    for leaf_id in plan.selected_artifact_leaf_ids:
        if not leaf_by_id[leaf_id].committed:
            errors.append(f"uncommitted artifact {leaf_id}")
    if request.constraints.maximum_cost_units is not None and (
            plan.total_cost_units > request.constraints.maximum_cost_units):
        errors.append("cost budget exceeded")
    if _has_cycle(tuple(invocation_by_id), dependency_edges):
        errors.append("selected invocation graph is cyclic")
    return tuple(errors)


def _has_cycle(
        invocation_ids: tuple[str, ...], edges: dict[str, set[str]],
) -> bool:
    states: dict[str, int] = {}

    def visit(node: str) -> bool:
        states[node] = 1
        for child in sorted(edges.get(node, ())):
            if states.get(child, 0) == 1:
                return True
            if states.get(child, 0) == 0 and visit(child):
                return True
        states[node] = 2
        return False

    return any(states.get(value, 0) == 0 and visit(value)
               for value in sorted(invocation_ids))


def _static_blockers(request: MilpSelectionProblem) -> tuple[SelectionBlocker, ...]:
    graph = request.graph
    values: list[SelectionBlocker] = []
    invocation_by_id = {value.invocation_id: value
                        for value in graph.invocations}
    leaf_by_id = {value.leaf_id: value for value in graph.artifact_leaves}
    mutable_options: dict[str, list[DeploymentOption]] = {
        invocation.invocation_id: [] for invocation in graph.invocations}
    for option in request.deployment_options:
        mutable_options[option.invocation_id].append(option)
    options_by_invocation = {
        key: tuple(values) for key, values in mutable_options.items()}
    use_by_id = {value.use_id: value for value in graph.uses}
    arcs_by_use: dict[str, list[SatisfactionArc]] = {
        value: [] for value in graph.root_use_ids}
    for arc in graph.satisfaction_arcs:
        if arc.use_id in arcs_by_use:
            arcs_by_use[arc.use_id].append(arc)
    for ref in request.constraints.include:
        if ref.producer_kind is ProducerKind.ARTIFACT_LEAF:
            if not leaf_by_id[ref.producer_id].committed:
                values.append(SelectionBlocker(
                    "ARTIFACT_NOT_COMMITTED", ref.producer_id,
                    "an explicitly included artifact is not committed"))
        else:
            invocation = invocation_by_id[ref.producer_id]
            feasible = [value for value in options_by_invocation[ref.producer_id]
                        if value.feasible]
            if not invocation.deployable or not feasible:
                codes = sorted({code for value in options_by_invocation[
                    ref.producer_id] for code in value.blocker_codes})
                values.append(SelectionBlocker(
                    "DEPLOYMENT_INFEASIBLE", ref.producer_id,
                    "an explicitly included invocation has no feasible "
                    "frozen deployment class",
                    {"site_blocker_codes": codes}))
            if not invocation.input_use_ids and not invocation.zero_input_source:
                values.append(SelectionBlocker(
                    "UNGROUNDED_INVOCATION", ref.producer_id,
                    "an explicitly included zero-input invocation is not a source"))
    if request.constraints.maximum_cost_units is not None:
        values.append(SelectionBlocker(
            "COST_BUDGET", request.selection_problem_id,
            "no derivation satisfies the declared maximum cost",
            {"maximum_cost_units": request.constraints.maximum_cost_units}))

    usable_producers = {
        (ProducerKind.ARTIFACT_LEAF, value.leaf_id)
        for value in graph.artifact_leaves if value.committed
    }
    usable_producers.update(
        (ProducerKind.INVOCATION, value.invocation_id)
        for value in graph.invocations
        if value.deployable and (value.input_use_ids or value.zero_input_source)
        and any(option.feasible
                for option in options_by_invocation[value.invocation_id]))
    for use_id in graph.root_use_ids:
        use = use_by_id[use_id]
        usable_arcs = [
            value for value in arcs_by_use[use_id]
            if (value.producer_kind, value.producer_id)
            in usable_producers]
        if (not usable_arcs and use.default_id is None and not use.optional):
            values.append(SelectionBlocker(
                "NO_ROOT_CANDIDATE", use_id,
                "no statically usable producer can satisfy this root use"))
    if not values:
        values.append(SelectionBlocker(
            "NO_FEASIBLE_DERIVATION", request.selection_problem_id,
            "the explored graph has no derivation satisfying all hard constraints"))
    unique = {value.blocker_id: value for value in values}
    return tuple(unique[key] for key in sorted(unique))


def _result(
        request: MilpSelectionProblem, *, status: MilpStatus,
        started: float, primary_cost_proven: bool = False,
        tie_break_complete: bool = False,
        plan: CandidateDerivationPlan | None = None,
        deployment_choices: tuple[tuple[str, str], ...] = (),
        solver_result: Any = None, solver_calls: int = 0,
        solver_message: str | None = None,
        blockers: tuple[SelectionBlocker, ...] = (),
) -> MilpSelectionResult:
    gap = getattr(solver_result, "mip_gap", None)
    gap = float(gap) if gap is not None and math.isfinite(float(gap)) else None
    nodes = getattr(solver_result, "mip_node_count", None)
    nodes = int(nodes) if nodes is not None else None
    message = solver_message or str(
        getattr(solver_result, "message", "selector did not invoke HiGHS"))
    return MilpSelectionResult(
        selection_problem_id=request.selection_problem_id,
        graph_problem_id=request.graph.problem_id,
        status=status,
        discovery_complete=request.discovery_complete,
        primary_cost_proven=primary_cost_proven,
        tie_break_complete=tie_break_complete,
        plan=plan,
        deployment_choices=deployment_choices,
        objective_cost_units=plan.total_cost_units if plan is not None else None,
        solver_status=(int(solver_result.status)
                       if solver_result is not None else None),
        solver_message=message,
        mip_gap=gap,
        mip_node_count=nodes,
        solver_calls=solver_calls,
        solve_seconds=time.perf_counter() - started,
        blockers=tuple(sorted(blockers, key=lambda value: value.blocker_id)),
    )


def solve_milp(
        request: MilpSelectionProblem, *,
        options: MilpSolveOptions | None = None,
) -> MilpSelectionResult:
    """Select and independently validate one global derivation.

    ``OPTIMAL`` means exact primary cost and deterministic tie order are proven
    over the explored frozen graph.  Discovery completeness remains a separate
    result field; callers must not infer global completeness from solver status.
    A limit with a valid incumbent returns
    ``FEASIBLE_NOT_PROVEN_OPTIMAL`` and never silently upgrades that incumbent.
    """
    if not isinstance(request, MilpSelectionProblem):
        raise TypeError("request must be MilpSelectionProblem")
    solve_options = options or MilpSolveOptions()
    if not isinstance(solve_options, MilpSolveOptions):
        raise TypeError("options must be MilpSolveOptions")
    started = time.perf_counter()
    try:
        from scipy.optimize import milp
    except Exception as exc:  # pragma: no cover - exercised without extras
        return _result(
            request, status=MilpStatus.ERROR, started=started,
            solver_message=f"SciPy/HiGHS is unavailable: {exc}",
            blockers=(SelectionBlocker(
                "SOLVER_UNAVAILABLE", request.selection_problem_id,
                "SciPy with scipy.optimize.milp is required"),),
        )
    try:
        model = _build_model(request)
    except Exception as exc:
        return _result(
            request, status=MilpStatus.ERROR, started=started,
            solver_message=f"MILP model construction failed: {exc}",
            blockers=(SelectionBlocker(
                "MODEL_CONSTRUCTION_ERROR", request.selection_problem_id,
                "the frozen selector problem could not be linearized",
                {"error": str(exc)}),),
        )

    deadline = (started + float(solve_options.time_limit_s)
                if solve_options.time_limit_s is not None else None)
    solver_calls = 0

    def invoke(objective: Sequence[float], extra_rows: Sequence[
            tuple[dict[int, float], float, float]]):
        nonlocal solver_calls
        arrays = _solver_arrays(model, extra_rows, objective)

        def call(*, presolve: bool):
            nonlocal solver_calls
            scipy_options: dict[str, Any] = {
                "disp": solve_options.display_solver_output,
                "presolve": presolve,
                "mip_rel_gap": float(solve_options.mip_relative_gap),
            }
            if solve_options.node_limit is not None:
                scipy_options["node_limit"] = solve_options.node_limit
            if deadline is not None:
                remaining = deadline - time.perf_counter()
                if remaining <= 0:
                    return None
                scipy_options["time_limit"] = remaining
            solver_calls += 1
            return milp(
                arrays[0], integrality=arrays[1], bounds=arrays[2],
                constraints=arrays[3], options=scipy_options)

        result = call(presolve=solve_options.presolve)
        # Never accept a presolver's infeasibility assertion as the scientific
        # answer without solving the original integer model.  If the global
        # deadline leaves no time for confirmation, return no result; the
        # caller reports a limit/error rather than a false UNSAT proof.
        if (result is not None and solve_options.presolve
                and int(result.status) == 2):
            return call(presolve=False)
        return result

    try:
        primary = invoke(model.cost, ())
    except Exception as exc:
        return _result(
            request, status=MilpStatus.ERROR, started=started,
            solver_calls=solver_calls,
            solver_message=f"HiGHS invocation failed: {exc}",
            blockers=(SelectionBlocker(
                "SOLVER_ERROR", request.selection_problem_id,
                "HiGHS failed while solving the global selector",
                {"error": str(exc)}),),
        )
    if primary is None:
        return _result(
            request, status=MilpStatus.LIMIT_NO_INCUMBENT, started=started,
            solver_calls=solver_calls,
            solver_message="MILP time limit expired before the primary solve",
            blockers=(SelectionBlocker(
                "TIME_LIMIT_NO_INCUMBENT", request.selection_problem_id,
                "no validated incumbent was found before the time limit"),),
        )
    if int(primary.status) == 2:
        return _result(
            request, status=MilpStatus.UNSATISFIABLE, started=started,
            solver_result=primary, solver_calls=solver_calls,
            blockers=_static_blockers(request),
        )
    primary_vector = _validated_vector(model, getattr(primary, "x", None))
    if primary_vector is None:
        code = ("UNBOUNDED_MODEL" if int(primary.status) == 3
                else "NO_VALID_INCUMBENT")
        status = (MilpStatus.LIMIT_NO_INCUMBENT
                  if int(primary.status) == 1 else MilpStatus.ERROR)
        return _result(
            request, status=status, started=started,
            solver_result=primary, solver_calls=solver_calls,
            blockers=(SelectionBlocker(
                code, request.selection_problem_id,
                "HiGHS did not return a valid integral incumbent"),),
        )

    primary_gap = getattr(primary, "mip_gap", None)
    exact_primary = (int(primary.status) == 0
                     and (primary_gap is None
                          or float(primary_gap) <= _FEASIBILITY_TOLERANCE))
    if not exact_primary:
        try:
            plan, deployments = _decode_plan(
                request, model, primary_vector)
        except Exception as exc:
            return _result(
                request, status=MilpStatus.ERROR, started=started,
                solver_result=primary, solver_calls=solver_calls,
                solver_message=f"invalid solver incumbent: {exc}",
                blockers=(SelectionBlocker(
                    "INCUMBENT_VALIDATION_FAILED",
                    request.selection_problem_id,
                    "the limited-solve incumbent failed independent validation",
                    {"error": str(exc)}),),
            )
        return _result(
            request, status=MilpStatus.FEASIBLE_NOT_PROVEN_OPTIMAL,
            started=started, plan=plan, deployment_choices=deployments,
            solver_result=primary, solver_calls=solver_calls,
        )

    primary_cost = int(round(sum(
        model.cost[index] * primary_vector[index]
        for index in model.producer_index.values())))
    extra_rows: list[tuple[dict[int, float], float, float]] = [(
        {index: model.cost[index]
         for index in model.producer_index.values()
         if model.cost[index] != 0},
        float(primary_cost), float(primary_cost),
    )]
    best_vector = primary_vector
    terminal = primary
    tie_complete = True
    def fix_lexicographic(indices: Sequence[int]) -> bool:
        nonlocal best_vector, terminal
        for start in range(0, len(indices), _LEXICOGRAPHIC_CHUNK_BITS):
            chunk = indices[start:start + _LEXICOGRAPHIC_CHUNK_BITS]
            objective = [0.0] * len(model.names)
            for position, variable_index in enumerate(chunk):
                # The first bit is lexicographically dominant.  Thirty-bit
                # chunks keep coefficients and sums exactly representable.
                objective[variable_index] = float(
                    1 << (len(chunk) - position - 1))
            try:
                result = invoke(objective, extra_rows)
            except Exception:
                result = None
            if result is None:
                return False
            candidate = _validated_vector(
                model, getattr(result, "x", None), extra_rows=extra_rows)
            if candidate is not None:
                best_vector = candidate
                terminal = result
            if int(result.status) != 0 or candidate is None:
                return False
            tie_value = int(round(sum(
                objective[index] * candidate[index] for index in chunk)))
            tie_row = {index: objective[index] for index in chunk}
            extra_rows.append((tie_row, float(tie_value), float(tie_value)))
        return True

    producer_ties = [
        index for index in model.producer_signature_indices
        if model.lower[index] != model.upper[index]]
    tie_complete = fix_lexicographic(producer_ties)
    if tie_complete:
        # Once every producer bit is frozen, arcs owned by unselected
        # producers are mathematically zero (z <= x) and cannot affect the
        # lexicographic order.  Omitting those redundant tie variables keeps
        # broad catalogs interactive without changing the exact signature.
        choice_ties = [
            index for index in model.choice_signature_indices
            if model.lower[index] != model.upper[index]
            and (index not in model.choice_producer_index
                 or _selected(best_vector[
                     model.choice_producer_index[index]]))
        ]
        tie_complete = fix_lexicographic(choice_ties)

    try:
        plan, deployments = _decode_plan(request, model, best_vector)
    except Exception as exc:
        return _result(
            request, status=MilpStatus.ERROR, started=started,
            solver_result=terminal, solver_calls=solver_calls,
            solver_message=f"selected plan validation failed: {exc}",
            blockers=(SelectionBlocker(
                "SELECTED_PLAN_VALIDATION_FAILED",
                request.selection_problem_id,
                "solver output failed backend-independent validation",
                {"error": str(exc)}),),
        )
    if tie_complete:
        return _result(
            request, status=MilpStatus.OPTIMAL, started=started,
            primary_cost_proven=True, tie_break_complete=True,
            plan=plan, deployment_choices=deployments,
            solver_result=primary, solver_calls=solver_calls,
            solver_message=str(primary.message),
        )
    return _result(
        request, status=MilpStatus.FEASIBLE_NOT_PROVEN_OPTIMAL,
        started=started, primary_cost_proven=True,
        tie_break_complete=False, plan=plan,
        # Public mip_gap always describes the primary scientific cost
        # objective.  A secondary canonicalization timeout must not relabel a
        # proven zero cost gap as the tie objective's unrelated gap.
        deployment_choices=deployments, solver_result=primary,
        solver_calls=solver_calls,
        solver_message=("primary cost is proven, but deterministic "
                        "tie-breaking stopped before completion"),
    )


__all__ = [
    "DeploymentOption",
    "MilpSelectionProblem",
    "MilpSelectionResult",
    "MilpSolveOptions",
    "MilpStatus",
    "ProducerSelectionRef",
    "SelectionBlocker",
    "SelectionConstraints",
    "solve_milp",
]
