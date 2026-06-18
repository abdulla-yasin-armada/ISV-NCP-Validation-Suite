"""Persist Bridge Slurm setup state between setup and teardown."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_DEFAULT_STATE_PATH = Path.home() / ".cache" / "isvctl" / "bridge-slurm-state.json"


def state_path() -> Path:
    return _DEFAULT_STATE_PATH


def load_state() -> dict[str, Any]:
    path = state_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def save_state(state: dict[str, Any]) -> None:
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2))
    path.chmod(0o600)


def clear_state() -> None:
    path = state_path()
    if path.exists():
        path.unlink()
