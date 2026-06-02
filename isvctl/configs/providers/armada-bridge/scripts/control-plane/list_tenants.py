#!/usr/bin/env python3
"""list_tenants — Armada Bridge control-plane suite, test phase.

Lists tenants and verifies the target tenant is present via:
  GET /orchestrator/tenants

Output: {success, platform, found_target, target_tenant, count}
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
from common.iam import extract_tenant_from_tenants

DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"


@handle_bridge_errors
def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tenant-name", required=True)
    args = parser.parse_args()

    result: dict[str, Any] = {"success": False, "platform": "control_plane"}

    if DEMO_MODE:
        result.update(
            {
                "success": True,
                "platform": "control_plane",
                "found_target": True,
                "target_tenant": "isv-test-tenant",
                "count": 1,
            }
        )
    else:
        client = BridgeClient.from_env()
        tenants = client.get("/orchestrator/tenants")

        tenant_info = extract_tenant_from_tenants(tenants, args.tenant_name)
        found = tenant_info is not None
        count = len(tenants) if isinstance(tenants, list) else 0

        if not found:
            result["error"] = f"Tenant '{args.tenant_name}' not found in list of {count} tenants"

        result.update(
            {
                "success": found,
                "found_target": found,
                "target_tenant": args.tenant_name,
                "count": count,
            }
        )

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
