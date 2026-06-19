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
import sys
import time
from pathlib import Path
from typing import Any

_SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPT_DIR.parent))
sys.path.insert(0, str(_SCRIPT_DIR))

from common.errors import handle_bridge_errors  # noqa: E402
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
from provision_nodes import build_slurm_nodes, provision_slurm_nodes  # noqa: E402

DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"


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

    if DEMO_MODE:
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

    from common.bridge_client import BridgeClient

    client = BridgeClient.from_env()
    tenant_id = resolve_tenant_id(client, args.tenant)
    nodes_info = provision_slurm_nodes(
        client,
        tenant_id,
        cli_node_ids=args.node_id,
        cli_master_id=args.master_node_id,
        cli_worker_ids=args.worker_node_id,
    )

    cluster_name = os.environ.get("BRIDGE_SLURM_CLUSTER_NAME", f"isv-slurm-{int(time.time())}")
    cluster_version = os.environ.get("BRIDGE_SLURM_VERSION", "24.05.6")
    cluster_description = os.environ.get("BRIDGE_SLURM_DESCRIPTION", "ISV Slurm validation cluster")

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

    key_file = os.environ.get("BRIDGE_SSH_KEY_FILE", nodes_info.key_file)
    if not key_file:
        raise RuntimeError("SSH key file required for Slurm CLI access (set BRIDGE_SSH_KEY_FILE)")

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
            "provisioned_nodes": nodes_info.provisioned,
            "key_file": key_file,
        }
    )

    print(json.dumps(inventory, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
