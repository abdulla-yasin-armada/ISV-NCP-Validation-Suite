#!/usr/bin/env python3
"""create_tenant — Armada Bridge control-plane suite, setup phase.

Creates a tenant (resource group / namespace) via:
  POST /orchestrator/tenants
  Body: {name, description} or extended M360 billing form when BRIDGE_BILLING=1

Response shape (Tenant model): {ID (uppercase), name, description, status, ...}
Bridge may return 201 with an empty body — tenant id is resolved via GET /orchestrator/tenants.

Idempotency: on 409/422 (tenant already exists), falls through to
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
from common.tenant import create_or_resolve_tenant, default_test_tenant_name, tenant_record_id

DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"


@handle_bridge_errors
def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--tenant-name",
        default="",
        help="Explicit tenant name (default: isv-test-tenant-<uuid8> per run)",
    )
    parser.add_argument(
        "--name-prefix",
        default="isv-test-tenant",
        help="Prefix when auto-generating a unique tenant name",
    )
    args = parser.parse_args()

    tenant_name = args.tenant_name.strip() or default_test_tenant_name(args.name_prefix)
    print(f"[control_plane] creating tenant: {tenant_name}", file=sys.stderr)

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
        tenant = create_or_resolve_tenant(
            client,
            tenant_name,
            "ISV test tenant",
        )
        tenant_id = tenant_record_id(tenant)
        if not tenant_id:
            result["error"] = (
                f"Tenant '{tenant_name}' was created but no id/ID field was returned"
            )
            print(json.dumps(result, indent=2))
            return 1

        result.update(
            {
                "success": True,
                "tenant_name": tenant.get("name", tenant_name),
                "tenant_id": tenant_id,
                "description": tenant.get("description", "ISV test tenant"),
            }
        )

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
