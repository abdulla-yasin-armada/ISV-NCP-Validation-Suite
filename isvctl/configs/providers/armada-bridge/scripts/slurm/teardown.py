#!/usr/bin/env python3
"""teardown — Armada Bridge Slurm suite, teardown phase.

1. DELETE Slurm cluster, poll until gone
2. Optionally deallocate BM / delete VM when setup provisioned them
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.bridge_client import BridgeClient  # noqa: E402
from common.errors import handle_bridge_errors  # noqa: E402
from common.network import is_managed_network_id  # noqa: E402
from common.polling import poll_until  # noqa: E402
from common.slurm_cluster import (  # noqa: E402
    delete_slurm_cluster,
    wait_slurm_deleted,
)
from common.slurm_state import clear_state, load_state  # noqa: E402
from common.tenant import resolve_tenant_id  # noqa: E402
from common.vm import get_vm, vm_path, wait_for_vm_deleted  # noqa: E402

DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"
_BM_POLL_TIMEOUT = 300
_BM_POLL_INTERVAL = 15


def _should_destroy_nodes(state: dict[str, Any]) -> bool:
    explicit = os.environ.get("BRIDGE_SLURM_DESTROY_NODES", "").strip().lower()
    if explicit in {"1", "true", "yes"}:
        return True
    if explicit in {"0", "false", "no"}:
        return False
    return bool(state.get("provisioned_nodes"))


def _deallocate_bm(client: BridgeClient, tenant_id: str, node_id: str) -> None:
    try:
        client.post(
            f"/orchestrator/tenants/{tenant_id}/metal/{node_id}/deallocate",
            {},
        )
    except ValueError as exc:
        if "404" not in str(exc):
            raise

    list_path = f"/orchestrator/tenants/{tenant_id}/metal/computes"

    def check_gone() -> tuple[bool, Any, str]:
        computes = client.get(list_path)
        nodes = computes if isinstance(computes, list) else (computes or {}).get("data", [])
        node = next(
            (n for n in nodes if str(n.get("id", "") or n.get("ID", "")) == node_id),
            None,
        )
        if node is None:
            return True, "absent", "server absent from list"
        alloc_status = str(node.get("allocateStatus", "") or "")
        return False, None, f"allocateStatus={alloc_status!r}"

    poll_until(
        check_gone,
        label="slurm_teardown_bm",
        interval=_BM_POLL_INTERVAL,
        timeout=_BM_POLL_TIMEOUT,
    )


def _delete_vm(client: BridgeClient, tenant_id: str, vm_id: str) -> None:
    try:
        get_vm(client, tenant_id, vm_id)
    except ValueError as exc:
        if "404" in str(exc):
            return
        raise
    client.delete(vm_path(tenant_id, vm_id))
    wait_for_vm_deleted(client, tenant_id, vm_id, timeout=300)


@handle_bridge_errors
def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--cluster-id", default="")
    parser.add_argument("--skip-destroy", action="store_true")
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "slurm",
        "resources_deleted": [],
    }

    if args.skip_destroy or os.environ.get("ARMADA_BRIDGE_SKIP_TEARDOWN", "").lower() == "true":
        result.update({"success": True, "skipped": True, "message": "Teardown skipped"})
        print(json.dumps(result, indent=2))
        return 0

    if DEMO_MODE:
        result.update(
            {
                "success": True,
                "cluster_id": args.cluster_id or "demo-slurm-id",
                "resources_deleted": ["slurm:demo-slurm-id"],
                "message": "Slurm cluster deleted",
            }
        )
        print(json.dumps(result, indent=2))
        return 0

    state = load_state()
    cluster_id = args.cluster_id or str(state.get("cluster_id") or "")
    if not cluster_id:
        raise RuntimeError(
            "cluster_id missing — pass --cluster-id or run setup in the same workspace first"
        )

    client = BridgeClient.from_env()
    tenant_id = resolve_tenant_id(client, args.tenant)

    try:
        delete_slurm_cluster(client, tenant_id, cluster_id)
    except ValueError as exc:
        if "404" not in str(exc):
            raise
    else:
        wait_slurm_deleted(client, tenant_id, cluster_id)

    result["cluster_id"] = cluster_id
    result["resources_deleted"].append(f"slurm:{cluster_id}")

    if _should_destroy_nodes(state):
        node_type = str(state.get("node_type") or "bareMetal")
        for node_id in state.get("node_ids") or []:
            if node_type == "vm":
                _delete_vm(client, tenant_id, str(node_id))
                result["resources_deleted"].append(f"vm:{node_id}")
            else:
                _deallocate_bm(client, tenant_id, str(node_id))
                result["resources_deleted"].append(f"bare_metal:{node_id}")

    vpc_id = str(state.get("vpc_id") or "")
    subnet_id = str(state.get("subnet_id") or "")
    if is_managed_network_id(vpc_id):
        if is_managed_network_id(subnet_id):
            client.delete(f"/orchestrator/tenants/{tenant_id}/subnets/{subnet_id}")
            result["resources_deleted"].append(f"subnet:{subnet_id}")
        client.delete(f"/orchestrator/tenants/{tenant_id}/vpcs/{vpc_id}")
        result["resources_deleted"].append(f"vpc:{vpc_id}")

    clear_state()
    result.update({"success": True, "message": "Slurm cluster deleted"})
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
