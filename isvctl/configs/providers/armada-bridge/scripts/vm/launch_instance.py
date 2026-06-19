#!/usr/bin/env python3
"""launch_instance — Armada Bridge VM suite, setup phase.

Flow (mirrors bare_metal launch topology branching):
  1. GET /orchestrator/network/topologies
     - All entries networkType "nonetwork" (or empty list) → import_flow
     - Otherwise → discovery_flow: create VPC + subnet, pass subnetIds on allocate

  2. POST /orchestrator/tenants/<tenant>/vms (multipart)
  3. Poll GET VM until running
  4. wait_for_ssh

Import flow (typical lab): all topologies nonetwork (or empty list);
vpc_id/security_group_id echo config placeholders (default "n/a").

Output: {success, platform, discovery_flow, instance_id, state, public_ip, private_ip,
         key_file, vpc_id, subnet_id, security_group_id, instance_type, ssh_user}
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
from common.network import is_discovery_flow, list_topologies, provision_discovery_network
from common.ssh_utils import wait_for_ssh
from common.tenant import resolve_tenant_id
from common.vm import (
    allocate_vm,
    extract_vm_id,
    find_vm_by_name,
    get_public_ip,
    list_vms,
    resolve_ssh_key,
    to_isv_state,
    wait_for_vm_status,
)

DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"
_POLL_TIMEOUT = 840  # vm.yaml launch step timeout is 900s


@handle_bridge_errors
def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--flavor", required=True)
    parser.add_argument("--vpc-id", default="")
    parser.add_argument("--security-group-id", default="")
    args = parser.parse_args()

    wiring_vpc_id = args.vpc_id or "n/a"

    result: dict[str, Any] = {
        "success": False,
        "platform": "vm",
        "vpc_id": wiring_vpc_id,
        "subnet_id": "",
        "security_group_id": args.security_group_id or "n/a",
        "discovery_flow": False,
    }

    if DEMO_MODE:
        result.update(
            {
                "success": True,
                "platform": "vm",
                "discovery_flow": False,
                "instance_id": "demo-vm-abc123",
                "state": "running",
                "public_ip": "203.0.113.10",
                "private_ip": "10.0.0.10",
                "key_file": "/tmp/demo-key.pem",
                "vpc_id": wiring_vpc_id,
                "subnet_id": "",
                "security_group_id": args.security_group_id or "n/a",
                "instance_type": args.flavor,
                "ssh_user": "ubuntu",
            }
        )
    else:
        client = BridgeClient.from_env()
        tenant_id = resolve_tenant_id(client, args.tenant)
        epoch = int(time.time())
        vm_name = f"{args.name}-{epoch}"

        topologies = list_topologies(client)
        discovery_flow = is_discovery_flow(topologies)
        result["discovery_flow"] = discovery_flow

        vpc_id = wiring_vpc_id
        subnet_id = ""
        subnet_ids: list[str] = []

        if discovery_flow:
            vpc_id, subnet_id, _topology = provision_discovery_network(
                client,
                tenant_id,
                epoch=epoch,
            )
            subnet_ids = [subnet_id]
            result["vpc_id"] = vpc_id
            result["subnet_id"] = subnet_id

        public_key, key_file = resolve_ssh_key(vm_name)

        vm_id = ""
        try:
            allocate_resp = allocate_vm(
                client,
                tenant_id,
                name=vm_name,
                flavor=args.flavor,
                public_key=public_key,
                subnet_ids=subnet_ids or None,
            )
            vm_id = extract_vm_id(allocate_resp)
        except ValueError as exc:
            if "status 409" not in str(exc):
                raise
            existing = find_vm_by_name(list_vms(client, tenant_id), vm_name)
            if existing is None:
                raise
            vm_id = extract_vm_id(existing)

        vm = wait_for_vm_status(
            client,
            tenant_id,
            vm_id,
            target="running",
            label="launch_instance",
            timeout=_POLL_TIMEOUT,
        )
        public_ip = get_public_ip(vm)
        if not public_ip:
            raise RuntimeError(f"VM {vm_id} is running but has no public IP")

        ssh_user = vm.get("userName") or "ubuntu"
        wait_for_ssh(public_ip, key_file, username=str(ssh_user))

        result.update(
            {
                "success": True,
                "instance_id": vm_id,
                "state": to_isv_state(vm.get("status") or vm.get("state") or "running"),
                "public_ip": public_ip,
                "private_ip": "",
                "key_file": key_file,
                "instance_type": args.flavor,
                "ssh_user": ssh_user,
            }
        )

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
