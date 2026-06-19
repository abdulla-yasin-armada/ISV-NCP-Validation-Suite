"""VPC and subnet lifecycle primitives for Armada Bridge discovery flow.

Functions:
  pick_compute_topology    Pick the compute ethernet topology from a topologies list.
  pick_converged_topology  Pick the converged topology from a topologies list.
  create_vpc               POST /vpcs — returns vpc_id.
  create_subnet            POST /subnets — returns subnet_id.
  delete_subnet            DELETE /subnets/{id} — ignores 404.
  delete_vpc               Delete all subnets of a VPC then the VPC — ignores 404.
  provision_discovery_vpcs
      Create compute VPC+subnet and converged VPC+subnet for BM discovery flow.
      Returns (compute_vpc_id, compute_subnet_id, converged_vpc_id, converged_subnet_id).
  deprovision_discovery_vpcs
      Best-effort delete both VPCs (and their subnets).
      Skips VPCs whose id is not an orchestrator UUID (e.g. "n/a" for import flow).

Discovery flow BM allocation requires two subnet IDs — one from a compute topology
VPC and one from a converged topology VPC. Passing only one causes a 500 from the
Bridge API ("none of the provided subnets is for converged or storage topology").
"""
from __future__ import annotations

import sys
import time
from typing import Any

from .bridge_client import BridgeClient
from .network import is_orchestrator_resource_id, list_topologies

COMPUTE_CIDR = "10.200.0.0/24"
CONVERGED_CIDR = "10.200.0.0/24"


def pick_compute_topology(topologies: list[dict[str, Any]]) -> dict[str, Any]:
    """Return the compute ethernet topology, falling back to first ethernet entry."""
    return next(
        (t for t in topologies if str(t.get("topology", "")).lower() == "compute"),
        next(
            (t for t in topologies if t.get("networkType") == "ethernet"),
            topologies[0],
        ),
    )


def pick_converged_topology(topologies: list[dict[str, Any]]) -> dict[str, Any]:
    """Return the converged topology, falling back to first ethernet entry."""
    return next(
        (t for t in topologies if str(t.get("topology", "")).lower() == "converged"),
        next(
            (t for t in topologies if t.get("networkType") == "ethernet"),
            topologies[0],
        ),
    )


def create_vpc(
    client: BridgeClient,
    tenant_id: str,
    topology_name: str,
    name: str,
) -> str:
    """POST /vpcs and return the new vpc_id."""
    print(f"[vpc] creating VPC {name!r} (topology={topology_name!r})", file=sys.stderr)
    resp = client.post(
        f"/orchestrator/tenants/{tenant_id}/vpcs",
        {
            "name": name,
            "topologyID": topology_name,
            "description": "",
            "capabilities": [],
        },
    )
    vpc_id = str(resp.get("id", ""))
    if not vpc_id:
        raise RuntimeError(
            f"Bridge API returned no VPC id after creating {name!r} "
            f"(topology={topology_name!r}). Response: {resp}"
        )
    print(f"[vpc] created VPC {name!r} → {vpc_id}", file=sys.stderr)
    return vpc_id


def create_subnet(
    client: BridgeClient,
    tenant_id: str,
    vpc_id: str,
    topology_name: str,
    name: str,
    cidr: str,
) -> str:
    """POST /subnets under vpc_id and return the new subnet_id."""
    print(f"[vpc] creating subnet {name!r} (vpc={vpc_id}, cidr={cidr})", file=sys.stderr)
    resp = client.post(
        f"/orchestrator/tenants/{tenant_id}/subnets",
        {
            "name": name,
            "subnetCIDR": cidr,
            "topology": topology_name,
            "parentVpcID": vpc_id,
        },
    )
    subnet_id = str(resp.get("id", ""))
    if not subnet_id:
        raise RuntimeError(
            f"Bridge API returned no subnet id after creating {name!r} "
            f"(vpc={vpc_id}, cidr={cidr}). Response: {resp}"
        )
    print(f"[vpc] created subnet {name!r} → {subnet_id}", file=sys.stderr)
    return subnet_id


def delete_subnet(client: BridgeClient, tenant_id: str, subnet_id: str) -> None:
    """DELETE a subnet. Ignores 404 (already gone). No-op for non-UUID ids."""
    if not subnet_id or not is_orchestrator_resource_id(subnet_id):
        return
    print(f"[vpc] deleting subnet {subnet_id}", file=sys.stderr)
    try:
        client.delete(f"/orchestrator/tenants/{tenant_id}/subnets/{subnet_id}")
    except ValueError as exc:
        if "404" not in str(exc):
            raise
        print(f"[vpc] subnet {subnet_id} already gone (404)", file=sys.stderr)


def delete_vpc(client: BridgeClient, tenant_id: str, vpc_id: str) -> None:
    """Delete all subnets of a VPC then the VPC itself.

    Ignores 404 on both operations. No-op for non-UUID ids (e.g. "n/a").
    """
    if not vpc_id or not is_orchestrator_resource_id(vpc_id):
        return
    print(f"[vpc] deleting VPC {vpc_id} (and its subnets)", file=sys.stderr)
    try:
        resp = client.get(f"/orchestrator/tenants/{tenant_id}/subnets")
        subnets = resp if isinstance(resp, list) else (resp or {}).get("data", [])
        for subnet in subnets:
            if str(subnet.get("parentVpcID", "") or "") == vpc_id:
                delete_subnet(client, tenant_id, str(subnet.get("id", "") or ""))
    except ValueError:
        pass  # best-effort — proceed to VPC delete regardless
    try:
        client.delete(f"/orchestrator/tenants/{tenant_id}/vpcs/{vpc_id}")
        print(f"[vpc] deleted VPC {vpc_id}", file=sys.stderr)
    except ValueError as exc:
        if "404" not in str(exc):
            raise
        print(f"[vpc] VPC {vpc_id} already gone (404)", file=sys.stderr)


def provision_discovery_vpcs(
    client: BridgeClient,
    tenant_id: str,
    *,
    epoch: int | None = None,
    prefix: str = "isv",
) -> tuple[str, str, str, str]:
    """Create compute and converged VPCs with one subnet each for BM discovery flow.

    Returns:
        (compute_vpc_id, compute_subnet_id, converged_vpc_id, converged_subnet_id)
    """
    if epoch is None:
        epoch = int(time.time())

    print("[vpc] provisioning discovery VPCs (compute + converged)...", file=sys.stderr)
    topologies = list_topologies(client)
    if not topologies:
        raise RuntimeError(
            "Bridge API returned no network topologies. "
            "Check that your tenant has at least one topology configured "
            "(BRIDGE_URL and BRIDGE_TENANT are correct)."
        )

    compute_topo = pick_compute_topology(topologies)
    converged_topo = pick_converged_topology(topologies)
    compute_topo_name = str(compute_topo.get("topology", "") or compute_topo.get("id", ""))
    converged_topo_name = str(converged_topo.get("topology", "") or converged_topo.get("id", ""))
    print(
        f"[vpc] using compute topology={compute_topo_name!r}, "
        f"converged topology={converged_topo_name!r}",
        file=sys.stderr,
    )

    compute_vpc_id = create_vpc(client, tenant_id, compute_topo_name, f"{prefix}-compute-vpc-{epoch}")
    compute_subnet_id = create_subnet(
        client, tenant_id, compute_vpc_id, compute_topo_name,
        f"{prefix}-compute-subnet-{epoch}", COMPUTE_CIDR,
    )

    converged_vpc_id = create_vpc(client, tenant_id, converged_topo_name, f"{prefix}-converged-vpc-{epoch}")
    converged_subnet_id = create_subnet(
        client, tenant_id, converged_vpc_id, converged_topo_name,
        f"{prefix}-converged-subnet-{epoch}", CONVERGED_CIDR,
    )

    print(
        f"[vpc] discovery VPCs ready: compute={compute_vpc_id}, converged={converged_vpc_id}",
        file=sys.stderr,
    )
    return compute_vpc_id, compute_subnet_id, converged_vpc_id, converged_subnet_id


def deprovision_discovery_vpcs(
    client: BridgeClient,
    tenant_id: str,
    compute_vpc_id: str,
    converged_vpc_id: str,
) -> None:
    """Best-effort delete compute and converged VPCs (subnets first, then VPC).

    Skips any vpc_id that is not a valid orchestrator UUID — safe to call with
    "n/a" or empty string from import flow or older single-VPC state files.
    """
    print("[vpc] deprovisioning discovery VPCs (compute + converged)...", file=sys.stderr)
    delete_vpc(client, tenant_id, compute_vpc_id)
    delete_vpc(client, tenant_id, converged_vpc_id)
    print("[vpc] discovery VPC deprovisioning complete", file=sys.stderr)
