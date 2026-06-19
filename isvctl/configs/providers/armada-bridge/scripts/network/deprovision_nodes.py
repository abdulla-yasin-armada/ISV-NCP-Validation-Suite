#!/usr/bin/env python3
"""deprovision_nodes — Armada Bridge network suite, teardown phase.

Deallocates BM nodes that were provisioned by provision_nodes (setup).

For each node ID:
  POST /orchestrator/tenants/{tenant}/metal/{node_id}/deallocate
  Poll until the node disappears from the computes list.

Pass --skip-destroy to skip all API calls (ARMADA_BRIDGE_SKIP_TEARDOWN=true).
Pre-existing nodes (BRIDGE_NETWORK_NODE_IDS was set during setup) are not deallocated.
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
from common.metal import deallocate_bm
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
        result.update({"success": True, "skipped": True})
    elif os.environ.get("BRIDGE_NETWORK_NODE_IDS", "").strip():
        result.update({"success": True, "skipped": True, "reason": "pre_existing_nodes_not_deallocated"})
    elif DEMO_MODE:
        result["success"] = True
    else:
        node_ids = _parse_instance_ids(args.instance_ids)

        if not node_ids:
            result.update({"success": True, "skipped": True, "reason": "no instance_ids provided"})
            print(json.dumps(result, indent=2))
            return 0

        client = BridgeClient.from_env()
        tenant = resolve_tenant_id(client, args.tenant)

        for node_id in node_ids:
            deallocate_bm(
                client, tenant, node_id,
                poll=True,
                poll_timeout=_POLL_TIMEOUT,
                poll_interval=_POLL_INTERVAL,
                label=f"network_deprovision_{node_id[:8]}",
            )

        result.update({"success": True, "deallocated": node_ids})

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
