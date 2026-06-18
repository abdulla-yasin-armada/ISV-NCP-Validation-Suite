#!/usr/bin/env python3
"""traffic_test — Armada Bridge network suite, test phase.

Validates real network traffic flow via password-based SSH into provisioned BM nodes.

Sub-tests:
  traffic_allowed  — node-to-node ping between the 2 provisioned BM nodes (fails if < 2 nodes)
  traffic_blocked  — ping from node1 to an isolated node in a separate VPC fails (VPC isolation)
  internet_icmp    — node1 can ping 8.8.8.8
  internet_http    — node1 can curl http://example.com

Isolated node for traffic_blocked:
  Creates 2 temp VPCs:
    - compute topology  CIDR 10.6.0.0/16
    - storage topology  CIDR 10.7.0.0/16
  Allocates 1 BM node using both subnet IDs.
  Polls until the node is ready and has an IP.
  SSH into node1 → ping isolated_ip → expects non-zero exit (unreachable = PASS).
  Cleans up in a finally block: deallocate node, delete storage VPC, delete compute VPC.

Environment:
  BRIDGE_BM_SSH_PASS   SSH password for BM nodes (required)
  BRIDGE_JUMPHOST      optional jumphost in user@host format (key-based auth)
  BRIDGE_BM_FLAVOR     BM product type ID (same as provision_nodes)
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
from common.catalog import discover_bm_product_type_id
from common.errors import handle_bridge_errors
from common.network import is_orchestrator_resource_id, list_topologies
from common.polling import poll_until
from common.tenant import resolve_tenant_id

DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"

_ISO_COMPUTE_CIDR = "10.6.0.0/16"
_ISO_ALT_CIDR = "10.7.0.0/16"
_ALT_TOPOLOGIES = ("storage", "converged")  # preference order for isolated VPC
_POLL_INTERVAL = 30
_POLL_TIMEOUT = 1800  # 30 min — isolated node may take time to provision


# ---------------------------------------------------------------------------
# SSH helpers
# ---------------------------------------------------------------------------

def _parse_jumphost(jumphost: str) -> tuple[str, str, int]:
    """Parse 'user@host' or 'user@host:port'. Returns (user, host, port)."""
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
    timeout: int = 30,
) -> tuple[int, str, str]:
    """Run command on BM node via SSH with password auth and optional jumphost.

    Jumphost uses the existing SSH key (~/.ssh/id_rsa) from the agent/key file.
    BM node uses password auth.
    Returns (exit_code, stdout, stderr).
    """
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
            # Jumphost uses key-based auth (existing SSH agent / ~/.ssh/id_rsa)
            jh_client.connect(
                jh_host,
                port=jh_port,
                username=jh_user,
                timeout=timeout,
                look_for_keys=True,
                allow_agent=True,
            )
            transport = jh_client.get_transport()
            assert transport is not None
            channel = transport.open_channel(
                "direct-tcpip", (host, 22), ("127.0.0.1", 0)
            )
            connect_kwargs["sock"] = channel

        client.connect(host, **connect_kwargs)
        stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
        # Read buffers before recv_exit_status() to avoid deadlock.
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        exit_code = stdout.channel.recv_exit_status()
        return exit_code, out, err
    finally:
        client.close()
        if jh_client:
            try:
                jh_client.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Isolated VPC helpers (for traffic_blocked)
# ---------------------------------------------------------------------------

def _create_iso_vpc(
    client: BridgeClient, tenant_id: str, topology: str, cidr: str, epoch: int
) -> tuple[str, str]:
    """Create an isolated VPC + subnet. Returns (vpc_id, subnet_id)."""
    vpc_resp = client.post(
        f"/orchestrator/tenants/{tenant_id}/vpcs",
        {
            "name": f"isv-traf-iso-{topology}-{epoch}",
            "topologyID": topology,
            "description": "isvctl traffic_blocked probe VPC (ephemeral)",
            "capabilities": [],
        },
    )
    vpc_id = str(vpc_resp.get("id", "") or "")
    if not vpc_id:
        raise RuntimeError(f"No VPC id returned for topology={topology}")

    subnet_resp = client.post(
        f"/orchestrator/tenants/{tenant_id}/subnets",
        {
            "name": f"isv-traf-iso-{topology}-subnet-{epoch}",
            "subnetCIDR": cidr,
            "topology": topology,
            "parentVpcID": vpc_id,
            "capabilities": [],
        },
    )
    subnet_id = str(subnet_resp.get("id", "") or "")
    return vpc_id, subnet_id


def _delete_iso_vpc(client: BridgeClient, tenant_id: str, vpc_id: str) -> None:
    """Delete an isolated VPC (best-effort, ignores 404)."""
    if not vpc_id or not is_orchestrator_resource_id(vpc_id):
        return
    # Delete subnets first
    try:
        subnets_resp = client.get(
            f"/orchestrator/tenants/{tenant_id}/subnets",
            params={"parentVpcID": vpc_id},
        )
        subnets = subnets_resp if isinstance(subnets_resp, list) else []
        for s in subnets:
            sid = str(s.get("id", "") or "")
            if sid:
                try:
                    client.delete(f"/orchestrator/tenants/{tenant_id}/subnets/{sid}")
                except ValueError:
                    pass
    except Exception:
        pass
    try:
        client.delete(f"/orchestrator/tenants/{tenant_id}/vpcs/{vpc_id}")
    except ValueError:
        pass


def _list_computes(client: BridgeClient, tenant_id: str) -> list[dict[str, Any]]:
    resp = client.get(f"/orchestrator/tenants/{tenant_id}/metal/computes")
    nodes = resp if isinstance(resp, list) else (resp or {}).get("data", [])
    return [n for n in nodes if isinstance(n, dict)]


def _node_ip(node: dict[str, Any]) -> str:
    """Return management IP for SSH access."""
    return str(
        node.get("externalIPAddress")
        or node.get("inBandIP")
        or node.get("ipAddress")
        or ""
    )


def _get_overlay_ip(client: BridgeClient, tenant_id: str, node: dict[str, Any]) -> str:
    """Return the first compute overlay IP for a node from the underlay API.

    Uses GET /orchestrator/tenants/{tenant_id}/network/underlay/{identifier}.
    The underlay API uses the node name/hostname (e.g. 'hgx-pod00-su00-h01'),
    not the UUID. Tries name/hostname fields first, then falls back to UUID.
    Returns ComputeNetwork.compute.ip_addresses[0], the VPC data-plane IP.
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
                    return str(ips[0])
        except Exception:
            continue
    return ""


def _provision_isolated_node(
    client: BridgeClient,
    tenant_id: str,
    product_type_id: str,
    compute_subnet_id: str,
    storage_subnet_id: str,
    existing_ids: set[str],
) -> dict[str, Any]:
    """Allocate 1 BM node into isolated VPC subnets. Returns node dict."""
    allocate_body: dict[str, Any] = {
        "ProductTypeID": product_type_id,
        "computeNodeCount": 1,
        "subnetIds": [compute_subnet_id, storage_subnet_id],
    }
    try:
        client.post(
            f"/orchestrator/tenants/{tenant_id}/metal/allocate",
            allocate_body,
        )
    except ValueError as exc:
        if "status 409" not in str(exc):
            raise

    def _check() -> tuple[bool, dict[str, Any] | None, str]:
        nodes = _list_computes(client, tenant_id)
        for n in nodes:
            nid = str(n.get("id") or n.get("ID") or "")
            if nid in existing_ids:
                continue
            if str(n.get("productTypeId", "") or "") != product_type_id:
                continue
            state = str(n.get("allocateStatus", "") or "").lower()
            if state in {"done", "success"}:
                ip = _node_ip(n)
                if ip:
                    return True, n, f"isolated node {nid} ready ip={ip}"
        return False, None, "waiting for isolated node"

    # poll_until raises TimeoutError on timeout; returns result directly on success
    node = poll_until(
        _check,
        label="isolated_node",
        interval=_POLL_INTERVAL,
        timeout=_POLL_TIMEOUT,
    )
    if node is None:
        raise RuntimeError("Isolated node did not reach ready state in time")
    return node


def _deallocate_node(client: BridgeClient, tenant_id: str, node_id: str) -> None:
    """Deallocate a BM node (best-effort). 404 = already gone, safe to ignore."""
    try:
        client.post(
            f"/orchestrator/tenants/{tenant_id}/metal/{node_id}/deallocate",
            {},
        )
    except ValueError as exc:
        if "status 404" not in str(exc):
            raise


# ---------------------------------------------------------------------------
# Traffic sub-tests
# ---------------------------------------------------------------------------

def _run_ssh_tests(
    instances: list[dict[str, Any]],
    ssh_user: str,
    ssh_pass: str,
    jumphost: str,
) -> dict[str, Any]:
    """Run traffic_allowed, internet_icmp, internet_http via SSH."""
    tests: dict[str, Any] = {}

    if len(instances) < 2:
        tests["traffic_allowed"] = {
            "passed": False,
            "error": f"Need ≥2 provisioned nodes, got {len(instances)}",
        }
    else:
        node1_ssh_ip = instances[0].get("private_ip", "")   # management IP for SSH
        node2_overlay_ip = instances[1].get("overlay_ip", instances[1].get("private_ip", ""))
        try:
            rc, out, err = _ssh_run(
                node1_ssh_ip, ssh_user, ssh_pass,
                f"ping -c 3 -W 5 {node2_overlay_ip}",
                jumphost=jumphost,
                timeout=30,
            )
            tests["traffic_allowed"] = {
                "passed": rc == 0,
                "latency_ms": _parse_ping_latency(out),
                "message": f"ping overlay {node2_overlay_ip} exit={rc}",
            }
        except Exception as exc:
            tests["traffic_allowed"] = {"passed": False, "error": str(exc)}

    node1_ip = instances[0].get("private_ip", "") if instances else ""
    if not node1_ip:
        for key in ("internet_icmp", "internet_http"):
            tests[key] = {"passed": False, "error": "No node1 IP available"}
        return tests

    # internet_icmp
    try:
        rc, out, _ = _ssh_run(
            node1_ip, ssh_user, ssh_pass,
            "ping -c 3 -W 5 8.8.8.8",
            jumphost=jumphost,
            timeout=30,
        )
        tests["internet_icmp"] = {
            "passed": rc == 0,
            "message": f"ping 8.8.8.8 exit={rc}",
        }
    except Exception as exc:
        tests["internet_icmp"] = {"passed": False, "error": str(exc)}

    # internet_http
    try:
        rc, out, _ = _ssh_run(
            node1_ip, ssh_user, ssh_pass,
            "curl -s -o /dev/null -w '%{http_code}' --max-time 10 http://example.com",
            jumphost=jumphost,
            timeout=30,
        )
        tests["internet_http"] = {
            "passed": rc == 0,
            "message": f"curl http://example.com exit={rc}",
        }
    except Exception as exc:
        tests["internet_http"] = {"passed": False, "error": str(exc)}

    return tests


def _pick_alt_topology(client: BridgeClient) -> str:
    """Return first available non-compute topology (storage preferred, then converged)."""
    try:
        topologies = list_topologies(client)
        available = {str(t.get("topology", "") or t.get("name", "") or "").lower() for t in topologies}
        for topo in _ALT_TOPOLOGIES:
            if topo in available:
                return topo
    except Exception:
        pass
    # Fall back to converged — present in most Bridge labs
    return "converged"


def _run_traffic_blocked(
    client: BridgeClient,
    tenant_id: str,
    instances: list[dict[str, Any]],
    ssh_user: str,
    ssh_pass: str,
    jumphost: str,
) -> dict[str, Any]:
    """Provision isolated node in separate VPC, verify node1 cannot ping it."""
    epoch = int(time.time())
    iso_compute_vpc_id = ""
    iso_alt_vpc_id = ""
    isolated_node_id = ""

    try:
        existing_ids = {
            str(n.get("id") or n.get("ID") or "")
            for n in _list_computes(client, tenant_id)
        }

        # Detect available non-compute topology (storage or converged)
        alt_topology = _pick_alt_topology(client)

        # Create isolated VPCs: one compute + one alt topology for BM allocation
        iso_compute_vpc_id, iso_compute_subnet_id = _create_iso_vpc(
            client, tenant_id, "compute", _ISO_COMPUTE_CIDR, epoch
        )
        iso_alt_vpc_id, iso_alt_subnet_id = _create_iso_vpc(
            client, tenant_id, alt_topology, _ISO_ALT_CIDR, epoch
        )

        # Allocate isolated BM node
        explicit_flavor = os.environ.get("BRIDGE_BM_FLAVOR", "").strip()
        product_type_id, _, _ = discover_bm_product_type_id(
            client,
            explicit_id=explicit_flavor,
            gpu_type_filter=os.environ.get("BRIDGE_BM_GPU_TYPE", ""),
        )

        iso_node = _provision_isolated_node(
            client, tenant_id, product_type_id,
            iso_compute_subnet_id, iso_alt_subnet_id,
            existing_ids,
        )
        isolated_node_id = str(iso_node.get("id") or iso_node.get("ID") or "")

        # Use compute overlay IP (data-plane) — not the management IP
        # Pass full node dict so _get_overlay_ip can use name/hostname for the underlay API
        isolated_ip = _get_overlay_ip(client, tenant_id, iso_node)
        if not isolated_ip:
            return {
                "passed": False,
                "error": (
                    f"Isolated node {isolated_node_id} has no compute overlay IP "
                    "(underlay API returned no ComputeNetwork.compute.ip_addresses)"
                ),
            }

        node1_ip = instances[0].get("private_ip", "") if instances else ""
        if not node1_ip:
            return {"passed": False, "error": "No node1 IP to ping from"}

        # Ping isolated node from node1 — expect FAILURE (VPC isolation)
        rc, out, _ = _ssh_run(
            node1_ip, ssh_user, ssh_pass,
            f"ping -c 3 -W 5 {isolated_ip}",
            jumphost=jumphost,
            timeout=40,
        )
        # rc != 0 means unreachable → VPC isolation is working
        if rc != 0:
            return {
                "passed": True,
                "message": (
                    f"node1 ({node1_ip}) cannot reach isolated node ({isolated_ip}) "
                    "— VPCs are isolated"
                ),
            }
        else:
            return {
                "passed": False,
                "node1_ip": node1_ip,
                "isolated_ip": isolated_ip,
                "error": (
                    f"node1 ({node1_ip}) reached isolated node ({isolated_ip}) "
                    "— Bridge VPC data-plane isolation not enforced for BM nodes"
                ),
            }

    except Exception as exc:
        return {"passed": False, "error": str(exc)}

    finally:
        if isolated_node_id:
            _deallocate_node(client, tenant_id, isolated_node_id)
            # Allow Bridge time to fully deallocate before deleting VPC
            time.sleep(15)
        _delete_iso_vpc(client, tenant_id, iso_compute_vpc_id)
        _delete_iso_vpc(client, tenant_id, iso_alt_vpc_id)


def _parse_ping_latency(output: str) -> float | None:
    """Extract avg latency from ping output (e.g. 'rtt min/avg/max/mdev = 1.2/2.3/3.4/0.5 ms')."""
    for line in output.splitlines():
        if "avg" in line and "=" in line:
            try:
                parts = line.split("=")[1].strip().split("/")
                return float(parts[1])
            except (IndexError, ValueError):
                pass
    return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

@handle_bridge_errors
def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--vpc-id", default="")
    parser.add_argument("--instance-ids", default="",
                        help="Comma-separated node IDs from provision_nodes")
    parser.add_argument("--ssh-user", default="ubuntu",
                        help="SSH username for BM nodes (password from BRIDGE_BM_SSH_PASS)")
    args = parser.parse_args()

    result: dict[str, Any] = {"success": False, "platform": "network"}

    if DEMO_MODE:
        result.update({
            "success": True,
            "platform": "network",
            "tests": {
                "traffic_allowed": {"passed": True, "latency_ms": 1.2},
                "traffic_blocked": {"passed": True, "message": "demo mode"},
                "internet_icmp": {"passed": True},
                "internet_http": {"passed": True},
            },
        })
        print(json.dumps(result, indent=2))
        return 0

    ssh_pass = os.environ.get("BRIDGE_BM_SSH_PASS", "").strip()
    if not ssh_pass:
        result["error"] = "BRIDGE_BM_SSH_PASS env var is required for traffic tests"
        print(json.dumps(result, indent=2))
        return 1

    jumphost = os.environ.get("BRIDGE_JUMPHOST", "").strip()

    # Parse instances from provision_nodes output
    instance_ids = [i.strip() for i in args.instance_ids.split(",") if i.strip()]
    # Build instances list: [{instance_id, private_ip}]
    # We get IPs from Bridge API directly since provision_nodes may not pass full JSON
    client = BridgeClient.from_env()
    tenant_id = resolve_tenant_id(client, args.tenant)

    all_nodes = _list_computes(client, tenant_id)
    node_map = {str(n.get("id") or n.get("ID") or ""): n for n in all_nodes}
    instances = []
    for nid in instance_ids:
        node = node_map.get(nid, {})
        mgmt_ip = _node_ip(node)
        if not mgmt_ip:
            continue
        overlay_ip = _get_overlay_ip(client, tenant_id, node)
        instances.append({
            "instance_id": nid,
            "private_ip": mgmt_ip,                  # management IP — used for SSH
            "overlay_ip": overlay_ip or mgmt_ip,    # compute overlay IP — used as ping target
        })

    if not instances:
        result["error"] = "No instances with IPs found — provision_nodes may have failed"
        print(json.dumps(result, indent=2))
        return 1

    # SSH-based tests
    tests = _run_ssh_tests(instances, args.ssh_user, ssh_pass, jumphost)

    # traffic_blocked — isolated VPC probe
    tests["traffic_blocked"] = _run_traffic_blocked(
        client, tenant_id, instances, args.ssh_user, ssh_pass, jumphost
    )

    all_passed = all(t.get("passed", False) for t in tests.values())
    result.update({
        "success": all_passed,
        "tests": tests,
        "node_count": len(instances),
    })

    print(json.dumps(result, indent=2))
    # Always exit 0 so isvctl stores the output for TrafficFlowCheck validation.
    # Pass/fail is determined by isvtest reading the JSON, not the exit code.
    return 0


if __name__ == "__main__":
    sys.exit(main())
