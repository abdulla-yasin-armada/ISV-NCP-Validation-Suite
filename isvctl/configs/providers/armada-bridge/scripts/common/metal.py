"""Bare-metal node lifecycle primitives for Armada Bridge.

Low-level helpers shared across K8s setup, teardown, and isolation tests.
All functions operate on a single tenant and raise on unexpected errors.

Functions:
  list_computes         GET /metal/computes for a tenant, return as list
  compute_node_id       Extract id/ID UUID from a compute dict
  node_mgmt_ip          Extract first available management IP from a compute dict
  allocate_bm           POST /metal/allocate (ignores 409 already-in-progress)
  poll_until_bm_ready   Poll until N new BM nodes reach ready state
  deallocate_bm         POST /metal/{id}/deallocate + optional poll until absent
"""
from __future__ import annotations

from typing import Any

from .bridge_client import BridgeClient
from .polling import poll_until

_BM_DONE_STATES = {"done", "success"}


def list_computes(client: BridgeClient, tenant_id: str) -> list[dict[str, Any]]:
    """GET /orchestrator/tenants/{tenant_id}/metal/computes and return as a list."""
    resp = client.get(f"/orchestrator/tenants/{tenant_id}/metal/computes")
    nodes = resp if isinstance(resp, list) else (resp or {}).get("data", [])
    return [node for node in nodes if isinstance(node, dict)]


def compute_node_id(node: dict[str, Any]) -> str:
    """Return the node UUID from a compute dict (id or ID key)."""
    return str(node.get("id", "") or node.get("ID", "") or "")


def node_mgmt_ip(node: dict[str, Any]) -> str:
    """Return the first available management IP from a compute dict.

    Checks externalIPAddress, inBandIP, ipAddress in that order.
    Returns empty string when none are set.
    """
    return str(
        node.get("externalIPAddress")
        or node.get("inBandIP")
        or node.get("ipAddress")
        or ""
    )


def allocate_bm(
    client: BridgeClient,
    tenant_id: str,
    product_type_id: str,
    *,
    count: int = 1,
    subnet_ids: list[str] | None = None,
) -> None:
    """POST /metal/allocate requesting `count` BM nodes.

    Ignores 409 (allocation already in progress — caller should poll for the
    node to appear rather than treating the conflict as an error).
    """
    body: dict[str, Any] = {
        "ProductTypeID": product_type_id,
        "computeNodeCount": count,
    }
    if subnet_ids:
        body["subnetIds"] = subnet_ids
    try:
        client.post(f"/orchestrator/tenants/{tenant_id}/metal/allocate", body)
    except ValueError as exc:
        if "status 409" not in str(exc):
            raise


def poll_until_bm_ready(
    client: BridgeClient,
    tenant_id: str,
    product_type_id: str,
    existing_ids: set[str],
    *,
    count: int = 1,
    require_mgmt_ip: bool = False,
    label: str = "bm",
    interval: int = 15,
    timeout: int = 540,
) -> list[dict[str, Any]]:
    """Poll until `count` new BM nodes with product_type_id reach ready state.

    A node qualifies when:
      - its UUID is not in existing_ids (i.e. it is newly allocated)
      - allocateStatus is in {done, success}
      - if require_mgmt_ip=True: a management IP is present

    Returns a list of ready node dicts (length == count).
    Raises RuntimeError if timeout is reached before enough nodes are ready.
    """

    def check() -> tuple[bool, list[dict[str, Any]] | None, str]:
        all_nodes = list_computes(client, tenant_id)
        ready: list[dict[str, Any]] = []
        pending: list[dict[str, Any]] = []
        for node in all_nodes:
            nid = compute_node_id(node)
            if not nid or nid in existing_ids:
                continue
            if str(node.get("productTypeId", "") or "") != product_type_id:
                continue
            pending.append(node)
            if str(node.get("allocateStatus", "") or "").lower() not in _BM_DONE_STATES:
                continue
            if require_mgmt_ip and not node_mgmt_ip(node):
                continue
            ready.append(node)

        if len(ready) >= count:
            return True, ready[:count], f"{count} node(s) ready"

        statuses = {str(n.get("allocateStatus", "")) for n in pending}
        return False, None, (
            f"{len(ready)}/{count} nodes ready for productTypeId={product_type_id!r} "
            f"(statuses: {statuses or 'none visible'})"
        )

    result = poll_until(check, label=label, interval=interval, timeout=timeout)
    if result is None:
        raise RuntimeError(
            f"BM node(s) did not reach ready state within {timeout}s "
            f"(productTypeId={product_type_id!r}, count={count})"
        )
    return result


def deallocate_bm(
    client: BridgeClient,
    tenant_id: str,
    node_id: str,
    *,
    poll: bool = True,
    poll_timeout: int = 300,
    poll_interval: int = 15,
    label: str = "bm_deallocate",
) -> None:
    """POST /metal/{node_id}/deallocate and optionally poll until node is absent.

    Ignores 404 (node already gone). When poll=True (default), waits until
    the node disappears from the compute list before returning — ensures
    subsequent VPC/subnet deletes don't race with node cleanup.
    Pass poll=False for fire-and-forget (caller handles its own wait/sleep).
    """
    try:
        client.post(
            f"/orchestrator/tenants/{tenant_id}/metal/{node_id}/deallocate",
            {},
        )
    except ValueError as exc:
        if "404" not in str(exc):
            raise

    if not poll:
        return

    def check_gone() -> tuple[bool, Any, str]:
        computes = client.get(f"/orchestrator/tenants/{tenant_id}/metal/computes")
        nodes = computes if isinstance(computes, list) else (computes or {}).get("data", [])
        node = next(
            (n for n in nodes if str(n.get("id", "") or n.get("ID", "")) == node_id),
            None,
        )
        if node is None:
            return True, "absent", "node absent from list"
        return False, None, f"allocateStatus={str(node.get('allocateStatus', '') or '')!r}"

    poll_until(
        check_gone,
        label=label,
        interval=poll_interval,
        timeout=poll_timeout,
    )
