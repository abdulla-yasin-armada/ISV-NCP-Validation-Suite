"""Shared VM helpers for Armada Bridge provider scripts.

Used by scripts/vm/*.py to talk to the Bridge orchestrator VM API via BridgeClient.
Scripts print ISV-suite JSON to stdout; this module handles HTTP paths, polling,
status translation, SSH key material, and response parsing.

Bridge API (orchestrator prefix):
  GET    /orchestrator/tenants/{tenant}/vms
  POST   /orchestrator/tenants/{tenant}/vms          (multipart: name, vmSSHKey, flavor, osName)
  GET    /orchestrator/tenants/{tenant}/vms/{vmId}
  DELETE /orchestrator/tenants/{tenant}/vms/{vmId}
  POST   /orchestrator/tenants/{tenant}/vms/{vmId}/power/{on|off|reboot}

Status mapping: Bridge statuses (running, active, poweredOff, …) are normalized to
ISV contract values ("running", "stopped") via to_isv_state() before scripts emit JSON.

SSH keys (allocate + ConnectivityCheck):
  BRIDGE_SSH_KEY_FILE   — existing private key path (optional .pub sibling or BRIDGE_SSH_PUBLIC_KEY)
  BRIDGE_SSH_PUBLIC_KEY — public key text when using BRIDGE_SSH_KEY_FILE without a .pub file
  Default: generate/cache RSA key under ~/.cache/isvctl/vm-keys/{name}.pem

Other env:
  BRIDGE_VM_OS — OS image name for allocate (default: ubuntu-20.04-cuda-12.7)

Higher-level provisioning:
  provision_vm_nodes    End-to-end VM provisioning: optional VPC + allocate N VMs + poll
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from .bridge_client import BridgeClient
from .polling import poll_until

RUNNING_BRIDGE_STATUSES = frozenset({"running", "active"})
STOPPED_BRIDGE_STATUSES = frozenset({"poweredoff", "stopped", "shutdown", "shutoff"})
FAILED_BRIDGE_STATUSES = frozenset({"failed"})

_DEFAULT_OS_NAME = "ubuntu-20.04-cuda-12.7"
_KEY_CACHE_DIR = Path.home() / ".cache" / "isvctl" / "vm-keys"


def vm_path(tenant: str, vm_id: str | None = None) -> str:
    base = f"/orchestrator/tenants/{tenant}/vms"
    return f"{base}/{vm_id}" if vm_id else base


def bridge_status(vm: dict[str, Any]) -> str:
    return str(vm.get("status") or vm.get("state") or "")


def to_isv_state(status: str) -> str:
    normalized = status.strip().lower().replace(" ", "").replace("_", "")
    if normalized in RUNNING_BRIDGE_STATUSES:
        return "running"
    if normalized in STOPPED_BRIDGE_STATUSES:
        return "stopped"
    return status


def extract_vm_id(data: dict[str, Any] | list[Any]) -> str:
    """Parse VM id from allocate response (object or single-element list)."""
    vm: dict[str, Any]
    if isinstance(data, list):
        if not data:
            raise ValueError("Empty VM list in allocate response")
        first = data[0]
        if not isinstance(first, dict):
            raise ValueError(f"Expected VM object in allocate response, got {type(first).__name__}")
        vm = first
    else:
        vm = data
    for key in ("id", "ID", "vmId", "vmID"):
        value = vm.get(key)
        if value:
            return str(value)
    raise ValueError(f"No VM id in response: {vm!r}")


def get_public_ip(vm: dict[str, Any]) -> str:
    for key in ("publicIp", "externalIp", "public_ip"):
        value = vm.get(key)
        if value:
            return str(value)
    return ""


def resolve_ssh_key(name: str) -> tuple[str, str]:
    """Return (public_key_text, private_key_path) for VM allocate + SSH checks."""
    key_file_env = os.environ.get("BRIDGE_SSH_KEY_FILE")
    pub_env = os.environ.get("BRIDGE_SSH_PUBLIC_KEY")

    if key_file_env:
        key_file = Path(key_file_env).expanduser()
        if not key_file.exists():
            raise FileNotFoundError(f"BRIDGE_SSH_KEY_FILE not found: {key_file}")
        if pub_env:
            return pub_env.strip(), str(key_file)
        pub_file = key_file.with_suffix(key_file.suffix + ".pub")
        if pub_file.exists():
            return pub_file.read_text().strip(), str(key_file)
        derived = subprocess.run(
            ["ssh-keygen", "-y", "-f", str(key_file)],
            capture_output=True,
            text=True,
            check=True,
        )
        return derived.stdout.strip(), str(key_file)

    _KEY_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    key_file = _KEY_CACHE_DIR / f"{name}.pem"
    pub_file = _KEY_CACHE_DIR / f"{name}.pem.pub"
    if key_file.exists() and pub_file.exists():
        key_file.chmod(0o600)
        return pub_file.read_text().strip(), str(key_file)

    subprocess.run(
        [
            "ssh-keygen",
            "-t",
            "rsa",
            "-b",
            "4096",
            "-f",
            str(key_file),
            "-N",
            "",
            "-C",
            f"isvctl-{name}",
        ],
        check=True,
        capture_output=True,
    )
    key_file.chmod(0o600)
    return pub_file.read_text().strip(), str(key_file)


def get_vm(client: BridgeClient, tenant: str, vm_id: str) -> dict[str, Any]:
    resp = client.get(vm_path(tenant, vm_id))
    if not isinstance(resp, dict):
        raise ValueError(f"GET VM {vm_id}: expected JSON object, got {type(resp).__name__}")
    return resp


def find_vm_by_name(vms: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    """Return the VM dict whose name matches, or None."""
    for vm in vms:
        if vm.get("name") == name:
            return vm
    return None


def list_vms(client: BridgeClient, tenant: str) -> list[dict[str, Any]]:
    """GET tenant VMs; unwrap list or {vms|items|data: [...]} response shapes."""
    resp = client.get(vm_path(tenant))
    if isinstance(resp, list):
        return resp
    if isinstance(resp, dict):
        for key in ("vms", "items", "data"):
            items = resp.get(key)
            if isinstance(items, list):
                return items
    return []


def vm_to_instance(vm: dict[str, Any], *, vpc_id: str) -> dict[str, Any]:
    """Map a Bridge VM dict to an ISV list_instances entry."""
    resolved_vpc = vpc_id or "n/a"
    subnets = vm.get("subnets") or []
    if subnets and isinstance(subnets[0], dict):
        parent = subnets[0].get("parentVpcID") or subnets[0].get("parentVpcId")
        if parent:
            resolved_vpc = str(parent)
    return {
        "instance_id": extract_vm_id(vm),
        "state": to_isv_state(bridge_status(vm)),
        "vpc_id": resolved_vpc,
    }


def wait_for_vm_status(
    client: BridgeClient,
    tenant: str,
    vm_id: str,
    *,
    target: str,
    label: str,
    timeout: int = 600,
    interval: int = 15,
) -> dict[str, Any]:
    """Poll GET VM until Bridge status maps to target ISV state."""

    def check() -> tuple[bool, dict[str, Any] | None, str]:
        vm = get_vm(client, tenant, vm_id)
        status = bridge_status(vm)
        mapped = to_isv_state(status)
        if status.lower() in FAILED_BRIDGE_STATUSES:
            raise RuntimeError(f"VM {vm_id} entered failed state: {status!r}")
        if mapped == target:
            return True, vm, f"status={status!r} -> {mapped!r}"
        if target == "running" and status.lower() in RUNNING_BRIDGE_STATUSES:
            return True, vm, f"status={status!r} -> running"
        if target == "stopped" and status.lower() in STOPPED_BRIDGE_STATUSES:
            return True, vm, f"status={status!r} -> stopped"
        return False, None, f"status={status!r} (want {target!r})"

    return poll_until(check, label=label, interval=interval, timeout=timeout)


def wait_for_vm_deleted(
    client: BridgeClient,
    tenant: str,
    vm_id: str,
    *,
    timeout: int = 600,
    interval: int = 15,
) -> None:
    """Poll GET VM until Bridge returns 404 (delete complete)."""
    def check() -> tuple[bool, None, str]:
        try:
            get_vm(client, tenant, vm_id)
        except ValueError as exc:
            if "404" in str(exc):
                return True, None, "VM not found (deleted)"
            raise
        return False, None, "VM still exists"

    poll_until(check, label="teardown", interval=interval, timeout=timeout)


def power_action(client: BridgeClient, tenant: str, vm_id: str, action: str) -> dict[str, Any]:
    """POST /power/{on|off|reboot} for the given VM."""
    if action not in {"on", "off", "reboot"}:
        raise ValueError(f"Invalid power action: {action!r}")
    return client.post(vm_path(tenant, vm_id) + f"/power/{action}")


def provision_vm_nodes(
    client: BridgeClient,
    tenant_id: str,
    *,
    epoch: int,
    discovery_flow: bool,
    vm_flavor: str,
    count: int = 1,
    name_prefix: str = "isv-vm-node",
    poll_timeout: int = 840,
) -> tuple[list[str], str, str]:
    """Allocate ``count`` VM nodes, provisioning a compute VPC when needed.

    Returns:
        (node_ids, vpc_id, subnet_id)

    Discovery flow: creates one compute VPC+subnet shared across all VMs.
    Import flow: allocates VMs without subnet IDs.
    VM names: ``{name_prefix}-{epoch}`` (count=1) or ``{name_prefix}-{epoch}-{i}`` (count>1).
    """
    # Lazy import to avoid circular dependency at module load time.
    from .network import list_topologies
    from .vpc import create_subnet, create_vpc, pick_compute_topology

    vpc_id = "n/a"
    subnet_id = ""
    subnet_ids: list[str] = []

    if discovery_flow:
        topologies = list_topologies(client)
        topo = pick_compute_topology(topologies)
        topo_name = str(topo.get("topology", "") or topo.get("id", ""))
        vpc_id = create_vpc(client, tenant_id, topo_name, f"{name_prefix}-vpc-{epoch}")
        subnet_id = create_subnet(
            client, tenant_id, vpc_id, topo_name,
            f"{name_prefix}-subnet-{epoch}", "10.200.0.0/24",
        )
        subnet_ids = [subnet_id]

    node_ids: list[str] = []
    for i in range(count):
        vm_name = f"{name_prefix}-{epoch}" if count == 1 else f"{name_prefix}-{epoch}-{i}"
        public_key, _key_file = resolve_ssh_key(vm_name)

        try:
            allocate_resp = allocate_vm(
                client, tenant_id,
                name=vm_name, flavor=vm_flavor,
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

        wait_for_vm_status(
            client, tenant_id, vm_id,
            target="running",
            label=f"{name_prefix}_vm_{i}",
            timeout=poll_timeout,
        )
        node_ids.append(vm_id)

    return node_ids, vpc_id, subnet_id


def allocate_vm(
    client: BridgeClient,
    tenant: str,
    *,
    name: str,
    flavor: str,
    public_key: str,
    os_name: str | None = None,
    subnet_ids: list[str] | None = None,
    region: str | None = None,
) -> dict[str, Any]:
    """POST multipart allocate (name, vmSSHKey, flavor, osName) for a new VM."""
    fields: dict[str, str | list[str]] = {
        "name": name,
        "vmSSHKey": public_key,
        "flavor": flavor,
        "osName": os_name or os.environ.get("BRIDGE_VM_OS", _DEFAULT_OS_NAME),
    }
    if region or os.environ.get("BRIDGE_VM_REGION"):
        fields["region"] = region or os.environ.get("BRIDGE_VM_REGION", "")
    if subnet_ids:
        fields["subnetIds"] = subnet_ids
    print(f"Allocating VM {name!r} (flavor={flavor!r})", file=sys.stderr)
    return client.post_multipart(vm_path(tenant), fields)
