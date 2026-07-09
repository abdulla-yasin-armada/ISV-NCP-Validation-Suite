#!/usr/bin/env python3
"""start_instance — Armada Bridge VM suite, test phase.

Starts a VM instance via:
  POST /orchestrator/tenants/<tenant>/vms/<vm_id>/power/on

Always sends power/on, then polls GET until Bridge status maps to ISV state
"running". Does not skip the power action based on pre-check state.
Then waits for SSH to be ready.

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
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common.bridge_client import BridgeClient
from common.errors import handle_bridge_errors
from common.ssh_utils import wait_for_ssh
from common.tenant import resolve_tenant_id
from common.vm import bridge_status, get_public_ip, power_action, to_isv_state, wait_for_vm_status

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

        _MAX_RETRIES = 3
        _RETRY_WAIT = 30
        for attempt in range(_MAX_RETRIES):
            try:
                power_action(client, tenant_id, args.vm_id, "on")
                break
            except ValueError as exc:
                if "domain is already running" in str(exc) and attempt < _MAX_RETRIES - 1:
                    print(
                        f"[start_instance] libvirt reports domain still running "
                        f"(attempt {attempt + 1}/{_MAX_RETRIES}), waiting {_RETRY_WAIT}s ...",
                        file=sys.stderr,
                    )
                    time.sleep(_RETRY_WAIT)
                else:
                    raise
        result["start_initiated"] = True
        print("[start_instance] waiting 60s for libvirt to complete startup ...", file=sys.stderr)
        time.sleep(60)
        vm = wait_for_vm_status(
            client,
            tenant_id,
            args.vm_id,
            target="running",
            label="start_instance",
            timeout=_POLL_TIMEOUT,
        )

        public_ip = get_public_ip(vm)
        if not public_ip:
            raise RuntimeError(f"VM {args.vm_id} is running but has no public IP")

        ssh_user = str(vm.get("userName") or args.ssh_user)
        print(f"[start_instance] waiting for SSH on {public_ip} ...", file=sys.stderr)
        wait_for_ssh(public_ip, args.key_file, username=ssh_user, timeout=_SSH_TIMEOUT)
        print(f"[start_instance] SSH confirmed ready on {public_ip}", file=sys.stderr)

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
