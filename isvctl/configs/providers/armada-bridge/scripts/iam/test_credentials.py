#!/usr/bin/env python3
"""test_credentials — Armada Bridge IAM suite, test phase.

Proves the API key from create_user is valid:

  Step 1: login_as(credential_id, TEST_PASSWORD) via BridgeClient
          Establishes a session — proves the user account is active.

  Step 2: GET /key-manager/api-key
          Returns the stored API key.
          Compared against credential_secret — proves the issued key is stored.

Output: {success, authenticated, account_id, identity_id, platform: "iam"}
"""
import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common.bridge_client import BridgeClient
from common.errors import handle_bridge_errors
from common.constants import TEST_PASSWORD

DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"


@handle_bridge_errors
def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--credential-id", required=True)
    parser.add_argument("--credential-secret", required=True)
    args = parser.parse_args()

    result: dict[str, Any] = {"success": False, "platform": "iam"}

    if DEMO_MODE:
        result.update(
            {
                "success": True,
                "authenticated": True,
                "account_id": args.credential_id,
                "identity_id": args.credential_id,
            }
        )
        print(json.dumps(result, indent=2))
        return 0

    # Step 1: log in as the test user using BridgeClient (same path as create_user)
    try:
        user_client = BridgeClient.from_env().login_as(args.credential_id, TEST_PASSWORD)
    except Exception as e:
        result.update({"error": f"Login failed: {e}", "authenticated": False})
        print(json.dumps(result, indent=2))
        return 1

    # Step 2: retrieve and verify the stored API key.
    # /key-manager/api-key returns plain text, not JSON, so use the client's
    # internal opener directly to get the raw response.
    try:
        import urllib.request as _urllib_request
        req = _urllib_request.Request(
            user_client.base_url + "/key-manager/api-key",
            method="GET",
        )
        user_client._with_host(req)
        with user_client._opener.open(req, timeout=30) as resp:
            stored_key = resp.read().decode().strip()
    except Exception as e:
        result.update({"error": f"API key retrieval failed: {e}", "authenticated": False})
        print(json.dumps(result, indent=2))
        return 1

    if not stored_key:
        result.update({"error": "No API key stored for user", "authenticated": False})
        print(json.dumps(result, indent=2))
        return 1

    if stored_key != args.credential_secret:
        result.update({"error": "Stored API key does not match issued credential", "authenticated": False})
        print(json.dumps(result, indent=2))
        return 1

    result.update(
        {
            "success": True,
            "authenticated": True,
            "account_id": args.credential_id,
            "identity_id": args.credential_id,
        }
    )
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
