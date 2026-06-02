"""Tenant-related utilities for Armada Bridge provider scripts."""
from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .bridge_client import BridgeClient

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def resolve_tenant_id(client: BridgeClient, tenant: str) -> str:
    """Return the tenant UUID for the given name-or-UUID string.

    If tenant already matches UUID format, returns it unchanged.
    Otherwise fetches GET /orchestrator/tenants, finds the matching tenant
    by name, and returns its ID field.
    Raises ValueError if the name cannot be resolved.
    """
    if _UUID_RE.match(tenant):
        return tenant
    tenants = client.get("/orchestrator/tenants")
    tenants = tenants if isinstance(tenants, list) else []
    for t in tenants:
        if t.get("name") == tenant:
            tid = str(t.get("ID") or t.get("id") or "")
            if tid:
                return tid
    raise ValueError(
        f"Tenant '{tenant}' not found in tenant list. "
        "Set BRIDGE_TENANT to the tenant UUID or a valid tenant name."
    )
