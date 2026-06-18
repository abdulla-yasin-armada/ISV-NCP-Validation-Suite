#!/usr/bin/env python3
"""launch_instance — Armada Bridge bare metal suite, setup phase.

Flow:
  1. GET /orchestrator/network/topologies
     - All entries networkType "nonetwork" (or empty list) → import flow
     - Otherwise → discovery flow: create VPC + subnet, pass subnetIds

  2. Resolve productTypeId:
     - If --flavor / BRIDGE_BM_FLAVOR is set: use it (productTypeId; catalog IDs
       are resolved to the first available product type in that catalog).
     - Otherwise: GET /orchestrator/catalog, server catalogs only, first
       productType with count >= 1. Optional BRIDGE_BM_GPU_TYPE name filter.

  3. POST /orchestrator/tenants/<tenant>/metal/allocate
     Body: {ProductTypeID, computeNodeCount} + subnetIds in discovery flow
     Note: allocate is async — response is {message, status}, no node ID.

  4. Poll GET /orchestrator/tenants/<tenant>/metal/computes until a node
     with matching productTypeId reaches allocateStatus "done" or "success".

Idempotency:
  - 409 on allocate → proceed to polling (node may already exist from prior run).
  - vpc_id/subnet_id emitted as "" in import flow so teardown skips VPC cleanup.

Output: {success, platform, discovery_flow, flavor_auto_discovered, product_type_id,
         flavor_name, instance_id, state, public_ip, instance_type, vpc_id, subnet_id}
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
from common.catalog import discover_bm_product_type_id
from common.errors import handle_bridge_errors
from common.network import is_discovery_flow, list_topologies, provision_discovery_network
from common.polling import poll_until
from common.tenant import resolve_tenant_id

DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"

_POLL_TIMEOUT = 540
_POLL_INTERVAL = 15
_DONE_STATES = {"done", "success"}


def _find_and_wait_for_node(
    client: BridgeClient, tenant: str, flavor: str
) -> dict[str, Any]:
    """Poll the computes list until a node with matching flavor reaches done/success."""
    path = f"/orchestrator/tenants/{tenant}/metal/computes"

    def check() -> tuple[bool, Any, str]:
        computes = client.get(path)
        nodes = computes if isinstance(computes, list) else computes.get("data", [])
        for node in nodes:
            product_type = str(node.get("productTypeId", "") or "")
            alloc_status = str(node.get("allocateStatus", "") or "")
            if product_type == flavor:
                if alloc_status in _DONE_STATES:
                    return True, node, f"allocateStatus='{alloc_status}'"
                return False, None, f"allocateStatus='{alloc_status}'"
        return False, None, f"productTypeId='{flavor}' not yet visible in list"

    return poll_until(
        check,
        label="launch_instance",
        interval=_POLL_INTERVAL,
        timeout=_POLL_TIMEOUT,
    )


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

    result: dict[str, Any] = {
        "success": False,
        "platform": "bare_metal",
        "discovery_flow": False,
        "flavor_auto_discovered": False,
        "product_type_id": "",
        "flavor_name": "",
        "vpc_id": "n/a",
        "subnet_id": "",
    }

    if DEMO_MODE:
        result.update(
            {
                "success": True,
                "discovery_flow": False,
                "flavor_auto_discovered": False,
                "product_type_id": "demo-product-type",
                "flavor_name": "demo.bm.gpu.8x",
                "instance_id": "demo-bm-node01",
                "state": "running",
                "public_ip": "203.0.114.20",
                "instance_type": "demo.bm.gpu.8x",
                "vpc_id": "n/a",
                "subnet_id": "",
            }
        )
    else:
        client = BridgeClient.from_env()
        tenant = resolve_tenant_id(client, args.tenant)
        epoch = int(time.time())
        vpc_id = "n/a"
        subnet_id = ""

        # ── 1. Detect discovery vs import flow ────────────────────────────
        topologies = list_topologies(client)
        discovery_flow = is_discovery_flow(topologies)
        result["discovery_flow"] = discovery_flow

        # ── 2. Discovery flow: provision fresh VPC + subnet ───────────────
        if discovery_flow:
            vpc_id, subnet_id, _topology = provision_discovery_network(
                client,
                tenant,
                epoch=epoch,
                prefix="isv-bm",
            )
            result["vpc_id"] = vpc_id
            result["subnet_id"] = subnet_id

        # ── 3. Resolve flavor (explicit env/arg or auto-discover from catalog) ─
        explicit_flavor = args.flavor or os.environ.get("BRIDGE_BM_FLAVOR", "")
        flavor, flavor_name, auto_discovered = discover_bm_product_type_id(
            client,
            explicit_id=explicit_flavor,
            gpu_type_filter=os.environ.get("BRIDGE_BM_GPU_TYPE", ""),
        )
        result["flavor_auto_discovered"] = auto_discovered
        result["product_type_id"] = flavor
        result["flavor_name"] = flavor_name

        # ── 4. Allocate BM node (async — response has no node ID) ─────────
        allocate_body: dict[str, Any] = {
            "ProductTypeID": flavor,
            "computeNodeCount": 1,
        }
        if subnet_id:
            allocate_body["subnetIds"] = [subnet_id]

        try:
            client.post(
                f"/orchestrator/tenants/{tenant}/metal/allocate",
                allocate_body,
            )
        except ValueError as e:
            if "status 409" not in str(e):
                raise
            # Already allocated — proceed to polling to find the existing node.

        # ── 5. Poll list until node with matching flavor reaches done/success
        node = _find_and_wait_for_node(client, tenant, flavor)
        node_id = str(node.get("id", ""))

        result.update(
            {
                "success": True,
                "instance_id": node_id,
                # Bridge allocateStatus "done"/"success" → provider-neutral "running"
                "state": "running",
                "public_ip": node.get("externalIPAddress") or node.get("inBandIP", ""),
                "instance_type": str(node.get("productTypeId", flavor)),
            }
        )

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
