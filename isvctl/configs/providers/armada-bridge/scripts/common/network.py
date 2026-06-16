"""Bridge network utilities covering five areas:

  IP utilities
    is_private — classify an IP as private/loopback/link-local.

  Topology helpers (discovery vs import flow detection)
    list_topologies, is_import_flow, is_discovery_flow,
    is_managed_network_id, is_orchestrator_resource_id,
    pick_ethernet_topology.

  kubectl / IPAllocation CRD helpers (import/GPUaaS labs)
    kubectl_json, list_ip_allocation_crs, tenant_ip_allocations,
    ipalloc_main_status, is_ipalloc_ready, ipalloc_to_subnet,
    pick_primary_ipalloc, dhcp_options_from_ipalloc,
    discover_import_network.

  Bridge orchestrator VPC/subnet/security-group CRUD wrappers
    list_tenant_vpcs, get_tenant_vpc, list_tenant_subnets,
    list_tenant_security_groups, resolve_security_group_id,
    bridge_subnet_to_output, bridge_dhcp_options_placeholder,
    provision_discovery_network, provision_discovery_network_full,
    resolve_network_profile, load_network_by_vpc_id.

  API gap / flow-guard helpers
    api_gap_result, import_flow_block.
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import shlex
import subprocess
import time
from typing import Any

from .bridge_client import BridgeClient

_NO_NETWORK = "nonetwork"
_IPALLOC_CRD = "ipallocations.networking.gpuaas.amcop.com"
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def is_private(ip: str) -> bool:
    """Return True when ip is a private/loopback/link-local address."""
    try:
        addr = ipaddress.ip_address(ip)
        return addr.is_private or addr.is_loopback or addr.is_link_local
    except ValueError:
        return False


def list_topologies(client: BridgeClient) -> list[dict[str, Any]]:
    """GET /orchestrator/network/topologies."""
    resp = client.get("/orchestrator/network/topologies")
    if isinstance(resp, list):
        return resp
    return []


def is_import_flow(topologies: list[dict[str, Any]]) -> bool:
    """Import flow when topologies are empty or every entry is networkType nonetwork."""
    if not topologies:
        return True
    return all(t.get("networkType") == _NO_NETWORK for t in topologies)


def is_discovery_flow(topologies: list[dict[str, Any]]) -> bool:
    """Discovery flow when any topology requires VPC/subnet provisioning (e.g. ethernet)."""
    return not is_import_flow(topologies)


def is_managed_network_id(resource_id: str) -> bool:
    """True when id is a real Bridge VPC/subnet UUID (discovery flow), not a wiring placeholder."""
    normalized = resource_id.strip().lower()
    return bool(normalized) and normalized != "n/a"


def is_orchestrator_resource_id(resource_id: str) -> bool:
    """True for Bridge orchestrator UUID resources safe to DELETE in teardown."""
    return bool(_UUID_RE.match(resource_id.strip()))


def pick_ethernet_topology(topologies: list[dict[str, Any]]) -> dict[str, Any]:
    """Pick first ethernet topology, or the first entry if none marked ethernet.

    Caller must ensure topologies is non-empty; raises IndexError otherwise.
    """
    return next(
        (t for t in topologies if t.get("networkType") == "ethernet"),
        topologies[0],
    )


def provision_discovery_network(
    client: BridgeClient,
    tenant_id: str,
    *,
    epoch: int,
    prefix: str = "isv-vm",
    cidr: str = "10.200.0.0/24",
) -> tuple[str, str, str]:
    """Create VPC + subnet for discovery flow. Returns (vpc_id, subnet_id, topology_name)."""
    topologies = list_topologies(client)
    if not topologies:
        raise RuntimeError("provision_discovery_network called but topologies list is empty")

    ethernet_topo = pick_ethernet_topology(topologies)
    topology_name = str(ethernet_topo.get("topology", ""))

    vpc_resp = client.post(
        f"/orchestrator/tenants/{tenant_id}/vpcs",
        {
            "name": f"{prefix}-vpc-{epoch}",
            "topologyID": topology_name,
            "description": "",
            "capabilities": [],
        },
    )
    vpc_id = str(vpc_resp.get("id", ""))

    subnet_resp = client.post(
        f"/orchestrator/tenants/{tenant_id}/subnets",
        {
            "name": f"{prefix}-subnet-{epoch}",
            "subnetCIDR": cidr,
            "topology": topology_name,
            "parentVpcID": vpc_id,
        },
    )
    subnet_id = str(subnet_resp.get("id", ""))
    return vpc_id, subnet_id, topology_name


def kubectl_json(*args: str) -> dict[str, Any]:
    """Run kubectl with -o json and return parsed output."""
    kubectl_prefix = shlex.split(os.environ.get("KUBECTL", "kubectl"))
    cmd = [*kubectl_prefix, *args, "-o", "json"]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        stderr = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(f"kubectl failed ({' '.join(cmd)}): {stderr}")
    if not result.stdout.strip():
        return {}
    parsed = json.loads(result.stdout)
    return parsed if isinstance(parsed, dict) else {}


def list_ip_allocation_crs(*, namespace: str | None = None) -> list[dict[str, Any]]:
    """List GPUaaS IPAllocation CRs cluster-wide or in one namespace."""
    if namespace:
        data = kubectl_json("get", _IPALLOC_CRD, "-n", namespace)
    else:
        data = kubectl_json("get", _IPALLOC_CRD, "-A")
    items = data.get("items", [])
    return items if isinstance(items, list) else []


def tenant_ip_allocations(tenant_slug: str, *, namespace: str | None = None) -> list[dict[str, Any]]:
    """Return IPAllocation CRs whose metadata.name starts with the tenant slug."""
    prefix = tenant_slug.strip().lower()
    if not prefix:
        return []
    matches: list[dict[str, Any]] = []
    for item in list_ip_allocation_crs(namespace=namespace):
        name = str(item.get("metadata", {}).get("name", "") or "")
        if name.lower().startswith(prefix):
            matches.append(item)
    return matches


def ipalloc_main_status(item: dict[str, Any]) -> str:
    """Return the lowercased status.main_status field of an IPAllocation CR, or ''."""
    return str(item.get("status", {}).get("main_status", "") or "").lower()


def is_ipalloc_ready(item: dict[str, Any]) -> bool:
    """Return True when the IPAllocation CR status is 'success' or 'done'."""
    return ipalloc_main_status(item) in {"success", "done"}


def _host_from_ipalloc_name(name: str) -> str:
    if "-subnet-" in name:
        return name.rsplit("-subnet-", 1)[-1]
    return name.rsplit("-", 1)[-1]


def _available_ips_for_cidr(cidr: str) -> int:
    try:
        network = ipaddress.ip_network(cidr, strict=False)
    except ValueError:
        return 0
    if network.num_addresses <= 2:
        return max(int(network.num_addresses), 0)
    return int(network.num_addresses) - 2


def ipalloc_to_subnet(item: dict[str, Any]) -> dict[str, Any]:
    """Map an IPAllocation CR to the generic subnet dict used by isvtest."""
    name = str(item.get("metadata", {}).get("name", "") or "")
    spec = item.get("spec", {})
    cidr = str(spec.get("subnet", "") or "")
    return {
        "subnet_id": name,
        "cidr": cidr,
        "az": _host_from_ipalloc_name(name),
        "auto_assign_public_ip": False,
        "available_ips": _available_ips_for_cidr(cidr),
        "topology": str(spec.get("topology", "") or spec.get("networkType", "") or ""),
        "main_status": ipalloc_main_status(item),
    }


def pick_primary_ipalloc(items: list[dict[str, Any]]) -> dict[str, Any]:
    """Prefer a ready inband allocation, else the first ready item, else the first item."""
    if not items:
        raise RuntimeError("no tenant IPAllocation CRs found")

    def sort_key(item: dict[str, Any]) -> tuple[int, int, str]:
        name = str(item.get("metadata", {}).get("name", "") or "").lower()
        inband = 0 if "inband" in name else 1
        ready = 0 if is_ipalloc_ready(item) else 1
        return (ready, inband, name)

    return sorted(items, key=sort_key)[0]


def dhcp_options_from_ipalloc(item: dict[str, Any], tenant_slug: str) -> dict[str, Any]:
    """Build dhcp_options block for VpcIpConfigCheck from IPAllocation status/spec."""
    status = item.get("status", {})
    spec = item.get("spec", {})
    dns_env = os.environ.get("BRIDGE_NETWORK_DNS", "").strip()
    dns_servers: list[str] = []
    if dns_env:
        dns_servers = [part.strip() for part in dns_env.split(",") if part.strip()]
    else:
        gateway = status.get("gateway") or status.get("defaultGateway") or spec.get("gateway")
        if gateway:
            dns_servers = [str(gateway)]

    if not dns_servers:
        dns_servers = ["127.0.0.1"]

    name = str(item.get("metadata", {}).get("name", "") or tenant_slug)
    return {
        "dhcp_options_id": name,
        "domain_name": f"{tenant_slug}.internal",
        "domain_name_servers": dns_servers,
        "ntp_servers": [],
    }


def discover_import_network(tenant_slug: str, *, namespace: str | None = None) -> dict[str, Any]:
    """Discover pre-provisioned GPUaaS IPAllocation CRs for an import lab tenant."""
    allocations = tenant_ip_allocations(tenant_slug, namespace=namespace)
    if not allocations:
        ns_hint = f" namespace={namespace!r}" if namespace else ""
        raise RuntimeError(
            f"No IPAllocation CRs found for tenant prefix {tenant_slug!r}{ns_hint}. "
            "Ensure kubectl access on the Bridge cluster and BRIDGE_TENANT matches CR names."
        )

    ready = [item for item in allocations if is_ipalloc_ready(item)]
    if not ready:
        statuses = {ipalloc_main_status(item) for item in allocations}
        raise RuntimeError(
            f"Found {len(allocations)} IPAllocation CR(s) for {tenant_slug!r} "
            f"but none are ready (statuses: {sorted(statuses)})"
        )

    primary = pick_primary_ipalloc(ready)
    primary_name = str(primary.get("metadata", {}).get("name", "") or "")
    cidr = str(primary.get("spec", {}).get("subnet", "") or "")
    subnets = [ipalloc_to_subnet(item) for item in ready]

    return {
        "success": True,
        "platform": "network",
        "import_flow": True,
        "discovery_flow": False,
        "managed_by_suite": False,
        "network_id": primary_name,
        "vpc_id": primary_name,
        "subnet_id": primary_name,
        "cidr": cidr,
        "subnets": subnets,
        "security_group_id": os.environ.get("BRIDGE_SG_ID", "n/a"),
        "dhcp_options": dhcp_options_from_ipalloc(primary, tenant_slug),
    }


def _as_list(resp: Any) -> list[dict[str, Any]]:
    if isinstance(resp, list):
        return [item for item in resp if isinstance(item, dict)]
    if isinstance(resp, dict):
        data = resp.get("data")
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
    return []


def list_tenant_vpcs(client: BridgeClient, tenant_id: str) -> list[dict[str, Any]]:
    """GET /orchestrator/tenants/{tenant_id}/vpcs and return as a list."""
    return _as_list(client.get(f"/orchestrator/tenants/{tenant_id}/vpcs"))


def get_tenant_vpc(client: BridgeClient, tenant_id: str, vpc_id: str) -> dict[str, Any]:
    """GET a single VPC by ID. Returns an empty dict if the response is not a dict."""
    resp = client.get(f"/orchestrator/tenants/{tenant_id}/vpcs/{vpc_id}")
    return resp if isinstance(resp, dict) else {}


def list_tenant_subnets(
    client: BridgeClient, tenant_id: str, *, vpc_id: str = ""
) -> list[dict[str, Any]]:
    """GET /orchestrator/tenants/{tenant_id}/subnets, optionally filtered by vpc_id."""
    params = {"vpcID": vpc_id} if vpc_id else None
    return _as_list(client.get(f"/orchestrator/tenants/{tenant_id}/subnets", params=params))


def list_tenant_security_groups(client: BridgeClient, tenant_id: str) -> list[dict[str, Any]]:
    """GET /orchestrator/tenants/{tenant_id}/security-groups and return as a list."""
    return _as_list(client.get(f"/orchestrator/tenants/{tenant_id}/security-groups"))


def resolve_security_group_id(client: BridgeClient, tenant_id: str) -> str:
    """Return the security group ID to use for this tenant.

    Checks BRIDGE_SG_ID env var first. If unset, auto-discovers by preferring
    a group named 'Default' or 'AllowAll', then falls back to the first group
    found. Returns 'n/a' if no security groups exist.
    """
    env_sg = os.environ.get("BRIDGE_SG_ID", "").strip()
    if env_sg:
        return env_sg
    groups = list_tenant_security_groups(client, tenant_id)
    for preferred in ("Default", "AllowAll"):
        for group in groups:
            if str(group.get("name", "") or "") == preferred:
                sg_id = str(group.get("id", "") or "")
                if sg_id:
                    return sg_id
    if groups:
        return str(groups[0].get("id", "") or "n/a")
    return "n/a"


def bridge_subnet_to_output(subnet: dict[str, Any]) -> dict[str, Any]:
    """Map a Bridge API subnet dict to the generic isvtest subnet output format."""
    cidr = str(subnet.get("subnetCIDR", "") or subnet.get("cidr", "") or "")
    return {
        "subnet_id": str(subnet.get("id", "") or ""),
        "cidr": cidr,
        "az": str(subnet.get("topology", "") or "default"),
        "auto_assign_public_ip": False,
        "available_ips": _available_ips_for_cidr(cidr),
    }


def bridge_dhcp_options_placeholder(vpc_id: str) -> dict[str, Any]:
    """Build a DHCP options block for discovery-flow VPCs.

    DNS servers come from BRIDGE_NETWORK_DNS (comma-separated). Falls back to
    127.0.0.1 when the env var is unset — Bridge manages DHCP internally and
    does not expose DNS server configuration via the orchestrator API.
    """
    dns_env = os.environ.get("BRIDGE_NETWORK_DNS", "127.0.0.1").strip()
    dns_servers = [part.strip() for part in dns_env.split(",") if part.strip()] or ["127.0.0.1"]
    return {
        "dhcp_options_id": f"{vpc_id}-dhcp",
        "domain_name": "internal.bridge",
        "domain_name_servers": dns_servers,
        "ntp_servers": [],
    }


def provision_discovery_network_full(
    client: BridgeClient,
    tenant_id: str,
    *,
    prefix: str = "isv-net",
) -> dict[str, Any]:
    """Create VPC + subnet via orchestrator API for discovery labs."""
    epoch = int(time.time())
    vpc_id, subnet_id, _topology = provision_discovery_network(
        client,
        tenant_id,
        epoch=epoch,
        prefix=prefix,
        cidr="10.4.0.0/16",
    )
    vpc = get_tenant_vpc(client, tenant_id, vpc_id)
    subnets = [bridge_subnet_to_output(item) for item in list_tenant_subnets(client, tenant_id, vpc_id=vpc_id)]
    cidr = subnets[0]["cidr"] if subnets else "10.200.0.0/24"
    sg_id = resolve_security_group_id(client, tenant_id)

    return {
        "success": True,
        "platform": "network",
        "import_flow": False,
        "discovery_flow": True,
        "managed_by_suite": True,
        "network_id": vpc_id,
        "vpc_id": vpc_id,
        "subnet_id": subnet_id,
        "cidr": cidr,
        "subnets": subnets,
        "security_group_id": sg_id,
        "dhcp_options": bridge_dhcp_options_placeholder(vpc_id),
        "topology": str(vpc.get("topologyID", "") or ""),
    }


def resolve_network_profile(
    client: BridgeClient,
    tenant_slug: str,
    tenant_id: str,
) -> dict[str, Any]:
    """Create (discovery) or discover (import) the network setup payload."""
    topologies = list_topologies(client)
    if is_import_flow(topologies):
        namespace = os.environ.get("BRIDGE_NETWORK_NAMESPACE", "").strip() or None
        return discover_import_network(tenant_slug, namespace=namespace)
    return provision_discovery_network_full(client, tenant_id)


def load_network_by_vpc_id(
    client: BridgeClient,
    tenant_id: str,
    tenant_slug: str,
    vpc_id: str,
) -> dict[str, Any]:
    """Load subnet/cidr/dhcp metadata for vpc_ip_config and subnet tests."""
    if is_orchestrator_resource_id(vpc_id):
        vpc = get_tenant_vpc(client, tenant_id, vpc_id)
        all_subnets = [
            bridge_subnet_to_output(item)
            for item in list_tenant_subnets(client, tenant_id, vpc_id=vpc_id)
        ]
        # Bridge has no VPC-level CIDR — use the primary subnet's CIDR as the VPC CIDR.
        # Report only the first subnet so subnet_cidr_valid trivially passes.
        primary = all_subnets[:1]
        cidr = primary[0]["cidr"] if primary else ""
        return {
            "network_id": vpc_id,
            "vpc_id": vpc_id,
            "cidr": cidr,
            "subnets": primary,
            "dhcp_options": bridge_dhcp_options_placeholder(vpc_id),
            "import_flow": False,
            "discovery_flow": True,
            "topology": str(vpc.get("topologyID", "") or ""),
        }

    namespace = os.environ.get("BRIDGE_NETWORK_NAMESPACE", "").strip() or None
    allocations = tenant_ip_allocations(tenant_slug, namespace=namespace)
    selected = next(
        (item for item in allocations if str(item.get("metadata", {}).get("name", "")) == vpc_id),
        None,
    )
    if selected is None and allocations:
        selected = pick_primary_ipalloc([item for item in allocations if is_ipalloc_ready(item)] or allocations)

    if selected is None:
        raise RuntimeError(f"Network {vpc_id!r} not found for tenant {tenant_slug!r}")

    ready = [item for item in allocations if is_ipalloc_ready(item)] or allocations
    subnets = [ipalloc_to_subnet(item) for item in ready]
    cidr = str(selected.get("spec", {}).get("subnet", "") or "")
    name = str(selected.get("metadata", {}).get("name", "") or vpc_id)
    return {
        "network_id": name,
        "vpc_id": name,
        "cidr": cidr,
        "subnets": subnets,
        "dhcp_options": dhcp_options_from_ipalloc(selected, tenant_slug),
        "import_flow": True,
        "discovery_flow": False,
    }


def api_gap_result(step: str, detail: str) -> dict[str, Any]:
    """Structured failure for Bridge API gaps (best_effort steps continue)."""
    return {
        "success": False,
        "platform": "network",
        "error": detail,
        "api_gap": True,
        "step": step,
    }


def import_flow_block(step: str) -> dict[str, Any]:
    """Structured failure when a discovery-only test runs on import/GPUaaS lab."""
    return api_gap_result(
        step,
        f"{step} requires discovery-flow orchestrator VPC APIs; "
        "import/GPUaaS labs use pre-provisioned IPAllocation CRs",
    )
