#!/usr/bin/env python3
"""Run-context printer for Armada Bridge provider scripts.

Call ``print_run_context(suite, extra)`` at the top of each suite's main()
to emit a clear, reviewer-friendly summary of what configuration was used.
Sensitive values (password, tokens) are masked.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone


def _mask(value: str, show: int = 0) -> str:
    """Mask a secret, optionally showing the first ``show`` characters."""
    if not value:
        return "(not set)"
    visible = value[:show] if show else ""
    return visible + "****"


def print_run_context(suite: str, extra: dict[str, str] | None = None) -> None:
    """Print suite configuration to stderr for log/review clarity.

    Args:
        suite:  Suite name, e.g. "VM", "Kubernetes", "Slurm".
        extra:  Suite-specific key/value pairs to append after common vars.
    """
    url      = os.environ.get("BRIDGE_URL", "(not set)")
    username = os.environ.get("BRIDGE_USERNAME", "(not set)")
    password = os.environ.get("BRIDGE_PASSWORD", "")
    tenant   = os.environ.get("BRIDGE_TENANT", "(not set)")
    now      = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    lines = [
        f"",
        f"┌─ [{suite} Suite] Run Context ─────────────────────────────",
        f"│  Time             : {now}",
        f"│  BRIDGE_URL       : {url}",
        f"│  BRIDGE_USERNAME  : {username}",
        f"│  BRIDGE_PASSWORD  : {_mask(password)}",
        f"│  BRIDGE_TENANT    : {tenant}",
    ]

    if extra:
        for key, value in extra.items():
            lines.append(f"│  {key:<17}: {value or '(not set)'}")

    lines.append(f"└───────────────────────────────────────────────────────")
    lines.append("")

    print("\n".join(lines), file=sys.stderr)
