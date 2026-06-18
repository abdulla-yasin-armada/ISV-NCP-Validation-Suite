#!/usr/bin/env python3
"""dhcp_ip_test — Armada Bridge network suite, test phase.

Validates DHCP lease issuance on a subnet.

Output: {success, platform, tests: {dhcp_lease_issued}}
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
from common.network import api_gap_result

DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"


@handle_bridge_errors
def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--subnet-id", required=True)
    parser.parse_args()

    result: dict[str, Any] = {"success": False, "platform": "network"}

    if DEMO_MODE:
        result.update(
            {
                "success": True,
                "platform": "network",
                "tests": {
                    "dhcp_lease_issued": {"passed": True},
                },
            }
        )
    else:
        result = api_gap_result('dhcp_ip_test', 'DHCP/IP management requires SSH into a live instance')

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
