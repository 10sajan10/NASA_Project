#!/usr/bin/env python3
"""Run the bounded Stage-10A artifact automation proof."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from stage10a import run_demo


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", required=True)
    args = parser.parse_args()
    print(json.dumps(run_demo(args.workspace), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
