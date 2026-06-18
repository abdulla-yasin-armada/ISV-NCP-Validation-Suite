"""provision_nodes — K8s worker nodes for Armada Bridge setup.

Called from ``setup.py`` after ``resolve_tenant_id()``. Detects lab topology,
then either uses explicit node UUIDs or allocates new workers from catalog.

Flow (``provision_nodes()``):
  1. ``list_topologies()`` → ``GET /orchestrator/network/topologies``;
     ``is_import_flow()`` / ``is_discovery_flow()``.
  2. ``resolve_node_type()`` — default bareMetal (import) or VM (discovery);
     override with ``BRIDGE_K8S_NODE_TYPE=bareMetal|vm``.
  3. If ``--node-id`` / ``BRIDGE_K8S_NODE_IDS`` is set → use those IDs for cluster
     create (``provisioned=False``; no catalog allocate).
  4. Else pick flavor from ``GET /orchestrator/catalog`` and allocate
     ``BRIDGE_K8S_NODE_COUNT`` (default 1) **new** nodes (nodes visible before
     allocate are ignored when polling):
     - **bareMetal** — ``POST .../metal/allocate`` with count N, poll until N
       new computes are ready; discovery provisions VPC + subnet once.
     - **vm** — ``allocate_vm()`` (``BRIDGE_VM_FLAVOR``, default ``gpu.1x``)
       called N times with unique names; discovery provisions network once.

Returns ``ProvisionedNodes`` (``node_ids``, ``node_type``, flow flags, ``vpc_id``,
``subnet_id``, ``provisioned``). Log lines go to stderr; stdout stays clean for
``setup.py`` JSON.

Env: ``BRIDGE_K8S_NODE_IDS``, ``BRIDGE_K8S_NODE_COUNT``,
``BRIDGE_K8S_SKIP_NODE_PROVISION``, ``BRIDGE_BM_FLAVOR``,
``BRIDGE_BM_GPU_TYPE``, ``BRIDGE_VM_FLAVOR``.
"""
from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.bridge_client import BridgeClient
from common.catalog import discover_bm_product_type_id
from common.metal import (
    allocate_bm,
    compute_node_id,
    list_computes,
    poll_until_bm_ready,
)
from common.network import (
    is_discovery_flow,
    is_import_flow,
    list_topologies,
    provision_discovery_network,
)
from common.vm import (
    allocate_vm,
    extract_vm_id,
    find_vm_by_name,
    list_vms,
    resolve_ssh_key,
    wait_for_vm_status,
)

_BM_POLL_TIMEOUT = 540
_BM_POLL_INTERVAL = 15
_VM_POLL_TIMEOUT = 840


@dataclass(frozen=True)
class ProvisionedNodes:
    """Result of node provisioning or reuse.

    Attributes:
        discovery_flow: True when lab topology is ethernet/discovery (VPC + subnet provisioned).
        import_flow: True when lab topology is import (no-network / bare BM).
        node_type: Bridge cluster nodeType — "bareMetal" or "vm".
        node_ids: UUIDs of the worker nodes to pass to cluster create.
        vpc_id: VPC UUID provisioned for discovery flow; "n/a" for import flow.
        subnet_id: Subnet UUID provisioned for discovery flow; "" for import flow.
        provisioned: True when nodes were newly allocated; False when reusing existing IDs.
        vm_name: Base name prefix used for VM nodes (e.g. "isv-k8s-node-{epoch}").
                 Empty string for bareMetal nodes.
    """

    discovery_flow: bool
    import_flow: bool
    node_type: str
    node_ids: list[str]
    vpc_id: str
    subnet_id: str
    provisioned: bool
    vm_name: str = ""


def resolve_node_type(*, discovery_flow: bool) -> str:
    """Return Bridge cluster nodeType: bareMetal or vm."""
    explicit = os.environ.get("BRIDGE_K8S_NODE_TYPE", "").strip().lower()
    if explicit in {"baremetal", "bare_metal", "bm", "metal"}:
        return "bareMetal"
    if explicit in {"vm", "virtualmachine"}:
        return "vm"
    return "vm" if discovery_flow else "bareMetal"


def parse_node_ids(*, cli_node_ids: list[str]) -> list[str]:
    """Merge BRIDGE_K8S_NODE_IDS env var and CLI --node-id args, deduplicating while preserving order."""
    env_ids = os.environ.get("BRIDGE_K8S_NODE_IDS", "").strip()
    ids: list[str] = []
    if env_ids:
        ids.extend(part.strip() for part in env_ids.split(",") if part.strip())
    ids.extend(part.strip() for part in cli_node_ids if part.strip())
    # Preserve order, drop duplicates.
    seen: set[str] = set()
    ordered: list[str] = []
    for node_id in ids:
        if node_id not in seen:
            seen.add(node_id)
            ordered.append(node_id)
    return ordered


def provision_bm_nodes(
    client: BridgeClient,
    tenant_id: str,
    *,
    epoch: int,
    discovery_flow: bool,
    count: int = 1,
) -> tuple[list[str], str, str]:
    """Allocate ``count`` BM nodes. Returns (node_ids, vpc_id, subnet_id).

    Network (VPC + subnet) is provisioned once and shared across all nodes.
    """
    vpc_id = "n/a"
    subnet_id = ""
    subnet_ids: list[str] = []

    if discovery_flow:
        vpc_id, subnet_id, _topology = provision_discovery_network(
            client,
            tenant_id,
            epoch=epoch,
            prefix="isv-k8s-bm",
        )
        subnet_ids = [subnet_id]

    existing_ids = {compute_node_id(node) for node in list_computes(client, tenant_id)}
    existing_ids.discard("")

    explicit_flavor = os.environ.get("BRIDGE_BM_FLAVOR", "")
    product_type_id, flavor_name, auto_discovered = discover_bm_product_type_id(
        client,
        explicit_id=explicit_flavor,
        gpu_type_filter=os.environ.get("BRIDGE_BM_GPU_TYPE", ""),
    )
    print(
        f"[k8s] allocating {count} bareMetal node(s) from catalog: "
        f"productTypeId={product_type_id} ({flavor_name}, auto_discovered={auto_discovered})",
        file=sys.stderr,
    )

    # Snapshot existing computes before allocate so polling only waits for newly allocated ones.
    allocate_bm(client, tenant_id, product_type_id, count=count, subnet_ids=subnet_ids or None)

    nodes = poll_until_bm_ready(
        client,
        tenant_id,
        product_type_id,
        existing_ids,
        count=count,
        label="k8s_provision_bm",
        interval=_BM_POLL_INTERVAL,
        timeout=_BM_POLL_TIMEOUT,
    )
    node_ids = [compute_node_id(n) for n in nodes]
    missing = [nid for nid in node_ids if not nid]
    if missing:
        raise RuntimeError(f"BM allocate completed but {len(missing)} node id(s) missing")
    return node_ids, vpc_id, subnet_id


def provision_vm_nodes(
    client: BridgeClient,
    tenant_id: str,
    *,
    epoch: int,
    discovery_flow: bool,
    vm_flavor: str,
    count: int = 1,
) -> tuple[list[str], str, str]:
    """Allocate ``count`` VM nodes. Returns (node_ids, vpc_id, subnet_id).

    Network (VPC + subnet) is provisioned once and shared across all nodes.
    VM names are ``isv-k8s-node-{epoch}`` (count=1) or
    ``isv-k8s-node-{epoch}-{i}`` (count>1).
    """
    vpc_id = "n/a"
    subnet_id = ""
    subnet_ids: list[str] = []

    if discovery_flow:
        vpc_id, subnet_id, _topology = provision_discovery_network(
            client,
            tenant_id,
            epoch=epoch,
            prefix="isv-k8s-vm",
        )
        subnet_ids = [subnet_id]

    node_ids: list[str] = []
    for i in range(count):
        vm_name = f"isv-k8s-node-{epoch}" if count == 1 else f"isv-k8s-node-{epoch}-{i}"
        public_key, _key_file = resolve_ssh_key(vm_name)

        try:
            allocate_resp = allocate_vm(
                client,
                tenant_id,
                name=vm_name,
                flavor=vm_flavor,
                public_key=public_key,
                subnet_ids=subnet_ids or None,
            )
            vm_id = extract_vm_id(allocate_resp)
        except ValueError as exc:
            if "status 409" not in str(exc):
                raise
            existing = find_vm_by_name(list_vms(client, tenant_id), vm_name)
            if existing is None:
                raise
            vm_id = extract_vm_id(existing)

        wait_for_vm_status(
            client,
            tenant_id,
            vm_id,
            target="running",
            label=f"k8s_provision_vm_{i}",
            timeout=_VM_POLL_TIMEOUT,
        )
        node_ids.append(vm_id)

    return node_ids, vpc_id, subnet_id


def provision_nodes(
    client: BridgeClient,
    tenant_id: str,
    *,
    cli_node_ids: list[str] | None = None,
) -> ProvisionedNodes:
    """Provision or reuse worker node(s) based on topology import/discovery flow.

    If ``cli_node_ids`` or ``BRIDGE_K8S_NODE_IDS`` are supplied, those nodes are
    reused directly (``provisioned=False``) and no catalog allocation is performed.
    Otherwise allocates ``BRIDGE_K8S_NODE_COUNT`` (default 1) new nodes of the
    resolved type. Raises ``RuntimeError`` if ``BRIDGE_K8S_SKIP_NODE_PROVISION``
    is set and no node IDs are provided.

    Env read here: ``BRIDGE_K8S_SKIP_NODE_PROVISION``, ``BRIDGE_K8S_NODE_COUNT``,
    ``BRIDGE_VM_FLAVOR`` (default ``gpu.1x``).
    """
    topologies = list_topologies(client)
    discovery = is_discovery_flow(topologies)
    import_flow = is_import_flow(topologies)
    node_type = resolve_node_type(discovery_flow=discovery)

    existing_ids = parse_node_ids(cli_node_ids=cli_node_ids or [])
    if existing_ids:
        print(
            f"[k8s] using existing node(s) from BRIDGE_K8S_NODE_IDS/--node-id: "
            f"{existing_ids} (skipping catalog allocate)",
            file=sys.stderr,
        )
        return ProvisionedNodes(
            discovery_flow=discovery,
            import_flow=import_flow,
            node_type=node_type,
            node_ids=existing_ids,
            vpc_id="n/a",
            subnet_id="",
            provisioned=False,
        )

    if os.environ.get("BRIDGE_K8S_SKIP_NODE_PROVISION", "").strip().lower() in {"1", "true", "yes"}:
        raise RuntimeError(
            "No node ids supplied and BRIDGE_K8S_SKIP_NODE_PROVISION is set. "
            "Provide --node-id / BRIDGE_K8S_NODE_IDS or unset the skip flag."
        )

    node_count = int(os.environ.get("BRIDGE_K8S_NODE_COUNT", "1"))
    if node_count < 1:
        raise RuntimeError(f"BRIDGE_K8S_NODE_COUNT must be >= 1, got {node_count}")

    epoch = int(time.time())
    vm_flavor = os.environ.get("BRIDGE_VM_FLAVOR", "gpu.1x")

    if node_type == "bareMetal":
        node_ids, vpc_id, subnet_id = provision_bm_nodes(
            client,
            tenant_id,
            epoch=epoch,
            discovery_flow=discovery,
            count=node_count,
        )
    else:
        node_ids, vpc_id, subnet_id = provision_vm_nodes(
            client,
            tenant_id,
            epoch=epoch,
            discovery_flow=discovery,
            vm_flavor=vm_flavor,
            count=node_count,
        )

    print(
        f"[k8s] provisioned {node_count} {node_type} node(s): {node_ids} "
        f"(import_flow={import_flow}, discovery_flow={discovery}, vpc_id={vpc_id})",
        file=sys.stderr,
    )
    return ProvisionedNodes(
        discovery_flow=discovery,
        import_flow=import_flow,
        node_type=node_type,
        node_ids=node_ids,
        vpc_id=vpc_id,
        subnet_id=subnet_id,
        provisioned=True,
        vm_name=f"isv-k8s-node-{epoch}" if node_type == "vm" else "",
    )
