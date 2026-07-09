#!/usr/bin/env python3
"""launch_instance — Armada Bridge bare metal suite, setup phase.

Flow:
  1. GET /orchestrator/network/topologies
     - All entries networkType "nonetwork" (or empty list) → import flow
     - Otherwise → discovery flow

  2. provision_bm_node() (common/metal.py):
     - Import flow: resolves productTypeId from catalog, allocates, polls — no VPCs.
     - Discovery flow: creates compute VPC+subnet AND converged VPC+subnet (both
       required by Bridge BM allocate API), then allocates, polls.

  3. Emit JSON with instance_id, vpc_id/subnet_id, converged_vpc_id/converged_subnet_id
     so teardown.py can clean up both VPCs in discovery flow.

Idempotency: 409 on allocate → proceed to polling (handled inside provision_bm_node).

Output: {success, platform, discovery_flow, instance_id, state, public_ip,
         vpc_id, subnet_id, converged_vpc_id, converged_subnet_id}
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common.bridge_client import BridgeClient
from common.context import print_run_context
from common.errors import handle_bridge_errors
from common.metal import provision_bm_node
from common.network import is_discovery_flow, list_topologies
from common.tenant import resolve_tenant_id

DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"


@handle_bridge_errors
def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tenant", required=True)
    parser.add_argument(
        "--flavor",
        default="",
        help="productTypeId UUID; if empty, auto-discovered from server catalog (first with count>=1)",
    )
    parser.add_argument("--name", required=True)
    args = parser.parse_args()

    bm_flavor = args.flavor.strip() or os.environ.get("BRIDGE_BM_FLAVOR", "").strip()
    print_run_context(
        "Bare Metal",
        {
            "instance_name": args.name,
            "BRIDGE_BM_FLAVOR": bm_flavor or "(auto-discover)",
            "BRIDGE_BM_GPU_TYPE": os.environ.get("BRIDGE_BM_GPU_TYPE", "").strip() or "(none)",
        },
    )

    result: dict[str, Any] = {
        "success": False,
        "platform": "bare_metal",
        "discovery_flow": False,
        "vpc_id": "n/a",
        "subnet_id": "",
        "converged_vpc_id": "n/a",
        "converged_subnet_id": "",
    }

    if DEMO_MODE:
        print("[bare_metal] DEMO_MODE: skipping API calls", file=sys.stderr)
        result.update(
            {
                "success": True,
                "discovery_flow": False,
                "instance_id": "demo-bm-node01",
                "state": "running",
                "public_ip": "203.0.114.20",
                "vpc_id": "n/a",
                "subnet_id": "",
                "converged_vpc_id": "n/a",
                "converged_subnet_id": "",
            }
        )
    else:
        client = BridgeClient.from_env()
        tenant = resolve_tenant_id(client, args.tenant)
        print(f"[bare_metal] launching instance {args.name!r} for tenant {tenant}", file=sys.stderr)

        topologies = list_topologies(client)
        discovery_flow = is_discovery_flow(topologies)
        result["discovery_flow"] = discovery_flow
        print(
            f"[bare_metal] network flow: {'discovery' if discovery_flow else 'import'}",
            file=sys.stderr,
        )

        # Forward --flavor arg to env so provision_bm_node picks it up.
        if bm_flavor:
            os.environ["BRIDGE_BM_FLAVOR"] = bm_flavor

        epoch = int(time.time())
        node_ids, vpc_id, subnet_id, converged_vpc_id, converged_subnet_id = provision_bm_node(
            client, tenant,
            epoch=epoch,
            discovery_flow=discovery_flow,
            count=1,
            prefix="isv-bm",
        )

        print(f"[bare_metal] instance ready: {node_ids[0]}", file=sys.stderr)
        result.update(
            {
                "success": True,
                "instance_id": node_ids[0],
                "state": "running",
                "public_ip": "",
                "vpc_id": vpc_id,
                "subnet_id": subnet_id,
                "converged_vpc_id": converged_vpc_id,
                "converged_subnet_id": converged_subnet_id,
            }
        )

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
