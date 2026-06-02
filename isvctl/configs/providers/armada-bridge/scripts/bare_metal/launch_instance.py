#!/usr/bin/env python3
"""launch_instance — Armada Bridge bare metal suite, setup phase.

Flow:
  1. GET /orchestrator/network/topologies
     - Non-empty list → discovery flow: create VPC + subnet, pass subnetIds
     - Empty list     → import flow: allocate without subnetIds

  2. Resolve productTypeId:
     - If --flavor is non-empty: use it directly.
     - Otherwise: GET /orchestrator/catalog and pick the first ProductType
       with count > 1 (more than one unallocated server of that type).

  3. POST /orchestrator/tenants/<tenant>/metal/allocate
     Body: {ProductTypeID, computeNodeCount} + subnetIds in discovery flow
     Note: allocate is async — response is {message, status}, no node ID.

  4. Poll GET /orchestrator/tenants/<tenant>/metal/computes until a node
     with matching productTypeId reaches allocateStatus "done" or "success".

Idempotency:
  - 409 on allocate → proceed to polling (node may already exist from prior run).
  - vpc_id/subnet_id emitted as "" in import flow so teardown skips VPC cleanup.

Output: {success, platform, instance_id, state, public_ip, instance_type,
         vpc_id, subnet_id}
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
from common.errors import handle_bridge_errors
from common.polling import poll_until
from common.tenant import resolve_tenant_id

DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"

_POLL_TIMEOUT = 540
_POLL_INTERVAL = 15
_DONE_STATES = {"done", "success"}


def _discover_flavor(client: BridgeClient) -> str:
    """Return the first productTypeId from the catalog with count > 1.

    Raises RuntimeError if the catalog is empty, has no eligible product type,
    or all eligible product types have an empty ID.
    """
    catalogs = client.get("/orchestrator/catalog")
    if not catalogs:
        raise RuntimeError(
            "Catalog is empty — no BM flavors available. "
            "Set BRIDGE_BM_FLAVOR to a productTypeId explicitly."
        )
    catalogs = catalogs if isinstance(catalogs, list) else []
    for catalog in catalogs:
        for pt in catalog.get("productTypes", []):
            try:
                count = int(pt.get("count", 0))
            except (TypeError, ValueError):
                continue
            pt_id = str(pt.get("id") or "")
            if count > 1 and pt_id:
                return pt_id
    raise RuntimeError(
        "No BM flavor with count > 1 found in catalog. "
        "Set BRIDGE_BM_FLAVOR to a productTypeId explicitly."
    )


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
    parser.add_argument("--flavor", default="", help="productTypeId UUID; if empty, auto-discovered from catalog (first with count>1)")
    parser.add_argument("--name", required=True)
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "bare_metal",
        "vpc_id": "",
        "subnet_id": "",
    }

    if DEMO_MODE:
        result.update(
            {
                "success": True,
                "instance_id": "demo-bm-node01",
                "state": "running",
                "public_ip": "203.0.114.20",
                "instance_type": "demo.bm.gpu.8x",
                "vpc_id": "",
                "subnet_id": "",
            }
        )
    else:
        client = BridgeClient.from_env()
        tenant = resolve_tenant_id(client, args.tenant)
        epoch = int(time.time())
        vpc_id = ""
        subnet_id = ""

        # ── 1. Detect discovery vs import flow ────────────────────────────
        topologies = client.get("/orchestrator/network/topologies")
        discovery_flow = bool(topologies)

        # ── 2. Discovery flow: provision fresh VPC + subnet ───────────────
        if discovery_flow:
            # Pick first Ethernet topology (VPC creation requires Ethernet).
            ethernet_topo = next(
                (t for t in topologies if t.get("networkType") == "ethernet"),
                topologies[0],
            )
            # topologyID in VpcDTO is the topology name string (e.g. "converged"),
            # not the NetworkConfig UUID — matches UI payload {"topologyID": "converged"}.
            topology_name = str(ethernet_topo.get("topology", ""))

            vpc_resp = client.post(
                f"/orchestrator/tenants/{tenant}/vpcs",
                {
                    "name": f"isv-bm-vpc-{epoch}",
                    "topologyID": topology_name,
                    "description": "",
                    "capabilities": [],
                },
            )
            vpc_id = str(vpc_resp.get("id", ""))
            result["vpc_id"] = vpc_id

            subnet_resp = client.post(
                f"/orchestrator/tenants/{tenant}/subnets",
                {
                    "name": f"isv-bm-subnet-{epoch}",
                    "subnetCIDR": "10.200.0.0/24",
                    "topology": topology_name,
                    "parentVpcID": vpc_id,
                },
            )
            subnet_id = str(subnet_resp.get("id", ""))
            result["subnet_id"] = subnet_id

        # ── 3. Resolve flavor (auto-discover if not specified) ────────────
        flavor = args.flavor or _discover_flavor(client)

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
