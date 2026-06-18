#!/usr/bin/env bash
# teardown.sh — delegates to teardown.py (Bridge Slurm suite, teardown phase).
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "${SCRIPT_DIR}/teardown.py" "$@"
