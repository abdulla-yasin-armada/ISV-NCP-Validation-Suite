#!/usr/bin/env python3
"""teardown — Armada Bridge network suite, teardown phase.

Discovery flow: DELETE suite-created subnets then VPCs (orchestrator UUIDs only).
  Deletes both the compute VPC (--vpc-id) and the converged VPC (--converged-vpc-id)
  created by create_network.
Import flow: no-op — IPAllocation CRs are pre-provisioned and must not be deleted.

Pass --skip-destroy to skip all API calls (ARMADA_BRIDGE_SKIP_TEARDOWN=true).

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
from common.network import is_orchestrator_resource_id, list_tenant_subnets
from common.tenant import resolve_tenant_id

DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"


@handle_bridge_errors
def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--vpc-id", required=True)
    parser.add_argument("--converged-vpc-id", default="",
                        help="Converged topology VPC to delete (created by create_network)")
    parser.add_argument("--skip-destroy", action="store_true")
    args = parser.parse_args()

    result: dict[str, Any] = {"success": False, "platform": "network"}

    if args.skip_destroy:
        result.update({"success": True, "skipped": True})
    elif DEMO_MODE:
        result["success"] = True
    elif not is_orchestrator_resource_id(args.vpc_id):
        result.update(
            {
                "success": True,
                "skipped": True,
                "reason": "import_flow_ipallocation_not_managed_by_suite",
            }
        )
    else:
        client = BridgeClient.from_env()
        tenant_id = resolve_tenant_id(client, args.tenant)

        def _delete_vpc(vpc_id: str) -> None:
            """Delete all subnets of vpc_id then the VPC itself (best-effort 404)."""
            if not vpc_id or not is_orchestrator_resource_id(vpc_id):
                return
            for subnet in list_tenant_subnets(client, tenant_id, vpc_id=vpc_id):
                subnet_id = str(subnet.get("id", "") or "")
                if is_orchestrator_resource_id(subnet_id):
                    try:
                        client.delete(f"/orchestrator/tenants/{tenant_id}/subnets/{subnet_id}")
                    except ValueError as exc:
                        if "status 404" not in str(exc):
                            raise
            try:
                client.delete(f"/orchestrator/tenants/{tenant_id}/vpcs/{vpc_id}")
            except ValueError as exc:
                if "status 404" not in str(exc):
                    raise

        _delete_vpc(args.vpc_id)
        _delete_vpc(args.converged_vpc_id)
        result["success"] = True

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
