#!/usr/bin/env python3
"""preflight — Armada Bridge security suite, setup phase.

Verifies the environment is ready before any security tests run.
Fails fast (exit 1) if any check fails — isvctl halts the suite.

Checks:
  1. BRIDGE_URL is set and reachable (HTTP response from Bridge)
  2. BRIDGE_USERNAME / BRIDGE_PASSWORD are valid (authenticated API call succeeds)
  3. bridge_tenant exists and is accessible

Output: {success, platform, test_name, checks}
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common.bridge_client import BridgeClient
from common.errors import handle_bridge_errors
from common.tenant import resolve_tenant_id

DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"


@handle_bridge_errors
def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tenant", required=True)
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "security",
        "test_name": "preflight",
        "checks": {},
    }

    if DEMO_MODE:
        result.update({
            "success": True,
            "checks": {
                "api_url_set": {"passed": True, "message": "BRIDGE_API_URL is set (demo)"},
                "api_reachable": {"passed": True, "message": "Bridge API reachable (demo)"},
                "tenant_accessible": {"passed": True, "message": f"Tenant '{args.tenant}' accessible (demo)"},
            },
        })
        print(json.dumps(result, indent=2))
        return 0

    checks: dict[str, Any] = {}

    # Check 1: BRIDGE_URL is set
    api_url = os.environ.get("BRIDGE_URL", "").strip()
    if not api_url:
        checks["api_url_set"] = {"passed": False, "message": "BRIDGE_URL env var is not set"}
        result["checks"] = checks
        result["error"] = "BRIDGE_URL is required"
        print(json.dumps(result, indent=2))
        return 1

    checks["api_url_set"] = {"passed": True, "message": f"BRIDGE_URL is set: {api_url}"}

    # Check 2: Credentials valid — make an authenticated call
    try:
        client = BridgeClient.from_env()
        client.get("/orchestrator/tenants")
        checks["api_reachable"] = {"passed": True, "message": "Bridge API reachable and token valid"}
    except Exception as exc:
        checks["api_reachable"] = {
            "passed": False,
            "message": f"Bridge API unreachable or token invalid: {str(exc)[:120]}",
        }
        result["checks"] = checks
        result["error"] = "Bridge API check failed — verify BRIDGE_URL, BRIDGE_USERNAME, and BRIDGE_PASSWORD"
        print(json.dumps(result, indent=2))
        return 1

    # Check 3: Tenant exists and is accessible
    try:
        tenant_id = resolve_tenant_id(client, args.tenant)
        checks["tenant_accessible"] = {
            "passed": True,
            "message": f"Tenant '{args.tenant}' resolved to {tenant_id}",
        }
    except Exception as exc:
        checks["tenant_accessible"] = {
            "passed": False,
            "message": f"Tenant '{args.tenant}' not found or not accessible: {str(exc)[:120]}",
        }
        result["checks"] = checks
        result["error"] = f"Tenant '{args.tenant}' is not accessible — verify bridge_tenant config"
        print(json.dumps(result, indent=2))
        return 1

    result.update({"success": True, "checks": checks})
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
