#!/usr/bin/env python3
"""list_instances — Armada Bridge VM suite, test phase.

Lists VM instances for a tenant via:
  GET /orchestrator/tenants/<tenant>/vms

Finds the VM matching --instance-id (from launch_instance) and emits the list
shape required by InstanceListCheck.

vpc_id per instance:
  - Import flow: wiring placeholder from launch (e.g. "n/a" via --vpc-id)
  - Discovery flow: real VPC from VM subnets when present, else --vpc-id from launch

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
from common.vm import extract_vm_id, list_vms, vm_to_instance

DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"


@handle_bridge_errors
def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--vpc-id", default="")
    parser.add_argument("--instance-id", required=True)
    args = parser.parse_args()

    wiring_vpc_id = args.vpc_id or "n/a"

    result: dict[str, Any] = {"success": False, "platform": "vm"}

    if DEMO_MODE:
        result.update(
            {
                "success": True,
                "platform": "vm",
                "instances": [
                    {
                        "instance_id": "demo-vm-abc123",
                        "state": "running",
                        "vpc_id": wiring_vpc_id,
                    }
                ],
                "count": 1,
                "found_target": True,
                "target_instance": "demo-vm-abc123",
            }
        )
    else:
        client = BridgeClient.from_env()
        tenant_id = resolve_tenant_id(client, args.tenant)
        vms = list_vms(client, tenant_id)
        instances = [vm_to_instance(vm, vpc_id=wiring_vpc_id) for vm in vms]
        found_target = any(extract_vm_id(vm) == args.instance_id for vm in vms)

        result.update(
            {
                "success": found_target,
                "platform": "vm",
                "instances": instances,
                "count": len(instances),
                "found_target": found_target,
                "target_instance": args.instance_id if found_target else None,
            }
        )
        if not found_target:
            ids = [extract_vm_id(vm) for vm in vms]
            raise RuntimeError(
                f"Target instance {args.instance_id!r} not found in tenant VM list "
                f"({len(ids)} VMs: {ids[:5]}{'...' if len(ids) > 5 else ''})"
            )

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
