#!/usr/bin/env python3
"""subnet_test — Armada Bridge network suite, test phase.

Import flow: reports IPAllocation CR subnets for the tenant.
Discovery flow: lists orchestrator subnets for the suite VPC.

Output: {success, platform, tests: {...}, subnets: [...]}
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common.bridge_client import BridgeClient
from common.errors import handle_bridge_errors
from common.network import load_network_by_vpc_id
from common.tenant import resolve_tenant_id

DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"


@handle_bridge_errors
def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--vpc-id", required=True)
    args = parser.parse_args()

    result: dict[str, Any] = {"success": False, "platform": "network"}

    if DEMO_MODE:
        result.update(
            {
                "success": True,
                "platform": "network",
                "tests": {
                    "create_subnets": {"passed": True},
                    "az_distribution": {"passed": True, "az_count": 2, "azs": ["demo-az-a", "demo-az-b"]},
                    "subnets_available": {"passed": True},
                },
                "subnets": [
                    {"subnet_id": "demo-subnet-0001", "cidr": "10.100.1.0/24", "az": "demo-az-a"},
                    {"subnet_id": "demo-subnet-0002", "cidr": "10.100.2.0/24", "az": "demo-az-b"},
                    {"subnet_id": "demo-subnet-0003", "cidr": "10.100.3.0/24", "az": "demo-az-a"},
                    {"subnet_id": "demo-subnet-0004", "cidr": "10.100.4.0/24", "az": "demo-az-b"},
                ],
            }
        )
    else:
        client = BridgeClient.from_env()
        tenant_id = resolve_tenant_id(client, args.tenant)
        profile = load_network_by_vpc_id(client, tenant_id, args.tenant, args.vpc_id)
        subnets = profile.get("subnets", [])
        # Bridge uses topology (compute/converged/storage) instead of AZ.
        # bridge_subnet_to_output maps topology → "az" field, so read from "az".
        topologies = sorted({str(item.get("az", "")) for item in subnets if item.get("az")})
        result.update(
            {
                "success": bool(subnets),
                "platform": "network",
                "subnets": subnets,
                "tests": {
                    "create_subnets": {"passed": bool(subnets)},
                    "az_distribution": {
                        "passed": len(topologies) >= 1,
                        "az_count": len(topologies),
                        "azs": topologies,
                    },
                    "subnets_available": {"passed": bool(subnets)},
                },
                "import_flow": profile.get("import_flow", False),
                "discovery_flow": profile.get("discovery_flow", False),
            }
        )

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
