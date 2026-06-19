#!/usr/bin/env python3
"""stop_instance — Armada Bridge VM suite, test phase.

Stops a running VM instance via:
  POST /orchestrator/tenants/<tenant>/vms/<vm_id>/power/off

Polls GET until Bridge status maps to ISV state "stopped" (e.g. poweredOff).

Idempotent: if the VM is already stopped, skips power/off and returns success.

Validation: InstanceStopCheck (instance_id, stop_initiated, state: stopped).

Output: {success, platform, instance_id, stop_initiated, state}
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
from common.vm import bridge_status, get_vm, power_action, to_isv_state, wait_for_vm_status

DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"
_POLL_TIMEOUT = 105  # vm.yaml stop step timeout is 120s


@handle_bridge_errors
def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--vm-id", required=True)
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "vm",
        "instance_id": args.vm_id,
        "stop_initiated": False,
    }

    if DEMO_MODE:
        result.update(
            {
                "success": True,
                "platform": "vm",
                "instance_id": "demo-vm-abc123",
                "stop_initiated": True,
                "state": "stopped",
            }
        )
    else:
        client = BridgeClient.from_env()
        tenant_id = resolve_tenant_id(client, args.tenant)
        vm = get_vm(client, tenant_id, args.vm_id)
        mapped = to_isv_state(bridge_status(vm))

        if mapped == "stopped":
            result.update(
                {
                    "success": True,
                    "stop_initiated": True,
                    "state": "stopped",
                }
            )
        else:
            if mapped != "running":
                raise RuntimeError(
                    f"VM {args.vm_id} is {mapped!r} (Bridge status={bridge_status(vm)!r}), "
                    "expected running or stopped"
                )
            power_action(client, tenant_id, args.vm_id, "off")
            vm = wait_for_vm_status(
                client,
                tenant_id,
                args.vm_id,
                target="stopped",
                label="stop_instance",
                timeout=_POLL_TIMEOUT,
            )
            result.update(
                {
                    "success": True,
                    "stop_initiated": True,
                    "state": to_isv_state(bridge_status(vm)),
                }
            )

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
