#!/usr/bin/env python3
"""Run the Stage-5 progressive-acquisition demonstration.

Use a fresh node-local temporary directory:

    runtime_root=$(mktemp -d /tmp/nasa-stage5-demo.XXXXXX)
    .venv/bin/python scripts/run_stage5_demo.py --runtime-root "$runtime_root"

No network is contacted: both connectors run in this process.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stage5.demo import run_demo


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-root", required=True,
                        help="absolute node-local directory for runtime state")
    arguments = parser.parse_args()
    print(json.dumps(run_demo(Path(arguments.runtime_root)), indent=2,
                     sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
