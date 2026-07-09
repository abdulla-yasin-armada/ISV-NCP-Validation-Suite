#!/usr/bin/env python3
"""setup — Armada Bridge Kubernetes suite, setup phase.

Flow (``main()``):
  1. Resolve ``--tenant`` name or UUID via ``resolve_tenant_id()`` (tenant must exist).
  2. ``provision_nodes()`` — import vs discovery (``GET /orchestrator/network/topologies``).
     If ``BRIDGE_K8S_NODE_IDS`` / ``--node-id`` is set, those nodes are used; otherwise
     allocate ``BRIDGE_K8S_NODE_COUNT`` (default 1) **new** workers from catalog.
     Node type defaults: import → bareMetal, discovery → VM; override with
     ``BRIDGE_K8S_NODE_TYPE=bareMetal|vm``.
  3. ``create_cluster()`` — POST ``/orchestrator/tenants/{tenant}/clusters``
     (``BRIDGE_K8S_INSTALL_GPU_TOOLS``, ``BRIDGE_K8S_DEPLOY_LOCAL_PROVISIONER``,
     ``BRIDGE_K8S_VERSION``).
  4. ``wait_cluster_running()`` → ``fetch_kubeconfig()`` → ``write_kubeconfig()``
     → ``wait_for_kubectl()``; then optionally ``_install_mpi_operator()`` when
     ``BRIDGE_K8S_INSTALL_MPI_OPERATOR=true``.
  5. ``_run_inventory()`` (``_common.sh``) — kubectl snapshot of nodes, GPUs, CSI, etc.
  6. ``_provision_acl_probe_bm()`` — when ``BRIDGE_TENANT_B`` is set, allocates a bare
     metal node in Tenant B and emits ``unauthorized_probe_cmd`` for
     ``K8sApiNetworkAclCheck``. The BM is kept alive through the test phase and
     deallocated in ``teardown.py`` via the k8s-state file.
  7. ``save_state()`` — persist cluster/node IDs (+ ACL probe node ID) for
     ``teardown.py``.

Stdout: ISV kubernetes setup JSON (``cluster_id``, ``kubeconfig_path``, ``kubernetes{}``,
``csi{}``, ``unauthorized_probe_cmd``, flow metadata). Consumed by isvctl as
``steps.setup``.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPT_DIR.parent))
sys.path.insert(0, str(_SCRIPT_DIR))

from common.bridge_client import BridgeClient  # noqa: E402
from common.catalog import discover_bm_product_type_id  # noqa: E402
from common.context import print_run_context  # noqa: E402
from common.cluster import (  # noqa: E402
    create_cluster,
    default_kubeconfig_path,
    extract_api_server,
    extract_cluster_id,
    fetch_kubeconfig,
    wait_cluster_running,
    wait_for_kubectl,
    write_kubeconfig,
)
from common.errors import handle_bridge_errors  # noqa: E402
from common.k8s_state import save_state  # noqa: E402
from common.metal import (  # noqa: E402
    allocate_bm,
    compute_node_id,
    deallocate_bm,
    list_computes,
    node_mgmt_ip,
    poll_until_bm_ready,
    provision_bm_node,
)
from common.network import (  # noqa: E402
    is_discovery_flow,
    is_import_flow,
    list_topologies,
)
from common.tenant import create_tenant_b_client, resolve_tenant_id  # noqa: E402
from common.vm import provision_vm_nodes, resolve_ssh_key  # noqa: E402
from common.vpc import deprovision_discovery_vpcs, provision_discovery_vpcs  # noqa: E402

DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"

_BM_POLL_TIMEOUT = 540
_BM_POLL_INTERVAL = 15
_VM_POLL_TIMEOUT = 840


@dataclass(frozen=True)
class ProvisionedNodes:
    """Result of k8s worker node provisioning or reuse."""

    discovery_flow: bool
    import_flow: bool
    node_type: str
    node_ids: list[str]
    vpc_id: str
    subnet_id: str
    provisioned: bool
    converged_vpc_id: str = "n/a"
    converged_subnet_id: str = ""
    vm_name: str = ""


def _resolve_k8s_node_type(*, discovery_flow: bool) -> str:
    """Return bareMetal or vm based on BRIDGE_K8S_NODE_TYPE env var or topology."""
    explicit = os.environ.get("BRIDGE_K8S_NODE_TYPE", "").strip().lower()
    if explicit in {"baremetal", "bare_metal", "bm", "metal"}:
        return "bareMetal"
    if explicit in {"vm", "virtualmachine"}:
        return "vm"
    return "vm" if discovery_flow else "bareMetal"


def _parse_k8s_node_ids(*, cli_node_ids: list[str]) -> list[str]:
    """Merge BRIDGE_K8S_NODE_IDS env var and CLI --node-id args, preserving order."""
    env_ids = os.environ.get("BRIDGE_K8S_NODE_IDS", "").strip()
    ids: list[str] = []
    if env_ids:
        ids.extend(part.strip() for part in env_ids.split(",") if part.strip())
    ids.extend(part.strip() for part in cli_node_ids if part.strip())
    seen: set[str] = set()
    ordered: list[str] = []
    for node_id in ids:
        if node_id not in seen:
            seen.add(node_id)
            ordered.append(node_id)
    return ordered


def _provision_k8s_nodes(
    client: BridgeClient,
    tenant_id: str,
    *,
    cli_node_ids: list[str] | None = None,
) -> ProvisionedNodes:
    """Provision or reuse k8s worker node(s) based on topology flow."""
    topologies = list_topologies(client)
    discovery = is_discovery_flow(topologies)
    import_flow = is_import_flow(topologies)
    node_type = _resolve_k8s_node_type(discovery_flow=discovery)

    existing_ids = _parse_k8s_node_ids(cli_node_ids=cli_node_ids or [])
    if existing_ids:
        print(
            f"[k8s] using existing node(s): {existing_ids} (skipping catalog allocate)",
            file=sys.stderr,
        )
        return ProvisionedNodes(
            discovery_flow=discovery, import_flow=import_flow,
            node_type=node_type, node_ids=existing_ids,
            vpc_id="n/a", subnet_id="", provisioned=False,
        )

    if os.environ.get("BRIDGE_K8S_SKIP_NODE_PROVISION", "").strip().lower() in {"1", "true", "yes"}:
        raise RuntimeError(
            "No node ids supplied and BRIDGE_K8S_SKIP_NODE_PROVISION is set. "
            "Provide --node-id / BRIDGE_K8S_NODE_IDS or unset the skip flag."
        )

    node_count = int(os.environ.get("BRIDGE_K8S_NODE_COUNT", "1"))
    if node_count < 1:
        raise RuntimeError(f"BRIDGE_K8S_NODE_COUNT must be >= 1, got {node_count}")

    epoch = int(time.time())
    vm_flavor = os.environ.get("BRIDGE_VM_FLAVOR", "gpu.1x")
    node_flavors_env = os.environ.get("BRIDGE_K8S_NODE_FLAVORS", "").strip()
    vm_flavors: list[str] = (
        [f.strip() for f in node_flavors_env.split(",") if f.strip()]
        if node_flavors_env
        else [vm_flavor]
    )
    converged_vpc_id = "n/a"
    converged_subnet_id = ""

    if node_type == "bareMetal":
        node_ids, vpc_id, subnet_id, converged_vpc_id, converged_subnet_id = provision_bm_node(
            client, tenant_id, epoch=epoch, discovery_flow=discovery,
            count=node_count, prefix="isv-k8s-bm",
            poll_interval=_BM_POLL_INTERVAL, poll_timeout=_BM_POLL_TIMEOUT,
            label="k8s_provision_bm",
        )
    else:
        node_ids, vpc_id, subnet_id = provision_vm_nodes(
            client, tenant_id, epoch=epoch, discovery_flow=discovery,
            vm_flavors=vm_flavors, count=node_count,
            name_prefix="isv-k8s-node", poll_timeout=_VM_POLL_TIMEOUT,
        )

    print(
        f"[k8s] provisioned {node_count} {node_type} node(s): {node_ids} "
        f"(import_flow={import_flow}, discovery_flow={discovery}, vpc_id={vpc_id})",
        file=sys.stderr,
    )
    return ProvisionedNodes(
        discovery_flow=discovery, import_flow=import_flow,
        node_type=node_type, node_ids=node_ids,
        vpc_id=vpc_id, subnet_id=subnet_id, provisioned=True,
        converged_vpc_id=converged_vpc_id, converged_subnet_id=converged_subnet_id,
        vm_name=f"isv-k8s-node-{epoch}" if node_type == "vm" else "",
    )


# Absolute path to acl_probe.py so K8sApiNetworkAclCheck can run it from any CWD
_ACL_PROBE_SCRIPT = str(_SCRIPT_DIR / "acl_probe.py")

_ACL_POLL_INTERVAL = 30
_ACL_POLL_TIMEOUT = 1800  # 30 min


def _provision_acl_probe_bm(
    client_b: BridgeClient,
    tenant_b: str,
    api_endpoint: str,
    state: dict[str, Any],
) -> str:
    """Allocate a BM node in Tenant B for K8sApiNetworkAclCheck's live probe.

    The BM is kept alive after this function returns. Its node_id is written
    into ``state`` (keys ``acl_probe_node_id``, ``acl_probe_tenant_b_id``,
    ``acl_probe_vpc_id``, ``acl_probe_subnet_id``, ``acl_probe_converged_vpc_id``)
    so that ``teardown.py`` can deallocate it later.

    Returns the full ``unauthorized_probe_cmd`` string for
    ``K8sApiNetworkAclCheck``, or an empty string if provisioning fails (the
    validation will auto-skip when the command is empty).
    """
    epoch = int(time.time())
    compute_vpc_id = ""
    converged_vpc_id = ""
    node_id = ""
    tenant_b_id = ""

    try:
        tenant_b_id = resolve_tenant_id(client_b, tenant_b)

        topologies = list_topologies(client_b)
        discovery = is_discovery_flow(topologies)

        subnet_ids: list[str] = []
        if discovery:
            compute_vpc_id, compute_subnet_id, converged_vpc_id, converged_subnet_id = (
                provision_discovery_vpcs(client_b, tenant_b_id, epoch=epoch, prefix="isv-acl-probe")
            )
            subnet_ids = [compute_subnet_id, converged_subnet_id]

        existing_ids = {compute_node_id(n) for n in list_computes(client_b, tenant_b_id)}
        existing_ids.discard("")

        explicit_flavor = os.environ.get("BRIDGE_BM_FLAVOR", "").strip()
        product_type_id, _, _ = discover_bm_product_type_id(
            client_b,
            explicit_id=explicit_flavor,
            gpu_type_filter=os.environ.get("BRIDGE_BM_GPU_TYPE", ""),
        )

        allocate_bm(
            client_b, tenant_b_id, product_type_id,
            count=1, subnet_ids=subnet_ids or None,
        )
        nodes = poll_until_bm_ready(
            client_b, tenant_b_id, product_type_id, existing_ids,
            count=1,
            require_mgmt_ip=True,
            label="acl_probe_node",
            interval=_ACL_POLL_INTERVAL,
            timeout=_ACL_POLL_TIMEOUT,
        )
        probe_node = nodes[0]
        node_id = compute_node_id(probe_node)
        mgmt_ip = node_mgmt_ip(probe_node)

        if not mgmt_ip:
            raise RuntimeError(
                f"ACL probe node {node_id} in Tenant B has no management IP after polling"
            )

        bm_user = os.environ.get("BRIDGE_BM_SSH_USER", "ubuntu").strip() or "ubuntu"

        state.update({
            "acl_probe_node_id": node_id,
            "acl_probe_node_type": "bm",
            "acl_probe_tenant_b_id": tenant_b_id,
            "acl_probe_vpc_id": compute_vpc_id,
            "acl_probe_converged_vpc_id": converged_vpc_id,
        })

        print(
            f"ACL probe BM provisioned in Tenant B: {mgmt_ip} ({node_id})",
            file=sys.stderr,
        )
        return (
            f"python3 {_ACL_PROBE_SCRIPT} "
            f"--bm-ip {mgmt_ip} "
            f"--bm-user {bm_user} "
            f"--api-endpoint {api_endpoint}"
        )

    except Exception as exc:
        print(f"WARNING: ACL probe BM provisioning failed: {exc}", file=sys.stderr)
        if node_id and tenant_b_id:
            try:
                deallocate_bm(client_b, tenant_b_id, node_id, label="acl_probe_cleanup")
            except Exception:
                pass
        if tenant_b_id:
            deprovision_discovery_vpcs(client_b, tenant_b_id, compute_vpc_id, converged_vpc_id)
        return ""


def _provision_acl_probe_vm(
    client_b: BridgeClient,
    tenant_b: str,
    api_endpoint: str,
    state: dict[str, Any],
) -> str:
    """Allocate a VM in Tenant B for K8sApiNetworkAclCheck's live probe.

    Uses SSH key auth (auto-generated key in ~/.cache/isvctl/vm-keys/acl-probe.pem).
    The VM is kept alive after this function returns. Its vm_id is written
    into ``state`` so that ``teardown.py`` can delete it later.

    Returns the full ``unauthorized_probe_cmd`` string, or an empty string if
    provisioning fails (the validation will auto-skip).
    """
    epoch = int(time.time())
    vpc_id = "n/a"
    vm_id = ""
    tenant_b_id = ""

    try:
        tenant_b_id = resolve_tenant_id(client_b, tenant_b)

        topologies = list_topologies(client_b)
        discovery = is_discovery_flow(topologies)

        vm_flavor = os.environ.get("BRIDGE_ACL_PROBE_VM_FLAVOR", "").strip()
        _, key_file = resolve_ssh_key("acl-probe")

        vm_ids, vpc_id, _ = provision_vm_nodes(
            client_b, tenant_b_id,
            epoch=epoch,
            discovery_flow=discovery,
            vm_flavor=vm_flavor,
            count=1,
            name_prefix="isv-acl-probe",
            poll_timeout=_ACL_POLL_TIMEOUT,
        )
        vm_id = vm_ids[0]

        from common.vm import get_vm, get_public_ip  # noqa: E402
        vm_data = get_vm(client_b, tenant_b_id, vm_id)
        probe_ip = get_public_ip(vm_data)
        vm_user = str(vm_data.get("userName") or "ubuntu")

        if not probe_ip:
            raise RuntimeError(
                f"ACL probe VM {vm_id} in Tenant B has no public IP after provisioning"
            )

        state.update({
            "acl_probe_node_id": vm_id,
            "acl_probe_node_type": "vm",
            "acl_probe_tenant_b_id": tenant_b_id,
            "acl_probe_vpc_id": vpc_id,
            "acl_probe_converged_vpc_id": "n/a",
        })

        print(
            f"ACL probe VM provisioned in Tenant B: {probe_ip} ({vm_id})",
            file=sys.stderr,
        )
        return (
            f"python3 {_ACL_PROBE_SCRIPT} "
            f"--bm-ip {probe_ip} "
            f"--bm-user {vm_user} "
            f"--key-file {key_file} "
            f"--api-endpoint {api_endpoint}"
        )

    except Exception as exc:
        print(f"WARNING: ACL probe VM provisioning failed: {exc}", file=sys.stderr)
        if vm_id:
            try:
                from common.vm import vm_path  # noqa: E402
                client_b.delete(vm_path(tenant_b_id, vm_id))
            except Exception:
                pass
        if tenant_b_id:
            deprovision_discovery_vpcs(client_b, tenant_b_id, vpc_id, "n/a")
        return ""


_MPI_OPERATOR_URL = (
    "https://raw.githubusercontent.com/kubeflow/mpi-operator/v0.5.0/deploy/v2beta1/mpi-operator.yaml"
)


def _install_mpi_operator(kubeconfig_path: Path) -> None:
    """Install Kubeflow MPI Operator on the cluster.

    The manifest is ~581 KB and includes the CRD with a large validation schema.
    Client-side ``kubectl apply`` silently truncates the
    ``last-applied-configuration`` annotation, causing the CRD to be omitted.
    ``--server-side`` avoids that limit and installs everything correctly.

    Required for K8sNcclMultiNodeWorkload. Opt-in via
    BRIDGE_K8S_INSTALL_MPI_OPERATOR=true. Failure is non-fatal — a warning
    is logged and setup continues so other tests are not blocked.
    """
    print("[k8s] installing MPI Operator (server-side apply)...", file=sys.stderr)
    try:
        result = subprocess.run(
            [
                "kubectl",
                f"--kubeconfig={kubeconfig_path}",
                "apply",
                "--server-side",
                "-f",
                _MPI_OPERATOR_URL,
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            print(
                f"[k8s] WARNING: MPI Operator install failed (non-fatal): {result.stderr.strip()}",
                file=sys.stderr,
            )
        else:
            print("[k8s] MPI Operator installed successfully", file=sys.stderr)
    except Exception as exc:
        print(f"[k8s] WARNING: MPI Operator install exception (non-fatal): {exc}", file=sys.stderr)


def _run_inventory(kubeconfig_path: Path, cluster_name: str) -> dict[str, Any]:
    """Run _common.sh via subprocess and return the parsed JSON inventory.

    Sets KUBECTL, CLUSTER_NAME, DEFAULT_GPU_NS, and REQUIRE_JQ in the
    subprocess environment. Raises RuntimeError if the script exits non-zero.
    Returns the full inventory dict (kubernetes{}, csi{}, cluster_name, etc.).
    """
    env = os.environ.copy()
    env["KUBECTL"] = f"kubectl --kubeconfig={kubeconfig_path}"
    env["CLUSTER_NAME"] = cluster_name
    env["DEFAULT_GPU_NS"] = "nvidia-gpu-operator"
    env["REQUIRE_JQ"] = "true"
    result = subprocess.run(
        ["bash", str(_SCRIPT_DIR / "_common.sh")],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Inventory collection failed: {result.stderr.strip() or result.stdout.strip()}"
        )
    return json.loads(result.stdout)


def _cluster_nodes(node_ids: list[str], node_type: str) -> list[dict[str, Any]]:
    """Build the node list payload for the Bridge cluster create API."""
    return [
        {"id": node_id, "isAllocated": True, "nodeType": node_type}
        for node_id in node_ids
    ]


@handle_bridge_errors
def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tenant", required=True)
    parser.add_argument(
        "--node-id",
        action="append",
        default=[],
        help="Existing BM/VM node UUID (repeatable). BRIDGE_K8S_NODE_IDS also accepted.",
    )
    args = parser.parse_args()

    if DEMO_MODE:
        result: dict[str, Any] = {
            "success": True,
            "platform": "kubernetes",
            "discovery_flow": False,
            "import_flow": True,
            "cluster_name": "demo-bridge-k8s",
            "cluster_id": "demo-cluster-id",
            "kubeconfig_path": str(default_kubeconfig_path()),
            "env_exports": {
                "KUBECONFIG": str(default_kubeconfig_path()),
            },
            "kubernetes": {
                "driver_version": "580.82.07",
                "node_count": 1,
                "nodes": ["demo-node"],
                "gpu_node_count": 1,
                "gpu_per_node": 1,
                "total_gpus": 1,
                "gpu_operator_namespace": "nvidia-gpu-operator",
                "control_plane_namespace": "kube-system",
                "runtime_class": "nvidia",
                "gpu_resource_name": "nvidia.com/gpu",
                "api_endpoint": "https://203.0.113.10:6443",
            },
            "csi": {
                "block_storage_class": "local-path",
                "shared_fs_storage_class": "",
                "nfs_storage_class": "",
                "static_volume_handle": "",
                "static_driver_name": "",
            },
            "unauthorized_probe_cmd": "",
        }
        print(json.dumps(result, indent=2))
        return 0

    client = BridgeClient.from_env()
    tenant_id = resolve_tenant_id(client, args.tenant)

    node_type = os.environ.get("BRIDGE_K8S_NODE_TYPE", "bm").strip().lower()
    node_count = int(os.environ.get("BRIDGE_K8S_NODE_COUNT", "1"))
    node_flavors = os.environ.get("BRIDGE_K8S_NODE_FLAVORS", "").strip() or os.environ.get("BRIDGE_VM_FLAVOR", "")
    node_ids_env = os.environ.get("BRIDGE_K8S_NODE_IDS", "").strip()
    tenant_b = os.environ.get("BRIDGE_TENANT_B", "").strip()
    probe_node_type = os.environ.get("BRIDGE_ACL_PROBE_NODE_TYPE", "bm").strip().lower()
    print_run_context("Kubernetes", {
        "BRIDGE_K8S_NODE_TYPE"       : node_type,
        "BRIDGE_K8S_NODE_COUNT"      : str(node_count),
        "BRIDGE_K8S_NODE_FLAVORS"    : node_flavors or "(default)",
        "BRIDGE_K8S_NODE_IDS"        : node_ids_env or "(provision new)",
        "BRIDGE_TENANT_B"            : tenant_b or "(not set — ACL probe skipped)",
        "BRIDGE_TENANT_B_USERNAME"   : os.environ.get("BRIDGE_TENANT_B_USERNAME", "(not set)") if tenant_b else "(n/a)",
        "BRIDGE_ACL_PROBE_NODE_TYPE" : probe_node_type if tenant_b else "(n/a)",
    })

    nodes_info = _provision_k8s_nodes(client, tenant_id, cli_node_ids=args.node_id)

    cluster_name = os.environ.get("BRIDGE_K8S_CLUSTER_NAME", f"isv-k8s-{int(time.time())}")
    install_gpu_tools = os.environ.get("BRIDGE_K8S_INSTALL_GPU_TOOLS", "true").lower() not in {
        "0",
        "false",
        "no",
    }
    deploy_local_provisioner = os.environ.get("BRIDGE_K8S_DEPLOY_LOCAL_PROVISIONER", "true").lower() not in {
        "0",
        "false",
        "no",
    }
    cluster_version = os.environ.get("BRIDGE_K8S_VERSION", "1.31")

    created = create_cluster(
        client,
        tenant_id,
        name=cluster_name,
        nodes=_cluster_nodes(nodes_info.node_ids, nodes_info.node_type),
        version=cluster_version,
        install_gpu_tools=install_gpu_tools,
        deploy_local_provisioner=deploy_local_provisioner,
    )
    cluster_id = extract_cluster_id(created)
    cluster = wait_cluster_running(client, tenant_id, cluster_id)

    kubeconfig_yaml = fetch_kubeconfig(client, tenant_id, cluster_id)
    kubeconfig_path = default_kubeconfig_path()
    if os.environ.get("KUBECONFIG_PATH"):
        kubeconfig_path = Path(os.environ["KUBECONFIG_PATH"]).expanduser()
    write_kubeconfig(kubeconfig_yaml, kubeconfig_path)
    wait_for_kubectl(kubeconfig_path)

    install_mpi = os.environ.get("BRIDGE_K8S_INSTALL_MPI_OPERATOR", "").strip().lower()
    if install_mpi in {"1", "true", "yes"}:
        _install_mpi_operator(kubeconfig_path)

    cluster_name = str(cluster.get("name") or created.get("name") or cluster_name)
    inventory = _run_inventory(kubeconfig_path, cluster_name)
    api_endpoint = extract_api_server(kubeconfig_yaml)
    if api_endpoint and isinstance(inventory.get("kubernetes"), dict):
        inventory["kubernetes"]["api_endpoint"] = api_endpoint

    # Provision a Tenant B node (BM or VM) for the K8sApiNetworkAclCheck live probe
    # when BRIDGE_TENANT_B is set. state_extra receives acl_probe_* keys to persist.
    # BRIDGE_ACL_PROBE_NODE_TYPE=vm → allocate a VM in Tenant B (key auth, default flavor).
    # BRIDGE_ACL_PROBE_NODE_TYPE=bm (default) → allocate a BM in Tenant B (password auth).
    # A separate BridgeClient is created for Tenant B using BRIDGE_TENANT_B_USERNAME /
    # BRIDGE_TENANT_B_PASSWORD (required) so Tenant B operations use the correct account.
    state_extra: dict[str, Any] = {}
    tenant_b = os.environ.get("BRIDGE_TENANT_B", "").strip()
    unauthorized_probe_cmd = ""
    if tenant_b:
        client_b = create_tenant_b_client()
        probe_node_type = os.environ.get("BRIDGE_ACL_PROBE_NODE_TYPE", "bm").strip().lower()
        if probe_node_type in {"vm", "virtualmachine"}:
            unauthorized_probe_cmd = _provision_acl_probe_vm(
                client_b, tenant_b, api_endpoint, state_extra
            )
        else:
            unauthorized_probe_cmd = _provision_acl_probe_bm(
                client_b, tenant_b, api_endpoint, state_extra
            )

    inventory.update(
        {
            "cluster_id": cluster_id,
            "kubeconfig_path": str(kubeconfig_path),
            "env_exports": {
                "KUBECONFIG": str(kubeconfig_path),
            },
            "discovery_flow": nodes_info.discovery_flow,
            "import_flow": nodes_info.import_flow,
            "node_type": nodes_info.node_type,
            "node_ids": nodes_info.node_ids,
            "vpc_id": nodes_info.vpc_id,
            "subnet_id": nodes_info.subnet_id,
            "converged_vpc_id": nodes_info.converged_vpc_id,
            "converged_subnet_id": nodes_info.converged_subnet_id,
            "unauthorized_probe_cmd": unauthorized_probe_cmd,
        }
    )

    save_state(
        {
            "tenant": args.tenant,
            "tenant_id": tenant_id,
            "cluster_id": cluster_id,
            "cluster_name": cluster_name,
            "kubeconfig_path": str(kubeconfig_path),
            "discovery_flow": nodes_info.discovery_flow,
            "import_flow": nodes_info.import_flow,
            "node_type": nodes_info.node_type,
            "node_ids": nodes_info.node_ids,
            "vpc_id": nodes_info.vpc_id,
            "subnet_id": nodes_info.subnet_id,
            "converged_vpc_id": nodes_info.converged_vpc_id,
            "converged_subnet_id": nodes_info.converged_subnet_id,
            "provisioned_nodes": nodes_info.provisioned,
            **state_extra,
        }
    )

    print(json.dumps(inventory, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
