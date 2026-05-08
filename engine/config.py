"""Pipeline configuration loader.

Configuration lives in YAML/JSON. CLI flags become overrides only — the
config is the source of truth so two runs with the same config hash are
bit-for-bit identical.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def load_config(path: str | Path) -> dict[str, Any]:
    """Load YAML or JSON config. YAML requires PyYAML; if it's missing and
    the file is JSON, we still load it."""
    p = Path(path)
    text = p.read_text()
    suffix = p.suffix.lower()
    if suffix in (".yaml", ".yml"):
        try:
            import yaml  # noqa: F401
        except ImportError as exc:
            raise ImportError(
                f"PyYAML required to load {p}; pip install pyyaml") from exc
        import yaml
        return yaml.safe_load(text) or {}
    if suffix == ".json":
        return json.loads(text)
    raise ValueError(f"unsupported config format {suffix!r} ({p})")


def merge_overrides(base: dict, overrides: dict | None) -> dict:
    """Deep-merge overrides into base. None values in overrides are skipped
    (so argparse defaults of None don't clobber a value from the config).

    Lists and scalars are replaced; dicts are merged recursively.
    """
    out = dict(base)
    if not overrides:
        return out
    for k, v in overrides.items():
        if v is None:
            continue
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = merge_overrides(out[k], v)
        else:
            out[k] = v
    return out
