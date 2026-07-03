"""Agentic layer: agent-queryable metadata + deterministic planning.

The design contract (LLM proposes, engine disposes):

  * `ontology`    - controlled variable vocabulary, so "economic_loss_usd"
                    means one thing across every adapter.
  * `metacatalog` - DuckDB registry of DatasetCards and ModelCards with
                    coverage, provenance, regimes, and cost models.
                    Deterministic structured filters do 90% of the
                    narrowing; an LLM only arbitrates among survivors.
  * `planner`     - EventSpec -> RunPlan. Backward-chains over cards,
                    scores candidates, solves resolution against a
                    compute budget. Pure code, no LLM required.
  * `tools`       - thin JSON-in/JSON-out tool surface for an LLM agent
                    (MCP-ready): search, resolve, estimate.

The agent never generates glue code or pipelines; it only chooses among
registered adapters and binds parameters. Execution stays inside the
engine's ProducerV2 contract.
"""
from .ontology import VarDef, VOCABULARY, lookup, validate_name
from .metacatalog import (Coverage, Provenance, Quality, CostModel,
                          DatasetCard, ModelCard, MetaCatalog)
from .planner import (EventSpec, ComputeBudget, Binding, RunPlan,
                      PlanError, plan, targets_for_intent, INTENT_TARGETS)
from .agent import PlannerAgent, PlanSession
from .executor import plan_to_command, execute_plan
from .critic import Finding, CritiqueReport, critique, replan
from .events import EventWatcher, handle_event, normalize_event

__all__ = [
    "Finding", "CritiqueReport", "critique", "replan",
    "EventWatcher", "handle_event", "normalize_event",
    "VarDef", "VOCABULARY", "lookup", "validate_name",
    "Coverage", "Provenance", "Quality", "CostModel",
    "DatasetCard", "ModelCard", "MetaCatalog",
    "EventSpec", "ComputeBudget", "Binding", "RunPlan",
    "PlanError", "plan", "targets_for_intent", "INTENT_TARGETS",
    "PlannerAgent", "PlanSession", "plan_to_command", "execute_plan",
]
