#!/usr/bin/env python3
"""create_tenant — Armada Bridge control-plane suite, setup phase.

Creates a tenant (resource group / namespace) via:
  POST /orchestrator/tenants
  Body: {name: <tenant_name>, description: "ISV test tenant"}

Response shape (Tenant model): {ID (uppercase), name, description, status, ...}

Idempotency: on 409 (tenant already exists), falls through to
  GET /orchestrator/tenants and locates the tenant by name.

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
                "tenant_name": "isv-test-tenant",
                "tenant_id": "demo-tenant-uuid-0001",
                "description": "ISV test tenant",
            }
        )
    else:
        client = BridgeClient.from_env()

        try:
            tenant = client.post(
                "/orchestrator/tenants",
                {"name": args.tenant_name, "description": "ISV test tenant"},
            )
        except ValueError as e:
            if "status 409" not in str(e):
                raise
            # Tenant already exists from a prior run — find it in the list
            tenants = client.get("/orchestrator/tenants")
            tenant = extract_tenant_from_tenants(tenants, args.tenant_name)
            if tenant is None:
                result["error"] = (
                    f"Tenant '{args.tenant_name}' returned 409 but was not found in list"
                )
                print(json.dumps(result, indent=2))
                return 1

        result.update(
            {
                "success": True,
                "tenant_name": tenant["name"],
                "tenant_id": tenant["ID"],
                "description": tenant.get("description", "ISV test tenant"),
            }
        )

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
