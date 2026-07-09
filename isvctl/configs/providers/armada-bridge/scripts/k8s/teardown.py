#!/usr/bin/env python3
"""teardown — Armada Bridge Kubernetes suite, teardown phase.

1. Early-exit if ``--skip-destroy`` or ``ARMADA_BRIDGE_SKIP_TEARDOWN=true``.
2. DELETE cluster, poll until gone.
3. Optionally deallocate BM / delete VM when setup provisioned them
   (``BRIDGE_K8S_DESTROY_NODES`` overrides; defaults to ``provisioned_nodes``
   from state).
4. Discovery flow: delete subnet/VPC when real UUIDs were created.
   Import flow uses vpc_id ``n/a`` — network delete is skipped.
5. Deallocate the Tenant B ACL probe node (BM or VM) + network (if ``BRIDGE_TENANT_B``
   was set during setup and state contains ``acl_probe_node_id``).
6. ``clear_state()`` — remove the k8s state file written by setup.

Pre-existing nodes (``--node-id`` / ``BRIDGE_K8S_NODE_IDS``) are kept unless
``BRIDGE_K8S_DESTROY_NODES=true``.
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
from common.cluster import delete_cluster, wait_cluster_deleted  # noqa: E402
from common.errors import handle_bridge_errors  # noqa: E402
from common.k8s_state import clear_state, load_state  # noqa: E402
from common.metal import deallocate_bm  # noqa: E402
from common.tenant import create_tenant_b_client, resolve_tenant_id  # noqa: E402
from common.vpc import deprovision_discovery_vpcs  # noqa: E402
from common.vm import get_vm, vm_path, wait_for_vm_deleted  # noqa: E402

DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"
_BM_POLL_TIMEOUT = 300
_BM_POLL_INTERVAL = 15


def _should_destroy_nodes(state: dict[str, Any]) -> bool:
    """Env var BRIDGE_K8S_DESTROY_NODES overrides; falls back to whether setup provisioned the nodes."""
    explicit = os.environ.get("BRIDGE_K8S_DESTROY_NODES", "").strip().lower()
    if explicit in {"1", "true", "yes"}:
        return True
    if explicit in {"0", "false", "no"}:
        return False
    return bool(state.get("provisioned_nodes"))


def _delete_vm(client: BridgeClient, tenant_id: str, vm_id: str) -> None:
    """Delete a VM and wait for it to be gone. Idempotent — skips if already deleted (404)."""
    print(f"[k8s] deleting VM {vm_id}", file=sys.stderr)
    try:
        get_vm(client, tenant_id, vm_id)
    except ValueError as exc:
        if "404" in str(exc):
            print(f"[k8s] VM {vm_id} already gone (404) — skipping", file=sys.stderr)
            return
        raise
    client.delete(vm_path(tenant_id, vm_id))
    wait_for_vm_deleted(client, tenant_id, vm_id, timeout=300)
    print(f"[k8s] VM {vm_id} deleted", file=sys.stderr)


@handle_bridge_errors
def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--cluster-id", default="")
    parser.add_argument("--skip-destroy", action="store_true")
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "kubernetes",
        "resources_deleted": [],
    }

    if args.skip_destroy or os.environ.get("ARMADA_BRIDGE_SKIP_TEARDOWN", "").lower() == "true":
        print("[k8s] teardown skipped (--skip-destroy / ARMADA_BRIDGE_SKIP_TEARDOWN)", file=sys.stderr)
        result.update({"success": True, "skipped": True, "message": "Teardown skipped"})
        print(json.dumps(result, indent=2))
        return 0

    if DEMO_MODE:
        print("[k8s] DEMO_MODE: skipping API calls", file=sys.stderr)
        result.update(
            {
                "success": True,
                "cluster_id": args.cluster_id or "demo-cluster-id",
                "resources_deleted": ["cluster:demo-cluster-id"],
                "message": "Cluster deleted",
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
    print(f"[k8s] tearing down: cluster={cluster_id}, tenant={tenant_id}", file=sys.stderr)

    try:
        delete_cluster(client, tenant_id, cluster_id)
    except ValueError as exc:
        if "404" not in str(exc):
            raise
        print(f"[k8s] cluster {cluster_id} already gone (404)", file=sys.stderr)
    else:
        wait_cluster_deleted(client, tenant_id, cluster_id)
        print(f"[k8s] cluster {cluster_id} deleted", file=sys.stderr)

    result["cluster_id"] = cluster_id
    result["resources_deleted"].append(f"cluster:{cluster_id}")

    if _should_destroy_nodes(state):
        node_type = str(state.get("node_type") or "bareMetal")
        for node_id in state.get("node_ids") or []:
            if node_type == "vm":
                _delete_vm(client, tenant_id, str(node_id))
                result["resources_deleted"].append(f"vm:{node_id}")
            else:
                deallocate_bm(
                    client, tenant_id, str(node_id),
                    label="k8s_teardown_bm",
                    poll_timeout=_BM_POLL_TIMEOUT,
                    poll_interval=_BM_POLL_INTERVAL,
                )
                result["resources_deleted"].append(f"bare_metal:{node_id}")

    vpc_id = str(state.get("vpc_id") or "")
    converged_vpc_id = str(state.get("converged_vpc_id") or "")
    deprovision_discovery_vpcs(client, tenant_id, vpc_id, converged_vpc_id)

    # Deallocate the Tenant B node provisioned by setup.py for K8sApiNetworkAclCheck.
    # acl_probe_node_type in state tells us whether it was a BM or VM.
    # A separate client authenticated as Tenant B user is required.
    acl_node_id = str(state.get("acl_probe_node_id") or "")
    acl_tenant_b_id = str(state.get("acl_probe_tenant_b_id") or "")
    if acl_node_id and acl_tenant_b_id:
        client_b = create_tenant_b_client()
        acl_node_type = str(state.get("acl_probe_node_type") or "bm")
        if acl_node_type == "vm":
            _delete_vm(client_b, acl_tenant_b_id, acl_node_id)
            result["resources_deleted"].append(f"acl_probe_vm:{acl_node_id}")
        else:
            deallocate_bm(
                client_b, acl_tenant_b_id, acl_node_id,
                label="acl_probe_teardown",
                poll_timeout=_BM_POLL_TIMEOUT,
                poll_interval=_BM_POLL_INTERVAL,
            )
            result["resources_deleted"].append(f"acl_probe_bm:{acl_node_id}")

        acl_vpc_id = str(state.get("acl_probe_vpc_id") or "")
        acl_converged_vpc_id = str(state.get("acl_probe_converged_vpc_id") or "")
        deprovision_discovery_vpcs(client_b, acl_tenant_b_id, acl_vpc_id, acl_converged_vpc_id)

    clear_state()
    result.update({"success": True, "message": "Cluster deleted"})
    print(f"[k8s] teardown complete: {result['resources_deleted']}", file=sys.stderr)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
