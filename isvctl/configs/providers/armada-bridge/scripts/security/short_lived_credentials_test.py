#!/usr/bin/env python3
"""short_lived_credentials_test — Armada Bridge security suite, test phase.

Validates that node and workload credentials issued by Keycloak have finite,
bounded TTLs.

Approach:
  POST to KC_TOKEN_URL with grant_type=password using BRIDGE_USERNAME /
  BRIDGE_PASSWORD.  The token response contains:
    - expires_in          → access token TTL  (node credential)
    - refresh_expires_in  → refresh token TTL (workload credential)
  Both must be present (non-zero) and within max_ttl_seconds (86400 = 24h).

  If Direct Access Grants are disabled on the Keycloak client the POST
  returns an error — the script emits skipped=true rather than failing.

  KC_CLIENT_ID defaults to "GPUaaS" (the Bridge Keycloak client).
  Override via KC_CLIENT_ID env var if needed.

  If the Keycloak client has "Client authentication" enabled (confidential
  client), set KC_CLIENT_SECRET to the client secret from:
  Keycloak Admin → Clients → GPUaaS → Credentials → Client secret.

Output: {success, platform, node_credential_ttl_seconds,
         workload_credential_ttl_seconds, max_ttl_seconds, tests}
"""
import argparse
import json
import os
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common.errors import handle_bridge_errors

DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"
MAX_TTL_SECONDS = 86400  # 24 hours — upper bound for "short-lived"
DEFAULT_KC_CLIENT_ID = "GPUaaS"


def _fetch_token(
    token_url: str,
    client_id: str,
    username: str,
    password: str,
    client_secret: str = "",
) -> dict[str, Any]:
    """POST password grant to Keycloak token endpoint.

    Returns the parsed token response dict.
    Raises ValueError with a descriptive message on HTTP error.
    Raises RuntimeError with skip_reason when Direct Access Grants are disabled.

    client_secret is required when the Keycloak client has
    "Client authentication" enabled (confidential client).
    """
    params: dict[str, str] = {
        "grant_type": "password",
        "client_id": client_id,
        "username": username,
        "password": password,
    }
    if client_secret:
        params["client_secret"] = client_secret
    body = urllib.parse.urlencode(params).encode()

    req = urllib.request.Request(
        token_url,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )

    ssl_context: ssl.SSLContext | None = None
    if os.environ.get("BRIDGE_INSECURE") == "1":
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE

    try:
        with urllib.request.urlopen(req, context=ssl_context, timeout=30) as resp:  # noqa: S310
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {}
        error = payload.get("error", "")
        description = payload.get("error_description", raw)
        if error == "unauthorized_client" or "DIRECT ACCESS" in description.upper():
            raise RuntimeError(
                f"Direct Access Grants not enabled on client '{client_id}': {description}"
            ) from exc
        # Cloudflare or network-level block (no JSON KC error body)
        if not error and exc.code in (403, 503):
            raise RuntimeError(
                f"Keycloak endpoint blocked by network/firewall (HTTP {exc.code}): {description[:120]}"
            ) from exc
        raise ValueError(
            f"Keycloak token request failed ({exc.code}): {description}"
        ) from exc


@handle_bridge_errors
def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--kc-token-url", required=True)
    args = parser.parse_args()

    result: dict[str, Any] = {"success": False, "platform": "security"}

    if DEMO_MODE:
        result.update(
            {
                "success": True,
                "platform": "security",
                "node_credential_ttl_seconds": 3600,
                "workload_credential_ttl_seconds": 3600,
                "max_ttl_seconds": MAX_TTL_SECONDS,
                "tests": {
                    "node_credential_has_expiry": {"passed": True},
                    "node_credential_ttl_within_bound": {"passed": True},
                    "workload_credential_has_expiry": {"passed": True},
                    "workload_credential_ttl_within_bound": {"passed": True},
                },
            }
        )
    else:
        username = os.environ.get("BRIDGE_USERNAME", "")
        password = os.environ.get("BRIDGE_PASSWORD", "")
        client_id = os.environ.get("KC_CLIENT_ID", DEFAULT_KC_CLIENT_ID)
        client_secret = os.environ.get("KC_CLIENT_SECRET", "")

        if not username or not password:
            result.update(
                {
                    "skipped": True,
                    "skip_reason": "BRIDGE_USERNAME or BRIDGE_PASSWORD not set — cannot fetch KC token",
                }
            )
            print(json.dumps(result, indent=2))
            return 0

        try:
            token_resp = _fetch_token(
                args.kc_token_url, client_id, username, password,
                client_secret=client_secret,
            )
        except RuntimeError as exc:
            result.update({"skipped": True, "skip_reason": str(exc)})
            print(json.dumps(result, indent=2))
            return 0

        # access token TTL → node credential
        node_ttl = token_resp.get("expires_in")
        # refresh token TTL → workload credential
        workload_ttl = token_resp.get("refresh_expires_in")

        node_has_expiry = isinstance(node_ttl, int) and node_ttl > 0
        workload_has_expiry = isinstance(workload_ttl, int) and workload_ttl > 0
        node_within_bound = node_has_expiry and node_ttl <= MAX_TTL_SECONDS
        workload_within_bound = workload_has_expiry and workload_ttl <= MAX_TTL_SECONDS

        tests: dict[str, Any] = {
            "node_credential_has_expiry": {
                "passed": node_has_expiry,
                "ttl_seconds": node_ttl,
                "message": (
                    f"Access token expires_in={node_ttl}s"
                    if node_has_expiry
                    else "Access token has no expires_in"
                ),
            },
            "node_credential_ttl_within_bound": {
                "passed": node_within_bound,
                "ttl_seconds": node_ttl,
                "max_ttl_seconds": MAX_TTL_SECONDS,
                "message": (
                    f"Access token TTL {node_ttl}s ≤ {MAX_TTL_SECONDS}s"
                    if node_within_bound
                    else f"Access token TTL {node_ttl}s exceeds max {MAX_TTL_SECONDS}s"
                ),
            },
            "workload_credential_has_expiry": {
                "passed": workload_has_expiry,
                "ttl_seconds": workload_ttl,
                "message": (
                    f"Refresh token refresh_expires_in={workload_ttl}s"
                    if workload_has_expiry
                    else "Refresh token has no refresh_expires_in"
                ),
            },
            "workload_credential_ttl_within_bound": {
                "passed": workload_within_bound,
                "ttl_seconds": workload_ttl,
                "max_ttl_seconds": MAX_TTL_SECONDS,
                "message": (
                    f"Refresh token TTL {workload_ttl}s ≤ {MAX_TTL_SECONDS}s"
                    if workload_within_bound
                    else f"Refresh token TTL {workload_ttl}s exceeds max {MAX_TTL_SECONDS}s"
                ),
            },
        }

        result.update(
            {
                "success": all(t["passed"] for t in tests.values()),
                "platform": "security",
                "node_credential_ttl_seconds": node_ttl,
                "workload_credential_ttl_seconds": workload_ttl,
                "max_ttl_seconds": MAX_TTL_SECONDS,
                "tests": tests,
            }
        )

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
