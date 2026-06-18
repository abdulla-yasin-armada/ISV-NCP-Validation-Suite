#!/usr/bin/env bash
# setup.sh — delegates to setup.py (Bridge Slurm suite, setup phase).
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "${SCRIPT_DIR}/setup.py" "$@"
