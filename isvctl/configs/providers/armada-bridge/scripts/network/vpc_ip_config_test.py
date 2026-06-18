#!/usr/bin/env python3
"""vpc_ip_config_test — Armada Bridge network suite, test phase.

Reads VPC/subnet IP configuration from orchestrator API (discovery) or
IPAllocation CRs (import) and emits JSON for VpcIpConfigCheck.

Output: {success, platform, cidr, subnets, dhcp_options}
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
                "cidr": "10.100.0.0/16",
                "subnets": [
                    {
                        "subnet_id": "demo-subnet-0001",
                        "cidr": "10.100.1.0/24",
                        "az": "demo-az-a",
                        "auto_assign_public_ip": True,
                        "available_ips": 251,
                    },
                    {
                        "subnet_id": "demo-subnet-0002",
                        "cidr": "10.100.2.0/24",
                        "az": "demo-az-b",
                        "auto_assign_public_ip": False,
                        "available_ips": 251,
                    },
                ],
                "dhcp_options": {
                    "dhcp_options_id": "demo-dopt-0001",
                    "domain_name": "internal.armada.demo",
                    "domain_name_servers": ["10.100.0.2"],
                    "ntp_servers": [],
                },
            }
        )
    else:
        client = BridgeClient.from_env()
        tenant_id = resolve_tenant_id(client, args.tenant)
        profile = load_network_by_vpc_id(client, tenant_id, args.tenant, args.vpc_id)
        result.update(
            {
                "success": True,
                "platform": "network",
                "network_id": profile["network_id"],
                "cidr": profile["cidr"],
                "subnets": profile["subnets"],
                "dhcp_options": profile["dhcp_options"],
                "import_flow": profile.get("import_flow", False),
                "discovery_flow": profile.get("discovery_flow", False),
            }
        )

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
