#!/usr/bin/env python3
"""Run the Stage-4 explicit-transformation demonstration.

Use a fresh node-local runtime root:

    runtime_root=$(mktemp -d /tmp/nasa-stage4-demo.XXXXXX)
    .venv/bin/python scripts/run_stage4_demo.py --runtime-root "$runtime_root"
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from stage4.demo import run_demo


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-root", type=Path, required=True)
    args = parser.parse_args()
    args.runtime_root.mkdir(parents=True, exist_ok=True)
    print(json.dumps(run_demo(args.runtime_root), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
