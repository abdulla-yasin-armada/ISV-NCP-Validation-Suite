"""Bridge orchestrator Slurm cluster lifecycle helpers."""
from __future__ import annotations

import os
from typing import Any

from .bridge_client import BridgeClient
from .polling import poll_until

_RUNNING_STATES = frozenset({"running", "success"})
_FAILED_STATES = frozenset({"failed", "error"})
_DEFAULT_POLL_INTERVAL = 15
_DEFAULT_CREATE_TIMEOUT = 3300
_DEFAULT_DELETE_TIMEOUT = 1500


def slurm_path(tenant_id: str, slurm_id: str | None = None) -> str:
    base = f"/orchestrator/tenants/{tenant_id}/slurm"
    return f"{base}/{slurm_id}" if slurm_id else base


def unwrap_slurm(data: Any, slurm_id: str | None = None) -> dict[str, Any]:
    if isinstance(data, list):
        if not data:
            raise ValueError("Empty Slurm list in Bridge response")
        if slurm_id:
            for item in data:
                if isinstance(item, dict) and str(item.get("id") or item.get("ID") or "") == slurm_id:
                    return item
            raise ValueError(f"Slurm cluster {slurm_id} not found in response list of {len(data)}")
        first = data[0]
        if not isinstance(first, dict):
            raise ValueError(f"Expected Slurm object, got {type(first).__name__}")
        return first
    if isinstance(data, dict):
        return data
    raise ValueError(f"Unexpected Slurm response type: {type(data).__name__}")


def extract_slurm_id(data: dict[str, Any]) -> str:
    for key in ("id", "ID", "slurmId", "slurmID"):
        value = data.get(key)
        if value:
            return str(value)
    raise ValueError(f"No Slurm id in response: {data!r}")


def create_slurm_cluster(
    client: BridgeClient,
    tenant_id: str,
    *,
    name: str,
    nodes: list[dict[str, Any]],
    version: str = "24.05.6",
    description: str = "",
) -> dict[str, Any]:
    """POST /orchestrator/tenants/{tenant}/slurm."""
    body = {
        "name": name,
        "description": description,
        "version": version,
        "nodes": nodes,
    }
    resp = client.post(slurm_path(tenant_id), body, timeout=120)
    import json as _json, sys as _sys
    print(f"[slurm] create response:\n{_json.dumps(resp, indent=2)}", file=_sys.stderr)
    return unwrap_slurm(resp)


def get_slurm_cluster(client: BridgeClient, tenant_id: str, slurm_id: str) -> dict[str, Any]:
    resp = client.get(slurm_path(tenant_id))
    return unwrap_slurm(resp, slurm_id=slurm_id)


def wait_slurm_running(
    client: BridgeClient,
    tenant_id: str,
    slurm_id: str,
    *,
    timeout: int | None = None,
    interval: int = _DEFAULT_POLL_INTERVAL,
) -> dict[str, Any]:
    """Poll GET slurm cluster until status is running/success."""
    if timeout is None:
        timeout = int(os.environ.get("BRIDGE_SLURM_POLL_TIMEOUT", str(_DEFAULT_CREATE_TIMEOUT)))

    def check() -> tuple[bool, dict[str, Any] | None, str]:
        cluster = get_slurm_cluster(client, tenant_id, slurm_id)
        status = str(cluster.get("status") or "").lower()
        if status in _FAILED_STATES:
            import json as _json
            print(f"[slurm] cluster response on failure:\n{_json.dumps(cluster, indent=2)}", file=__import__('sys').stderr)
            message = cluster.get("statusMessage") or cluster.get("message") or cluster.get("error") or status
            raise RuntimeError(f"Slurm cluster {slurm_id} failed: {message}")
        if status in _RUNNING_STATES:
            return True, cluster, f"status={status!r}"
        return False, None, f"status={status!r}"

    return poll_until(check, label="slurm_setup", interval=interval, timeout=timeout)


def delete_slurm_cluster(client: BridgeClient, tenant_id: str, slurm_id: str) -> None:
    client.delete(slurm_path(tenant_id, slurm_id))


def wait_slurm_deleted(
    client: BridgeClient,
    tenant_id: str,
    slurm_id: str,
    *,
    timeout: int | None = None,
    interval: int = _DEFAULT_POLL_INTERVAL,
) -> None:
    if timeout is None:
        timeout = int(os.environ.get("BRIDGE_SLURM_DELETE_TIMEOUT", str(_DEFAULT_DELETE_TIMEOUT)))

    def check() -> tuple[bool, Any, str]:
        try:
            resp = client.get(slurm_path(tenant_id))
        except Exception as exc:
            if "404" in str(exc):
                return True, "absent", "GET returned 404"
            raise
        if isinstance(resp, list):
            present_ids = {
                str(item.get("id") or item.get("ID") or "")
                for item in resp
                if isinstance(item, dict)
            }
            if slurm_id not in present_ids:
                return True, "absent", "cluster not in list"
        elif isinstance(resp, dict) and not resp:
            return True, "absent", "empty response"
        return False, None, "cluster still present"

    poll_until(check, label="slurm_teardown", interval=interval, timeout=timeout)
