"""MCP server exposing the deterministic planning surface.

Lets any MCP client (Claude Code, Claude Desktop, another agent harness)
search the catalog and dry-run plans against this installation. Search
and resolve only — execution stays behind the human-driven entry points
(`scripts/ask_cascade.py --execute` / `scripts/run_cascade.py`).

Run:  .venv/bin/python -m agentic.mcp_server

Claude Code registration (.mcp.json):
    {"mcpServers": {"cascade-planner": {
        "command": ".venv/bin/python",
        "args": ["-m", "agentic.mcp_server"],
        "cwd": "<repo root>"}}}
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcp.server.fastmcp import FastMCP

from agentic import tools as T
from agentic.metacatalog import MetaCatalog

mcp = FastMCP("cascade-planner")
_metacat: Optional[MetaCatalog] = None


def get_metacat() -> MetaCatalog:
    """Seed from the built-in cascade on first use."""
    global _metacat
    if _metacat is None:
        from models.catalog import default_catalog
        _metacat = MetaCatalog()
        default_catalog().seed_metacatalog(_metacat)
    return _metacat


@mcp.tool()
def describe_ontology() -> dict:
    """List the controlled variable vocabulary, grouped by domain."""
    return T.describe_ontology()


@mcp.tool()
def search_datasets(variable: Optional[str] = None,
                    bbox: Optional[list] = None,
                    t_start: Optional[str] = None,
                    t_end: Optional[str] = None,
                    max_native_res_m: Optional[float] = None,
                    min_trust: str = "unverified") -> dict:
    """Search registered data sources by variable/coverage/resolution/trust."""
    return T.search_datasets(get_metacat(), variable=variable, bbox=bbox,
                             t_start=t_start, t_end=t_end,
                             max_native_res_m=max_native_res_m,
                             min_trust=min_trust)


@mcp.tool()
def search_models(produces: Optional[str] = None,
                  domain: Optional[str] = None,
                  bbox: Optional[list] = None,
                  magnitude: Optional[dict] = None,
                  fidelity: Optional[str] = None,
                  min_trust: str = "unverified") -> dict:
    """Search registered models by variable/domain/regime/fidelity/trust."""
    return T.search_models(get_metacat(), produces=produces, domain=domain,
                           bbox=bbox, magnitude=magnitude,
                           fidelity=fidelity, min_trust=min_trust)


@mcp.tool()
def resolve_plan(event: dict, targets: Optional[list] = None,
                 budget: Optional[dict] = None,
                 min_trust: str = "unverified") -> dict:
    """Dry-run the deterministic planner: event -> bindings + resolution
    + cost. Errors name the unsatisfiable variable and dependency chain."""
    return T.resolve_plan(get_metacat(), event, targets=targets,
                          budget=budget, min_trust=min_trust)


@mcp.tool()
def estimate_cost(event: dict, targets: Optional[list] = None,
                  wall_s: float = 3600.0) -> dict:
    """Resolution/wall-time table across core counts for an event."""
    return T.estimate_cost(get_metacat(), event, targets=targets,
                           wall_s=wall_s)


if __name__ == "__main__":
    mcp.run()
