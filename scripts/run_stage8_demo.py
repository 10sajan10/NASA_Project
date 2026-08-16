#!/usr/bin/env python3
"""Run the Stage-8 resource-aware scheduling demonstration.

    .venv/bin/python scripts/run_stage8_demo.py

Needs no runtime root: the schedule is a deterministic discrete-event
simulation over declared or measured durations, not a live run.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stage8.demo import run_demo

if __name__ == "__main__":
    print(json.dumps(run_demo(), indent=2, sort_keys=True))
