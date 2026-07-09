"""Tenant-related utilities for Armada Bridge provider scripts."""
from __future__ import annotations

import os
import re
import ssl
import sys
import uuid
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .bridge_client import BridgeClient

_PLATFORM_TENANT_API_HINT = (
    "POST/GET /orchestrator/tenants requires a platform (super) admin account — "
    "not a tenant-scoped admin. Use the same credentials as bridge-api-test-automation "
    "(platform admin / admin login) or a user that can create tenants in the Bridge UI."
)

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def billing_tenant_create_enabled() -> bool:
    """True when BRIDGE_BILLING is set (matches bridge-api-test-automation BILLING=true)."""
    raw = os.environ.get("BRIDGE_BILLING", "").strip().lower()
    return raw in ("1", "true", "yes")


def billing_contact_email() -> str:
    """Return a valid billing contact email for M360 tenant create.

    M360 rejects non-email values. BRIDGE_USERNAME is often a short login (e.g.
    ``admin``), so only use it when it already looks like an email address.
    Matches bridge-api-test-automation default of ``test@test.com``.
    """
    explicit = os.environ.get("BRIDGE_BILLING_CONTACT_EMAIL", "").strip()
    if explicit:
        return explicit
    username = os.environ.get("BRIDGE_USERNAME", "").strip()
    if "@" in username:
        return username
    return "test@test.com"


def default_test_tenant_name(prefix: str = "isv-test-tenant") -> str:
    """Unique tenant name per run (avoids stale M360 billing account collisions)."""
    override = os.environ.get("BRIDGE_TEST_TENANT_NAME", "").strip()
    if override:
        return override
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def build_tenant_create_body(
    name: str,
    description: str,
    *,
    force_billing: bool = False,
) -> dict[str, Any]:
    """Build POST /orchestrator/tenants body (minimal or M360 billing form)."""
    use_billing = force_billing or billing_tenant_create_enabled()
    if not use_billing:
        return {"name": name, "description": description}

    contact_email = billing_contact_email()
    if force_billing and not billing_tenant_create_enabled():
        print(
            "[control_plane] cluster requires billing fields; using M360 billing form",
            file=sys.stderr,
        )
    else:
        print(
            "[control_plane] BRIDGE_BILLING=1: using extended tenant create payload (M360 billing form)",
            file=sys.stderr,
        )
    return {
        "name": name,
        "description": description,
        "currency": os.environ.get("BRIDGE_BILLING_CURRENCY", "US Dollar"),
        "taxStatus": os.environ.get("BRIDGE_BILLING_TAX_STATUS", "Tax Exempt"),
        "contactInfo": [
            {
                "uid": str(uuid.uuid4()),
                "firstName": os.environ.get("BRIDGE_BILLING_CONTACT_FIRST", "isv"),
                "lastName": os.environ.get("BRIDGE_BILLING_CONTACT_LAST", "test"),
                "emailTo": contact_email,
            }
        ],
        "address": {
            "city": os.environ.get("BRIDGE_BILLING_CITY", "bengaluru"),
            "countryId": os.environ.get("BRIDGE_BILLING_COUNTRY", "India"),
            "line1": os.environ.get("BRIDGE_BILLING_ADDRESS_LINE1", "111"),
            "state": os.environ.get("BRIDGE_BILLING_STATE", "Karnataka"),
            "addressType": "Bill To",
            "zipCode": os.environ.get("BRIDGE_BILLING_ZIP", "123456"),
        },
    }


def tenant_record_id(tenant: dict[str, Any]) -> str:
    """Return tenant UUID from a tenant dict (ID or id key)."""
    return str(tenant.get("ID") or tenant.get("id") or "")


def _list_tenants(client: BridgeClient) -> list[dict[str, Any]]:
    try:
        tenants = client.get("/orchestrator/tenants")
    except ValueError as exc:
        if "403" in str(exc):
            raise RuntimeError(f"{_PLATFORM_TENANT_API_HINT} API: {exc}") from exc
        raise
    if isinstance(tenants, list):
        return [t for t in tenants if isinstance(t, dict)]
    return []


def find_tenant_by_name(client: BridgeClient, tenant_name: str) -> dict[str, Any] | None:
    """Locate a tenant by name via GET /orchestrator/tenants."""
    from .iam import extract_tenant_from_tenants

    tenants = _list_tenants(client)
    return extract_tenant_from_tenants(tenants, tenant_name)


def _raise_tenant_create_error(exc: ValueError, tenant_name: str) -> None:
    msg = str(exc)
    if "403" in msg:
        raise RuntimeError(f"{_PLATFORM_TENANT_API_HINT} API: {msg}") from exc
    if "status 500" in msg and "billing" in msg.lower():
        raise RuntimeError(
            f"M360 billing account provisioning failed for tenant '{tenant_name}'. "
            f"Billing contact email is '{billing_contact_email()}' "
            "(override with BRIDGE_BILLING_CONTACT_EMAIL). "
            "A previous failed run may have left a stale M360 account for the same "
            "tenant name — this suite generates a unique name per run to avoid that. "
            f"API: {msg}"
        ) from exc
    raise exc


def create_or_resolve_tenant(
    client: BridgeClient,
    tenant_name: str,
    description: str,
) -> dict[str, Any]:
    """POST /orchestrator/tenants and return the tenant record (resolve from list if needed)."""
    body = build_tenant_create_body(tenant_name, description)
    tenant: dict[str, Any] | None = None

    try:
        tenant = client.post("/orchestrator/tenants", body)
    except ValueError as exc:
        msg = str(exc)
        if "status 400" in msg and "billing" in msg.lower() and not billing_tenant_create_enabled():
            body = build_tenant_create_body(tenant_name, description, force_billing=True)
            try:
                tenant = client.post("/orchestrator/tenants", body)
            except ValueError as retry_exc:
                _raise_tenant_create_error(retry_exc, tenant_name)
        elif "status 409" in msg or "status 422" in msg:
            tenant = find_tenant_by_name(client, tenant_name)
            if tenant is None:
                raise RuntimeError(
                    f"Tenant '{tenant_name}' returned conflict but was not found in "
                    "GET /orchestrator/tenants"
                ) from exc
        else:
            _raise_tenant_create_error(exc, tenant_name)

    if tenant is None or not tenant_record_id(tenant):
        tenant = find_tenant_by_name(client, tenant_name)
        if tenant is None:
            raise RuntimeError(
                f"POST /orchestrator/tenants for '{tenant_name}' did not return a tenant id "
                "and the tenant was not found via GET /orchestrator/tenants"
            )
    return tenant


def resolve_tenant_id(client: BridgeClient, tenant: str) -> str:
    """Return the tenant UUID for the given name-or-UUID string.

    If tenant already matches UUID format, returns it unchanged.
    Otherwise fetches GET /orchestrator/tenants, finds the matching tenant
    by name, and returns its ID field.
    Raises ValueError if the name cannot be resolved.
    """
    if _UUID_RE.match(tenant):
        return tenant
    try:
        tenants = client.get("/orchestrator/tenants")
    except ValueError as exc:
        if "403" in str(exc):
            raise RuntimeError(
                f"Cannot list tenants — the Bridge API requires super-admin access for "
                f"GET /orchestrator/tenants (got 403). "
                f"Set BRIDGE_TENANT (or BRIDGE_TENANT_B) to the tenant UUID directly "
                f"instead of the name '{tenant}'. "
                f"Find the UUID in the Bridge UI under tenant settings."
            ) from exc
        raise
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


def create_tenant_b_client() -> "BridgeClient":
    """Create a BridgeClient authenticated as the Tenant B user.

    Requires BRIDGE_TENANT_B_USERNAME and BRIDGE_TENANT_B_PASSWORD — no fallback
    to Tenant A credentials. Fails with a clear error if either is missing.

    Uses BRIDGE_TENANT_B_URL if set; otherwise falls back to BRIDGE_URL
    (same Bridge instance, different user account).

    The session is ephemeral (not cached to disk) to avoid polluting the
    Tenant A session cookie used by the main client.
    """
    from .bridge_client import BridgeClient  # local import to avoid circular dependency

    username = os.environ.get("BRIDGE_TENANT_B_USERNAME", "").strip()
    password = os.environ.get("BRIDGE_TENANT_B_PASSWORD", "").strip()
    missing = []
    if not username:
        missing.append("BRIDGE_TENANT_B_USERNAME")
    if not password:
        missing.append("BRIDGE_TENANT_B_PASSWORD")
    if missing:
        raise RuntimeError(
            f"Tenant B credentials required but not set: {', '.join(missing)}\n"
            "Set them before running the suite:\n"
            "  export BRIDGE_TENANT_B_USERNAME=<tenant-b-email>\n"
            "  export BRIDGE_TENANT_B_PASSWORD=<tenant-b-password>"
        )

    url = (
        os.environ.get("BRIDGE_TENANT_B_URL", "").strip()
        or os.environ.get("BRIDGE_URL", "").strip()
    )
    ssl_ctx: ssl.SSLContext | None = None
    if os.environ.get("BRIDGE_INSECURE") == "1":
        ssl_ctx = ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE
    host_header = os.environ.get("BRIDGE_HOST", "").strip() or None

    client = BridgeClient(
        base_url=url,
        username=username,
        password=password,
        totp_secret=os.environ.get("BRIDGE_TENANT_B_TOTP_SECRET"),
        ssl_context=ssl_ctx,
        cookie_path=None,  # ephemeral — do not overwrite Tenant A session on disk
        host_header=host_header,
    )
    client.login()
    return client
