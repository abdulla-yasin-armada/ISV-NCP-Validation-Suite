#!/usr/bin/env python3
"""deprovision_nodes — Armada Bridge network suite, teardown phase.

Deallocates BM nodes that were provisioned by provision_nodes (setup).

For each node ID:
  1. POST /orchestrator/tenants/{tenant}/metal/{node_id}/deallocate
  2. Poll GET .../metal/computes until the node disappears from the list.

Pass --skip-destroy to skip all API calls (ARMADA_BRIDGE_SKIP_TEARDOWN=true).
404 on deallocate is treated as success (already gone).

Output: {success, platform} or {success, platform, skipped: true}
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common.bridge_client import BridgeClient
from common.errors import handle_bridge_errors
from common.polling import poll_until
from common.tenant import resolve_tenant_id

DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"

_POLL_TIMEOUT = 300
_POLL_INTERVAL = 15


def _parse_instance_ids(raw: str) -> list[str]:
    """Parse comma-separated or JSON-array string of node IDs."""
    raw = raw.strip()
    if not raw:
        return []
    if raw.startswith("["):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [str(i) for i in parsed if i]
        except json.JSONDecodeError:
            pass
    return [part.strip() for part in raw.split(",") if part.strip()]


def _deallocate_node(client: BridgeClient, tenant: str, node_id: str) -> None:
    """Fire deallocate request; ignore 404 (already gone)."""
    try:
        client.post(
            f"/orchestrator/tenants/{tenant}/metal/{node_id}/deallocate",
            {},
        )
    except ValueError as exc:
        if "status 404" not in str(exc):
            raise


def _poll_until_gone(client: BridgeClient, tenant: str, node_id: str) -> None:
    """Poll until the node is absent from the computes list."""
    list_path = f"/orchestrator/tenants/{tenant}/metal/computes"

    def check() -> tuple[bool, Any, str]:
        resp = client.get(list_path)
        nodes = resp if isinstance(resp, list) else (resp or {}).get("data", [])
        node = next(
            (n for n in nodes if str(n.get("id") or n.get("ID") or "") == node_id),
            None,
        )
        if node is None:
            return True, "absent", "node absent from computes list"
        status = str(node.get("allocateStatus", "") or "")
        return False, None, f"allocateStatus='{status}'"

    poll_until(
        check,
        label=f"deprovision_{node_id[:8]}",
        interval=_POLL_INTERVAL,
        timeout=_POLL_TIMEOUT,
    )


@handle_bridge_errors
def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tenant", required=True)
    parser.add_argument(
        "--instance-ids",
        default="",
        help="Comma-separated node IDs to deallocate",
    )
    parser.add_argument("--skip-destroy", action="store_true")
    args = parser.parse_args()

    result: dict[str, Any] = {"success": False, "platform": "network"}

    if args.skip_destroy:
        result["success"] = True
        result["skipped"] = True
    elif os.environ.get("BRIDGE_NETWORK_NODE_IDS", "").strip():
        result.update({"success": True, "skipped": True, "reason": "pre_existing_nodes_not_deallocated"})
    elif DEMO_MODE:
        result["success"] = True
    else:
        node_ids = _parse_instance_ids(args.instance_ids)

        if not node_ids:
            # Nothing to deallocate — treat as success
            result["success"] = True
            result["skipped"] = True
            result["reason"] = "no instance_ids provided"
            print(json.dumps(result, indent=2))
            return 0

        client = BridgeClient.from_env()
        tenant = resolve_tenant_id(client, args.tenant)

        for node_id in node_ids:
            _deallocate_node(client, tenant, node_id)

        for node_id in node_ids:
            _poll_until_gone(client, tenant, node_id)

        result["success"] = True
        result["deallocated"] = node_ids

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
