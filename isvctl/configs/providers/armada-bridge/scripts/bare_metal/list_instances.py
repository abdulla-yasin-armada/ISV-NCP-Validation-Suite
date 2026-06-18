#!/usr/bin/env python3
"""list_instances — Armada Bridge bare metal suite, test phase.

Lists bare metal compute nodes via:
  GET /orchestrator/tenants/<tenant>/metal/computes

Scans the list for the node matching --instance-id.

vpc_id per instance:
  - Import flow: wiring placeholder from launch (e.g. "n/a" via --vpc-id)
  - Discovery flow: real VPC from subnets[0].parentVpcID when present

Output: {success, platform, instances, count, found_target, target_instance}
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
from common.tenant import resolve_tenant_id

DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"


@handle_bridge_errors
def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--vpc-id", default="")
    parser.add_argument("--instance-id", required=True)
    args = parser.parse_args()

    wiring_vpc_id = args.vpc_id or "n/a"

    result: dict[str, Any] = {"success": False, "platform": "bare_metal"}

    if DEMO_MODE:
        result.update(
            {
                "success": True,
                "platform": "bare_metal",
                "instances": [
                    {
                        "instance_id": "demo-bm-node01",
                        "state": "running",
                        "vpc_id": "demo-vpc-bm",
                    }
                ],
                "count": 1,
                "found_target": True,
                "target_instance": "demo-bm-node01",
            }
        )
    else:
        client = BridgeClient.from_env()
        tenant = resolve_tenant_id(client, args.tenant)
        computes = client.get(f"/orchestrator/tenants/{tenant}/metal/computes")

        nodes = computes if isinstance(computes, list) else computes.get("data", [])
        count = len(nodes)

        # Bridge uses "id" or "ID" — check both
        found = next(
            (n for n in nodes if str(n.get("id") or n.get("ID", "")) == args.instance_id),
            None,
        )
        found_target = found is not None

        if not found_target:
            result["error"] = (
                f"Instance '{args.instance_id}' not found in list of {count} compute nodes"
            )

        def _normalize_state(node: dict) -> str:
            alloc = str(node.get("allocateStatus", "") or "")
            return "running" if alloc in ("done", "success") else alloc

        def _vpc_of(node: dict) -> str:
            subnets = node.get("subnets") or []
            if subnets and subnets[0].get("parentVpcID"):
                return str(subnets[0]["parentVpcID"])
            return wiring_vpc_id

        result.update(
            {
                "success": found_target,
                "instances": [
                    {
                        "instance_id": str(n.get("id") or n.get("ID", "")),
                        "state": _normalize_state(n),
                        "vpc_id": _vpc_of(n),
                    }
                    for n in nodes
                ],
                "count": count,
                "found_target": found_target,
                "target_instance": args.instance_id,
            }
        )

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
