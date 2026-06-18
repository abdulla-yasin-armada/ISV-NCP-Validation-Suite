#!/usr/bin/env python3
"""stable_ip_test — Armada Bridge network suite, test phase.

Validates that a BM node retains its private IP across a reboot cycle.

Bridge does not have stop/start VM APIs for BM nodes. Instead this test:
  1. Records the node's current IP from Bridge API  (record_ip)
  2. SSHes into the node and issues `sudo reboot`    (stop_instance)
  3. Polls SSH until the node is reachable again     (start_instance)
  4. Polls Bridge API until IP is re-populated       (ip_unchanged)
  5. Compares before/after IPs

Sub-test keys produced match StablePrivateIpCheck requirements:
  create_instance, record_ip, stop_instance, start_instance, ip_unchanged

Environment:
  BRIDGE_BM_SSH_PASS   SSH password for BM nodes (required)
  BRIDGE_JUMPHOST      optional jumphost user@host
"""
from __future__ import annotations

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
from common.tenant import resolve_tenant_id

DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"

_SSH_TIMEOUT = 30
_REBOOT_POLL_INTERVAL = 15
_REBOOT_WAIT_MAX = 480      # 8 min for BM to come back after reboot
_IP_REPOPULATE_MAX = 120    # 2 min for Bridge API to re-show IP after reboot


# ---------------------------------------------------------------------------
# Bridge API helpers
# ---------------------------------------------------------------------------

def _list_computes(client: BridgeClient, tenant_id: str) -> list[dict[str, Any]]:
    resp = client.get(f"/orchestrator/tenants/{tenant_id}/metal/computes")
    nodes = resp if isinstance(resp, list) else (resp or {}).get("data", [])
    return [n for n in nodes if isinstance(n, dict)]


def _node_ip(node: dict[str, Any]) -> str:
    return str(
        node.get("externalIPAddress")
        or node.get("inBandIP")
        or node.get("ipAddress")
        or ""
    )


def _get_node_ip(client: BridgeClient, tenant_id: str, node_id: str) -> str:
    """Return current management IP for node_id from Bridge API."""
    nodes = _list_computes(client, tenant_id)
    for n in nodes:
        nid = str(n.get("id") or n.get("ID") or "")
        if nid == node_id:
            return _node_ip(n)
    return ""


def _get_node_dict(client: BridgeClient, tenant_id: str, node_id: str) -> dict[str, Any]:
    """Return full node dict for node_id from Bridge API."""
    nodes = _list_computes(client, tenant_id)
    for n in nodes:
        nid = str(n.get("id") or n.get("ID") or "")
        if nid == node_id:
            return n
    return {}


def _get_overlay_ips(client: BridgeClient, tenant_id: str, node: dict[str, Any]) -> set[str]:
    """Return the full set of compute overlay IPs from the underlay API.

    The API returns ip_addresses as a list in non-deterministic order.
    Returning a set avoids false positives when comparing before/after reboot.
    Uses node name/hostname for the endpoint (not UUID).
    """
    candidates: list[str] = []
    for field in ("name", "hostname", "hostName", "host_id", "hostId"):
        val = str(node.get(field) or "").strip()
        if val:
            candidates.append(val)
    uuid_val = str(node.get("id") or node.get("ID") or "").strip()
    if uuid_val:
        candidates.append(uuid_val)

    for identifier in candidates:
        try:
            resp = client.get(
                f"/orchestrator/tenants/{tenant_id}/network/underlay/{identifier}"
            )
            items = resp if isinstance(resp, list) else [resp]
            for item in items:
                ips = (
                    (item.get("ComputeNetwork") or {})
                    .get("compute", {})
                    .get("ip_addresses", [])
                )
                if ips:
                    return {str(ip) for ip in ips}
        except Exception:
            continue
    return set()


# ---------------------------------------------------------------------------
# SSH helpers (reuse paramiko pattern from traffic_test)
# ---------------------------------------------------------------------------

def _parse_jumphost(jumphost: str) -> tuple[str, str, int]:
    user, rest = jumphost.split("@", 1)
    if ":" in rest:
        host, port_str = rest.rsplit(":", 1)
        return user, host, int(port_str)
    return user, rest, 22


def _ssh_run(
    host: str,
    user: str,
    password: str,
    command: str,
    jumphost: str = "",
    timeout: int = _SSH_TIMEOUT,
) -> tuple[int, str, str]:
    """Run SSH command with password auth and optional jumphost."""
    import paramiko

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    jh_client: paramiko.SSHClient | None = None

    try:
        connect_kwargs: dict[str, Any] = {
            "username": user,
            "password": password,
            "timeout": timeout,
            "look_for_keys": False,
            "allow_agent": False,
        }
        if jumphost:
            jh_user, jh_host, jh_port = _parse_jumphost(jumphost)
            jh_client = paramiko.SSHClient()
            jh_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            jh_client.connect(
                jh_host, port=jh_port, username=jh_user,
                timeout=timeout, look_for_keys=True, allow_agent=True,
            )
            transport = jh_client.get_transport()
            assert transport is not None
            channel = transport.open_channel("direct-tcpip", (host, 22), ("127.0.0.1", 0))
            connect_kwargs["sock"] = channel

        client.connect(host, **connect_kwargs)
        stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
        # Read buffers before recv_exit_status() to avoid deadlock.
        stdout_data = stdout.read().decode("utf-8", errors="replace")
        stderr_data = stderr.read().decode("utf-8", errors="replace")
        exit_code = stdout.channel.recv_exit_status()
        return exit_code, stdout_data, stderr_data
    finally:
        client.close()
        if jh_client:
            try:
                jh_client.close()
            except Exception:
                pass


def _ssh_reachable(host: str, user: str, password: str, jumphost: str = "") -> bool:
    """Return True if SSH login succeeds (used for post-reboot polling)."""
    try:
        rc, _, _ = _ssh_run(host, user, password, "echo ok", jumphost=jumphost, timeout=10)
        return rc == 0
    except Exception:
        return False


def _wait_for_ssh(
    host: str,
    user: str,
    password: str,
    jumphost: str,
    max_wait: int = _REBOOT_WAIT_MAX,
    interval: int = _REBOOT_POLL_INTERVAL,
) -> bool:
    """Poll until SSH is reachable or timeout. Returns True if reachable."""
    deadline = time.time() + max_wait
    while time.time() < deadline:
        if _ssh_reachable(host, user, password, jumphost):
            return True
        time.sleep(interval)
    return False


def _wait_for_ip(
    client: BridgeClient,
    tenant_id: str,
    node_id: str,
    max_wait: int = _IP_REPOPULATE_MAX,
    interval: int = 10,
) -> str:
    """Poll Bridge API until node has a management IP. Returns IP or empty string."""
    deadline = time.time() + max_wait
    while time.time() < deadline:
        ip = _get_node_ip(client, tenant_id, node_id)
        if ip:
            return ip
        time.sleep(interval)
    return ""


def _wait_for_overlay_ips(
    client: BridgeClient,
    tenant_id: str,
    node_id: str,
    max_wait: int = _IP_REPOPULATE_MAX,
    interval: int = 10,
) -> set[str]:
    """Poll underlay API until compute overlay IPs are re-populated. Returns set or empty set."""
    deadline = time.time() + max_wait
    while time.time() < deadline:
        node = _get_node_dict(client, tenant_id, node_id)
        if node:
            ips = _get_overlay_ips(client, tenant_id, node)
            if ips:
                return ips
        time.sleep(interval)
    return set()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

@handle_bridge_errors
def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--vpc-id", default="")
    parser.add_argument("--instance-ids", default="",
                        help="Comma-separated node IDs from provision_nodes (first used)")
    parser.add_argument("--ssh-user", default="ubuntu")
    args = parser.parse_args()

    result: dict[str, Any] = {"success": False, "platform": "network"}
    tests: dict[str, Any] = {}

    if DEMO_MODE:
        result.update({
            "success": True,
            "platform": "network",
            "tests": {
                "create_instance": {"passed": True, "message": "reusing provisioned BM node (demo)"},
                "record_ip":       {"passed": True, "ip": "10.255.0.1", "overlay_ips": ["10.4.0.1"]},
                "stop_instance":   {"passed": True, "message": "reboot initiated via SSH (demo)"},
                "start_instance":  {"passed": True, "message": "node reachable after reboot (demo)"},
                "ip_unchanged":    {"passed": True, "ip_before": "10.255.0.1", "ip_after": "10.255.0.1", "overlay_ips_before": ["10.4.0.1"], "overlay_ips_after": ["10.4.0.1"]},
            },
        })
        print(json.dumps(result, indent=2))
        return 0

    ssh_pass = os.environ.get("BRIDGE_BM_SSH_PASS", "").strip()
    if not ssh_pass:
        result["error"] = "BRIDGE_BM_SSH_PASS env var is required"
        print(json.dumps(result, indent=2))
        return 0

    jumphost = os.environ.get("BRIDGE_JUMPHOST", "").strip()

    instance_ids = [i.strip() for i in args.instance_ids.split(",") if i.strip()]
    if not instance_ids:
        result["error"] = "No instance IDs provided — provision_nodes may have failed"
        print(json.dumps(result, indent=2))
        return 0

    node_id = instance_ids[0]

    client = BridgeClient.from_env()
    tenant_id = resolve_tenant_id(client, args.tenant)

    # Step 1: create_instance — BM node already provisioned, just verify it exists
    node_dict = _get_node_dict(client, tenant_id, node_id)
    ip_before = _node_ip(node_dict)
    if not ip_before:
        tests["create_instance"] = {"passed": False, "error": f"Node {node_id} not found or has no IP"}
        result["tests"] = tests
        print(json.dumps(result, indent=2))
        return 0

    tests["create_instance"] = {
        "passed": True,
        "message": f"Reusing provisioned BM node {node_id}",
    }
    print(f"[stable_ip] node: {node_id}", file=sys.stderr)

    # Step 2: record_ip — record both management and overlay IPs before reboot
    overlay_ips_before = _get_overlay_ips(client, tenant_id, node_dict)
    tests["record_ip"] = {
        "passed": True,
        "ip": ip_before,
        "overlay_ips": sorted(overlay_ips_before) if overlay_ips_before else "unavailable",
    }
    print(f"[stable_ip] IP before reboot (Bridge API): mgmt={ip_before}  overlay={sorted(overlay_ips_before)}", file=sys.stderr)

    # Step 3: stop_instance — SSH reboot
    print(f"[stable_ip] initiating reboot via SSH ({ip_before}) ...", file=sys.stderr)
    try:
        # Use nohup so the reboot completes even if SSH drops mid-command
        _ssh_run(
            ip_before, args.ssh_user, ssh_pass,
            "sudo nohup sh -c 'sleep 2 && reboot' &",
            jumphost=jumphost,
            timeout=15,
        )
        tests["stop_instance"] = {
            "passed": True,
            "message": "Reboot initiated via SSH (sudo reboot)",
        }
        print("[stable_ip] reboot command sent, waiting for node to go down ...", file=sys.stderr)
    except Exception as exc:
        tests["stop_instance"] = {"passed": False, "error": str(exc)}
        result["tests"] = tests
        print(json.dumps(result, indent=2))
        return 0

    # Brief wait to let the reboot actually start before polling
    time.sleep(20)

    # Step 4: start_instance — wait for SSH to come back
    print(f"[stable_ip] polling SSH until node is reachable (up to {_REBOOT_WAIT_MAX}s) ...", file=sys.stderr)
    ssh_back = _wait_for_ssh(ip_before, args.ssh_user, ssh_pass, jumphost)
    if not ssh_back:
        tests["start_instance"] = {
            "passed": False,
            "error": f"Node did not become reachable via SSH within {_REBOOT_WAIT_MAX}s after reboot",
        }
        result["tests"] = tests
        print(json.dumps(result, indent=2))
        return 0

    tests["start_instance"] = {
        "passed": True,
        "message": "Node reachable via SSH after reboot",
    }
    print("[stable_ip] node is back — SSH reachable after reboot", file=sys.stderr)

    # Step 5: ip_unchanged — verify both management and overlay IPs are stable after reboot
    print("[stable_ip] verifying IPs from Bridge API after reboot ...", file=sys.stderr)
    ip_after = _wait_for_ip(client, tenant_id, node_id)
    overlay_ips_after = _wait_for_overlay_ips(client, tenant_id, node_id) if overlay_ips_before else set()
    print(f"[stable_ip] IP after reboot  (Bridge API): mgmt={ip_after}  overlay={sorted(overlay_ips_after)}", file=sys.stderr)

    failures = []
    if not ip_after:
        failures.append("management IP missing after reboot")
    elif ip_before != ip_after:
        failures.append(f"management IP changed: {ip_before} → {ip_after}")

    if overlay_ips_before and not overlay_ips_after:
        failures.append("overlay IPs missing after reboot")
    elif overlay_ips_before and overlay_ips_before != overlay_ips_after:
        added = overlay_ips_after - overlay_ips_before
        removed = overlay_ips_before - overlay_ips_after
        failures.append(f"overlay IP set changed (added={sorted(added)}, removed={sorted(removed)})")

    if failures:
        tests["ip_unchanged"] = {
            "passed": False,
            "ip_before": ip_before,
            "ip_after": ip_after or "",
            "overlay_ips_before": sorted(overlay_ips_before),
            "overlay_ips_after": sorted(overlay_ips_after),
            "error": "; ".join(failures),
        }
    else:
        tests["ip_unchanged"] = {
            "passed": True,
            "ip_before": ip_before,
            "ip_after": ip_after,
            "overlay_ips_before": sorted(overlay_ips_before),
            "overlay_ips_after": sorted(overlay_ips_after),
        }

    all_passed = all(t.get("passed", False) for t in tests.values())
    result.update({"success": all_passed, "tests": tests})

    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
