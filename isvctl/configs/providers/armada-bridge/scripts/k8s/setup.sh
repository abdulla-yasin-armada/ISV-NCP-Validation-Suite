#!/usr/bin/env bash
# setup.sh — Armada Bridge Kubernetes suite setup (wrapper).
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$SCRIPT_DIR/setup.py" "$@"
