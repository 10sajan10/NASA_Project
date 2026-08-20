#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

if [ -n "${PYTHON_BIN:-}" ]; then
  PYTHON_BIN="$PYTHON_BIN"
else
  # Prefer an explicitly versioned supported interpreter.  On many HPC
  # login nodes `python3` is the operating-system Python (3.6 on the current
  # development node) even though a supported Python is installed alongside
  # it.  Falling straight to python3 made the documented setup fail before it
  # could create the environment.
  PYTHON_BIN=""
  for candidate in python3.13 python3.12 python3; do
    if command -v "$candidate" >/dev/null 2>&1 && \
       "$candidate" -c 'import sys; raise SystemExit(sys.version_info < (3, 12))'; then
      PYTHON_BIN="$candidate"
      break
    fi
  done
  if [ -z "$PYTHON_BIN" ]; then
    echo "Python 3.12 or newer was not found." >&2
    echo "Set PYTHON_BIN=/path/to/a/supported/python and rerun." >&2
    exit 1
  fi
fi
VENV_DIR="${VENV_DIR:-.venv}"

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "Python command not found: $PYTHON_BIN" >&2
  echo "Set PYTHON_BIN=/path/to/python3.12 and rerun." >&2
  exit 1
fi

"$PYTHON_BIN" - <<'PY'
import sys
if sys.version_info < (3, 12):
    raise SystemExit("Python 3.12 or newer is required for this environment")
PY

if [ ! -d "$VENV_DIR" ]; then
  "$PYTHON_BIN" -m venv "$VENV_DIR"
fi

"$VENV_DIR/bin/python" -m pip install --upgrade pip
"$VENV_DIR/bin/python" -m pip install -r requirements.txt

mkdir -p logs data cube_snapshots data/raw/dem

"$VENV_DIR/bin/python" - <<'PY'
import duckdb
import gcsfs
import lxml
import matplotlib
import numpy
import planetary_computer
import pyproj
import pystac_client
import rasterio
import scipy
import shapely
import xarray
import zarr

print("NASA_Project setup complete.")
PY
