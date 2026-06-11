#!/usr/bin/env python3
"""setup — Armada Bridge Kubernetes suite, setup phase.

Flow (``main()``):
  1. Resolve ``--tenant`` name or UUID via ``resolve_tenant_id()`` (tenant must exist).
  2. ``provision_nodes()`` — import vs discovery (``GET /orchestrator/network/topologies``).
     If ``BRIDGE_K8S_NODE_IDS`` / ``--node-id`` is set, those nodes are used; otherwise
     pick an available catalog flavor and allocate a **new** worker (pre-existing computes
     are not reused). Node type defaults: import → bareMetal, discovery → VM; override with
     ``BRIDGE_K8S_NODE_TYPE=bareMetal|vm``.
  3. ``create_cluster()`` — POST ``/orchestrator/tenants/{tenant}/clusters``
     (``BRIDGE_K8S_INSTALL_GPU_TOOLS``, ``BRIDGE_K8S_DEPLOY_LOCAL_PROVISIONER``,
     ``BRIDGE_K8S_VERSION``).
  4. ``wait_cluster_running()`` → ``fetch_kubeconfig()`` → ``write_kubeconfig()``
     → ``wait_for_kubectl()``.
  5. ``_run_inventory()`` (``_common.sh``) — kubectl snapshot of nodes, GPUs, CSI, etc.
  6. ``save_state()`` — persist cluster/node IDs for ``teardown.py``.

Stdout: ISV kubernetes setup JSON (``cluster_id``, ``kubeconfig_path``, ``kubernetes{}``,
``csi{}``, flow metadata). Consumed by isvctl as ``steps.setup``.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

_SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPT_DIR.parent))
sys.path.insert(0, str(_SCRIPT_DIR))

from common.cluster import (  # noqa: E402
    create_cluster,
    default_kubeconfig_path,
    extract_api_server,
    extract_cluster_id,
    fetch_kubeconfig,
    wait_cluster_running,
    wait_for_kubectl,
    write_kubeconfig,
)
from common.errors import handle_bridge_errors  # noqa: E402
from common.k8s_state import save_state  # noqa: E402
from common.tenant import resolve_tenant_id  # noqa: E402
from provision_nodes import provision_nodes  # noqa: E402

DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"


def _run_inventory(kubeconfig_path: Path, cluster_name: str) -> dict[str, Any]:
    env = os.environ.copy()
    env["KUBECTL"] = f"kubectl --kubeconfig={kubeconfig_path}"
    env["CLUSTER_NAME"] = cluster_name
    env["DEFAULT_GPU_NS"] = "nvidia-gpu-operator"
    env["REQUIRE_JQ"] = "true"
    result = subprocess.run(
        ["bash", str(_SCRIPT_DIR / "_common.sh")],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Inventory collection failed: {result.stderr.strip() or result.stdout.strip()}"
        )
    return json.loads(result.stdout)


def _cluster_nodes(node_ids: list[str], node_type: str) -> list[dict[str, Any]]:
    return [
        {"id": node_id, "isAllocated": True, "nodeType": node_type}
        for node_id in node_ids
    ]


@handle_bridge_errors
def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tenant", required=True)
    parser.add_argument(
        "--node-id",
        action="append",
        default=[],
        help="Existing BM/VM node UUID (repeatable). BRIDGE_K8S_NODE_IDS also accepted.",
    )
    args = parser.parse_args()

    if DEMO_MODE:
        result = {
            "success": True,
            "platform": "kubernetes",
            "discovery_flow": False,
            "import_flow": True,
            "cluster_name": "demo-bridge-k8s",
            "cluster_id": "demo-cluster-id",
            "kubeconfig_path": str(default_kubeconfig_path()),
            "kubernetes": {
                "driver_version": "580.82.07",
                "node_count": 1,
                "nodes": ["demo-node"],
                "gpu_node_count": 1,
                "gpu_per_node": 1,
                "total_gpus": 1,
                "gpu_operator_namespace": "nvidia-gpu-operator",
                "control_plane_namespace": "kube-system",
                "runtime_class": "nvidia",
                "gpu_resource_name": "nvidia.com/gpu",
                "api_endpoint": "https://203.0.113.10:6443",
            },
            "csi": {
                "block_storage_class": "local-path",
                "shared_fs_storage_class": "",
                "nfs_storage_class": "",
                "static_volume_handle": "",
                "static_driver_name": "",
            },
        }
        print(json.dumps(result, indent=2))
        return 0

    from common.bridge_client import BridgeClient

    client = BridgeClient.from_env()
    tenant_id = resolve_tenant_id(client, args.tenant)
    nodes_info = provision_nodes(client, tenant_id, cli_node_ids=args.node_id)

    cluster_name = os.environ.get("BRIDGE_K8S_CLUSTER_NAME", f"isv-k8s-{int(time.time())}")
    install_gpu_tools = os.environ.get("BRIDGE_K8S_INSTALL_GPU_TOOLS", "true").lower() not in {
        "0",
        "false",
        "no",
    }
    deploy_local_provisioner = os.environ.get("BRIDGE_K8S_DEPLOY_LOCAL_PROVISIONER", "true").lower() not in {
        "0",
        "false",
        "no",
    }
    cluster_version = os.environ.get("BRIDGE_K8S_VERSION", "1.31")

    created = create_cluster(
        client,
        tenant_id,
        name=cluster_name,
        nodes=_cluster_nodes(nodes_info.node_ids, nodes_info.node_type),
        version=cluster_version,
        install_gpu_tools=install_gpu_tools,
        deploy_local_provisioner=deploy_local_provisioner,
    )
    cluster_id = extract_cluster_id(created)
    cluster = wait_cluster_running(client, tenant_id, cluster_id)

    kubeconfig_yaml = fetch_kubeconfig(client, tenant_id, cluster_id)
    kubeconfig_path = default_kubeconfig_path()
    if os.environ.get("KUBECONFIG_PATH"):
        kubeconfig_path = Path(os.environ["KUBECONFIG_PATH"]).expanduser()
    write_kubeconfig(kubeconfig_yaml, kubeconfig_path)
    wait_for_kubectl(kubeconfig_path)

    cluster_name = str(cluster.get("name") or created.get("name") or cluster_name)
    inventory = _run_inventory(kubeconfig_path, cluster_name)
    api_endpoint = extract_api_server(kubeconfig_yaml)
    if api_endpoint and isinstance(inventory.get("kubernetes"), dict):
        inventory["kubernetes"]["api_endpoint"] = api_endpoint

    inventory.update(
        {
            "cluster_id": cluster_id,
            "kubeconfig_path": str(kubeconfig_path),
            "discovery_flow": nodes_info.discovery_flow,
            "import_flow": nodes_info.import_flow,
            "node_type": nodes_info.node_type,
            "node_ids": nodes_info.node_ids,
            "vpc_id": nodes_info.vpc_id,
            "subnet_id": nodes_info.subnet_id,
        }
    )

    save_state(
        {
            "tenant": args.tenant,
            "tenant_id": tenant_id,
            "cluster_id": cluster_id,
            "cluster_name": cluster_name,
            "kubeconfig_path": str(kubeconfig_path),
            "discovery_flow": nodes_info.discovery_flow,
            "import_flow": nodes_info.import_flow,
            "node_type": nodes_info.node_type,
            "node_ids": nodes_info.node_ids,
            "vpc_id": nodes_info.vpc_id,
            "subnet_id": nodes_info.subnet_id,
            "provisioned_nodes": nodes_info.provisioned,
        }
    )

    print(json.dumps(inventory, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
