"""LLM planner agent: natural-language question -> validated RunPlan.

The agent is a Claude tool-use loop over the deterministic tool surface
(`agentic.tools`). Its entire authority is *selection*: it parses the
question into an EventSpec + intent, searches the metacatalog, dry-runs
plans through the real resolver, and finally calls `submit_plan` — which
re-runs the deterministic planner one last time and stores *that* result.
The accepted plan is therefore always a product of `agentic.planner.plan`,
never of LLM-generated JSON. PlanError messages flow back as tool results,
so the model revises against real resolver feedback instead of guessing
(the resolver's rejection is the anti-hallucination loop).

The Anthropic client is injectable, so tests drive the loop with a
scripted fake and never touch the network.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Optional

from engine.log import get_logger

from . import tools as T
from .metacatalog import MetaCatalog

_log = get_logger(__name__)

DEFAULT_MODEL = "claude-opus-4-8"
MAX_TOKENS = 16_000

SYSTEM_PROMPT = """\
You are the planning agent for an event-driven Earth-systems cascade \
engine (asteroid impact -> fire -> smoke -> exposure -> economy). \
Producers (data drivers and models) are registered in a metacatalog with \
coverage, provenance, regime, and cost metadata.

Your job: turn the user's question into a validated run plan.

Rules — these are hard constraints:
1. You only SELECT among registered producers and bind parameters. Never \
invent variable names, producers, file paths, or pipelines. Use \
describe_ontology to see the controlled vocabulary.
2. Choose the narrowest target variables that answer the question — the \
resolver backward-chains, so asking only for e.g. economic_loss_usd \
automatically pulls its upstream chain and nothing else.
3. Always validate with resolve_plan before submitting. If it returns \
ok=false, read the error (it names the unsatisfiable variable and the \
dependency chain), revise — different targets, wider trust, different \
event geometry — and try again. Do not submit a plan you have not seen \
validate.
4. Use estimate_cost when the user cares about runtime/resources, and \
pick a compute budget that fits what they asked for.
5. Finish by calling submit_plan exactly once with the validated event/\
targets/budget and a short rationale (why these targets, why this \
budget). Then summarise the plan for the user in plain language: what \
will run, at what resolution, roughly how long, and what was rejected.

If the question cannot be answered with the registered producers, do not \
submit; explain what is missing (which variable, what kind of producer \
would satisfy it)."""

# Anthropic tool definitions for the deterministic surface. Inputs mirror
# agentic.tools signatures; `metacat` is bound server-side, never exposed.
_EVENT_SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "description": "e.g. asteroid_impact"},
        "lat": {"type": "number"},
        "lon": {"type": "number"},
        "time": {"type": "string", "description": "ISO 8601, optional"},
        "radius_m": {"type": "number"},
        "duration_s": {"type": "number"},
        "magnitude": {"type": "object",
                      "description": "e.g. {\"energy_mt\": 5.0}"},
        "intent": {"type": "string",
                   "description": "full_cascade|fire|air_quality|"
                                  "economic|exposure"},
    },
    "required": ["kind", "lat", "lon"],
}
_BUDGET_SCHEMA = {
    "type": "object",
    "properties": {
        "cores": {"type": "integer"},
        "wall_s": {"type": "number"},
        "backend": {"type": "string"},
    },
}

TOOL_DEFINITIONS: list[dict] = [
    {
        "name": "describe_ontology",
        "description": "List the controlled variable vocabulary grouped "
                       "by domain. Call this first when unsure which "
                       "variable answers the question.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "search_datasets",
        "description": "Deterministic dataset search: which registered "
                       "data sources can produce a variable, filtered by "
                       "coverage bbox, time window, resolution, trust.",
        "input_schema": {
            "type": "object",
            "properties": {
                "variable": {"type": "string"},
                "bbox": {"type": "array", "items": {"type": "number"},
                         "description": "[min_lon, min_lat, max_lon, max_lat]"},
                "t_start": {"type": "string"},
                "t_end": {"type": "string"},
                "max_native_res_m": {"type": "number"},
                "min_trust": {"type": "string"},
            },
        },
    },
    {
        "name": "search_models",
        "description": "Deterministic model search: which registered "
                       "models produce a variable, filtered by domain, "
                       "coverage, regime validity (event magnitude), "
                       "fidelity tier, trust.",
        "input_schema": {
            "type": "object",
            "properties": {
                "produces": {"type": "string"},
                "domain": {"type": "string"},
                "bbox": {"type": "array", "items": {"type": "number"}},
                "magnitude": {"type": "object"},
                "fidelity": {"type": "string"},
                "min_trust": {"type": "string"},
            },
        },
    },
    {
        "name": "resolve_plan",
        "description": "Dry-run the deterministic planner: event + "
                       "targets/intent + budget -> full plan with "
                       "bindings, alternatives, resolution, cost "
                       "estimate. On failure the error names the "
                       "unsatisfiable variable and dependency chain — "
                       "revise and call again.",
        "input_schema": {
            "type": "object",
            "properties": {
                "event": _EVENT_SCHEMA,
                "targets": {"type": "array", "items": {"type": "string"}},
                "budget": _BUDGET_SCHEMA,
                "min_trust": {"type": "string"},
            },
            "required": ["event"],
        },
    },
    {
        "name": "estimate_cost",
        "description": "Resolution/wall-time table across core counts "
                       "for an event — use to negotiate resolution vs "
                       "compute budget.",
        "input_schema": {
            "type": "object",
            "properties": {
                "event": _EVENT_SCHEMA,
                "targets": {"type": "array", "items": {"type": "string"}},
                "wall_s": {"type": "number"},
            },
            "required": ["event"],
        },
    },
    {
        "name": "submit_plan",
        "description": "Finalise: re-validates through the deterministic "
                       "planner and stores the result as the accepted "
                       "plan. Call exactly once, only after resolve_plan "
                       "succeeded with the same arguments.",
        "input_schema": {
            "type": "object",
            "properties": {
                "event": _EVENT_SCHEMA,
                "targets": {"type": "array", "items": {"type": "string"}},
                "budget": _BUDGET_SCHEMA,
                "min_trust": {"type": "string"},
                "rationale": {"type": "string",
                              "description": "Why these targets/bindings/"
                                             "budget — recorded in lineage."},
            },
            "required": ["event", "rationale"],
        },
    },
]


@dataclass
class PlanSession:
    """Outcome of one agent run."""
    question: str
    plan: Optional[dict]           # validated RunPlan.to_dict(), or None
    rationale: str = ""
    narrative: str = ""            # the model's final plain-language summary
    iterations: int = 0
    stop_reason: str = ""
    transcript: list = field(default_factory=list)   # (tool_name, ok) pairs


class PlannerAgent:
    """Claude tool-use loop over the deterministic planning surface."""

    def __init__(self, metacat: MetaCatalog, *, client: Any = None,
                 model: str = DEFAULT_MODEL, max_iterations: int = 12):
        self.metacat = metacat
        self.model = model
        self.max_iterations = max_iterations
        self._client = client

    def client(self):
        if self._client is None:
            import anthropic
            self._client = anthropic.Anthropic()
        return self._client

    # ---- tool dispatch ---------------------------------------------------
    def _run_tool(self, name: str, args: dict, session: PlanSession) -> dict:
        mc = self.metacat
        if name == "describe_ontology":
            return T.describe_ontology()
        if name == "search_datasets":
            return T.search_datasets(mc, **args)
        if name == "search_models":
            return T.search_models(mc, **args)
        if name == "resolve_plan":
            return T.resolve_plan(mc, args["event"],
                                  targets=args.get("targets"),
                                  budget=args.get("budget"),
                                  min_trust=args.get("min_trust",
                                                     "unverified"))
        if name == "estimate_cost":
            return T.estimate_cost(mc, args["event"],
                                   targets=args.get("targets"),
                                   wall_s=args.get("wall_s", 3600.0))
        if name == "submit_plan":
            result = T.resolve_plan(mc, args["event"],
                                    targets=args.get("targets"),
                                    budget=args.get("budget"),
                                    min_trust=args.get("min_trust",
                                                       "unverified"))
            if result["ok"]:
                session.plan = result["plan"]
                session.rationale = args.get("rationale", "")
            return result
        return {"ok": False, "error": f"unknown tool {name!r}",
                "error_type": "UnknownTool"}

    # ---- the loop ----------------------------------------------------------
    def ask(self, question: str) -> PlanSession:
        session = PlanSession(question=question, plan=None)
        messages: list[dict] = [{"role": "user", "content": question}]

        for _ in range(self.max_iterations):
            session.iterations += 1
            response = self.client().messages.create(
                model=self.model,
                max_tokens=MAX_TOKENS,
                thinking={"type": "adaptive"},
                system=SYSTEM_PROMPT,
                tools=TOOL_DEFINITIONS,
                messages=messages,
            )
            session.stop_reason = response.stop_reason or ""

            if response.stop_reason == "refusal":
                session.narrative = ("planning request was refused by "
                                     "the model's safety layer")
                return session

            if response.stop_reason == "pause_turn":
                messages.append({"role": "assistant",
                                 "content": response.content})
                continue

            tool_uses = [b for b in response.content
                         if getattr(b, "type", None) == "tool_use"]
            texts = [b.text for b in response.content
                     if getattr(b, "type", None) == "text"]
            if texts:
                session.narrative = "\n".join(texts)

            if not tool_uses:                      # end_turn: model is done
                return session

            messages.append({"role": "assistant",
                             "content": response.content})
            results = []
            for tu in tool_uses:
                out = self._run_tool(tu.name, dict(tu.input or {}), session)
                session.transcript.append((tu.name, bool(out.get("ok"))))
                _log.info("[agent] tool %s -> ok=%s", tu.name, out.get("ok"))
                results.append({"type": "tool_result",
                                "tool_use_id": tu.id,
                                "content": json.dumps(out, default=str)})
            messages.append({"role": "user", "content": results})

        session.narrative = session.narrative or (
            f"stopped after {self.max_iterations} iterations without a "
            f"final answer")
        return session
