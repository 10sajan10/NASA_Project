#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

PYTHON_BIN="${PYTHON_BIN:-python3}"
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
