"""provision_nodes — K8s worker nodes for Armada Bridge setup.

Called from ``setup.py`` after ``resolve_tenant_id()``. Detects lab topology,
then either uses explicit node UUIDs or allocates a new worker from catalog.

Flow (``provision_nodes()``):
  1. ``list_topologies()`` → ``GET /orchestrator/network/topologies``;
     ``is_import_flow()`` / ``is_discovery_flow()``.
  2. ``resolve_node_type()`` — default bareMetal (import) or VM (discovery);
     override with ``BRIDGE_K8S_NODE_TYPE=bareMetal|vm``.
  3. If ``--node-id`` / ``BRIDGE_K8S_NODE_IDS`` is set → use those IDs for cluster
     create (``provisioned=False``; no catalog allocate).
  4. Else pick flavor from ``GET /orchestrator/catalog`` and allocate one **new**
     node (nodes visible before allocate are ignored when polling):
     - **bareMetal** — ``POST .../metal/allocate``, poll until a new compute is ready;
       discovery may call ``provision_discovery_network()`` first (VPC + subnet).
     - **vm** — ``allocate_vm()`` (``BRIDGE_VM_FLAVOR``, default ``gpu.1x``),
       poll until running; discovery may provision network first.

Returns ``ProvisionedNodes`` (``node_ids``, ``node_type``, flow flags, ``vpc_id``,
``subnet_id``, ``provisioned``). Log lines go to stderr; stdout stays clean for
``setup.py`` JSON.

Env: ``BRIDGE_K8S_NODE_IDS``, ``BRIDGE_K8S_SKIP_NODE_PROVISION``, ``BRIDGE_BM_FLAVOR``,
``BRIDGE_BM_GPU_TYPE``, ``BRIDGE_VM_FLAVOR``.
"""
from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.bridge_client import BridgeClient
from common.catalog import discover_bm_product_type_id
from common.network import (
    is_discovery_flow,
    is_import_flow,
    list_topologies,
    provision_discovery_network,
)
from common.polling import poll_until
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
_BM_DONE_STATES = {"done", "success"}
_VM_POLL_TIMEOUT = 840


@dataclass(frozen=True)
class ProvisionedNodes:
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


def _list_computes(client: BridgeClient, tenant_id: str) -> list[dict[str, Any]]:
    resp = client.get(f"/orchestrator/tenants/{tenant_id}/metal/computes")
    nodes = resp if isinstance(resp, list) else (resp or {}).get("data", [])
    return [node for node in nodes if isinstance(node, dict)]


def _compute_node_id(node: dict[str, Any]) -> str:
    return str(node.get("id", "") or node.get("ID", "") or "")


def _poll_until_new_bm_ready(
    client: BridgeClient,
    tenant_id: str,
    product_type_id: str,
    existing_ids: set[str],
) -> dict[str, Any]:
    """Poll until one newly allocated BM node (not in *existing_ids*) is ready."""

    def check() -> tuple[bool, Any, str]:
        ready = [
            node
            for node in _list_computes(client, tenant_id)
            if str(node.get("productTypeId", "") or "") == product_type_id
            and str(node.get("allocateStatus", "") or "").lower() in _BM_DONE_STATES
            and _compute_node_id(node) not in existing_ids
        ]
        if ready:
            return True, ready[0], f"new node ready (id={_compute_node_id(ready[0])!r})"

        pending = [
            node
            for node in _list_computes(client, tenant_id)
            if str(node.get("productTypeId", "") or "") == product_type_id
            and _compute_node_id(node) not in existing_ids
        ]
        statuses = {str(node.get("allocateStatus", "")) for node in pending}
        return False, None, (
            f"0/1 new nodes ready for productTypeId={product_type_id!r} "
            f"(statuses: {statuses or 'none visible'})"
        )

    return poll_until(
        check,
        label="k8s_provision_bm",
        interval=_BM_POLL_INTERVAL,
        timeout=_BM_POLL_TIMEOUT,
    )


def provision_bm_node(
    client: BridgeClient,
    tenant_id: str,
    *,
    epoch: int,
    discovery_flow: bool,
) -> tuple[str, str, str]:
    """Allocate one BM node. Returns (node_id, vpc_id, subnet_id)."""
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

    existing_ids = {_compute_node_id(node) for node in _list_computes(client, tenant_id)}
    existing_ids.discard("")

    explicit_flavor = os.environ.get("BRIDGE_BM_FLAVOR", "")
    product_type_id, flavor_name, auto_discovered = discover_bm_product_type_id(
        client,
        explicit_id=explicit_flavor,
        gpu_type_filter=os.environ.get("BRIDGE_BM_GPU_TYPE", ""),
    )
    print(
        f"[k8s] allocating bareMetal from catalog: productTypeId={product_type_id} "
        f"({flavor_name}, auto_discovered={auto_discovered})",
        file=sys.stderr,
    )

    allocate_body: dict[str, Any] = {
        "ProductTypeID": product_type_id,
        "computeNodeCount": 1,
    }
    if subnet_ids:
        allocate_body["subnetIds"] = subnet_ids

    try:
        client.post(
            f"/orchestrator/tenants/{tenant_id}/metal/allocate",
            allocate_body,
        )
    except ValueError as exc:
        if "status 409" not in str(exc):
            raise
        # Allocation may already be in progress; poll for a new node only.

    node = _poll_until_new_bm_ready(client, tenant_id, product_type_id, existing_ids)
    node_id = _compute_node_id(node)
    if not node_id:
        raise RuntimeError(f"BM allocate completed but node id missing: {node!r}")
    return node_id, vpc_id, subnet_id


def provision_vm_node(
    client: BridgeClient,
    tenant_id: str,
    *,
    epoch: int,
    discovery_flow: bool,
    vm_flavor: str,
) -> tuple[str, str, str]:
    """Allocate one VM node. Returns (node_id, vpc_id, subnet_id)."""
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

    vm_name = f"isv-k8s-node-{epoch}"
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
        label="k8s_provision_vm",
        timeout=_VM_POLL_TIMEOUT,
    )
    return vm_id, vpc_id, subnet_id


def provision_nodes(
    client: BridgeClient,
    tenant_id: str,
    *,
    cli_node_ids: list[str] | None = None,
) -> ProvisionedNodes:
    """Provision or reuse worker node(s) based on topology import/discovery flow."""
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
        explicit_type = os.environ.get("BRIDGE_K8S_NODE_TYPE", "").strip()
        if explicit_type:
            node_type = resolve_node_type(discovery_flow=discovery)
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

    epoch = int(time.time())
    vm_flavor = os.environ.get("BRIDGE_VM_FLAVOR", "gpu.1x")

    if node_type == "bareMetal":
        node_id, vpc_id, subnet_id = provision_bm_node(
            client,
            tenant_id,
            epoch=epoch,
            discovery_flow=discovery,
        )
    else:
        node_id, vpc_id, subnet_id = provision_vm_node(
            client,
            tenant_id,
            epoch=epoch,
            discovery_flow=discovery,
            vm_flavor=vm_flavor,
        )

    print(
        f"[k8s] provisioned {node_type} node {node_id} "
        f"(import_flow={import_flow}, discovery_flow={discovery}, vpc_id={vpc_id})",
        file=sys.stderr,
    )
    return ProvisionedNodes(
        discovery_flow=discovery,
        import_flow=import_flow,
        node_type=node_type,
        node_ids=[node_id],
        vpc_id=vpc_id,
        subnet_id=subnet_id,
        provisioned=True,
        vm_name=f"isv-k8s-node-{epoch}" if node_type == "vm" else "",
    )
