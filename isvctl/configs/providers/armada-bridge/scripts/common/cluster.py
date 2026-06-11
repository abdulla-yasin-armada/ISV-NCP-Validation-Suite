"""Bridge orchestrator cluster lifecycle helpers."""
from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

from .bridge_client import BridgeClient
from .polling import poll_until

_RUNNING_STATES = frozenset({"running", "success"})
_FAILED_STATES = frozenset({"failed", "error"})
_DEFAULT_POLL_INTERVAL = 15
_DEFAULT_CREATE_TIMEOUT = 900
_DEFAULT_DELETE_TIMEOUT = 180


def cluster_path(tenant_id: str, cluster_id: str | None = None) -> str:
    base = f"/orchestrator/tenants/{tenant_id}/clusters"
    return f"{base}/{cluster_id}" if cluster_id else base


def unwrap_cluster(data: Any) -> dict[str, Any]:
    """Normalise Bridge response — API returns either a single dict or a list depending on endpoint."""
    if isinstance(data, list):
        if not data:
            raise ValueError("Empty cluster list in Bridge response")
        first = data[0]
        if not isinstance(first, dict):
            raise ValueError(f"Expected cluster object, got {type(first).__name__}")
        return first
    if isinstance(data, dict):
        return data
    raise ValueError(f"Unexpected cluster response type: {type(data).__name__}")


def extract_cluster_id(data: dict[str, Any]) -> str:
    """Try multiple key variants — Bridge API is inconsistent across versions."""
    for key in ("id", "ID", "clusterId", "clusterID"):
        value = data.get(key)
        if value:
            return str(value)
    raise ValueError(f"No cluster id in response: {data!r}")


def extract_api_server(kubeconfig_yaml: str) -> str:
    match = re.search(r"^\s*server:\s*(\S+)\s*$", kubeconfig_yaml, re.MULTILINE)
    return match.group(1) if match else ""


def create_cluster(
    client: BridgeClient,
    tenant_id: str,
    *,
    name: str,
    nodes: list[dict[str, Any]],
    version: str = "1.31",
    install_gpu_tools: bool = True,
    deploy_local_provisioner: bool = True,
    cni: str = "cilium",
    cluster_template: str | None = None,
    #cluster_template: str | None = "nvidiaNim",
) -> dict[str, Any]:
    """POST /orchestrator/tenants/{tenant}/clusters."""
    body = {
        "name": name,
        "description": "",
        "version": version,
        "installGpuTools": install_gpu_tools,
        "enableNetworkAcceleration": False,
        "cni": cni,
        "distribution": "kubernetes",
        "deployLocalProvisioner": deploy_local_provisioner,
        "autoScaling": False,
        "nodes": nodes,
    }
    if cluster_template:
        body["clusterTemplate"] = {"name": cluster_template}
    resp = client.post(cluster_path(tenant_id), body, timeout=120)
    return unwrap_cluster(resp)


def get_cluster(client: BridgeClient, tenant_id: str, cluster_id: str) -> dict[str, Any]:
    resp = client.get(cluster_path(tenant_id, cluster_id))
    return unwrap_cluster(resp)


def wait_cluster_running(
    client: BridgeClient,
    tenant_id: str,
    cluster_id: str,
    *,
    timeout: int = _DEFAULT_CREATE_TIMEOUT,
    interval: int = _DEFAULT_POLL_INTERVAL,
) -> dict[str, Any]:
    """Poll GET cluster until status is running/success."""

    def check() -> tuple[bool, dict[str, Any] | None, str]:
        cluster = get_cluster(client, tenant_id, cluster_id)
        status = str(cluster.get("status") or "").lower()
        if status in _FAILED_STATES:
            message = cluster.get("statusMessage") or cluster.get("message") or status
            raise RuntimeError(f"Cluster {cluster_id} failed: {message}")
        if status in _RUNNING_STATES:
            return True, cluster, f"status={status!r}"
        return False, None, f"status={status!r}"

    return poll_until(check, label="k8s_setup", interval=interval, timeout=timeout)


def fetch_kubeconfig(client: BridgeClient, tenant_id: str, cluster_id: str) -> str:
    """GET .../clusters/{id}/kubeconfig and return YAML text."""
    resp = client.get(cluster_path(tenant_id, cluster_id) + "/kubeconfig")
    if isinstance(resp, dict):
        kubeconfig = resp.get("kubeconfig")
        if isinstance(kubeconfig, str) and kubeconfig.strip():
            return kubeconfig
    raise ValueError(f"Kubeconfig missing from Bridge response for cluster {cluster_id}")


def write_kubeconfig(kubeconfig_yaml: str, path: Path) -> Path:
    """Write kubeconfig YAML to path, creating parent dirs, and lock to owner-read-only (0o600)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(kubeconfig_yaml)
    path.chmod(0o600)
    return path


def delete_cluster(client: BridgeClient, tenant_id: str, cluster_id: str) -> None:
    client.delete(cluster_path(tenant_id, cluster_id))


def wait_cluster_deleted(
    client: BridgeClient,
    tenant_id: str,
    cluster_id: str,
    *,
    timeout: int = _DEFAULT_DELETE_TIMEOUT,
    interval: int = _DEFAULT_POLL_INTERVAL,
) -> None:
    """Poll GET cluster until Bridge returns 404."""

    def check() -> tuple[bool, None, str]:
        try:
            get_cluster(client, tenant_id, cluster_id)
        except ValueError as exc:
            if "404" in str(exc):
                return True, None, "cluster not found (deleted)"
            raise
        return False, None, "cluster still exists"

    poll_until(check, label="k8s_teardown", interval=interval, timeout=timeout)


def default_kubeconfig_path() -> Path:
    return Path.home() / ".cache" / "isvctl" / "bridge-k8s.kubeconfig"


def wait_for_kubectl(kubeconfig_path: Path, *, timeout: int = 120, interval: int = 10) -> None:
    """Poll kubectl cluster-info until the API server is reachable."""
    import subprocess

    deadline = time.monotonic() + timeout
    cmd = ["kubectl", "--kubeconfig", str(kubeconfig_path), "cluster-info"]
    while True:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            return
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"kubectl cluster-info failed after {timeout}s using {kubeconfig_path}: {result.stderr.strip()}"
            )
        time.sleep(interval)
