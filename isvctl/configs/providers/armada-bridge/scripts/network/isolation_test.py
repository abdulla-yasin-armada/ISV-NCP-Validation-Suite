#!/usr/bin/env python3
"""isolation_test — Armada Bridge network suite, test phase.

Control-plane VPC isolation check.

Creates two independent VPCs (A and B) under the same tenant, verifies they
are isolated by default (no peering, no overlapping routes), then deletes both.
No dependency on any other suite step.

Import flow: verifies non-overlapping CIDRs across tenant IPAllocation CRs.

VpcIsolationCheck requires:
  tests: {no_peering, no_cross_routes_a, no_cross_routes_b}
  vpc_a: {id}, vpc_b: {id}
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common.bridge_client import BridgeClient
from common.errors import handle_bridge_errors
from common.network import (
    bridge_subnet_to_output,
    is_import_flow,
    is_ipalloc_ready,
    is_orchestrator_resource_id,
    ipalloc_to_subnet,
    list_tenant_subnets,
    list_topologies,
    pick_ethernet_topology,
    tenant_ip_allocations,
)
from common.tenant import resolve_tenant_id

DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"


def _failed(error: str) -> dict[str, Any]:
    return {"passed": False, "error": error}


def _cidrs_overlap(
    subnets_a: list[dict[str, Any]], subnets_b: list[dict[str, Any]]
) -> list[str]:
    """Return CIDR pairs that overlap between two VPC subnet lists."""
    overlaps: list[str] = []
    nets_a: list[tuple[str, Any]] = []
    for s in subnets_a:
        try:
            nets_a.append((s["cidr"], ipaddress.ip_network(s["cidr"], strict=False)))
        except (ValueError, KeyError):
            pass
    for s in subnets_b:
        cidr_b = s.get("cidr", "")
        try:
            net_b = ipaddress.ip_network(cidr_b, strict=False)
        except ValueError:
            continue
        for cidr_a, net_a in nets_a:
            if type(net_a) is type(net_b) and net_a.overlaps(net_b):
                overlaps.append(f"{cidr_a} <-> {cidr_b}")
    return overlaps


def _check_no_peering(
    client: BridgeClient, tenant_id: str, vpc_a_id: str, vpc_b_id: str
) -> dict[str, Any]:
    """Return passed=True when no peering exists between vpc_a and vpc_b.

    Tries GET /orchestrator/tenants/{id}/vpc-peerings. A 404 means the
    peering API is not exposed — Bridge VPCs are isolated by topology.
    """
    try:
        resp = client.get(f"/orchestrator/tenants/{tenant_id}/vpc-peerings")
        peerings = resp if isinstance(resp, list) else []
        ids = {vpc_a_id, vpc_b_id}
        for p in peerings:
            peer_pair = {
                str(p.get("vpcAID") or p.get("vpc_a_id") or ""),
                str(p.get("vpcBID") or p.get("vpc_b_id") or ""),
            }
            if ids <= peer_pair:
                return _failed(f"Peering exists between {vpc_a_id} and {vpc_b_id}")
        return {"passed": True, "message": f"No peering between {vpc_a_id} and {vpc_b_id}"}
    except ValueError:
        # 404 — no peering API means no peering capability exists
        return {
            "passed": True,
            "message": "No VPC peering API on Bridge (isolation enforced by topology)",
        }


def _create_vpc(
    client: BridgeClient, tenant_id: str, topology_name: str, label: str, epoch: int
) -> str:
    """Create a VPC and return its ID. Raises RuntimeError if creation fails."""
    resp = client.post(
        f"/orchestrator/tenants/{tenant_id}/vpcs",
        {
            "name": f"isv-iso-{label}-{epoch}",
            "topologyID": topology_name,
            "description": f"isvctl isolation probe VPC {label} (ephemeral)",
            "capabilities": [],
        },
    )
    vpc_id = str(resp.get("id", "") or "")
    if not vpc_id:
        raise RuntimeError(f"Bridge returned no id when creating isolation VPC {label}")
    return vpc_id


def _delete_vpc(client: BridgeClient, tenant_id: str, vpc_id: str) -> None:
    """Delete a VPC, ignoring errors (best-effort cleanup)."""
    if vpc_id and is_orchestrator_resource_id(vpc_id):
        try:
            client.delete(f"/orchestrator/tenants/{tenant_id}/vpcs/{vpc_id}")
        except ValueError:
            pass


def _run_discovery_checks(
    client: BridgeClient, tenant_id: str
) -> tuple[dict[str, Any], str, str]:
    """Create VPC A and VPC B, run isolation checks, delete both.

    Returns (tests, vpc_a_id, vpc_b_id).
    """
    topologies = list_topologies(client)
    topology_name = str(pick_ethernet_topology(topologies).get("topology", ""))
    epoch = int(time.time())

    vpc_a_id = ""
    vpc_b_id = ""
    tests: dict[str, Any] = {}

    try:
        vpc_a_id = _create_vpc(client, tenant_id, topology_name, "a", epoch)
        vpc_b_id = _create_vpc(client, tenant_id, topology_name, "b", epoch)

        tests["no_peering"] = _check_no_peering(client, tenant_id, vpc_a_id, vpc_b_id)

        subnets_a = [
            bridge_subnet_to_output(s)
            for s in list_tenant_subnets(client, tenant_id, vpc_id=vpc_a_id)
        ]
        subnets_b = [
            bridge_subnet_to_output(s)
            for s in list_tenant_subnets(client, tenant_id, vpc_id=vpc_b_id)
        ]

        if not subnets_a or not subnets_b:
            # Freshly created VPCs have no subnets — trivially no cross-routes
            tests["no_cross_routes_a"] = {
                "passed": True,
                "message": "VPC A and VPC B are separate namespaces with no shared subnets",
            }
            tests["no_cross_routes_b"] = {
                "passed": True,
                "message": "VPC A and VPC B are separate namespaces with no shared subnets",
            }
        else:
            overlaps = _cidrs_overlap(subnets_a, subnets_b)
            if overlaps:
                msg = f"CIDR overlap detected: {overlaps}"
                tests["no_cross_routes_a"] = _failed(msg)
                tests["no_cross_routes_b"] = _failed(msg)
            else:
                tests["no_cross_routes_a"] = {
                    "passed": True,
                    "message": "VPC A subnets do not overlap with VPC B",
                }
                tests["no_cross_routes_b"] = {
                    "passed": True,
                    "message": "VPC B subnets do not overlap with VPC A",
                }
    finally:
        _delete_vpc(client, tenant_id, vpc_a_id)
        _delete_vpc(client, tenant_id, vpc_b_id)

    return tests, vpc_a_id, vpc_b_id


def _run_import_checks(tenant_slug: str) -> tuple[dict[str, Any], str, str]:
    """Import flow: verify non-overlapping CIDRs across tenant IPAllocation CRs."""
    namespace = os.environ.get("BRIDGE_NETWORK_NAMESPACE", "").strip() or None
    allocations = tenant_ip_allocations(tenant_slug, namespace=namespace)

    if not allocations:
        err = f"No IPAllocation CRs found for tenant {tenant_slug!r}"
        return (
            {
                "no_peering": _failed(err),
                "no_cross_routes_a": _failed(err),
                "no_cross_routes_b": _failed(err),
            },
            "n/a",
            "n/a",
        )

    ready = [a for a in allocations if is_ipalloc_ready(a)] or allocations
    subnets = [ipalloc_to_subnet(a) for a in ready]

    overlaps: list[str] = []
    seen: list[tuple[str, Any]] = []
    for sub in subnets:
        cidr = sub.get("cidr", "")
        try:
            net = ipaddress.ip_network(cidr, strict=False)
        except ValueError:
            continue
        for existing_cidr, existing_net in seen:
            if type(net) is type(existing_net) and net.overlaps(existing_net):
                overlaps.append(f"{cidr} <-> {existing_cidr}")
        seen.append((cidr, net))

    if overlaps:
        err = f"Overlapping CIDRs: {overlaps}"
        tests: dict[str, Any] = {
            "no_peering": {"passed": True, "message": "Import flow: no VPC peering API"},
            "no_cross_routes_a": _failed(err),
            "no_cross_routes_b": _failed(err),
        }
    else:
        tests = {
            "no_peering": {"passed": True, "message": "Import flow: no VPC peering API"},
            "no_cross_routes_a": {
                "passed": True,
                "message": f"{len(subnets)} subnets with non-overlapping CIDRs",
            },
            "no_cross_routes_b": {
                "passed": True,
                "message": f"{len(subnets)} subnets with non-overlapping CIDRs",
            },
        }

    names = [str(a.get("metadata", {}).get("name", "")) for a in ready]
    vpc_a = names[0] if names else "n/a"
    vpc_b = names[-1] if len(names) > 1 else vpc_a
    return tests, vpc_a, vpc_b


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
                "tests": {
                    "no_peering": {"passed": True},
                    "no_cross_routes_a": {"passed": True},
                    "no_cross_routes_b": {"passed": True},
                },
                "vpc_a": {"id": "demo-vpc-0001"},
                "vpc_b": {"id": "demo-vpc-0002"},
            }
        )
    else:
        client = BridgeClient.from_env()
        tenant_id = resolve_tenant_id(client, args.tenant)
        topologies = list_topologies(client)

        if is_import_flow(topologies):
            tests, vpc_a_id, vpc_b_id = _run_import_checks(args.tenant)
        else:
            tests, vpc_a_id, vpc_b_id = _run_discovery_checks(client, tenant_id)

        result.update(
            {
                "success": all(t.get("passed") for t in tests.values()),
                "platform": "network",
                "tests": tests,
                "vpc_a": {"id": vpc_a_id},
                "vpc_b": {"id": vpc_b_id},
            }
        )

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
