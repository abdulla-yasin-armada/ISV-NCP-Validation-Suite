#!/usr/bin/env python3
"""delete_tenant — Armada Bridge control-plane suite, teardown phase.

Resolves the tenant ID (if not supplied directly) then deletes via:
  GET  /orchestrator/tenants/<tenant_id>   ← idempotency guard
  DELETE /orchestrator/tenants/<tenant_id>

ID resolution order:
  1. --tenant-id (from steps.create_tenant.tenant_id context)
  2. --tenant-name fallback: GET /orchestrator/tenants, find by name

Idempotency: if the tenant is not found by ID (404) or by name (absent from
list), return success with already_deleted=True. Safe to run multiple times
and after partial setup failures where create_tenant never ran.

Output: {success, platform}
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
    parser.add_argument("--tenant-id", default="")
    parser.add_argument("--tenant-name", default="")
    parser.add_argument("--skip-destroy", action="store_true")
    args = parser.parse_args()

    result: dict[str, Any] = {"success": False, "platform": "control_plane"}

    if args.skip_destroy:
        result["success"] = True
        result["skipped"] = True
    elif DEMO_MODE:
        result["success"] = True
    else:
        client = BridgeClient.from_env()
        tenant_id = args.tenant_id

        if not tenant_id and args.tenant_name:
            # ID not in context (setup aborted before create_tenant) — resolve by name.
            tenants = client.get("/orchestrator/tenants")
            tenant = extract_tenant_from_tenants(tenants, args.tenant_name)
            if tenant is None:
                result["success"] = True
                result["already_deleted"] = True
                print(json.dumps(result, indent=2))
                return 0
            tenant_id = tenant["ID"]

        if not tenant_id:
            result["success"] = True
            result["skipped"] = True
            print(json.dumps(result, indent=2))
            return 0

        # Idempotency guard: 404 means already deleted.
        try:
            client.get(f"/orchestrator/tenants/{tenant_id}")
        except ValueError:
            result["success"] = True
            result["already_deleted"] = True
            print(json.dumps(result, indent=2))
            return 0

        try:
            client.delete(f"/orchestrator/tenants/{tenant_id}")
        except ValueError as e:
            result["error"] = str(e)
            print(json.dumps(result, indent=2))
            return 1
        result["success"] = True

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
