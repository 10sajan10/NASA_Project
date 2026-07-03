"""Event bus: normalized events in, plans (and optionally runs) out.

The thin front door for event-driven autonomy. An event is a JSON
document — from an alert feed, a synthetic scenario, or a user — that
gets normalized into an EventSpec and handed to the planner:

    {"kind": "asteroid_impact", "lat": 32.78, "lon": -96.81,
     "energy_mt": 5.0, "intent": "economic", "radius_km": 50}

`EventWatcher` is a polling directory watcher (portable on HPC
filesystems — no inotify): drop `*.json` into the inbox, get a
`<name>.report.json` (plan + bindings + rationale + optional execution
result) in the outbox, with the event file archived next to it.

Execution defaults to planning only; pass execute=True for a
run_cascade --dry-run preview and for_real=True to consume compute.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from engine.log import get_logger

from .executor import execute_plan
from .metacatalog import MetaCatalog
from .planner import ComputeBudget, EventSpec, PlanError, plan

_log = get_logger(__name__)

# Accepted aliases -> EventSpec field
_ALIASES = {"latitude": "lat", "longitude": "lon", "lng": "lon",
            "time_iso": "time", "timestamp": "time"}
_MAGNITUDE_KEYS = ("energy_mt", "impactor_diam_m", "impactor_velocity_kms")


def normalize_event(raw: dict) -> EventSpec:
    """Coerce a loosely-shaped event dict into an EventSpec.

    Unknown keys are ignored; magnitude keys given at the top level
    (e.g. energy_mt) fold into EventSpec.magnitude; radius_km is
    accepted alongside radius_m.
    """
    d = {(_ALIASES.get(k, k)): v for k, v in raw.items()}
    for key in ("kind", "lat", "lon"):
        if key not in d:
            raise ValueError(f"event is missing required field {key!r}")

    magnitude = dict(d.get("magnitude") or {})
    for k in _MAGNITUDE_KEYS:
        if k in d:
            magnitude.setdefault(k, float(d[k]))

    kwargs = {"kind": str(d["kind"]), "lat": float(d["lat"]),
              "lon": float(d["lon"]), "magnitude": magnitude}
    if d.get("time"):
        kwargs["time"] = str(d["time"])
    if "radius_m" in d:
        kwargs["radius_m"] = float(d["radius_m"])
    elif "radius_km" in d:
        kwargs["radius_m"] = float(d["radius_km"]) * 1000.0
    if "duration_s" in d:
        kwargs["duration_s"] = float(d["duration_s"])
    elif "duration_h" in d:
        kwargs["duration_s"] = float(d["duration_h"]) * 3600.0
    if d.get("intent"):
        kwargs["intent"] = str(d["intent"])
    return EventSpec(**kwargs)


def handle_event(raw: dict, metacat: MetaCatalog, *,
                 budget: Optional[ComputeBudget] = None,
                 execute: bool = False,
                 for_real: bool = False) -> dict:
    """One event through the pipeline: normalize -> plan -> (execute).

    Never raises: failures come back as {"ok": False, ...} so a watcher
    keeps running and the report says what went wrong.
    """
    report: dict = {"ok": False, "event_raw": raw}
    try:
        ev = normalize_event(raw)
    except (ValueError, TypeError) as e:
        report["error"] = f"invalid event: {e}"
        return report

    try:
        p = plan(ev, metacat, budget=budget)
    except PlanError as e:
        report["error"] = str(e)
        report["variable"] = e.variable
        return report

    report["ok"] = True
    report["plan"] = p.to_dict()
    if execute:
        report["execution"] = execute_plan(p.to_dict(),
                                           dry_run=not for_real)
        report["ok"] = report["execution"]["ok"]
    return report


@dataclass
class EventWatcher:
    """Poll an inbox directory for event JSON files and process them."""
    inbox: Path
    outbox: Path
    metacat: MetaCatalog
    handler: Callable[..., dict] = handle_event
    budget: Optional[ComputeBudget] = None
    execute: bool = False
    for_real: bool = False
    processed: list[str] = field(default_factory=list)

    def __post_init__(self):
        self.inbox = Path(self.inbox)
        self.outbox = Path(self.outbox)
        self.inbox.mkdir(parents=True, exist_ok=True)
        self.outbox.mkdir(parents=True, exist_ok=True)

    def poll_once(self) -> list[Path]:
        """Process every pending event; returns the report paths."""
        reports = []
        for path in sorted(self.inbox.glob("*.json")):
            try:
                raw = json.loads(path.read_text())
            except json.JSONDecodeError as e:
                raw, report = None, {"ok": False,
                                     "error": f"unparseable JSON: {e}"}
            if raw is not None:
                report = self.handler(raw, self.metacat,
                                      budget=self.budget,
                                      execute=self.execute,
                                      for_real=self.for_real)
            out = self.outbox / f"{path.stem}.report.json"
            out.write_text(json.dumps(report, indent=2, default=str))
            path.rename(self.outbox / path.name)   # archive the event
            self.processed.append(path.stem)
            _log.info("[events] %s -> ok=%s", path.name, report.get("ok"))
            reports.append(out)
        return reports

    def watch(self, interval_s: float = 5.0) -> None:
        """Block forever, polling. Ctrl-C to stop."""
        _log.info("[events] watching %s every %.0fs", self.inbox,
                  interval_s)
        while True:
            self.poll_once()
            time.sleep(interval_s)
