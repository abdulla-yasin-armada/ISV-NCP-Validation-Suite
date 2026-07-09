"""Shared VM helpers for Armada Bridge provider scripts.

Used by scripts/vm/*.py to talk to the Bridge orchestrator VM API via BridgeClient.
Scripts print ISV-suite JSON to stdout; this module handles HTTP paths, polling,
status translation, SSH key material, and response parsing.

Bridge API (orchestrator prefix):
  GET    /orchestrator/tenants/{tenant}/vms
  GET    /orchestrator/tenants/{tenant}/vms/flavours
  POST   /orchestrator/tenants/{tenant}/vms          (JSON: flavorTemplateId, osName, vmSSHKey|sshKeyId)
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
  BRIDGE_VM_COUNT — number of VMs to allocate in launch/teardown (default: 1)
  BRIDGE_VM_FLAVOR_TEMPLATE_ID — explicit flavour template UUID (fallback when auto-pick fails)
  BRIDGE_VM_FLAVOR — flavour template UUID or name hint (fallback after template ID)
  BRIDGE_VM_GPU_TYPE — optional filter when auto-discovering flavours
  BRIDGE_VM_FLAVOR_STRICT — when 1/true, env flavour vars win before auto-discover (legacy)

Higher-level provisioning:
  provision_vm_nodes    End-to-end VM provisioning: optional VPC + allocate N VMs + poll
"""
from __future__ import annotations

import os
import re
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
_DEFAULT_USERNAME = "ubuntu"
_DEFAULT_PASSTHROUGH_TYPE = "full"
_KEY_CACHE_DIR = Path.home() / ".cache" / "isvctl" / "vm-keys"
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


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


def _unwrap_flavour_list(resp: Any) -> list[dict[str, Any]]:
    if isinstance(resp, list):
        return [item for item in resp if isinstance(item, dict)]
    if isinstance(resp, dict):
        for key in ("flavours", "flavors", "items", "data"):
            items = resp.get(key)
            if isinstance(items, list):
                return [item for item in items if isinstance(item, dict)]
    return []


def list_vm_flavours(client: BridgeClient, tenant: str) -> list[dict[str, Any]]:
    """GET tenant VM flavour templates (orchestrator-ms / vm.js: GET .../vms/flavours)."""
    resp = client.get(f"/orchestrator/tenants/{tenant}/vms/flavours")
    return _unwrap_flavour_list(resp)


def _flavour_available_count(flavour: dict[str, Any]) -> int:
    try:
        return int(flavour.get("available", 0))
    except (TypeError, ValueError):
        return 0


def _flavour_gpu_count(flavour: dict[str, Any]) -> int:
    for key in ("gpuCount", "gpu_count"):
        value = flavour.get(key)
        if value is not None:
            try:
                return int(value)
            except (TypeError, ValueError):
                return 0
    return 0


def _flavour_id(flavour: dict[str, Any]) -> str:
    for key in ("id", "ID", "flavorTemplateId", "flavourId"):
        value = flavour.get(key)
        if value:
            return str(value)
    return ""


def _flavour_name(flavour: dict[str, Any]) -> str:
    return str(flavour.get("name") or "")


def looks_like_uuid(value: str) -> bool:
    return bool(_UUID_RE.match(value.strip()))


def _flavor_strict_mode() -> bool:
    return os.environ.get("BRIDGE_VM_FLAVOR_STRICT", "").strip().lower() in {"1", "true", "yes"}


def pick_gpu_flavour(
    flavours: list[dict[str, Any]],
    *,
    explicit_id: str = "",
    explicit_name: str = "",
    gpu_type: str = "",
    min_available: int = 1,
    exact_gpu_count: int | None = None,
) -> dict[str, Any] | None:
    """Pick a GPU flavour with capacity, mirroring bridge-api-test-automation vm.js."""
    explicit_id = explicit_id.strip()
    explicit_name = explicit_name.strip()
    gpu_type = gpu_type.strip().lower()
    min_available = max(1, min_available)

    for flavour in flavours:
        flavour_id = _flavour_id(flavour)
        flavour_name = _flavour_name(flavour)
        if explicit_id and flavour_id != explicit_id:
            continue
        if explicit_name and flavour_name != explicit_name:
            continue
        if gpu_type:
            flavour_gpu = str(flavour.get("gpuType") or flavour.get("gpu_type") or "").lower()
            if flavour_gpu and flavour_gpu != gpu_type:
                continue
        if _flavour_available_count(flavour) < min_available:
            continue
        gpu_count = _flavour_gpu_count(flavour)
        if exact_gpu_count is not None:
            if gpu_count != exact_gpu_count:
                continue
        elif gpu_count < 1:
            continue
        return flavour
    return None


def _label_for_flavour_id(flavours: list[dict[str, Any]], flavour_id: str) -> str:
    for flavour in flavours:
        if _flavour_id(flavour) == flavour_id:
            return _flavour_name(flavour) or flavour_id
    return flavour_id


def _resolve_from_env_fallback(
    flavours: list[dict[str, Any]],
    *,
    explicit_id: str,
    hint: str,
    gpu_type: str,
    vm_count: int,
) -> tuple[str, str] | None:
    """Resolve flavour from env vars when auto-discover did not match."""
    pick_kwargs = {"gpu_type": gpu_type, "min_available": vm_count}

    if explicit_id:
        matched = pick_gpu_flavour(flavours, explicit_id=explicit_id, **pick_kwargs) if flavours else None
        label = _flavour_name(matched) if matched else _label_for_flavour_id(flavours, explicit_id)
        print(
            f"[vm] Fallback BRIDGE_VM_FLAVOR_TEMPLATE_ID={explicit_id!r} ({label})",
            file=sys.stderr,
        )
        return explicit_id, label or explicit_id

    if hint and looks_like_uuid(hint):
        matched = pick_gpu_flavour(flavours, explicit_id=hint, **pick_kwargs) if flavours else None
        label = _flavour_name(matched) if matched else hint
        print(f"[vm] Fallback BRIDGE_VM_FLAVOR UUID {hint!r} ({label})", file=sys.stderr)
        return hint, label or hint

    if hint:
        if not flavours:
            raise RuntimeError(
                f"BRIDGE_VM_FLAVOR={hint!r} set but GET .../vms/flavours returned no entries."
            )
        matched = pick_gpu_flavour(flavours, explicit_name=hint, **pick_kwargs)
        if matched:
            flavour_id = _flavour_id(matched)
            flavour_name = _flavour_name(matched) or flavour_id
            print(
                f"[vm] Fallback matched flavour name {hint!r} -> {flavour_name} ({flavour_id})",
                file=sys.stderr,
            )
            return flavour_id, flavour_name
        raise RuntimeError(
            f"BRIDGE_VM_FLAVOR={hint!r} did not match any GPU flavour with "
            f"available>={vm_count} (checked {len(flavours)} entries)."
        )

    return None


def _parse_gpu_count_hint(hint: str) -> int | None:
    """Parse GPU count from shorthand hints like '1x', '2x', 'gpu.2x', '2xH100' → int.

    Returns None if the hint doesn't contain a recognisable NxGPU pattern.
    """
    import re
    m = re.match(r".*?(\d+)[xX]", hint.strip())
    return int(m.group(1)) if m else None


def resolve_vm_flavor_template_id(
    client: BridgeClient,
    tenant: str,
    flavor_hint: str = "",
    *,
    vm_count: int = 1,
) -> tuple[str, str]:
    """Resolve flavourTemplateId for VM allocate.

    Default priority:
      1. Auto-discover 1-GPU flavour with available >= vm_count
      2. BRIDGE_VM_FLAVOR_TEMPLATE_ID
      3. BRIDGE_VM_FLAVOR (UUID or name via GET flavours)

    When BRIDGE_VM_FLAVOR_STRICT=1, env vars win first (legacy option C order).
    """
    vm_count = max(1, vm_count)
    explicit = os.environ.get("BRIDGE_VM_FLAVOR_TEMPLATE_ID", "").strip()
    hint = (flavor_hint or os.environ.get("BRIDGE_VM_FLAVOR", "")).strip()
    gpu_type = os.environ.get("BRIDGE_VM_GPU_TYPE", "").strip()
    strict = _flavor_strict_mode()

    if strict and explicit:
        print(f"[vm] Using BRIDGE_VM_FLAVOR_TEMPLATE_ID={explicit!r} (strict)", file=sys.stderr)
        return explicit, explicit

    flavours = list_vm_flavours(client, tenant)

    if strict:
        if not flavours:
            if hint and looks_like_uuid(hint):
                print(f"[vm] No flavours listed; using hint UUID {hint!r}", file=sys.stderr)
                return hint, hint
            raise RuntimeError(
                "No VM flavours returned from GET /orchestrator/tenants/{tenant}/vms/flavours. "
                "Set BRIDGE_VM_FLAVOR_TEMPLATE_ID or configure tenant VM pricing/quota first."
            )
        if hint and looks_like_uuid(hint):
            matched = pick_gpu_flavour(flavours, explicit_id=hint, min_available=vm_count)
            label = _flavour_name(matched) if matched else hint
            print(f"[vm] Using flavour template UUID {hint!r} ({label})", file=sys.stderr)
            return hint, label or hint
        if hint:
            matched = pick_gpu_flavour(
                flavours, explicit_name=hint, gpu_type=gpu_type, min_available=vm_count,
            )
            if matched:
                flavour_id = _flavour_id(matched)
                flavour_name = _flavour_name(matched) or flavour_id
                print(f"[vm] Matched flavour name {hint!r} -> {flavour_name} ({flavour_id})", file=sys.stderr)
                return flavour_id, flavour_name
            raise RuntimeError(
                f"BRIDGE_VM_FLAVOR={hint!r} did not match any available GPU flavour "
                f"(checked {len(flavours)} entries from GET .../vms/flavours)."
            )
        matched = pick_gpu_flavour(flavours, gpu_type=gpu_type, min_available=vm_count)
        if not matched:
            raise RuntimeError(
                f"No eligible GPU VM flavour found (need available>={vm_count} and gpuCount>=1). "
                "Set BRIDGE_VM_FLAVOR_TEMPLATE_ID or BRIDGE_VM_FLAVOR."
            )
        flavour_id = _flavour_id(matched)
        flavour_name = _flavour_name(matched) or flavour_id
        print(
            f"[vm] Auto-selected flavour {flavour_name!r} ({flavour_id}) "
            f"(available={_flavour_available_count(matched)}, gpuCount={_flavour_gpu_count(matched)})",
            file=sys.stderr,
        )
        return flavour_id, flavour_name

    if flavours:
        # If a hint was given, try to honour it before falling back to 1-GPU auto-select.
        # "1x" → exact_gpu_count=1, "2x" → exact_gpu_count=2, UUID/name → exact match.
        if hint and not looks_like_uuid(hint):
            matched = pick_gpu_flavour(
                flavours, explicit_name=hint, gpu_type=gpu_type, min_available=vm_count,
            )
            if not matched:
                gpu_count_from_hint = _parse_gpu_count_hint(hint)
                if gpu_count_from_hint is not None:
                    matched = pick_gpu_flavour(
                        flavours, gpu_type=gpu_type, min_available=vm_count,
                        exact_gpu_count=gpu_count_from_hint,
                    )
            if matched:
                flavour_id = _flavour_id(matched)
                flavour_name = _flavour_name(matched) or flavour_id
                print(
                    f"[vm] Matched hint {hint!r} → {flavour_name} ({flavour_id}) "
                    f"(available={_flavour_available_count(matched)}, gpuCount={_flavour_gpu_count(matched)})",
                    file=sys.stderr,
                )
                return flavour_id, flavour_name

        matched = pick_gpu_flavour(
            flavours,
            gpu_type=gpu_type,
            min_available=vm_count,
            exact_gpu_count=1,
        )
        if matched:
            flavour_id = _flavour_id(matched)
            flavour_name = _flavour_name(matched) or flavour_id
            print(
                f"[vm] Auto-selected 1-GPU flavour {flavour_name!r} ({flavour_id}) "
                f"(available={_flavour_available_count(matched)}, need>={vm_count})",
                file=sys.stderr,
            )
            return flavour_id, flavour_name

    fallback = _resolve_from_env_fallback(
        flavours,
        explicit_id=explicit,
        hint=hint,
        gpu_type=gpu_type,
        vm_count=vm_count,
    )
    if fallback:
        return fallback

    if not flavours and not explicit and not hint:
        raise RuntimeError(
            "No VM flavours returned from GET /orchestrator/tenants/{tenant}/vms/flavours "
            f"and no BRIDGE_VM_FLAVOR_TEMPLATE_ID / BRIDGE_VM_FLAVOR set "
            f"(vm_count={vm_count})."
        )

    raise RuntimeError(
        f"No eligible 1-GPU VM flavour with available>={vm_count} and no env fallback configured. "
        "Set BRIDGE_VM_FLAVOR_TEMPLATE_ID or BRIDGE_VM_FLAVOR."
    )


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
    vm_flavor: str = "",
    vm_flavors: list[str] | None = None,
    count: int = 1,
    name_prefix: str = "isv-vm-node",
    poll_timeout: int = 840,
    shared_key_name: str = "",
) -> tuple[list[str], str, str]:
    """Allocate ``count`` VM nodes, provisioning a compute VPC when needed.

    ``vm_flavors`` (list) takes precedence over ``vm_flavor`` (single string).
    If ``vm_flavors`` has fewer entries than ``count``, the last entry is reused
    for remaining nodes. If neither is set, auto-discovery picks the first
    available 1-GPU flavor.

    ``shared_key_name``: when set, all VMs in this batch share the same SSH key
    (required for Slurm clusters where the master must SSH to workers).

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

    # Build the per-node flavor list. vm_flavors takes precedence over vm_flavor.
    _flavors: list[str] = vm_flavors if vm_flavors else ([vm_flavor] if vm_flavor else [""])

    node_ids: list[str] = []
    _flavor_cache: dict[str, str] = {}  # flavor hint → resolved template ID

    for i in range(count):
        flavor_hint = _flavors[min(i, len(_flavors) - 1)]
        if flavor_hint not in _flavor_cache:
            flavor_template_id, flavor_label = resolve_vm_flavor_template_id(
                client, tenant_id, flavor_hint, vm_count=1,
            )
            _flavor_cache[flavor_hint] = flavor_template_id
            print(
                f"[provision_vm_nodes] node {i}: flavor={flavor_hint!r} → {flavor_label} ({flavor_template_id})",
                file=sys.stderr,
            )
        flavor_template_id = _flavor_cache[flavor_hint]
        vm_name = f"{name_prefix}-{epoch}" if count == 1 else f"{name_prefix}-{epoch}-{i}"
        key_name = shared_key_name or vm_name
        public_key, _key_file = resolve_ssh_key(key_name)

        try:
            allocate_resp = allocate_vm(
                client, tenant_id,
                name=vm_name,
                flavor_template_id=flavor_template_id,
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
    flavor_template_id: str,
    public_key: str | None = None,
    ssh_key_id: str | None = None,
    os_name: str | None = None,
    username: str | None = None,
    passthrough_type: str | None = None,
    subnet_ids: list[str] | None = None,
    region: str | None = None,
    infini_band_passthrough: bool = False,
) -> dict[str, Any]:
    """POST JSON allocate (flavorTemplateId, osName, vmSSHKey|sshKeyId) for a new VM."""
    if not flavor_template_id:
        raise ValueError("flavor_template_id is required for VM allocate")
    if not public_key and not ssh_key_id:
        raise ValueError("allocate_vm requires public_key (vmSSHKey) or ssh_key_id")

    body: dict[str, Any] = {
        "name": name,
        "flavorTemplateId": flavor_template_id,
        "osName": os_name or os.environ.get("BRIDGE_VM_OS", _DEFAULT_OS_NAME),
        "username": username or os.environ.get("BRIDGE_VM_USERNAME", _DEFAULT_USERNAME),
        "passthroughType": passthrough_type or os.environ.get(
            "BRIDGE_VM_PASSTHROUGH_TYPE", _DEFAULT_PASSTHROUGH_TYPE,
        ),
        "infiniBandPassthrough": infini_band_passthrough,
        "tenantId": tenant,
    }
    if ssh_key_id:
        body["sshKeyId"] = ssh_key_id
    else:
        body["vmSSHKey"] = public_key
    region_val = region or os.environ.get("BRIDGE_VM_REGION", "").strip()
    if region_val:
        body["region"] = region_val
    if subnet_ids:
        body["subnetIds"] = subnet_ids

    print(
        f"Allocating VM {name!r} (flavorTemplateId={flavor_template_id!r})",
        file=sys.stderr,
    )
    return client.post(vm_path(tenant), body, timeout=120)
