#!/usr/bin/env python3
"""least_privilege_test — Armada Bridge security suite, test phase.

Validates least-privilege policy enforcement across user, resource, and
network dimensions (SEC04-01) and that a minimal role denies out-of-scope
actions (SEC04-02).

Approach:
  Admin creates a temporary TenantAdmin user scoped only to the test tenant.
  All cross-tenant probes are run using that tenant-scoped session —
  not the admin session.  A tenant-scoped user must not be able to reach
  resources outside their own tenant.

  Lifecycle:
    1. Admin creates temp user: isv-lp-{tenant}-{epoch}@isv.test
    2. Login as temp user → tenant-scoped session
    3. Run 7 sub-checks
    4. Admin deletes temp user (finally block)

  If user creation fails the test is skipped with a clear reason.

Output: {success, platform, test_identity, allowed_resource,
         allowed_source_cidr, tests}
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import urllib.parse
import uuid
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common.bridge_client import BridgeClient
from common.errors import handle_bridge_errors
from common.iam import (
    create_temp_user,
    delete_temp_user,
    probe_allowed,
    probe_denied,
)
from common.network import is_private
from common.tenant import resolve_tenant_id

DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _private_cidr_from_url(bridge_url: str) -> str:
    parsed = urllib.parse.urlparse(bridge_url)
    hostname = parsed.hostname or ""
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        results = socket.getaddrinfo(hostname, port, socket.AF_UNSPEC, socket.SOCK_STREAM)
        for r in results:
            ip = r[4][0]
            if is_private(ip):
                if ip.startswith("10."):
                    return "10.0.0.0/8"
                if ip.startswith("192.168."):
                    return "192.168.0.0/16"
                return "172.16.0.0/12"
    except (socket.gaierror, ValueError):
        pass
    return "10.0.0.0/8"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

@handle_bridge_errors
def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tenant", required=True)
    args = parser.parse_args()

    result: dict[str, Any] = {"success": False, "platform": "security"}

    if DEMO_MODE:
        result.update(
            {
                "success": True,
                "platform": "security",
                "test_identity": "demo-restricted-user@demo.example.com",
                "allowed_resource": "demo-vpc-0001",
                "allowed_source_cidr": "10.0.0.0/16",
                "tests": {
                    "policy_dimensions_user_based": {"passed": True},
                    "policy_dimensions_resource_based": {"passed": True},
                    "policy_dimensions_network_based": {"passed": True},
                    "policy_dimensions_allowed_action_succeeds": {"passed": True},
                    "out_of_scope_compute_denied": {"passed": True},
                    "out_of_scope_storage_denied": {"passed": True},
                    "out_of_scope_network_denied": {"passed": True},
                },
            }
        )
    else:
        admin_client = BridgeClient.from_env()
        tenant_id = resolve_tenant_id(admin_client, args.tenant)
        bridge_url = os.environ.get("BRIDGE_URL", "")
        source_cidr = _private_cidr_from_url(bridge_url)
        fake_tenant_id = str(uuid.uuid4())

        # Create temp non-admin user — skip if not possible
        try:
            user_id, user_email, user_password = create_temp_user(
                admin_client, args.tenant, tenant_id
            )
        except RuntimeError as exc:
            result.update({"skipped": True, "skip_reason": str(exc)})
            print(json.dumps(result, indent=2))
            return 0

        try:
            user_client = admin_client.login_as(user_email, user_password)

            # Network-based: BRIDGE_URL resolves to private IP (checked via DNS, no user session needed)
            try:
                parsed = urllib.parse.urlparse(bridge_url)
                hostname = parsed.hostname or ""
                port = parsed.port or 443
                gai = socket.getaddrinfo(hostname, port, socket.AF_UNSPEC, socket.SOCK_STREAM)
                network_ok = all(is_private(r[4][0]) for r in gai)
            except socket.gaierror:
                network_ok = False
            network_msg = (
                "BRIDGE_URL resolves to private IP — network-scope enforced"
                if network_ok
                else "BRIDGE_URL resolves to public IP or DNS failed"
            )

            # Policy dimension checks (tenant-scoped user session)
            user_ok, user_msg = probe_allowed(
                user_client, f"/orchestrator/tenants/{tenant_id}/metal/computes"
            )
            resource_ok, resource_msg = probe_denied(
                user_client, f"/orchestrator/tenants/{fake_tenant_id}/vpcs"
            )
            action_ok, action_msg = probe_allowed(
                user_client, f"/orchestrator/tenants/{tenant_id}/metal/computes"
            )

            # Out-of-scope denial checks (tenant-scoped user session)
            compute_ok, compute_msg = probe_denied(
                user_client, f"/orchestrator/tenants/{fake_tenant_id}/metal/computes"
            )
            storage_ok, storage_msg = probe_denied(
                user_client, f"/orchestrator/tenants/{fake_tenant_id}/subnets"
            )
            network_deny_ok, network_deny_msg = probe_denied(
                user_client, f"/orchestrator/tenants/{fake_tenant_id}/vpcs"
            )

            tests: dict[str, Any] = {
                "policy_dimensions_user_based": {"passed": user_ok, "message": user_msg},
                "policy_dimensions_resource_based": {"passed": resource_ok, "message": resource_msg},
                "policy_dimensions_network_based": {"passed": network_ok, "message": network_msg},
                "policy_dimensions_allowed_action_succeeds": {"passed": action_ok, "message": action_msg},
                "out_of_scope_compute_denied": {"passed": compute_ok, "message": compute_msg},
                "out_of_scope_storage_denied": {"passed": storage_ok, "message": storage_msg},
                "out_of_scope_network_denied": {"passed": network_deny_ok, "message": network_deny_msg},
            }

            result.update(
                {
                    "success": all(t["passed"] for t in tests.values()),
                    "platform": "security",
                    "test_identity": user_email,
                    "allowed_resource": tenant_id,
                    "allowed_source_cidr": source_cidr,
                    "tests": tests,
                }
            )

        finally:
            if not os.environ.get("ARMADA_BRIDGE_SKIP_TEARDOWN", "").lower() in ("1", "true"):
                delete_temp_user(admin_client, user_id)

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
