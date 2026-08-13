"""Subprocess worker for the Stage-0A synthetic fixture only."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from stage0a.conformance import AttemptSpec, AttemptState, execute_operation


def _atomic_json(path: Path, value: dict) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, sort_keys=True))
    os.replace(temporary, path)


def main() -> int:
    attempt_dir = Path(sys.argv[1]).resolve()
    if not str(attempt_dir).startswith("/tmp/"):
        raise RuntimeError("fixture worker accepts only /tmp attempt directories")
    spec = AttemptSpec.from_dict(json.loads(
        (attempt_dir / "attempt.json").read_text()))
    try:
        output = execute_operation(spec.operation, spec.payload)
        _atomic_json(attempt_dir / "result.json", {
            "state": AttemptState.SUCCEEDED.value,
            "output": output,
            "error": None,
        })
        return 0
    except BaseException as exc:
        _atomic_json(attempt_dir / "error.json", {
            "state": AttemptState.FAILED.value,
            "error": f"{type(exc).__name__}: {exc}",
        })
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
