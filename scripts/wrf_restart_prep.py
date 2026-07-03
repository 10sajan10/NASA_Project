#!/usr/bin/env python3
"""Prepare namelist.input for the next segment of a WRF restart chain.

Each SLURM job in the chain runs at most 3 days of wall time (the partition
cap). WRF writes restart files (``wrfrst_d0N_<date>``) at restart_interval;
when a job ends, the next one resumes from the latest restart.

This script, run at the start of every segment:
  * scans the run dir for ``wrfrst_d01_*`` restart files,
  * if none exist  -> first segment: restart=.false., keep start_date,
  * if some exist  -> set restart=.true. and move start_* to the latest
    restart timestamp (all domains),
  * prints whether the run has already reached the configured end date
    (exit code 3 = finished, 0 = more to do).

Usage:
  wrf_restart_prep.py <run_dir>
"""
from __future__ import annotations

import re
import sys
from datetime import datetime
from pathlib import Path

_RST_RE = re.compile(r"wrfrst_d01_(\d{4})-(\d{2})-(\d{2})_(\d{2}):(\d{2}):(\d{2})")


def latest_restart(run_dir: Path) -> datetime | None:
    times = []
    for p in run_dir.glob("wrfrst_d01_*"):
        m = _RST_RE.search(p.name)
        if m:
            times.append(datetime(*(int(x) for x in m.groups())))
    return max(times) if times else None


def _read_nl(nl: str) -> dict[str, str]:
    """Pull the &time_control scalars we need (first-domain values)."""
    out = {}
    for key in ("end_year", "end_month", "end_day", "end_hour"):
        m = re.search(rf"^\s*{key}\s*=\s*([0-9]+)", nl, re.M)
        if m:
            out[key] = m.group(1)
    return out


def set_field(nl: str, key: str, per_dom_value: str, ndom: int) -> str:
    """Replace a per-domain time_control field with a uniform value list."""
    row = ", ".join([per_dom_value] * ndom) + ","
    pat = re.compile(rf"^(\s*{key}\s*=).*$", re.M)
    if pat.search(nl):
        return pat.sub(rf"\g<1> {row}", nl, count=1)
    return nl


def set_scalar(nl: str, key: str, value: str) -> str:
    pat = re.compile(rf"^(\s*{key}\s*=).*$", re.M)
    if pat.search(nl):
        return pat.sub(rf"\g<1> {value},", nl, count=1)
    return nl


def count_domains(nl: str) -> int:
    m = re.search(r"^\s*max_dom\s*=\s*([0-9]+)", nl, re.M)
    return int(m.group(1)) if m else 1


def main() -> int:
    run_dir = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    nl_path = run_dir / "namelist.input"
    nl = nl_path.read_text()
    ndom = count_domains(nl)

    end = _read_nl(nl)
    end_dt = None
    if all(k in end for k in ("end_year", "end_month", "end_day", "end_hour")):
        end_dt = datetime(int(end["end_year"]), int(end["end_month"]),
                          int(end["end_day"]), int(end["end_hour"]))

    rst = latest_restart(run_dir)
    if rst is None:
        nl = set_scalar(nl, "restart", ".false.")
        print(f"[chain] first segment: restart=.false. ndom={ndom}")
    else:
        if end_dt is not None and rst >= end_dt:
            print(f"[chain] FINISHED — latest restart {rst} >= end {end_dt}")
            return 3
        nl = set_scalar(nl, "restart", ".true.")
        nl = set_field(nl, "start_year", f"{rst.year}", ndom)
        nl = set_field(nl, "start_month", f"{rst.month:02d}", ndom)
        nl = set_field(nl, "start_day", f"{rst.day:02d}", ndom)
        nl = set_field(nl, "start_hour", f"{rst.hour:02d}", ndom)
        nl = set_field(nl, "start_minute", f"{rst.minute:02d}", ndom)
        nl = set_field(nl, "start_second", f"{rst.second:02d}", ndom)
        print(f"[chain] resume segment: restart=.true. from {rst} "
              f"(end {end_dt})")

    nl_path.write_text(nl)
    return 0


if __name__ == "__main__":
    sys.exit(main())
