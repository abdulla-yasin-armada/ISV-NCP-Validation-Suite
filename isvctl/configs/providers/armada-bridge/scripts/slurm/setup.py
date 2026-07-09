#!/usr/bin/env python3
"""setup — Armada Bridge Slurm suite, setup phase.

Flow:
  1. Detect import vs discovery from GET /orchestrator/network/topologies
  2. Provision master/worker node(s) or reuse BRIDGE_SLURM_* node UUIDs (BM or VM)
  3. POST /orchestrator/tenants/{tenant}/slurm
  4. Poll cluster running, configure Slurm CLI (local or SSH), run sinfo inventory

Output: ISV slurm setup JSON (cluster_id, slurm_host, slurm{partitions,...}).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPT_DIR.parent))
sys.path.insert(0, str(_SCRIPT_DIR))

from common.bridge_client import BridgeClient  # noqa: E402
from common.context import print_run_context  # noqa: E402
from common.errors import handle_bridge_errors  # noqa: E402
from common.metal import provision_bm_node  # noqa: E402
from common.network import is_discovery_flow, is_import_flow, list_topologies  # noqa: E402
from common.slurm_cli import (  # noqa: E402
    configure_slurm_cli,
    remap_partitions,
    resolve_node_host,
    run_inventory_script,
)
from common.slurm_cluster import (  # noqa: E402
    create_slurm_cluster,
    extract_slurm_id,
    wait_slurm_running,
)
from common.slurm_state import save_state  # noqa: E402
from common.tenant import resolve_tenant_id  # noqa: E402
from common.vm import provision_vm_nodes, resolve_ssh_key  # noqa: E402

DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"

_BM_POLL_TIMEOUT = 540
_BM_POLL_INTERVAL = 15
_VM_POLL_TIMEOUT = 840


@dataclass(frozen=True)
class SlurmNodes:
    """Result of Slurm master/worker node provisioning or reuse."""

    discovery_flow: bool
    import_flow: bool
    node_type: str
    master_node_id: str
    worker_node_ids: list[str]
    node_ids: list[str]
    vpc_id: str
    subnet_id: str
    provisioned: bool
    converged_vpc_id: str = "n/a"
    converged_subnet_id: str = ""
    key_file: str = ""
    ssh_user: str = ""


def _split_env_ids(name: str) -> list[str]:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


def _parse_slurm_node_ids(
    *,
    cli_node_ids: list[str] | None = None,
    cli_master_id: str = "",
    cli_worker_ids: list[str] | None = None,
) -> tuple[str, list[str], list[str]]:
    """Return (master_id, worker_ids, all_ids) from env/CLI."""
    master = cli_master_id.strip() or os.environ.get("BRIDGE_SLURM_MASTER_NODE_ID", "").strip()
    workers: list[str] = []
    workers.extend(_split_env_ids("BRIDGE_SLURM_WORKER_NODE_IDS"))
    workers.extend(part.strip() for part in (cli_worker_ids or []) if part.strip())

    # Merge generic BRIDGE_K8S_NODE_IDS / BRIDGE_SLURM_NODE_IDS
    env_generic = os.environ.get("BRIDGE_K8S_NODE_IDS", "").strip()
    all_from_env = [p.strip() for p in env_generic.split(",") if p.strip()] if env_generic else []
    all_from_env += [p.strip() for p in (cli_node_ids or []) if p.strip()]
    all_from_slurm = _split_env_ids("BRIDGE_SLURM_NODE_IDS")

    combined: list[str] = []
    seen: set[str] = set()
    for node_id in all_from_slurm + all_from_env:
        if node_id not in seen:
            seen.add(node_id)
            combined.append(node_id)

    if not master and combined:
        master = combined[0]
        workers = [nid for nid in combined[1:] if nid != master]
    elif master and combined:
        for nid in combined:
            if nid != master and nid not in workers:
                workers.append(nid)

    all_ids = [master, *workers] if master else workers
    return master, workers, all_ids


def _resolve_slurm_node_type(*, discovery_flow: bool) -> str:
    """Return bareMetal or vm. BRIDGE_SLURM_NODE_TYPE overrides auto-detection."""
    explicit = os.environ.get("BRIDGE_SLURM_NODE_TYPE", "").strip()
    if explicit:
        return explicit
    explicit_k8s = os.environ.get("BRIDGE_K8S_NODE_TYPE", "").strip().lower()
    if explicit_k8s in {"baremetal", "bare_metal", "bm", "metal"}:
        return "bareMetal"
    if explicit_k8s in {"vm", "virtualmachine"}:
        return "vm"
    return "vm" if discovery_flow else "bareMetal"


def build_slurm_nodes(
    master_id: str,
    worker_ids: list[str],
    *,
    is_allocated: bool = True,
) -> list[dict[str, Any]]:
    """Build the node list payload for the Bridge slurm cluster create API."""
    nodes: list[dict[str, Any]] = [
        {"id": master_id, "role": "master", "isAllocated": is_allocated},
    ]
    for worker_id in worker_ids:
        nodes.append({"id": worker_id, "role": "worker", "isAllocated": is_allocated})
    return nodes


def _provision_slurm_nodes(
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
    node_type = _resolve_slurm_node_type(discovery_flow=discovery)

    master_id, worker_ids, all_ids = _parse_slurm_node_ids(
        cli_node_ids=cli_node_ids,
        cli_master_id=cli_master_id,
        cli_worker_ids=cli_worker_ids,
    )

    if master_id:
        _, key_file = resolve_ssh_key(f"isv-slurm-{master_id[:8]}")
        ssh_user = (
            os.environ.get("BRIDGE_SLURM_SSH_USER")
            or os.environ.get("BRIDGE_SSH_USER")
            or "ubuntu"
        )
        return SlurmNodes(
            discovery_flow=discovery, import_flow=import_flow,
            node_type=node_type, master_node_id=master_id,
            worker_node_ids=worker_ids, node_ids=all_ids,
            vpc_id="n/a", subnet_id="", provisioned=False,
            key_file=key_file, ssh_user=ssh_user,
        )

    if os.environ.get("BRIDGE_SLURM_SKIP_NODE_PROVISION", "").strip().lower() in {"1", "true", "yes"}:
        raise RuntimeError(
            "No Slurm node ids supplied and BRIDGE_SLURM_SKIP_NODE_PROVISION is set. "
            "Provide --node-id / BRIDGE_SLURM_NODE_IDS or unset the skip flag."
        )

    epoch = int(time.time())
    vm_flavor = os.environ.get("BRIDGE_VM_FLAVOR", "gpu.1x")
    worker_count = int(os.environ.get("BRIDGE_SLURM_WORKER_COUNT", "0"))
    total_count = 1 + worker_count
    converged_vpc_id = "n/a"
    converged_subnet_id = ""

    if node_type == "bareMetal":
        all_ids, vpc_id, subnet_id, converged_vpc_id, converged_subnet_id = provision_bm_node(
            client, tenant_id, epoch=epoch, discovery_flow=discovery,
            count=total_count, prefix="isv-slurm-bm",
            poll_interval=_BM_POLL_INTERVAL, poll_timeout=_BM_POLL_TIMEOUT,
            label="slurm_provision_bm",
        )
    else:
        # Use a single shared key for all Slurm VMs so the master node can SSH
        # to workers (Bridge's Ansible tests this during Slurm installation).
        slurm_shared_key_name = f"isv-slurm-node-{epoch}"
        all_ids, vpc_id, subnet_id = provision_vm_nodes(
            client, tenant_id, epoch=epoch, discovery_flow=discovery,
            vm_flavor=vm_flavor, count=total_count,
            name_prefix="isv-slurm-node", poll_timeout=_VM_POLL_TIMEOUT,
            shared_key_name=slurm_shared_key_name,
        )

    master_id = all_ids[0]
    worker_ids = all_ids[1:]
    # Resolve the key using the same name used during provisioning.
    # For BM, BRIDGE_SSH_KEY_FILE overrides this value in main() anyway.
    if node_type != "bareMetal":
        master_key_name = slurm_shared_key_name
    else:
        master_key_name = (
            f"isv-slurm-node-{epoch}" if total_count == 1 else f"isv-slurm-node-{epoch}-0"
        )
    _, key_file = resolve_ssh_key(master_key_name)
    ssh_user = (
        os.environ.get("BRIDGE_SLURM_SSH_USER")
        or os.environ.get("BRIDGE_SSH_USER")
        or "ubuntu"
    )
    print(
        f"[slurm] provisioned {node_type} master={master_id} workers={worker_ids} "
        f"(import_flow={import_flow}, discovery_flow={discovery})",
        file=sys.stderr,
    )
    return SlurmNodes(
        discovery_flow=discovery, import_flow=import_flow,
        node_type=node_type, master_node_id=master_id,
        worker_node_ids=worker_ids, node_ids=all_ids,
        vpc_id=vpc_id, subnet_id=subnet_id, provisioned=True,
        converged_vpc_id=converged_vpc_id, converged_subnet_id=converged_subnet_id,
        key_file=key_file, ssh_user=ssh_user,
    )


@handle_bridge_errors
def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tenant", required=True)
    parser.add_argument(
        "--node-id",
        action="append",
        default=[],
        help="Existing BM/VM UUID (repeatable). First id is master unless --master-node-id is set.",
    )
    parser.add_argument("--master-node-id", default="", help="Explicit Slurm master node UUID.")
    parser.add_argument(
        "--worker-node-id",
        action="append",
        default=[],
        help="Worker node UUID (repeatable).",
    )
    args = parser.parse_args()

    node_type = os.environ.get("BRIDGE_SLURM_NODE_TYPE") or os.environ.get("BRIDGE_K8S_NODE_TYPE", "bm")
    worker_count = os.environ.get("BRIDGE_SLURM_WORKER_COUNT", "0")
    vm_flavor = os.environ.get("BRIDGE_VM_FLAVOR", "")
    print_run_context("Slurm", {
        "NODE_TYPE"               : node_type,
        "BRIDGE_SLURM_WORKER_COUNT": worker_count,
        "BRIDGE_VM_FLAVOR"        : vm_flavor or "(not set — BM)",
    })

    if DEMO_MODE:
        print("[slurm] DEMO_MODE: skipping API calls", file=sys.stderr)
        result = {
            "success": True,
            "platform": "slurm",
            "cluster_name": "demo-bridge-slurm",
            "cluster_id": "demo-slurm-id",
            "slurm_host": "203.0.113.30",
            "slurm_cli_mode": "local",
            "slurm": {
                "partitions": {
                    "cpu": {"nodes": ["cpu-node-1"]},
                    "gpu": {"nodes": ["gpu-node-1", "gpu-node-2"]},
                },
                "cuda_arch": "90",
                "storage_path": "/tmp",
                "default_partition": "gpu",
                "driver_version": "560.35.03",
                "gpu_per_node": 4,
                "total_gpus": 8,
            },
        }
        print(json.dumps(result, indent=2))
        return 0

    client = BridgeClient.from_env()
    tenant_id = resolve_tenant_id(client, args.tenant)
    print(f"[slurm] setting up Slurm cluster for tenant {tenant_id}", file=sys.stderr)
    nodes_info = _provision_slurm_nodes(
        client,
        tenant_id,
        cli_node_ids=args.node_id,
        cli_master_id=args.master_node_id,
        cli_worker_ids=args.worker_node_id,
    )

    cluster_name = os.environ.get("BRIDGE_SLURM_CLUSTER_NAME", f"isv-slurm-{int(time.time())}")
    cluster_version = os.environ.get("BRIDGE_SLURM_VERSION", "24.05.6")
    cluster_description = os.environ.get("BRIDGE_SLURM_DESCRIPTION", "ISV Slurm validation cluster")

    # Wait for SSH on the master VM before creating the Slurm cluster.
    # Bridge's Ansible installs Slurm by SSH-ing into the nodes immediately after
    # cluster creation. If SSH isn't ready yet the cluster goes to 'failed' with
    # "Connection refused". We wait here (from our network) as a proxy for SSH
    # being generally available, then allow an extra settling delay.
    key_file = os.environ.get("BRIDGE_SSH_KEY_FILE", nodes_info.key_file)
    if not key_file:
        raise RuntimeError("SSH key file required for Slurm CLI access (set BRIDGE_SSH_KEY_FILE)")

    if nodes_info.node_type == "vm":
        from common.slurm_cli import resolve_node_host, resolve_ssh_user  # noqa: E402
        from common.ssh_utils import wait_for_ssh  # noqa: E402
        master_ssh_user = resolve_ssh_user(node_type=nodes_info.node_type)
        ssh_timeout = int(os.environ.get("BRIDGE_SLURM_SSH_TIMEOUT", "600"))

        # Wait for SSH on ALL nodes — Bridge's Ansible connects to each one.
        all_node_ids = [nodes_info.master_node_id] + nodes_info.worker_node_ids
        for node_id in all_node_ids:
            node_host = resolve_node_host(
                client, tenant_id, node_id, node_type=nodes_info.node_type
            )
            label = "master" if node_id == nodes_info.master_node_id else "worker"
            print(
                f"[slurm] waiting for SSH on {label} VM {node_host} ({node_id[:8]}...) before creating cluster...",
                file=sys.stderr,
            )
            wait_for_ssh(node_host, key_file, username=master_ssh_user, timeout=ssh_timeout)

        # Wait for cloud-init to complete on master so Bridge's Ansible finds all
        # prerequisites ready (package repos, user setup, etc.)
        master_host = resolve_node_host(
            client, tenant_id, nodes_info.master_node_id, node_type=nodes_info.node_type
        )
        print("[slurm] waiting for cloud-init to complete on master VM...", file=sys.stderr)
        _ci_result = subprocess.run(
            [
                "ssh",
                "-o", "StrictHostKeyChecking=no",
                "-o", "UserKnownHostsFile=/dev/null",
                "-o", "ConnectTimeout=10",
                "-o", "BatchMode=yes",
                "-i", key_file,
                f"{master_ssh_user}@{master_host}",
                "cloud-init status --wait 2>/dev/null || true",
            ],
            capture_output=True, text=True, timeout=300, check=False,
        )
        print(f"[slurm] cloud-init output: {_ci_result.stdout.strip() or _ci_result.stderr.strip()!r}", file=sys.stderr)

    created = create_slurm_cluster(
        client,
        tenant_id,
        name=cluster_name,
        nodes=build_slurm_nodes(nodes_info.master_node_id, nodes_info.worker_node_ids),
        version=cluster_version,
        description=cluster_description,
    )
    cluster_id = extract_slurm_id(created)
    cluster = wait_slurm_running(client, tenant_id, cluster_id)

    cli_mode, slurm_bin_path, slurm_conf_path = configure_slurm_cli(
        client=client,
        tenant_id=tenant_id,
        master_node_id=nodes_info.master_node_id,
        node_type=nodes_info.node_type,
        key_file=key_file,
    )

    env = os.environ.copy()
    if slurm_bin_path:
        env["PATH"] = f"{slurm_bin_path}:{env.get('PATH', '')}"
    if slurm_conf_path:
        env["SLURM_CONF"] = slurm_conf_path

    inventory = run_inventory_script(env=env)
    inventory = remap_partitions(inventory)

    slurm_host = resolve_node_host(
        client,
        tenant_id,
        nodes_info.master_node_id,
        node_type=nodes_info.node_type,
    )
    cluster_name = str(cluster.get("name") or created.get("name") or cluster_name)

    inventory.update(
        {
            "cluster_id": cluster_id,
            "cluster_name": cluster_name,
            "slurm_host": slurm_host,
            "slurm_cli_mode": cli_mode,
            "node_type": nodes_info.node_type,
            "master_node_id": nodes_info.master_node_id,
            "worker_node_ids": nodes_info.worker_node_ids,
            "node_ids": nodes_info.node_ids,
            "key_file": key_file,
            "ssh_user": nodes_info.ssh_user,
            "discovery_flow": nodes_info.discovery_flow,
            "import_flow": nodes_info.import_flow,
            "vpc_id": nodes_info.vpc_id,
            "subnet_id": nodes_info.subnet_id,
            "converged_vpc_id": nodes_info.converged_vpc_id,
            "converged_subnet_id": nodes_info.converged_subnet_id,
        }
    )
    if slurm_bin_path:
        inventory["slurm_bin_path"] = slurm_bin_path
    if slurm_conf_path:
        inventory["slurm_conf_path"] = slurm_conf_path

    env_exports: dict[str, str] = {}
    if slurm_bin_path:
        env_exports["PATH"] = f"{slurm_bin_path}:{os.environ.get('PATH', '')}"
    if slurm_conf_path:
        env_exports["SLURM_CONF"] = slurm_conf_path
    if env_exports:
        inventory["env_exports"] = env_exports

    save_state(
        {
            "tenant": args.tenant,
            "tenant_id": tenant_id,
            "cluster_id": cluster_id,
            "cluster_name": cluster_name,
            "slurm_host": slurm_host,
            "slurm_cli_mode": cli_mode,
            "slurm_bin_path": slurm_bin_path,
            "slurm_conf_path": slurm_conf_path,
            "discovery_flow": nodes_info.discovery_flow,
            "import_flow": nodes_info.import_flow,
            "node_type": nodes_info.node_type,
            "master_node_id": nodes_info.master_node_id,
            "worker_node_ids": nodes_info.worker_node_ids,
            "node_ids": nodes_info.node_ids,
            "vpc_id": nodes_info.vpc_id,
            "subnet_id": nodes_info.subnet_id,
            "converged_vpc_id": nodes_info.converged_vpc_id,
            "converged_subnet_id": nodes_info.converged_subnet_id,
            "provisioned_nodes": nodes_info.provisioned,
            "key_file": key_file,
        }
    )

    print(json.dumps(inventory, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
