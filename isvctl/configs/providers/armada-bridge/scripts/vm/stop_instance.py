#!/usr/bin/env python3
"""stop_instance — Armada Bridge VM suite, test phase.

Stops a VM instance via:
  POST /orchestrator/tenants/<tenant>/vms/<vm_id>/power/off

Always sends power/off, then polls GET until Bridge status maps to ISV state
"stopped". Does not skip the power action based on pre-check state.

After API confirms stopped, TCP-probes port 22 on --public-ip to verify the
VM is actually unreachable. Fails if SSH port is still reachable (VM not
truly stopped).

Validation: InstanceStopCheck (instance_id, stop_initiated, state: stopped,
            ssh_unreachable: True).

Output: {success, platform, instance_id, stop_initiated, state, ssh_unreachable}
"""
import argparse
import json
import os
import socket
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common.bridge_client import BridgeClient
from common.errors import handle_bridge_errors
from common.tenant import resolve_tenant_id
from common.vm import bridge_status, power_action, to_isv_state, wait_for_vm_status

DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"
_POLL_TIMEOUT = 105  # vm.yaml stop step timeout is 120s
_SSH_PROBE_TIMEOUT = 10  # seconds for TCP probe to port 22


def _probe_ssh_port(host: str, timeout: int = _SSH_PROBE_TIMEOUT) -> bool:
    """Return True if TCP port 22 is reachable on host, False otherwise."""
    try:
        with socket.create_connection((host, 22), timeout=timeout):
            return True
    except (OSError, TimeoutError):
        return False


@handle_bridge_errors
def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--vm-id", required=True)
    parser.add_argument("--public-ip", required=True)
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "vm",
        "instance_id": args.vm_id,
        "stop_initiated": False,
        "ssh_unreachable": False,
    }

    if DEMO_MODE:
        result.update(
            {
                "success": True,
                "platform": "vm",
                "instance_id": "demo-vm-abc123",
                "stop_initiated": True,
                "state": "stopped",
                "ssh_unreachable": True,
            }
        )
    else:
        client = BridgeClient.from_env()
        tenant_id = resolve_tenant_id(client, args.tenant)

        print(f"[stop_instance] sending power/off to {args.vm_id}", file=sys.stderr)
        power_action(client, tenant_id, args.vm_id, "off")
        print("[stop_instance] waiting 60s for libvirt to complete shutdown ...", file=sys.stderr)
        time.sleep(60)
        vm = wait_for_vm_status(
            client,
            tenant_id,
            args.vm_id,
            target="stopped",
            label="stop_instance",
            timeout=_POLL_TIMEOUT,
        )

        print(f"[stop_instance] probing SSH port 22 on {args.public_ip} ...", file=sys.stderr)
        ssh_still_up = _probe_ssh_port(args.public_ip)
        if ssh_still_up:
            raise RuntimeError(
                f"VM {args.vm_id} API reports stopped but SSH port 22 is still "
                f"reachable on {args.public_ip} — VM did not actually stop"
            )
        print(f"[stop_instance] SSH port 22 unreachable on {args.public_ip} — VM confirmed stopped", file=sys.stderr)

        result.update(
            {
                "success": True,
                "stop_initiated": True,
                "state": to_isv_state(bridge_status(vm)),
                "ssh_unreachable": True,
            }
        )

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
