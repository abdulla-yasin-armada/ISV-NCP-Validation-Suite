#!/usr/bin/env python3
"""teardown — Armada Bridge security suite, teardown phase.

Output: {success, platform}
"""
import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common.bridge_client import BridgeClient  # noqa: F401 — used in the live impl block
from common.errors import handle_bridge_errors

DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"


@handle_bridge_errors
def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tenant", required=True)
    parser.parse_args()

    result: dict[str, Any] = {"success": False, "platform": "security"}

    # The security suite creates no persistent resources — all scripts are
    # read-only API probes or use fake/ephemeral UUIDs.  Nothing to clean up.
    result.update({"success": True, "platform": "security"})

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
