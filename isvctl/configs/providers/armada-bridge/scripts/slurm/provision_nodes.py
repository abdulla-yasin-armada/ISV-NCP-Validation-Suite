"""Provision Slurm master/worker nodes for import vs discovery flows."""
from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPT_DIR.parent))
sys.path.insert(0, str(_SCRIPT_DIR))

from common.bridge_client import BridgeClient
from common.network import is_discovery_flow, is_import_flow, list_topologies
from k8s.provision_nodes import (
    parse_node_ids as _parse_k8s_node_ids,
    provision_bm_node,
    provision_vm_node,
    resolve_node_type as _resolve_k8s_node_type,
)


@dataclass(frozen=True)
class SlurmNodes:
    discovery_flow: bool
    import_flow: bool
    node_type: str
    master_node_id: str
    worker_node_ids: list[str]
    node_ids: list[str]
    vpc_id: str
    subnet_id: str
    provisioned: bool
    key_file: str = ""
    ssh_user: str = ""


def _split_env_ids(name: str) -> list[str]:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


def parse_slurm_node_ids(
    *,
    cli_node_ids: list[str] | None = None,
    cli_master_id: str = "",
    cli_worker_ids: list[str] | None = None,
) -> tuple[str, list[str], list[str]]:
    """Return (master_id, worker_ids, all_ids) from env/CLI."""
    master = (
        cli_master_id.strip()
        or os.environ.get("BRIDGE_SLURM_MASTER_NODE_ID", "").strip()
    )
    workers: list[str] = []
    workers.extend(_split_env_ids("BRIDGE_SLURM_WORKER_NODE_IDS"))
    workers.extend(part.strip() for part in (cli_worker_ids or []) if part.strip())

    all_from_env = _parse_k8s_node_ids(cli_node_ids=cli_node_ids or [])
    all_from_slurm_env = _split_env_ids("BRIDGE_SLURM_NODE_IDS")
    combined: list[str] = []
    seen: set[str] = set()
    for node_id in all_from_slurm_env + all_from_env:
        if node_id not in seen:
            seen.add(node_id)
            combined.append(node_id)

    if not master and combined:
        master = combined[0]
        workers = [node_id for node_id in combined[1:] if node_id != master]
    elif master and combined:
        for node_id in combined:
            if node_id != master and node_id not in workers:
                workers.append(node_id)

    all_ids = [master, *workers] if master else workers
    return master, workers, all_ids


def resolve_node_type(*, discovery_flow: bool) -> str:
    """Return bareMetal or vm. BRIDGE_SLURM_NODE_TYPE overrides auto-detection."""
    explicit = os.environ.get("BRIDGE_SLURM_NODE_TYPE", "").strip()
    if explicit:
        return explicit
    return _resolve_k8s_node_type(discovery_flow=discovery_flow)


def build_slurm_nodes(
    master_id: str,
    worker_ids: list[str],
    *,
    is_allocated: bool = True,
) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = [
        {"id": master_id, "role": "master", "isAllocated": is_allocated},
    ]
    for worker_id in worker_ids:
        nodes.append({"id": worker_id, "role": "worker", "isAllocated": is_allocated})
    return nodes


def provision_slurm_nodes(
    client: BridgeClient,
    tenant_id: str,
    *,
    cli_node_ids: list[str] | None = None,
    cli_master_id: str = "",
    cli_worker_ids: list[str] | None = None,
) -> SlurmNodes:
    """Provision or reuse master/worker nodes for Slurm cluster creation."""
    topologies = list_topologies(client)
    discovery = is_discovery_flow(topologies)
    import_flow = is_import_flow(topologies)
    node_type = resolve_node_type(discovery_flow=discovery)

    master_id, worker_ids, all_ids = parse_slurm_node_ids(
        cli_node_ids=cli_node_ids,
        cli_master_id=cli_master_id,
        cli_worker_ids=cli_worker_ids,
    )

    if master_id:
        from common.vm import resolve_ssh_key

        _, key_file = resolve_ssh_key(f"isv-slurm-{master_id[:8]}")
        ssh_user = (
            os.environ.get("BRIDGE_SLURM_SSH_USER")
            or os.environ.get("BRIDGE_SSH_USER")
            or "ubuntu"
        )
        return SlurmNodes(
            discovery_flow=discovery,
            import_flow=import_flow,
            node_type=node_type,
            master_node_id=master_id,
            worker_node_ids=worker_ids,
            node_ids=all_ids,
            vpc_id="n/a",
            subnet_id="",
            provisioned=False,
            key_file=key_file,
            ssh_user=ssh_user,
        )

    if os.environ.get("BRIDGE_SLURM_SKIP_NODE_PROVISION", "").strip().lower() in {"1", "true", "yes"}:
        raise RuntimeError(
            "No Slurm node ids supplied and BRIDGE_SLURM_SKIP_NODE_PROVISION is set. "
            "Provide --node-id / BRIDGE_SLURM_NODE_IDS or unset the skip flag."
        )

    epoch = int(time.time())
    vm_flavor = os.environ.get("BRIDGE_VM_FLAVOR", "gpu.1x")
    worker_count = int(os.environ.get("BRIDGE_SLURM_WORKER_COUNT", "0"))

    if node_type == "bareMetal":
        master_id, vpc_id, subnet_id = provision_bm_node(
            client,
            tenant_id,
            epoch=epoch,
            discovery_flow=discovery,
        )
        worker_ids = []
        for index in range(worker_count):
            worker_id, _, _ = provision_bm_node(
                client,
                tenant_id,
                epoch=epoch + index + 1,
                discovery_flow=discovery,
            )
            worker_ids.append(worker_id)
    else:
        master_id, vpc_id, subnet_id = provision_vm_node(
            client,
            tenant_id,
            epoch=epoch,
            discovery_flow=discovery,
            vm_flavor=vm_flavor,
        )
        worker_ids = []
        for index in range(worker_count):
            worker_id, _, _ = provision_vm_node(
                client,
                tenant_id,
                epoch=epoch + index + 1,
                discovery_flow=discovery,
                vm_flavor=vm_flavor,
            )
            worker_ids.append(worker_id)

    from common.vm import resolve_ssh_key

    _, key_file = resolve_ssh_key(f"isv-slurm-{epoch}")
    ssh_user = (
        os.environ.get("BRIDGE_SLURM_SSH_USER")
        or os.environ.get("BRIDGE_SSH_USER")
        or "ubuntu"
    )
    all_ids = [master_id, *worker_ids]
    print(
        f"[slurm] provisioned {node_type} master {master_id} workers={worker_ids} "
        f"(import_flow={import_flow}, discovery_flow={discovery})",
        file=sys.stderr,
    )
    return SlurmNodes(
        discovery_flow=discovery,
        import_flow=import_flow,
        node_type=node_type,
        master_node_id=master_id,
        worker_node_ids=worker_ids,
        node_ids=all_ids,
        vpc_id=vpc_id,
        subnet_id=subnet_id,
        provisioned=True,
        key_file=key_file,
        ssh_user=ssh_user,
    )
