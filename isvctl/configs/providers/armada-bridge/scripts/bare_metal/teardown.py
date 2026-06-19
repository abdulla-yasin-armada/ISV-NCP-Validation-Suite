#!/usr/bin/env python3
"""teardown — Armada Bridge bare metal suite, teardown phase.

1. deallocate_bm()  — POST .../metal/:id/deallocate, poll until node is absent.
2. deprovision_discovery_vpcs() — delete subnets then VPCs for both compute and
   converged VPCs created during launch (discovery flow only; import flow passes
   vpc_id "n/a" which is skipped automatically).

Pass --skip-destroy to skip all API calls (ARMADA_BRIDGE_SKIP_TEARDOWN=true).
404 on any delete is treated as success (already gone).

Output: {success, platform} or {success, platform, skipped: true}
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
from common.metal import deallocate_bm
from common.tenant import resolve_tenant_id
from common.vpc import deprovision_discovery_vpcs

DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"

_POLL_TIMEOUT = 300
_POLL_INTERVAL = 15


@handle_bridge_errors
def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--compute-node-id", required=True)
    parser.add_argument("--vpc-id", default="")
    parser.add_argument("--subnet-id", default="")
    parser.add_argument("--converged-vpc-id", default="")
    parser.add_argument("--skip-destroy", action="store_true")
    args = parser.parse_args()

    result: dict[str, Any] = {"success": False, "platform": "bare_metal"}

    if args.skip_destroy:
        result.update({"success": True, "skipped": True})
    elif DEMO_MODE:
        result["success"] = True
    else:
        client = BridgeClient.from_env()
        tenant = resolve_tenant_id(client, args.tenant)

        deallocate_bm(
            client, tenant, args.compute_node_id,
            poll=True,
            poll_timeout=_POLL_TIMEOUT,
            poll_interval=_POLL_INTERVAL,
            label="bm_teardown",
        )

        deprovision_discovery_vpcs(
            client, tenant,
            str(args.vpc_id or ""),
            str(args.converged_vpc_id or ""),
        )

        result["success"] = True

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
