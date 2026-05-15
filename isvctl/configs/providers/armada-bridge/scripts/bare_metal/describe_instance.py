#!/usr/bin/env python3
"""describe_instance — Armada Bridge bare metal suite, test phase.

Priority: Bridge API fields first; SSH probes fill any gaps.

1. GET /orchestrator/tenants/<tenant>/metal/computes/<id>
   Relevant fields: externalIPAddress (public IP), inBandIP (mgmt fallback),
   allocateStatus (lifecycle state), totalGpuCount, gpus[].cudaDriver,
   hardware.cpu.count, osStatus.
   Note: SSH credentials are sanitized out by the orchestrator — the SSH
   block is best-effort and will be skipped if credentials are unavailable.

2. SSH in with userName/password if available (currently always absent from API).
3. For fields still missing after Bridge API, probe via SSH (if connected).
4. SSH key injection via SFTP (idempotent, tagged with 'isv-bm-<id>').

Required fields (success: false if absent): public_ip, instance_id, state
Optional (null / "" on failure): ssh_user, gpu_count, driver_version, cpu_count,
                                  container_runtime, key_file

Output: {success, platform, instance_id, state, public_ip, key_file, ssh_user,
         os, gpu_count, driver_version, cpu_count, container_runtime}
"""
import argparse
import io
import json
import os
import sys
from pathlib import Path
from typing import Any

import paramiko

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common.bridge_client import BridgeClient
from common.errors import handle_bridge_errors
from common.tenant import resolve_tenant_id

DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"

_REQUIRED_FIELDS = {"public_ip", "instance_id", "state"}


def _run_ssh(ssh: paramiko.SSHClient, cmd: str, timeout: int = 30) -> str:
    """Run a command over SSH, return stdout stripped. Returns '' on error."""
    try:
        _, stdout, _ = ssh.exec_command(cmd, timeout=timeout)
        stdout.channel.recv_exit_status()
        return stdout.read().decode().strip()
    except Exception:
        return ""


def _inject_key(ssh: paramiko.SSHClient, instance_id: str) -> str:
    """Inject tagged RSA pubkey via SFTP; reuse if already present. Returns key_file path."""
    tag = f"isv-bm-{instance_id}"
    key_path = f"/tmp/bm-{instance_id}.pem"

    # Read existing authorized_keys via SFTP (avoids shell quoting issues).
    existing = ""
    try:
        with ssh.open_sftp() as sftp:
            try:
                with sftp.open(".ssh/authorized_keys", "r") as f:
                    existing = f.read().decode()
            except OSError:
                pass
    except Exception:
        pass

    if tag in existing and Path(key_path).exists():
        return key_path

    rsa_key = paramiko.RSAKey.generate(bits=2048)
    pub_line = f"{rsa_key.get_name()} {rsa_key.get_base64()} {tag}\n"

    try:
        with ssh.open_sftp() as sftp:
            try:
                sftp.mkdir(".ssh")
            except OSError:
                pass  # already exists
            try:
                sftp.chmod(".ssh", 0o700)
            except Exception:
                pass
            with sftp.open(".ssh/authorized_keys", "a") as f:
                f.write(pub_line.encode())
            try:
                sftp.chmod(".ssh/authorized_keys", 0o600)
            except Exception:
                pass
    except Exception:
        return ""

    buf = io.StringIO()
    rsa_key.write_private_key(buf)
    Path(key_path).write_text(buf.getvalue())
    Path(key_path).chmod(0o600)
    return key_path


def _ssh_probes(ssh: paramiko.SSHClient) -> dict[str, Any]:
    """Run hardware probes. Returns dict of discovered values (None if unavailable)."""
    probes: dict[str, Any] = {}

    gpu_count_str = _run_ssh(ssh, "nvidia-smi --list-gpus 2>/dev/null | wc -l")
    probes["gpu_count"] = int(gpu_count_str) if gpu_count_str.isdigit() else None

    driver = _run_ssh(
        ssh,
        "nvidia-smi --query-gpu=driver_version --format=csv,noheader,nounits 2>/dev/null | head -1",
    )
    probes["driver_version"] = driver or None

    cpu_str = _run_ssh(ssh, "nproc 2>/dev/null")
    probes["cpu_count"] = int(cpu_str) if cpu_str.isdigit() else None

    runtime = _run_ssh(ssh, "docker info --format '{{.ServerVersion}}' 2>/dev/null")
    probes["container_runtime"] = runtime or None

    return probes


@handle_bridge_errors
def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--compute-node-id", required=True)
    args = parser.parse_args()

    result: dict[str, Any] = {"success": False, "platform": "bare_metal"}

    if DEMO_MODE:
        result.update(
            {
                "success": True,
                "instance_id": "demo-bm-node01",
                "state": "running",
                "public_ip": "203.0.114.20",
                "key_file": "/tmp/demo-bm-key.pem",
                "ssh_user": "ubuntu",
                "os": "ubuntu",
                "gpu_count": 8,
                "driver_version": "535.129.03",
                "cpu_count": 128,
                "container_runtime": "docker",
            }
        )
    else:
        client = BridgeClient.from_env()
        tenant = resolve_tenant_id(client, args.tenant)

        # ── 1. Fetch from Bridge API ──────────────────────────────────────
        node = client.get(
            f"/orchestrator/tenants/{tenant}/metal/computes/{args.compute_node_id}"
        )

        instance_id = str(node.get("id") or "")
        # externalIPAddress is the public IP; fall back to inBandIP (mgmt).
        public_ip = node.get("externalIPAddress") or node.get("inBandIP") or ""
        alloc_status = str(node.get("allocateStatus") or "")
        # Normalize Bridge allocateStatus to the provider-neutral "state" contract.
        # Validations expect "running" for an allocated, operational node.
        state = "running" if alloc_status in ("done", "success") else alloc_status
        # SSH credentials are sanitized out by sanitizeServer() — typically absent.
        ssh_block = node.get("ssh") or {}
        ssh_user = ssh_block.get("authUsername") or ""
        password = ssh_block.get("authPassword") or ""
        os_name = str(node.get("osStatus") or "")

        # GPU fields from Bridge API.
        gpu_count: int | None = node.get("totalGpuCount") or None
        gpus = node.get("gpus") or []
        driver_version: str | None = gpus[0].get("cudaDriver") or None if gpus else None
        cpu_count: int | None = (
            (node.get("hardware") or {}).get("cpu", {}).get("count") or None
        )
        container_runtime: str | None = None
        key_file = ""

        # ── 2 & 3. SSH in (best-effort), probe missing fields, inject key ─
        if public_ip and ssh_user and password:
            ssh = paramiko.SSHClient()
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            try:
                ssh.connect(
                    hostname=public_ip,
                    username=ssh_user,
                    password=password,
                    timeout=30,
                    allow_agent=False,
                    look_for_keys=False,
                )
                probes = _ssh_probes(ssh)
                if gpu_count is None:
                    gpu_count = probes.get("gpu_count")
                if driver_version is None:
                    driver_version = probes.get("driver_version")
                if cpu_count is None:
                    cpu_count = probes.get("cpu_count")
                if container_runtime is None:
                    container_runtime = probes.get("container_runtime")

                key_file = _inject_key(ssh, instance_id or args.compute_node_id)
            except Exception as exc:
                result["ssh_error"] = str(exc)
            finally:
                ssh.close()

        # ── 4. Failure policy ─────────────────────────────────────────────
        required = {"public_ip": public_ip, "instance_id": instance_id, "state": state}
        missing = [k for k, v in required.items() if not v]
        if missing:
            result["error"] = f"Required fields unresolvable from Bridge API: {missing}"
            print(json.dumps(result, indent=2))
            return 1

        result.update(
            {
                "success": True,
                "instance_id": instance_id,
                "state": state,
                "public_ip": public_ip,
                "key_file": key_file,
                "ssh_user": ssh_user,
                "os": os_name,
                "gpu_count": gpu_count,
                "driver_version": driver_version,
                "cpu_count": cpu_count,
                "container_runtime": container_runtime,
            }
        )

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
