#!/usr/bin/env python3
"""check_api — Armada Bridge control-plane suite, setup phase.

Probes two Bridge health endpoints:
  GET /health             → auth-gateway liveness
  GET /orchestrator/health-check → orchestrator liveness

Output: {success, platform, account_id, tests: {auth_gateway_health, orchestrator_health}}
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common.bridge_client import BridgeClient
from common.context import print_run_context
from common.errors import handle_bridge_errors
from common.tenant import billing_contact_email, billing_tenant_create_enabled

DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"


def _probe(client: BridgeClient, path: str) -> tuple[bool, str]:
    """GET a health endpoint using the authenticated session; return (passed, message).

    Uses client._opener so the session cookie is sent. Treats any 2xx as success
    regardless of response body format (health endpoints may return plain text).
    """
    req = urllib.request.Request(client.base_url + path, method="GET")
    try:
        with client._opener.open(req, timeout=10) as resp:
            resp.read()
            return True, f"GET {path} returned OK"
    except urllib.error.HTTPError as e:
        e.read()
        return False, f"GET {path} returned HTTP {e.code}"
    except Exception as e:
        return False, f"GET {path} connection failed: {type(e).__name__}: {e}"


@handle_bridge_errors
def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tenant", required=True)
    args = parser.parse_args()

    print_run_context(
        "Control Plane",
        {
            "test_tenant_prefix": "isv-test-tenant (auto-suffixed per run)",
            "BRIDGE_BILLING": "true" if billing_tenant_create_enabled() else "false",
            "billing_contact_email": billing_contact_email(),
        },
    )

    result: dict[str, Any] = {"success": False, "platform": "control_plane"}

    if DEMO_MODE:
        result.update(
            {
                "success": True,
                "platform": "control_plane",
                "account_id": "armada-bridge-demo",
                "tests": {
                    "auth_gateway_health": {
                        "passed": True,
                        "message": "GET /health returned OK",
                    },
                    "orchestrator_health": {
                        "passed": True,
                        "message": "GET /orchestrator/health-check returned healthy",
                    },
                },
            }
        )
    else:
        client = BridgeClient.from_env()

        gw_passed, gw_msg = _probe(client, "/health")
        orch_passed, orch_msg = _probe(client, "/orchestrator/health-check")

        result.update(
            {
                "success": gw_passed and orch_passed,
                "account_id": os.environ.get("BRIDGE_USERNAME", ""),
                "tests": {
                    "auth_gateway_health": {"passed": gw_passed, "message": gw_msg},
                    "orchestrator_health": {"passed": orch_passed, "message": orch_msg},
                },
            }
        )

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
