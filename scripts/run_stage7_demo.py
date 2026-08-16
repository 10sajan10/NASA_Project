#!/usr/bin/env python3
"""Run the Stage-7 bounded-partition demonstration.

    runtime_root=$(mktemp -d /tmp/nasa-stage7-demo.XXXXXX)
    .venv/bin/python scripts/run_stage7_demo.py --runtime-root "$runtime_root"
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stage7.demo import run_demo


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-root", required=True,
                        help="absolute node-local directory for partition state")
    arguments = parser.parse_args()
    print(json.dumps(run_demo(Path(arguments.runtime_root)), indent=2,
                     sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
