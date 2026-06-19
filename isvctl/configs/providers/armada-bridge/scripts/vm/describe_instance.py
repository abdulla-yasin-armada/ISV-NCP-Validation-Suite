#!/usr/bin/env python3
"""describe_instance — Armada Bridge VM suite, test phase.

Retrieves current VM state via:
  GET /orchestrator/tenants/<tenant>/vms/<vm_id>

Runs after stop/start/reboot so downstream SSH validations (GPU, driver,
pinning, docker, etc.) prove the host survived the full lifecycle.
deploy_nim wires public_ip and key_file from this step.

Validations: InstanceStateCheck, ConnectivityCheck, OsCheck, GpuCheck,
VcpuPinningCheck, PciBusCheck, HostSoftwareCheck, DriverCheck, CpuInfoCheck,
ContainerRuntimeCheck (instance_id, state: running, public_ip, key_file).

Output: {success, platform, instance_id, state, public_ip, key_file, ssh_user}
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
from common.vm import bridge_status, extract_vm_id, get_public_ip, get_vm, to_isv_state

DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"


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
    }

    if DEMO_MODE:
        result.update(
            {
                "success": True,
                "platform": "vm",
                "instance_id": "demo-vm-abc123",
                "state": "running",
                "public_ip": "203.0.113.10",
                "key_file": args.key_file,
                "ssh_user": "ubuntu",
            }
        )
    else:
        client = BridgeClient.from_env()
        tenant_id = resolve_tenant_id(client, args.tenant)
        vm = get_vm(client, tenant_id, args.vm_id)

        state = to_isv_state(bridge_status(vm))
        if state != "running":
            raise RuntimeError(
                f"VM {args.vm_id} is {state!r} (Bridge status={bridge_status(vm)!r}), "
                "expected running"
            )

        public_ip = get_public_ip(vm)
        if not public_ip:
            raise RuntimeError(f"VM {args.vm_id} is running but has no public IP")

        ssh_user = str(vm.get("userName") or args.ssh_user)
        result.update(
            {
                "success": True,
                "instance_id": extract_vm_id(vm),
                "state": state,
                "public_ip": public_ip,
                "key_file": args.key_file,
                "ssh_user": ssh_user,
            }
        )

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
