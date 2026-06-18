"""Slurm CLI access helpers — local inventory and optional SSH wrappers."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from .bridge_client import BridgeClient
from .ssh_utils import wait_for_ssh
from .vm import get_public_ip, get_vm

_DEFAULT_SLURM_BIN = Path.home() / ".cache" / "isvctl" / "slurm-bin"
_DEFAULT_SLURM_CONF = Path.home() / ".cache" / "isvctl" / "bridge-slurm.conf"
_SLURM_COMMANDS = ("sinfo", "srun", "sbatch", "scontrol", "squeue", "scancel")


def default_slurm_bin_dir() -> Path:
    return _DEFAULT_SLURM_BIN


def default_slurm_conf_path() -> Path:
    return _DEFAULT_SLURM_CONF


def resolve_cli_mode() -> str:
    """Return local, ssh, or auto (default)."""
    mode = os.environ.get("BRIDGE_SLURM_CLI_MODE", "auto").strip().lower()
    if mode in {"local", "ssh"}:
        return mode
    return "auto"


def resolve_ssh_user(*, node_type: str) -> str:
    # Both VM and BM nodes on Bridge use 'ubuntu'. The node_type parameter is
    # kept for forward-compatibility in case BM nodes ever require a different user.
    return (
        os.environ.get("BRIDGE_SLURM_SSH_USER")
        or os.environ.get("BRIDGE_SSH_USER")
        or "ubuntu"
    ).strip()


def resolve_node_host(
    client: BridgeClient,
    tenant_id: str,
    node_id: str,
    *,
    node_type: str,
) -> str:
    """Resolve SSH-reachable host for a VM or bare-metal node."""
    override = os.environ.get("BRIDGE_SLURM_HEAD_HOST", "").strip()
    if override:
        return override

    if node_type == "vm":
        vm = get_vm(client, tenant_id, node_id)
        return get_public_ip(vm)

    node = client.get(f"/orchestrator/tenants/{tenant_id}/metal/computes/{node_id}")
    if not isinstance(node, dict):
        raise ValueError(f"Unexpected BM node response for {node_id}")
    host = node.get("externalIPAddress") or node.get("inBandIP") or ""
    return str(host or "")


def _ssh_base_args(key_file: str, host: str, username: str) -> list[str]:
    return [
        "ssh",
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "ConnectTimeout=15",
        "-o",
        "BatchMode=yes",
        "-i",
        key_file,
        f"{username}@{host}",
    ]


def _scp_base_args(key_file: str, host: str, username: str) -> list[str]:
    return [
        "scp",
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "ConnectTimeout=15",
        "-o",
        "BatchMode=yes",
        "-i",
        key_file,
    ]


def fetch_remote_slurm_conf(
    *,
    host: str,
    username: str,
    key_file: str,
    dest: Path,
    remote_path: str | None = None,
) -> Path:
    """Copy slurm.conf from head node to local dest for slurm-client use."""
    remote = remote_path or os.environ.get("BRIDGE_SLURM_REMOTE_CONF", "/etc/slurm/slurm.conf")
    dest.parent.mkdir(parents=True, exist_ok=True)
    scp_args = _scp_base_args(key_file, host, username) + [
        f"{username}@{host}:{remote}",
        str(dest),
    ]
    result = subprocess.run(scp_args, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(
            f"Failed to copy {remote} from {host}: {result.stderr.strip() or result.stdout.strip()}"
        )
    dest.chmod(0o600)
    return dest


def install_ssh_wrappers(
    bin_dir: Path,
    *,
    host: str,
    username: str,
    key_file: str,
) -> Path:
    """Install sinfo/srun/sbatch/... wrappers that SSH to the Slurm head node."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    ssh_cmd = " ".join(_ssh_base_args(key_file, host, username))
    scp_cmd = " ".join(_scp_base_args(key_file, host, username))

    for name in _SLURM_COMMANDS:
        path = bin_dir / name
        if name == "sbatch":
            script = f"""#!/usr/bin/env bash
set -euo pipefail
if [[ $# -ge 1 && -f "$1" ]]; then
  remote="/tmp/isvctl-sbatch-$(basename "$1")"
  {scp_cmd} "$1" "{username}@{host}:$remote"
  exec {ssh_cmd} sbatch "$remote" "${{@:2}}"
fi
exec {ssh_cmd} sbatch "$@"
"""
        else:
            script = f"""#!/usr/bin/env bash
set -euo pipefail
exec {ssh_cmd} {name} "$@"
"""
        path.write_text(script)
        path.chmod(0o755)

    return bin_dir


def configure_slurm_cli(
    *,
    client: BridgeClient,
    tenant_id: str,
    master_node_id: str,
    node_type: str,
    key_file: str,
) -> tuple[str, str | None, str | None]:
    """Configure local or SSH Slurm CLI. Returns (mode, slurm_bin_path, slurm_conf_path)."""
    mode = resolve_cli_mode()
    username = resolve_ssh_user(node_type=node_type)
    host = resolve_node_host(client, tenant_id, master_node_id, node_type=node_type)
    if not host:
        raise RuntimeError(f"Could not resolve SSH host for master node {master_node_id}")

    wait_for_ssh(host, key_file, username=username, timeout=int(os.environ.get("BRIDGE_SLURM_SSH_TIMEOUT", "600")))

    local_ok = shutil.which("sinfo") is not None
    if mode == "local" or (mode == "auto" and local_ok):
        probe = subprocess.run(["sinfo", "-h", "-o", "%P"], capture_output=True, text=True, check=False)
        if probe.returncode == 0 and probe.stdout.strip():
            return "local", None, os.environ.get("SLURM_CONF")

    conf_path: Path | None = None
    if shutil.which("sinfo") is not None:
        try:
            conf_path = fetch_remote_slurm_conf(
                host=host,
                username=username,
                key_file=key_file,
                dest=default_slurm_conf_path(),
            )
            os.environ["SLURM_CONF"] = str(conf_path)
            probe = subprocess.run(["sinfo", "-h", "-o", "%P"], capture_output=True, text=True, check=False)
            if probe.returncode == 0 and probe.stdout.strip():
                return "local", None, str(conf_path)
        except RuntimeError as exc:
            print(f"[slurm] local slurm-client with copied conf failed: {exc}", file=sys.stderr)

    bin_dir = install_ssh_wrappers(
        default_slurm_bin_dir(),
        host=host,
        username=username,
        key_file=key_file,
    )
    probe = subprocess.run(
        [str(bin_dir / "sinfo"), "-h", "-o", "%P"],
        capture_output=True,
        text=True,
        check=False,
    )
    if probe.returncode != 0 or not probe.stdout.strip():
        raise RuntimeError(
            f"Slurm CLI not reachable via SSH wrappers on {host}: "
            f"{probe.stderr.strip() or probe.stdout.strip()}"
        )
    return "ssh", str(bin_dir), None


def run_inventory_script(*, env: dict[str, str] | None = None) -> dict[str, Any]:
    """Run my-isv Slurm inventory bash script and parse JSON stdout."""
    script = (
        Path(__file__).resolve().parents[3]
        / "my-isv"
        / "scripts"
        / "slurm"
        / "setup.sh"
    )
    if not script.exists():
        raise FileNotFoundError(f"Inventory script not found: {script}")

    result = subprocess.run(
        ["bash", str(script)],
        env=env or os.environ.copy(),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Slurm inventory failed: {result.stderr.strip() or result.stdout.strip()}"
        )
    data = json.loads(result.stdout)
    if not isinstance(data, dict):
        raise ValueError("Inventory script did not return a JSON object")
    return data


def remap_partitions(inventory: dict[str, Any]) -> dict[str, Any]:
    """Rename partitions to cpu/gpu when Bridge uses different names."""
    slurm = inventory.get("slurm")
    if not isinstance(slurm, dict):
        return inventory

    partitions = slurm.get("partitions")
    if not isinstance(partitions, dict):
        return inventory

    cpu_alias = os.environ.get("BRIDGE_SLURM_CPU_PARTITION", "cpu").strip()
    gpu_alias = os.environ.get("BRIDGE_SLURM_GPU_PARTITION", "gpu").strip()
    cpu_src = os.environ.get("BRIDGE_SLURM_CPU_PARTITION_SOURCE", "").strip()
    gpu_src = os.environ.get("BRIDGE_SLURM_GPU_PARTITION_SOURCE", "").strip()

    if cpu_src and cpu_src in partitions and cpu_src != cpu_alias:
        partitions.setdefault(cpu_alias, partitions[cpu_src])
    if gpu_src and gpu_src in partitions and gpu_src != gpu_alias:
        partitions.setdefault(gpu_alias, partitions[gpu_src])

    if cpu_alias not in partitions and gpu_alias in partitions and len(partitions) == 1:
        partitions[cpu_alias] = {"nodes": []}

    slurm["partitions"] = partitions
    inventory["slurm"] = slurm
    return inventory
