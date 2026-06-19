#!/usr/bin/env python3
"""reboot_instance — Armada Bridge VM suite, test phase.

Reboots a running VM instance via:
  POST /orchestrator/tenants/<tenant>/vms/<vm_id>/power/reboot

Samples pre-reboot uptime over SSH, polls GET until running, waits for SSH,
then compares post-reboot uptime to confirm the reboot occurred.

Validations: InstanceRebootCheck, StableIdentifierCheck, InstanceStateCheck,
ConnectivityCheck, OsCheck, GpuCheck (reboot_initiated, state: running,
ssh_ready, reboot_confirmed: true, uptime_seconds ≤ 600, public_ip, key_file).

Output: {success, platform, instance_id, reboot_initiated, state, ssh_ready,
         uptime_seconds, reboot_confirmed, public_ip, key_file, ssh_user}
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
from common.ssh_utils import get_uptime_via_ssh, wait_for_ssh
from common.tenant import resolve_tenant_id
from common.vm import bridge_status, get_public_ip, get_vm, power_action, to_isv_state, wait_for_vm_status

DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"
_POLL_TIMEOUT = 90  # vm.yaml reboot step timeout is 300s
_SSH_TIMEOUT = 120


@handle_bridge_errors
def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--vm-id", required=True)
    parser.add_argument("--key-file", required=True)
    parser.add_argument("--public-ip", required=True)
    parser.add_argument("--ssh-user", default="ubuntu")
    parser.add_argument("--wait-before-check", type=int, default=60)
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "vm",
        "instance_id": args.vm_id,
        "key_file": args.key_file,
        "public_ip": args.public_ip,
        "ssh_user": args.ssh_user,
        "reboot_initiated": False,
        "ssh_ready": False,
        "reboot_confirmed": False,
    }

    if DEMO_MODE:
        result.update(
            {
                "success": True,
                "platform": "vm",
                "instance_id": "demo-vm-abc123",
                "reboot_initiated": True,
                "state": "running",
                "ssh_ready": True,
                "uptime_seconds": 30,
                "reboot_confirmed": True,
                "public_ip": args.public_ip,
                "key_file": args.key_file,
                "ssh_user": "ubuntu",
            }
        )
    else:
        client = BridgeClient.from_env()
        tenant_id = resolve_tenant_id(client, args.tenant)
        vm = get_vm(client, tenant_id, args.vm_id)
        mapped = to_isv_state(bridge_status(vm))
        if mapped != "running":
            raise RuntimeError(
                f"VM {args.vm_id} is {mapped!r} (Bridge status={bridge_status(vm)!r}), "
                "expected running before reboot"
            )

        public_ip = get_public_ip(vm) or args.public_ip
        ssh_user = str(vm.get("userName") or args.ssh_user)

        pre_uptime = get_uptime_via_ssh(public_ip, args.key_file, ssh_user)
        if pre_uptime is not None:
            result["pre_reboot_uptime"] = round(pre_uptime, 1)

        reboot_requested_at = time.time()
        power_action(client, tenant_id, args.vm_id, "reboot")
        result["reboot_initiated"] = True

        time.sleep(args.wait_before_check)
        vm = wait_for_vm_status(
            client,
            tenant_id,
            args.vm_id,
            target="running",
            label="reboot_instance",
            timeout=_POLL_TIMEOUT,
        )
        public_ip = get_public_ip(vm) or public_ip
        ssh_user = str(vm.get("userName") or ssh_user)
        wait_for_ssh(public_ip, args.key_file, username=ssh_user, timeout=_SSH_TIMEOUT)

        post_uptime = get_uptime_via_ssh(public_ip, args.key_file, ssh_user)
        if post_uptime is None:
            raise RuntimeError("Could not sample post-reboot uptime via SSH")

        result["uptime_seconds"] = round(post_uptime, 1)
        boot_started_at = time.time() - post_uptime
        if boot_started_at >= reboot_requested_at:
            reboot_confirmed = True
        elif pre_uptime is not None and post_uptime < pre_uptime:
            reboot_confirmed = True
        else:
            reboot_confirmed = False

        if not reboot_confirmed:
            raise RuntimeError(
                f"Reboot not confirmed (pre={pre_uptime}, post={post_uptime}, "
                f"boot_started_at={boot_started_at:.0f}, reboot_at={reboot_requested_at:.0f})"
            )

        result.update(
            {
                "success": True,
                "state": to_isv_state(bridge_status(vm)),
                "ssh_ready": True,
                "reboot_confirmed": True,
                "public_ip": public_ip,
                "key_file": args.key_file,
                "ssh_user": ssh_user,
            }
        )

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
