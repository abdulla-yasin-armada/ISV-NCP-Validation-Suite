#!/usr/bin/env python3
"""create_vpc — Armada Bridge network suite, setup phase.

Import flow (networkType nonetwork / GPUaaS lab):
  Discovers tenant IPAllocation CRs via kubectl and maps them to the generic
  network JSON contract (network_id, cidr, subnets).

Discovery flow (ethernet topology):
  Creates two VPCs and subnets:
    1. compute topology  (vpc_id / subnet_id)       — for network validation tests
    2. converged topology (converged_vpc_id / converged_subnet_id) — for BM allocation

  BM nodes on Bridge require a converged (or storage) subnet; the compute subnet
  alone causes a 500 error from the metal/allocate API.

Output: {success, platform, network_id, vpc_id, subnet_id, security_group_id,
         cidr, subnets, dhcp_options, import_flow, discovery_flow,
         converged_vpc_id, converged_subnet_id}
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common.bridge_client import BridgeClient
from common.errors import handle_bridge_errors
from common.network import resolve_network_profile, resolve_security_group_id
from common.tenant import resolve_tenant_id

DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"


def _create_converged_network(client: BridgeClient, tenant_id: str, epoch: int) -> tuple[str, str]:
    """Create a converged topology VPC + subnet for BM allocation. Returns (vpc_id, subnet_id)."""
    vpc_resp = client.post(
        f"/orchestrator/tenants/{tenant_id}/vpcs",
        {
            "name": f"isv-net-converged-vpc-{epoch}",
            "description": "",
            "topologyID": "converged",
            "capabilities": [],
        },
    )
    converged_vpc_id = str(vpc_resp.get("id", "") or "")

    subnet_resp = client.post(
        f"/orchestrator/tenants/{tenant_id}/subnets",
        {
            "name": f"isv-net-converged-subnet-{epoch}",
            "subnetCIDR": "10.5.0.0/16",
            "capabilities": [],
            "parentVpcID": converged_vpc_id,
            "topology": "converged",
        },
    )
    converged_subnet_id = str(subnet_resp.get("id", "") or "")
    return converged_vpc_id, converged_subnet_id


@handle_bridge_errors
def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tenant", required=True)
    args = parser.parse_args()

    result: dict[str, Any] = {"success": False, "platform": "network"}

    if DEMO_MODE:
        result.update(
            {
                "success": True,
                "platform": "network",
                "import_flow": True,
                "discovery_flow": False,
                "managed_by_suite": False,
                "network_id": "demo-vpc-0001",
                "vpc_id": "demo-vpc-0001",
                "subnet_id": "demo-subnet-0001",
                "converged_vpc_id": "",
                "converged_subnet_id": "",
                "cidr": "10.100.0.0/16",
                "subnets": [
                    {
                        "subnet_id": "demo-subnet-0001",
                        "cidr": "10.100.1.0/24",
                        "az": "demo-az-a",
                        "auto_assign_public_ip": True,
                        "available_ips": 251,
                    },
                ],
                "security_group_id": "demo-sg-0001",
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
        result = resolve_network_profile(client, args.tenant, tenant_id)
        if result.get("security_group_id") in {"", "n/a", None}:
            result["security_group_id"] = resolve_security_group_id(client, tenant_id)

        # Discovery flow: also create converged VPC+subnet for BM allocation.
        # BM nodes require a converged/storage topology subnet; compute subnet alone
        # causes 500 from metal/allocate.
        if result.get("managed_by_suite"):
            epoch = int(time.time())
            converged_vpc_id, converged_subnet_id = _create_converged_network(
                client, tenant_id, epoch
            )
            result["converged_vpc_id"] = converged_vpc_id
            result["converged_subnet_id"] = converged_subnet_id
        else:
            result.setdefault("converged_vpc_id", "")
            result.setdefault("converged_subnet_id", "")

    print(json.dumps(result, indent=2))
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    sys.exit(main())
