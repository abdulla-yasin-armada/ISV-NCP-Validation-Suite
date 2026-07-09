#!/usr/bin/env python3
"""launch_instance — Armada Bridge VM suite, setup phase.

Aligned with bridge-api-test-automation ``vm.js`` / ai-studio Postman allocate:
  POST JSON ``/orchestrator/tenants/{tenant}/vms`` with name, flavorTemplateId,
  osName, username, passthroughType, vmSSHKey|sshKeyId — no VPC/SG CLI inputs.

Optional discovery flow: when topologies require networking, creates VPC+subnet
and passes subnetIds on allocate (same as k8s discovery path).

Env (read directly — not passed via vm.yaml args):
  BRIDGE_TENANT, BRIDGE_VM_COUNT, BRIDGE_VM_FLAVOR*, BRIDGE_URL, BRIDGE_USERNAME,
  BRIDGE_PASSWORD, BRIDGE_SSH_KEY_FILE, ...

Output: {success, platform, discovery_flow, instance_id, instance_ids, count,
         state, public_ip, private_ip, key_file, vpc_id, subnet_id,
         security_group_id, instance_type, ssh_user}
         vpc_id is ``n/a`` on import/nonetwork labs (ISV contract only).
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
from common.context import print_run_context
from common.errors import handle_bridge_errors
from common.network import is_discovery_flow, list_topologies, provision_discovery_network
from common.ssh_utils import wait_for_ssh
from common.tenant import resolve_tenant_id
from common.vm import (
    allocate_vm,
    extract_vm_id,
    find_vm_by_name,
    get_public_ip,
    get_vm,
    list_vms,
    resolve_ssh_key,
    resolve_vm_flavor_template_id,
    to_isv_state,
    wait_for_vm_status,
)

DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"
_POLL_TIMEOUT = 840  # vm.yaml launch step timeout is 900s
_SSH_TIMEOUT = 600   # 10 min; VM reports 'running' before SSH service is ready


def _tenant_from_env() -> str:
    tenant = os.environ.get("BRIDGE_TENANT", "").strip()
    if not tenant:
        raise RuntimeError(
            "BRIDGE_TENANT is not set. "
            "export BRIDGE_TENANT=<tenant-name-or-uuid> before running the VM suite."
        )
    return tenant


def _vm_count_from_env() -> int:
    raw = os.environ.get("BRIDGE_VM_COUNT", "1").strip()
    try:
        count = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"BRIDGE_VM_COUNT must be an integer, got {raw!r}") from exc
    if count < 1:
        raise RuntimeError(f"BRIDGE_VM_COUNT must be >= 1, got {count}")
    return count


def _vm_names(prefix: str, epoch: int, count: int) -> list[str]:
    if count == 1:
        return [f"{prefix}-{epoch}"]
    return [f"{prefix}-{epoch}-{i}" for i in range(count)]


def _allocate_one_vm(
    client: BridgeClient,
    tenant_id: str,
    *,
    vm_name: str,
    flavor_template_id: str,
    public_key: str,
    subnet_ids: list[str] | None,
    label: str,
) -> str:
    try:
        allocate_resp = allocate_vm(
            client,
            tenant_id,
            name=vm_name,
            flavor_template_id=flavor_template_id,
            public_key=public_key,
            subnet_ids=subnet_ids,
        )
        vm_id = extract_vm_id(allocate_resp)
    except ValueError as exc:
        if "status 409" not in str(exc):
            raise
        existing = find_vm_by_name(list_vms(client, tenant_id), vm_name)
        if existing is None:
            raise
        vm_id = extract_vm_id(existing)

    wait_for_vm_status(
        client,
        tenant_id,
        vm_id,
        target="running",
        label=label,
        timeout=_POLL_TIMEOUT,
    )
    return vm_id


@handle_bridge_errors
def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tenant", default="", help="Override BRIDGE_TENANT env")
    parser.add_argument("--name", required=True)
    parser.add_argument("--count", type=int, default=0, help="Override BRIDGE_VM_COUNT env")
    args = parser.parse_args()

    tenant = args.tenant.strip() or _tenant_from_env()
    vm_count = args.count if args.count > 0 else _vm_count_from_env()
    flavor_hint = os.environ.get("BRIDGE_VM_FLAVOR", "").strip()

    print_run_context("VM", {
        "BRIDGE_VM_COUNT"  : str(vm_count),
        "BRIDGE_VM_FLAVOR" : flavor_hint or "(auto-discover 1x GPU)",
    })
    wiring_vpc_id = "n/a"

    result: dict[str, Any] = {
        "success": False,
        "platform": "vm",
        "count": vm_count,
        "instance_ids": [],
        "vpc_id": wiring_vpc_id,
        "subnet_id": "",
        "security_group_id": "n/a",
        "discovery_flow": False,
    }

    if DEMO_MODE:
        demo_ids = [f"demo-vm-abc{i}" for i in range(vm_count)]
        result.update(
            {
                "success": True,
                "platform": "vm",
                "discovery_flow": False,
                "instance_id": demo_ids[0],
                "instance_ids": demo_ids,
                "count": vm_count,
                "state": "running",
                "public_ip": "203.0.113.10",
                "private_ip": "10.0.0.10",
                "key_file": "/tmp/demo-key.pem",
                "vpc_id": wiring_vpc_id,
                "subnet_id": "",
                "security_group_id": "n/a",
                "instance_type": flavor_hint or "auto",
                "ssh_user": "ubuntu",
            }
        )
    else:
        client = BridgeClient.from_env()
        tenant_id = resolve_tenant_id(client, tenant)
        epoch = int(time.time())
        vm_names = _vm_names(args.name, epoch, vm_count)

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

        public_key, key_file = resolve_ssh_key(vm_names[0])
        flavor_template_id, flavor_label = resolve_vm_flavor_template_id(
            client, tenant_id, flavor_hint, vm_count=vm_count,
        )

        instance_ids: list[str] = []
        for i, vm_name in enumerate(vm_names):
            vm_id = _allocate_one_vm(
                client,
                tenant_id,
                vm_name=vm_name,
                flavor_template_id=flavor_template_id,
                public_key=public_key,
                subnet_ids=subnet_ids or None,
                label=f"launch_instance_{i}",
            )
            instance_ids.append(vm_id)

        primary_vm = get_vm(client, tenant_id, instance_ids[0])
        public_ip = get_public_ip(primary_vm)
        if not public_ip:
            raise RuntimeError(f"VM {instance_ids[0]} is running but has no public IP")

        ssh_user = primary_vm.get("userName") or "ubuntu"
        wait_for_ssh(public_ip, key_file, username=str(ssh_user), timeout=_SSH_TIMEOUT)

        result.update(
            {
                "success": True,
                "instance_id": instance_ids[0],
                "instance_ids": instance_ids,
                "count": vm_count,
                "state": to_isv_state(primary_vm.get("status") or primary_vm.get("state") or "running"),
                "public_ip": public_ip,
                "private_ip": "",
                "key_file": key_file,
                "instance_type": flavor_label,
                "flavor_template_id": flavor_template_id,
                "ssh_user": ssh_user,
            }
        )

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
