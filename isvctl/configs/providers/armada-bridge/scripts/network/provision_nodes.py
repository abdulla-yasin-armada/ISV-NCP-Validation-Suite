#!/usr/bin/env python3
"""provision_nodes — Armada Bridge network suite, setup phase.

Allocates BM nodes for use by connectivity, traffic, and IP stability test steps.
Nodes are provisioned into the VPC/subnet created by create_network (setup).

Flow:
  Import flow  — no VPC/subnet creation; nodes allocated without subnetIds.
  Discovery flow — subnetIds passed from create_network step output.
                   Both compute subnet (--subnet-id) AND converged subnet
                   (--converged-subnet-id) are required by the Bridge BM allocate API.

Allocation is async: POST .../metal/allocate returns immediately with no node ID.
Poll GET .../metal/computes until --count nodes with matching productTypeId reach
allocateStatus "done" or "success".

Output:
  {success, platform, instance_ids: [...], instance_ids_csv: "id1,id2",
   instances: [{instance_id, private_ip, public_ip}], provisioned, node_count}
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
from common.catalog import discover_bm_product_type_id
from common.errors import handle_bridge_errors
from common.metal import allocate_bm, compute_node_id, list_computes, node_mgmt_ip, poll_until_bm_ready
from common.network import is_import_flow, list_topologies
from common.tenant import resolve_tenant_id

DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"

_POLL_TIMEOUT = 540
_POLL_INTERVAL = 15


def _parse_explicit_node_ids() -> list[str]:
    """Return deduplicated node IDs from BRIDGE_NETWORK_NODE_IDS; empty list if unset."""
    raw = os.environ.get("BRIDGE_NETWORK_NODE_IDS", "").strip()
    if not raw:
        return []
    seen: set[str] = set()
    ids: list[str] = []
    for part in raw.split(","):
        node_id = part.strip()
        if node_id and node_id not in seen:
            seen.add(node_id)
            ids.append(node_id)
    return ids


def _node_to_instance(node: dict[str, Any]) -> dict[str, Any]:
    ip = node_mgmt_ip(node)
    return {
        "instance_id": compute_node_id(node),
        "private_ip": ip,
        "public_ip": ip,
    }


@handle_bridge_errors
def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--count", type=int, default=2,
                        help="Number of BM nodes to allocate (default: 2)")
    parser.add_argument("--vpc-id", default="")
    parser.add_argument("--subnet-id", default="")
    parser.add_argument("--converged-subnet-id", default="",
                        help="Converged topology subnet ID required by Bridge for BM allocation")
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "network",
        "instance_ids": [],
        "instance_ids_csv": "",
        "instances": [],
        "provisioned": False,
        "node_count": 0,
    }

    explicit_ids = _parse_explicit_node_ids()
    if explicit_ids:
        print(
            f"[network] reusing existing nodes from BRIDGE_NETWORK_NODE_IDS: {explicit_ids} "
            "(skipping catalog allocate)",
            file=sys.stderr,
        )
        client = BridgeClient.from_env()
        tenant = resolve_tenant_id(client, args.tenant)
        all_nodes = list_computes(client, tenant)
        node_map = {compute_node_id(n): n for n in all_nodes}
        instances = [
            {
                "instance_id": node_id,
                "private_ip": node_mgmt_ip(node_map.get(node_id, {})),
                "public_ip": node_mgmt_ip(node_map.get(node_id, {})),
            }
            for node_id in explicit_ids
        ]
        result.update({
            "success": True,
            "instance_ids": explicit_ids,
            "instance_ids_csv": ",".join(explicit_ids),
            "instances": instances,
            "provisioned": False,
            "node_count": len(explicit_ids),
        })
        print(json.dumps(result, indent=2))
        return 0

    if DEMO_MODE:
        print("[network] DEMO_MODE: skipping API calls", file=sys.stderr)
        result.update({
            "success": True,
            "instance_ids": ["demo-node-0001", "demo-node-0002"],
            "instance_ids_csv": "demo-node-0001,demo-node-0002",
            "instances": [
                {"instance_id": "demo-node-0001", "private_ip": "10.200.0.11", "public_ip": "10.200.0.11"},
                {"instance_id": "demo-node-0002", "private_ip": "10.200.0.12", "public_ip": "10.200.0.12"},
            ],
            "provisioned": True,
            "node_count": 2,
        })
    else:
        client = BridgeClient.from_env()
        tenant = resolve_tenant_id(client, args.tenant)
        print(f"[network] provisioning {args.count} BM node(s) for tenant {tenant}", file=sys.stderr)

        existing_nodes = list_computes(client, tenant)
        existing_ids = {compute_node_id(n) for n in existing_nodes}
        existing_ids.discard("")

        explicit_flavor = os.environ.get("BRIDGE_BM_FLAVOR", "").strip()
        product_type_id, flavor_name, auto_discovered = discover_bm_product_type_id(
            client,
            explicit_id=explicit_flavor,
            gpu_type_filter=os.environ.get("BRIDGE_BM_GPU_TYPE", ""),
        )

        topologies = list_topologies(client)
        import_flow = is_import_flow(topologies)
        print(
            f"[network] productTypeId={product_type_id} ({flavor_name}), "
            f"flow={'import' if import_flow else 'discovery'}",
            file=sys.stderr,
        )

        # Both compute and converged subnet IDs are required by the Bridge BM allocate API
        # in discovery flow. Import flow passes no subnet IDs.
        subnet_ids = [s for s in [args.subnet_id, args.converged_subnet_id] if s]

        allocate_bm(
            client, tenant, product_type_id,
            count=args.count,
            subnet_ids=subnet_ids or None,
        )

        nodes = poll_until_bm_ready(
            client, tenant, product_type_id, existing_ids,
            count=args.count,
            label="network_provision_nodes",
            interval=_POLL_INTERVAL,
            timeout=_POLL_TIMEOUT,
        )

        instances = [_node_to_instance(n) for n in nodes]
        ids = [inst["instance_id"] for inst in instances]

        result.update({
            "success": bool(ids),
            "instance_ids": ids,
            "instance_ids_csv": ",".join(ids),
            "instances": instances,
            "provisioned": True,
            "node_count": len(ids),
            "product_type_id": product_type_id,
            "flavor_name": flavor_name,
            "import_flow": import_flow,
        })

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
