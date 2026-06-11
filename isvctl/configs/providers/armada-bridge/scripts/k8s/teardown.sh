#!/usr/bin/env bash
# teardown.sh — Armada Bridge Kubernetes suite teardown (wrapper).
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$SCRIPT_DIR/teardown.py" "$@"
