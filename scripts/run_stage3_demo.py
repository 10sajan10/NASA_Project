#!/usr/bin/env python3
"""Run the consequence-free Stage-3 resolver demonstration."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from stage3.demo import run_demo


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--runtime-root",
        type=Path,
        required=True,
        help="verified node-local directory for the Stage-1 runtime",
    )
    arguments = parser.parse_args()
    print(json.dumps(run_demo(arguments.runtime_root), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
