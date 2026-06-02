#!/usr/bin/env python3
"""get_tenant — Armada Bridge control-plane suite, test phase.

Retrieves a specific tenant by ID via:
  GET /orchestrator/tenants/:tenantId

Output: {success, platform, tenant_name, tenant_id, description}
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

DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"


@handle_bridge_errors
def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tenant-id", required=True)
    args = parser.parse_args()

    result: dict[str, Any] = {"success": False, "platform": "control_plane"}

    if DEMO_MODE:
        result.update(
            {
                "success": True,
                "platform": "control_plane",
                "tenant_name": "isv-test-tenant",
                "tenant_id": "demo-tenant-uuid-0001",
                "description": "ISV test tenant",
            }
        )
    else:
        client = BridgeClient.from_env()
        tenant = client.get(f"/orchestrator/tenants/{args.tenant_id}")

        result.update(
            {
                "success": True,
                "tenant_name": tenant["name"],
                "tenant_id": tenant["ID"],
                "description": tenant.get("description", ""),
            }
        )

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
