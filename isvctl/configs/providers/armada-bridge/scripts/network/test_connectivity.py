#!/usr/bin/env python3
"""test_connectivity — Armada Bridge network suite, test phase.

Verifies that provisioned BM nodes have IP addresses assigned and are
visible in the Bridge compute inventory.

Reads node IDs from --instance-ids (provisioned by provision_nodes in setup).
Looks up each node in GET /orchestrator/tenants/{id}/metal/computes and
confirms it has a private IP address.

NetworkConnectivityCheck requires:
  instances: [{instance_id, private_ip, public_ip}]   — non-empty list
  tests: {instance_attached_to_subnet: {passed}}       — optional
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
from common.network import api_gap_result
from common.tenant import resolve_tenant_id

DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"


def _parse_instance_ids(raw: str) -> list[str]:
    raw = raw.strip()
    if not raw:
        return []
    if raw.startswith("["):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [str(i) for i in parsed if i]
        except json.JSONDecodeError:
            pass
    return [part.strip() for part in raw.split(",") if part.strip()]


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


@handle_bridge_errors
def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--vpc-id", default="")
    parser.add_argument(
        "--instance-ids",
        default="",
        help="Comma-separated node IDs from provision_nodes step",
    )
    args = parser.parse_args()

    result: dict[str, Any] = {"success": False, "platform": "network"}

    if DEMO_MODE:
        result.update({
            "success": True,
            "instances": [
                {"instance_id": "demo-node-0001", "private_ip": "10.200.0.11", "public_ip": "10.200.0.11"},
                {"instance_id": "demo-node-0002", "private_ip": "10.200.0.12", "public_ip": "10.200.0.12"},
            ],
            "tests": {
                "instance_attached_to_subnet": {"passed": True},
            },
        })
    else:
        node_ids = _parse_instance_ids(args.instance_ids)

        if not node_ids:
            result = api_gap_result(
                "test_connectivity",
                "No instance IDs provided — provision_nodes step may have failed or been skipped",
            )
            print(json.dumps(result, indent=2))
            return 1

        client = BridgeClient.from_env()
        tenant = resolve_tenant_id(client, args.tenant)
        all_nodes = _list_computes(client, tenant)
        node_map = {
            str(n.get("id") or n.get("ID") or ""): n
            for n in all_nodes
        }

        instances: list[dict[str, Any]] = []
        missing: list[str] = []
        no_ip: list[str] = []

        for node_id in node_ids:
            node = node_map.get(node_id)
            if node is None:
                missing.append(node_id)
                continue
            ip = _node_ip(node)
            if not ip:
                no_ip.append(node_id)
            instances.append({
                "instance_id": node_id,
                "private_ip": ip,
                "public_ip": ip,
            })

        if missing:
            result["error"] = f"Nodes not found in computes list: {missing}"
            result["instances"] = instances
            print(json.dumps(result, indent=2))
            return 1

        # instance_attached_to_subnet: all nodes have an IP assigned
        all_have_ip = len(no_ip) == 0
        tests: dict[str, Any] = {
            "instance_attached_to_subnet": {
                "passed": all_have_ip,
                "message": (
                    f"All {len(instances)} nodes have IPs"
                    if all_have_ip
                    else f"Nodes without IP: {no_ip}"
                ),
            }
        }

        result.update({
            "success": all_have_ip and bool(instances),
            "instances": instances,
            "tests": tests,
        })

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
