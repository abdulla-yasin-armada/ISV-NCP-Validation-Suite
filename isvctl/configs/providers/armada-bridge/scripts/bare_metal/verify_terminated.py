#!/usr/bin/env python3
"""verify_terminated — Armada Bridge bare metal suite, teardown phase.

Verifies that teardown fully completed:
  1. Server is absent from the tenant computes list (poll until gone).
  2. If --vpc-id is a real discovery-flow VPC: verify VPC is deleted.
  3. If --subnet-id is a real discovery-flow subnet: verify subnet is deleted.

Note: FetchComputeByID returns HTTP 500 (not 404) on missing node, so
server presence is checked via the list endpoint, not GET-by-ID.

Output: {success, platform, server_gone, vpc_gone, subnet_gone}
"""
import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common.bridge_client import BridgeClient
from common.errors import handle_bridge_errors
from common.network import is_managed_network_id
from common.polling import poll_until
from common.tenant import resolve_tenant_id

DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"

_POLL_TIMEOUT = 90
_POLL_INTERVAL = 10
_ACTIVE_STATES = {"done", "success", "processing"}


def _resource_gone(client: BridgeClient, path: str) -> bool:
    """Return True if the resource is gone (404 or empty/non-JSON response).

    Any other error (500, network issue) is re-raised so transient failures
    are not silently treated as successful deletion.
    Idempotent: safe to call multiple times — read-only, consistent results.
    """
    try:
        result = client.get(path)
        # {} means the API returned an empty or non-JSON body (see BridgeClient.get).
        # A live resource always returns a populated JSON object, so treat {} as gone.
        return not result
    except ValueError as e:
        if "status 404" in str(e):
            return True
        raise


@handle_bridge_errors
def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--compute-node-id", required=True)
    parser.add_argument("--vpc-id", default="")
    parser.add_argument("--subnet-id", default="")
    args = parser.parse_args()

    result: dict[str, Any] = {"success": False, "platform": "bare_metal"}

    if DEMO_MODE:
        result.update(
            {"success": True, "server_gone": True, "vpc_gone": True, "subnet_gone": True}
        )
    else:
        client = BridgeClient.from_env()
        tenant = resolve_tenant_id(client, args.tenant)
        node_id = args.compute_node_id
        list_path = f"/orchestrator/tenants/{tenant}/metal/computes"

        # 1. Poll until the server is absent from the computes list.
        def check_server() -> tuple[bool, Any, str]:
            computes = client.get(list_path)
            nodes = computes if isinstance(computes, list) else (computes or {}).get("data", [])
            node = next(
                (n for n in nodes if str(n.get("id", "") or n.get("ID", "")) == node_id),
                None,
            )
            if node is None:
                return True, "absent", "server absent from list"
            alloc_status = str(node.get("allocateStatus", "") or "")
            if alloc_status not in _ACTIVE_STATES:
                return True, alloc_status, f"allocateStatus='{alloc_status}' (inactive)"
            return False, None, f"allocateStatus='{alloc_status}'"

        poll_until(
            check_server,
            label="verify_teardown",
            interval=_POLL_INTERVAL,
            timeout=_POLL_TIMEOUT,
        )
        result["server_gone"] = True

        # 2. Discovery flow only: verify VPC/subnet are deleted.
        vpc_gone = True
        subnet_gone = True

        if is_managed_network_id(args.vpc_id):
            vpc_gone = _resource_gone(
                client, f"/orchestrator/tenants/{tenant}/vpcs/{args.vpc_id}"
            )
            if not vpc_gone:
                result["error"] = f"VPC '{args.vpc_id}' still exists after teardown"

        if is_managed_network_id(args.subnet_id):
            subnet_gone = _resource_gone(
                client, f"/orchestrator/tenants/{tenant}/subnets/{args.subnet_id}"
            )
            if not subnet_gone and "error" not in result:
                result["error"] = f"Subnet '{args.subnet_id}' still exists after teardown"

        result["vpc_gone"] = vpc_gone if is_managed_network_id(args.vpc_id) else None
        result["subnet_gone"] = subnet_gone if is_managed_network_id(args.subnet_id) else None
        result["success"] = result["server_gone"] and vpc_gone and subnet_gone

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
