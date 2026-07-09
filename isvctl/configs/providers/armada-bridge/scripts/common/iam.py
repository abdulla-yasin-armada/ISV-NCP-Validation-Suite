"""Bridge IAM helpers: tenant/org/user lookups, temp user lifecycle, and access probes.

Three groups of utilities:

  Lookup helpers (extract_*)
    Scan API list responses to find a specific tenant, org, or user by a key
    field. Return the matching dict or None — never raise.

  Temp user lifecycle (create_temp_user, create_tenant_user, delete_temp_user)
    Create short-lived TenantAdmin users for security test probing, then clean
    them up. Raise RuntimeError (with a human-readable message) when creation
    cannot proceed so callers can skip gracefully.

  Access probes (probe_denied, probe_allowed)
    Make a single GET request and return (passed, message). Used by security
    tests to assert that a tenant-scoped user can or cannot reach a resource.
"""
from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .bridge_client import BridgeClient

JsonList = list[dict[str, Any]]


def expect_json_list(value: dict[str, Any] | list[Any], label: str) -> JsonList:
    """Return value cast as JsonList, or raise ValueError if it is not a list."""
    if not isinstance(value, list):
        raise ValueError(f"{label}: expected JSON array, got {type(value).__name__}")
    return value


def extract_tenant_org(all_orgs: JsonList, tenant_id: str) -> dict[str, Any] | None:
    """Return the Keycloak org whose attributes['tenant ID'][0] matches tenant_id, or None."""
    for org in all_orgs:
        if not org.get("attributes"):
            continue

        tenant_ids = org["attributes"].get("tenant ID")
        if not tenant_ids:
            continue

        if len(tenant_ids) <= 0:
            continue

        if tenant_ids[0] != tenant_id:
            continue

        return org

    return None


def extract_tenant_from_tenants(tenants: JsonList, tenant_name: str) -> dict[str, Any] | None:
    """Return the tenant dict matching tenant_name by name or UUID (ID field), or None."""
    for tenant in tenants:
        if tenant.get("name") == tenant_name or tenant.get("ID") == tenant_name:
            return tenant
    return None


def extract_user_from_users(users: JsonList, user_email: str) -> dict[str, Any] | None:
    """Return the user dict whose 'email' field matches user_email, or None."""
    for user in users:
        if user.get("email") != user_email:
            continue
        return user

    return None


# ---------------------------------------------------------------------------
# Temp user lifecycle
# ---------------------------------------------------------------------------

def create_temp_user(
    admin_client: "BridgeClient",
    tenant_name: str,
    tenant_id: str,
) -> tuple[str, str, str]:
    """Create a TenantAdmin user scoped to tenant_id for least-privilege probing.

    Username pattern: isv-lp-{tenant}-{epoch}@isv.test
    Returns (user_id, email, password).
    Raises RuntimeError with a human-readable message if creation cannot proceed;
    callers should catch this and emit it as skip_reason.
    """
    from .constants import TEST_PASSWORD

    epoch = int(time.time())
    safe_tenant = tenant_name.replace(" ", "-").lower()[:30]
    username = f"isv-lp-{safe_tenant}-{epoch}"
    email = f"{username}@isv.test"

    orgs = expect_json_list(
        admin_client.get("/users/organizations"),
        "GET /users/organizations",
    )
    all_tenants = expect_json_list(
        admin_client.get("/orchestrator/tenants"),
        "GET /orchestrator/tenants",
    )
    tenant_info = extract_tenant_from_tenants(all_tenants, tenant_name)
    if tenant_info is None:
        tenant_info = next(
            (t for t in all_tenants if str(t.get("ID") or t.get("id") or "") == tenant_id),
            None,
        )
    if tenant_info is None:
        raise RuntimeError(f"Cannot find tenant '{tenant_name}' in tenant list — skipping")

    tenant_org = extract_tenant_org(orgs, tenant_info["ID"])
    if tenant_org is None:
        raise RuntimeError(
            f"No Keycloak organization found for tenant '{tenant_name}' — skipping"
        )

    body = {
        "username": username,
        "email": email,
        "firstName": "ISV",
        "lastName": "LPTest",
        "password": TEST_PASSWORD,
        "enabled": True,
        "emailVerified": False,
        "organization": tenant_org["id"],
        "roleRef": {
            "scope": "tenant",
            "name": "TenantAdmin",
            "tenant_id": tenant_info["ID"],
        },
    }

    try:
        admin_client.post("/users/create", body)
    except ValueError as exc:
        if "409" not in str(exc):
            raise RuntimeError(f"User creation failed: {exc}") from exc

    users = expect_json_list(admin_client.get("/users"), "GET /users")
    user_info = extract_user_from_users(users, email)
    if user_info is None:
        raise RuntimeError(f"Created user '{email}' not found in /users — skipping")

    return str(user_info["id"]), email, TEST_PASSWORD


def create_tenant_user(
    admin_client: "BridgeClient",
    tenant_name: str,
    tenant_id: str,
    label: str,
) -> tuple[str, str, str]:
    """Create a TenantAdmin user scoped to tenant_id for tenant-isolation probing.

    Username pattern: isv-ti-{label}-{tenant}-{epoch}@isv.test
    Returns (user_id, email, password).
    Raises RuntimeError with a human-readable message if creation cannot proceed;
    callers should catch this and emit it as skip_reason.
    """
    from .constants import TEST_PASSWORD

    epoch = int(time.time())
    safe_tenant = tenant_name.replace(" ", "-").lower()[:20]
    username = f"isv-ti-{label}-{safe_tenant}-{epoch}"
    email = f"{username}@isv.test"

    orgs = expect_json_list(
        admin_client.get("/users/organizations"),
        "GET /users/organizations",
    )
    all_tenants = expect_json_list(
        admin_client.get("/orchestrator/tenants"),
        "GET /orchestrator/tenants",
    )
    tenant_info = extract_tenant_from_tenants(all_tenants, tenant_name)
    if tenant_info is None:
        tenant_info = next(
            (t for t in all_tenants if str(t.get("ID") or t.get("id") or "") == tenant_id),
            None,
        )
    if tenant_info is None:
        raise RuntimeError(f"Cannot find tenant '{tenant_name}' in tenant list — skipping")

    tenant_org = extract_tenant_org(orgs, tenant_info["ID"])
    if tenant_org is None:
        raise RuntimeError(
            f"No Keycloak organization found for tenant '{tenant_name}' — skipping"
        )

    body = {
        "username": username,
        "email": email,
        "firstName": "ISV",
        "lastName": "TITest",
        "password": TEST_PASSWORD,
        "enabled": True,
        "emailVerified": False,
        "organization": tenant_org["id"],
        "roleRef": {
            "scope": "tenant",
            "name": "TenantAdmin",
            "tenant_id": tenant_info["ID"],
        },
    }

    try:
        admin_client.post("/users/create", body)
    except ValueError as exc:
        if "409" not in str(exc):
            raise RuntimeError(f"User creation failed for {label}: {exc}") from exc

    users = expect_json_list(admin_client.get("/users"), "GET /users")
    user_info = extract_user_from_users(users, email)
    if user_info is None:
        raise RuntimeError(f"Created user '{email}' not found in /users — skipping")

    return str(user_info["id"]), email, TEST_PASSWORD


def delete_temp_user(admin_client: "BridgeClient", user_id: str) -> None:
    """Delete a temp user by ID. Silently ignores all errors (best-effort cleanup)."""
    try:
        admin_client.delete(f"/users/{user_id}")
    except ValueError:
        pass


def probe_denied(client: "BridgeClient", path: str) -> tuple[bool, str]:
    """Return (True, msg) when GET path is denied (4xx or redirect with no data).

    Bridge returns 302 (redirect to SPA) for cross-tenant access instead of 403.
    urllib follows the redirect and returns HTML which bridge_client parses as {}.
    Treat empty response as correctly denied.
    """
    try:
        result = client.get(path)
        if not result:
            return True, f"GET {path} correctly denied (redirected — no data returned)"
        return False, f"GET {path} unexpectedly returned data — access not denied"
    except ValueError as exc:
        msg = str(exc)
        if any(code in msg for code in ("403", "404", "401", "302")):
            return True, f"GET {path} correctly denied"
        return False, f"GET {path} unexpected error: {msg[:80]}"


def probe_allowed(client: "BridgeClient", path: str) -> tuple[bool, str]:
    """Return (True, msg) when GET path succeeds (2xx)."""
    try:
        client.get(path)
        return True, f"GET {path} succeeded as expected"
    except ValueError as exc:
        return False, f"GET {path} failed unexpectedly: {str(exc)[:80]}"
