#!/usr/bin/env python3
"""teardown — Armada Bridge bare metal suite, teardown phase.

1. POST .../metal/:id/deallocate — fire deallocation request.
2. Poll the computes list until the server is absent (deallocation confirmed).
   VPC/subnet deletion is deferred until the server is fully gone to avoid
   failures from resources still being in use.
3. If --vpc-id is a real discovery-flow VPC (not import placeholder "n/a"),
   DELETE subnet then VPC.

Pass --skip-destroy to skip all API calls (ARMADA_BRIDGE_SKIP_TEARDOWN=true).
404 on any delete is treated as success (already gone).

Output: {success, platform} or {success, platform, skipped: true}
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common.bridge_client import BridgeClient
from common.errors import handle_bridge_errors
from common.network import is_managed_network_id
from common.polling import poll_until
from common.tenant import resolve_tenant_id

DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"

_POLL_TIMEOUT = 300
_POLL_INTERVAL = 15
_ACTIVE_STATES = {"done", "success", "processing"}


@handle_bridge_errors
def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--compute-node-id", required=True)
    parser.add_argument("--vpc-id", default="")
    parser.add_argument("--subnet-id", default="")
    parser.add_argument("--skip-destroy", action="store_true")
    args = parser.parse_args()

    result: dict[str, Any] = {"success": False, "platform": "bare_metal"}

    if args.skip_destroy:
        result["success"] = True
        result["skipped"] = True
    elif DEMO_MODE:
        result["success"] = True
    else:
        client = BridgeClient.from_env()
        tenant = resolve_tenant_id(client, args.tenant)
        node_id = args.compute_node_id

        # 1. Fire the deallocate request.
        # Bridge uses POST .../deallocate (not DELETE) to release BM nodes.
        try:
            client.post(
                f"/orchestrator/tenants/{tenant}/metal/{node_id}/deallocate",
                {},
            )
        except ValueError as e:
            if "status 404" not in str(e):
                raise

        # 2. Poll until the server is gone before touching VPC/subnet —
        #    deletion will fail while the server still holds the subnet.
        list_path = f"/orchestrator/tenants/{tenant}/metal/computes"

        def check_server_gone() -> tuple[bool, Any, str]:
            computes = client.get(list_path)
            nodes = computes if isinstance(computes, list) else (computes or {}).get("data", [])
            node = next(
                (n for n in nodes if str(n.get("id", "") or n.get("ID", "")) == node_id),
                None,
            )
            if node is None:
                return True, "absent", "server absent from list"
            alloc_status = str(node.get("allocateStatus", "") or "")
            # Intentionally not treating inactive allocateStatus as "gone":
            # Bridge occasionally transitions through unexpected transient states
            # during deallocation. Waiting for the node to fully disappear from
            # the list is more reliable than trusting a non-active status.
            return False, None, f"allocateStatus='{alloc_status}'"

        poll_until(
            check_server_gone,
            label="teardown",
            interval=_POLL_INTERVAL,
            timeout=_POLL_TIMEOUT,
        )

        # Wait a bit to avoid race conditions with the next step.
        time.sleep(0.3)

        # 3. Discovery flow only: clean up VPC/subnet created at launch.
        # Import flow passes vpc_id "n/a" — skip network delete.
        if is_managed_network_id(args.vpc_id):
            if is_managed_network_id(args.subnet_id):
                client.delete(f"/orchestrator/tenants/{tenant}/subnets/{args.subnet_id}")
            client.delete(f"/orchestrator/tenants/{tenant}/vpcs/{args.vpc_id}")

        result["success"] = True

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
