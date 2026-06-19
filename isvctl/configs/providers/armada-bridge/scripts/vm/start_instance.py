#!/usr/bin/env python3
"""start_instance — Armada Bridge VM suite, test phase.

Starts a stopped VM instance via:
  POST /orchestrator/tenants/<tenant>/vms/<vm_id>/power/on

Polls GET until Bridge status maps to ISV state "running", then wait_for_ssh.

Idempotent: if the VM is already running, skips power/on and probes SSH only.

Validations: InstanceStartCheck, StableIdentifierCheck, ConnectivityCheck,
OsCheck, GpuCheck (instance_id, start_initiated, state: running, ssh_ready,
public_ip, key_file).

Output: {success, platform, instance_id, start_initiated, state, ssh_ready,
         public_ip, key_file, ssh_user}
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
from common.ssh_utils import wait_for_ssh
from common.tenant import resolve_tenant_id
from common.vm import bridge_status, get_public_ip, get_vm, power_action, to_isv_state, wait_for_vm_status

DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"
_POLL_TIMEOUT = 120  # vm.yaml start step timeout is 300s; leave room for SSH
_SSH_TIMEOUT = 165


@handle_bridge_errors
def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--vm-id", required=True)
    parser.add_argument("--key-file", required=True)
    parser.add_argument("--ssh-user", default="ubuntu")
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "vm",
        "instance_id": args.vm_id,
        "key_file": args.key_file,
        "ssh_user": args.ssh_user,
        "start_initiated": False,
        "ssh_ready": False,
    }

    if DEMO_MODE:
        result.update(
            {
                "success": True,
                "platform": "vm",
                "instance_id": "demo-vm-abc123",
                "start_initiated": True,
                "state": "running",
                "ssh_ready": True,
                "public_ip": "203.0.113.10",
                "key_file": args.key_file,
                "ssh_user": "ubuntu",
            }
        )
    else:
        client = BridgeClient.from_env()
        tenant_id = resolve_tenant_id(client, args.tenant)
        vm = get_vm(client, tenant_id, args.vm_id)
        mapped = to_isv_state(bridge_status(vm))

        if mapped == "running":
            result["start_initiated"] = True
        elif mapped == "stopped":
            power_action(client, tenant_id, args.vm_id, "on")
            result["start_initiated"] = True
            vm = wait_for_vm_status(
                client,
                tenant_id,
                args.vm_id,
                target="running",
                label="start_instance",
                timeout=_POLL_TIMEOUT,
            )
        else:
            raise RuntimeError(
                f"VM {args.vm_id} is {mapped!r} (Bridge status={bridge_status(vm)!r}), "
                "expected stopped or running"
            )

        public_ip = get_public_ip(vm)
        if not public_ip:
            raise RuntimeError(f"VM {args.vm_id} is running but has no public IP")

        ssh_user = str(vm.get("userName") or args.ssh_user)
        wait_for_ssh(public_ip, args.key_file, username=ssh_user, timeout=_SSH_TIMEOUT)

        result.update(
            {
                "success": True,
                "state": to_isv_state(bridge_status(vm)),
                "ssh_ready": True,
                "public_ip": public_ip,
                "key_file": args.key_file,
                "ssh_user": ssh_user,
            }
        )

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
