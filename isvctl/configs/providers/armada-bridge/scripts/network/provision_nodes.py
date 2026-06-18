#!/usr/bin/env python3
"""provision_nodes — Armada Bridge network suite, setup phase.

Allocates BM nodes for use by connectivity, traffic, and IP stability test steps.
Nodes are provisioned into the VPC/subnet created by create_network (setup).

Flow:
  Import flow  — no VPC/subnet creation; nodes allocated without subnetIds.
  Discovery flow — subnetIds passed from create_network step output.

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
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common.bridge_client import BridgeClient
from common.catalog import discover_bm_product_type_id
from common.errors import handle_bridge_errors
from common.network import is_import_flow, list_topologies
from common.polling import poll_until
from common.tenant import resolve_tenant_id

DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"

_POLL_TIMEOUT = 540
_POLL_INTERVAL = 15
_DONE_STATES = {"done", "success"}


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


def _list_computes(client: BridgeClient, tenant: str) -> list[dict[str, Any]]:
    resp = client.get(f"/orchestrator/tenants/{tenant}/metal/computes")
    nodes = resp if isinstance(resp, list) else (resp or {}).get("data", [])
    return [n for n in nodes if isinstance(n, dict)]


def _node_ip(node: dict[str, Any]) -> str:
    return str(
        node.get("externalIPAddress")
        or node.get("inBandIP")
        or node.get("ipAddress")
        or ""
    )


def _node_to_instance(node: dict[str, Any]) -> dict[str, Any]:
    node_id = str(node.get("id") or node.get("ID") or "")
    ip = _node_ip(node)
    return {
        "instance_id": node_id,
        "private_ip": ip,
        "public_ip": ip,
    }


def _poll_until_n_ready(
    client: BridgeClient,
    tenant: str,
    product_type_id: str,
    count: int,
    existing_ids: set[str],
) -> list[dict[str, Any]]:
    """Poll computes list until `count` new nodes with matching productTypeId are ready."""

    def check() -> tuple[bool, Any, str]:
        nodes = _list_computes(client, tenant)
        ready = [
            n for n in nodes
            if str(n.get("productTypeId", "") or "") == product_type_id
            and str(n.get("allocateStatus", "") or "").lower() in _DONE_STATES
            and str(n.get("id") or n.get("ID") or "") not in existing_ids
        ]
        if len(ready) >= count:
            return True, ready[:count], f"{len(ready)}/{count} nodes ready"
        # Surface any in-progress status for logging
        pending = [
            n for n in nodes
            if str(n.get("productTypeId", "") or "") == product_type_id
            and str(n.get("id") or n.get("ID") or "") not in existing_ids
        ]
        statuses = {str(n.get("allocateStatus", "")) for n in pending}
        return False, None, f"{len(ready)}/{count} ready (statuses: {statuses or 'none visible'})"

    return poll_until(
        check,
        label="provision_nodes",
        interval=_POLL_INTERVAL,
        timeout=_POLL_TIMEOUT,
    )


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
        all_nodes = _list_computes(client, tenant)
        node_map = {str(n.get("id") or n.get("ID") or ""): n for n in all_nodes}
        instances = [
            {
                "instance_id": node_id,
                "private_ip": _node_ip(node_map.get(node_id, {})),
                "public_ip": _node_ip(node_map.get(node_id, {})),
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

        # Snapshot existing node IDs so we can distinguish newly allocated ones
        existing_nodes = _list_computes(client, tenant)
        existing_ids = {
            str(n.get("id") or n.get("ID") or "")
            for n in existing_nodes
        }

        # Resolve flavor
        explicit_flavor = os.environ.get("BRIDGE_BM_FLAVOR", "").strip()
        product_type_id, flavor_name, auto_discovered = discover_bm_product_type_id(
            client,
            explicit_id=explicit_flavor,
            gpu_type_filter=os.environ.get("BRIDGE_BM_GPU_TYPE", ""),
        )

        # Build allocate body
        allocate_body: dict[str, Any] = {
            "ProductTypeID": product_type_id,
            "computeNodeCount": args.count,
        }

        topologies = list_topologies(client)
        import_flow = is_import_flow(topologies)

        # BM allocation requires both compute and converged subnet IDs.
        # Passing only the compute subnet causes a 500: "none of the provided subnets
        # is for converged or storage topology".
        subnet_ids = [s for s in [args.subnet_id, args.converged_subnet_id] if s]
        if subnet_ids:
            allocate_body["subnetIds"] = subnet_ids

        try:
            client.post(
                f"/orchestrator/tenants/{tenant}/metal/allocate",
                allocate_body,
            )
        except ValueError as exc:
            if "status 409" not in str(exc):
                raise
            # 409 — allocation already in progress, proceed to polling

        # Poll until count nodes are ready
        nodes = _poll_until_n_ready(
            client, tenant, product_type_id, args.count, existing_ids
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
