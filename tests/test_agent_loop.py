"""LLM planner agent loop, driven by a scripted fake Anthropic client.

Proves the resolver-in-the-loop contract without network access:
  * PlanError feedback flows back to the model as a tool result,
  * the accepted plan comes from the deterministic planner (submit_plan
    re-validates), never from model-generated JSON,
  * refusals and iteration caps degrade gracefully,
  * a validated plan maps onto a correct run_cascade.py command.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

from agentic.agent import PlannerAgent
from agentic.executor import plan_to_command
from tests.test_agentic import toy_metacat


# ---------------------------------------------------------------- fakes
def _text(t):
    return SimpleNamespace(type="text", text=t)


def _tool_use(name, args, id_="tu1"):
    return SimpleNamespace(type="tool_use", name=name, input=args, id=id_)


def _resp(blocks, stop_reason="tool_use"):
    return SimpleNamespace(content=blocks, stop_reason=stop_reason)


class FakeClient:
    """Replays scripted responses; records every request payload."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.requests: list[dict] = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.requests.append(kwargs)
        return self._responses.pop(0)


DALLAS_EVENT = {"kind": "asteroid_impact", "lat": 32.78, "lon": -96.81,
                "radius_m": 50_000, "magnitude": {"energy_mt": 5.0},
                "intent": "economic"}
PARIS_EVENT = {**DALLAS_EVENT, "lat": 48.85, "lon": 2.35}


# ---------------------------------------------------------------- tests
def test_agent_revises_after_plan_error_and_submits():
    client = FakeClient([
        # 1) tries Paris — resolver rejects (assets don't cover Europe)
        _resp([_tool_use("resolve_plan", {"event": PARIS_EVENT}, "tu1")]),
        # 2) revises to Dallas, validates, submits in the same turn
        _resp([_tool_use("resolve_plan", {"event": DALLAS_EVENT}, "tu2"),
               _tool_use("submit_plan",
                         {"event": DALLAS_EVENT,
                          "rationale": "CONUS coverage; econ_io wins on "
                                       "fidelity+trust"}, "tu3")]),
        # 3) wraps up
        _resp([_text("Plan ready: economic loss chain over Dallas.")],
              stop_reason="end_turn"),
    ])
    agent = PlannerAgent(toy_metacat(), client=client)
    session = agent.ask("economic consequences of the Dallas airburst?")

    assert session.plan is not None
    assert session.plan["bindings"]["economic_loss_usd"]["producer"] == "econ_io"
    assert session.rationale.startswith("CONUS coverage")
    assert session.iterations == 3
    assert ("resolve_plan", False) in session.transcript   # the rejection
    assert ("submit_plan", True) in session.transcript

    # PlanError feedback actually reached the model, naming the variable
    second_request = client.requests[1]
    fed_back = json.dumps(second_request["messages"], default=str)
    assert "asset_value_usd" in fed_back

    # request shape follows the API contract
    first = client.requests[0]
    assert first["thinking"] == {"type": "adaptive"}
    assert any(t["name"] == "submit_plan" for t in first["tools"])


def test_agent_handles_refusal():
    client = FakeClient([_resp([], stop_reason="refusal")])
    session = PlannerAgent(toy_metacat(), client=client).ask("q")
    assert session.plan is None
    assert "refused" in session.narrative


def test_agent_iteration_cap():
    # model loops on searches forever; agent must stop at the cap
    looping = _resp([_tool_use("describe_ontology", {})])
    client = FakeClient([looping] * 3)
    session = PlannerAgent(toy_metacat(), client=client,
                           max_iterations=3).ask("q")
    assert session.plan is None
    assert session.iterations == 3
    assert "stopped after 3 iterations" in session.narrative


def test_submit_plan_revalidates_bad_input():
    # model submits an invalid plan directly — submit must NOT accept it
    client = FakeClient([
        _resp([_tool_use("submit_plan",
                         {"event": PARIS_EVENT, "rationale": "wrong"})]),
        _resp([_text("could not plan")], stop_reason="end_turn"),
    ])
    session = PlannerAgent(toy_metacat(), client=client).ask("q")
    assert session.plan is None
    assert ("submit_plan", False) in session.transcript


def test_plan_to_command_maps_plan_onto_run_cascade():
    from agentic import ComputeBudget, EventSpec, plan
    from tests.test_agentic import DALLAS

    p = plan(DALLAS, toy_metacat(),
             budget=ComputeBudget(cores=56, wall_s=7200, backend="process"))
    cmd = plan_to_command(p.to_dict(), python="python")

    joined = " ".join(cmd)
    assert "run_cascade.py" in joined
    assert "--targets economic_loss_usd" in joined
    assert f"--pixel-m {p.resolution_m}" in joined
    assert "--center -96.81 32.78" in joined
    assert "--radius-km 50.0" in joined
    assert "--np 56" in joined
    assert "--dry-run" in joined                      # safe default
    assert "--dry-run" not in " ".join(
        plan_to_command(p.to_dict(), python="python", dry_run=False))
