#!/usr/bin/env python3
"""create_user — Armada Bridge IAM suite, setup phase.

Bridge flow:
  Step 1: POST /users/create
          Body: {username, email, firstName, lastName, password, enabled, ...}
  Step 2: GET /users → find user by email → user_id
  Step 3: login_as(new_user) via POST /auth/login (session cookie)
  Step 4: POST /key-manager/api-key as the new user → secret_access_key

Output: {success, user_id, username, access_key_id, secret_access_key, platform: "iam"}

Note: access_key_id is the user email (credential_id for test_credentials).
      delete_user.py calls DELETE /users/:userId which cleans up API keys too.
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
from common.iam import (
    expect_json_list,
    extract_tenant_from_tenants,
    extract_tenant_org,
    extract_user_from_users,
)

DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"


def _fail(result: dict[str, Any]) -> int:
    print(json.dumps(result, indent=2))
    return 1


@handle_bridge_errors
def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--username", default="isv-test-user")
    args = parser.parse_args()

    result: dict[str, Any] = {"success": False, "platform": "iam"}

    if DEMO_MODE:
        result.update(
            {
                "success": True,
                "user_id": "demo-user-uuid-0001",
                "username": f"{args.username}@demo.example.com",
                "access_key_id": "demo-user-uuid-0001",
                "secret_access_key": "demo-secret-key-armada-0001",
            }
        )
    else:
        admin_client = BridgeClient.from_env()
        user_email = f"{args.username}@{args.tenant}.example.com"

        # Step 1: Create user
        try:
            all_tenants = expect_json_list(
                admin_client.get("/orchestrator/tenants"),
                "GET /orchestrator/tenants",
            )
            tenant_info = extract_tenant_from_tenants(all_tenants, args.tenant)
            if tenant_info is None:
                raise ValueError(f"Tenant '{args.tenant}' not found")

            orgs = expect_json_list(
                admin_client.get("/users/organizations"),
                "GET /users/organizations",
            )
            tenant_org = extract_tenant_org(orgs, tenant_info["ID"])
            if tenant_org is None:
                raise ValueError(f"Tenant organization for tenant '{args.tenant}' not found")

            create_user_dto = CreateUserDTO(
                username=args.username,
                email=user_email,
                first_name=args.username,
                last_name="TestUser",
                password=TEST_PASSWORD,
                enabled=True,
                email_verified=False,
                org_id=tenant_org["id"],
                role_ref={
                    "scope": "tenant",
                    "name": "TenantAdmin",
                    "tenant_id": tenant_info["ID"],
                },
            )

            try:
                admin_client.post("/users/create", create_user_dto.to_dict())
            except Exception as e:
                if "status 409" not in str(e):
                    raise
                # user already exists from a prior run; proceed to lookup

        except Exception as e:
            result.update({"error": f"User create failed: {e}"})
            return _fail(result)

        # Step 2: Get user details
        try:
            users = expect_json_list(admin_client.get("/users"), "GET /users")
            user_info = extract_user_from_users(users, user_email)
            if user_info is None:
                raise ValueError(f"User with email '{user_email}' not found")

            result["username"] = user_info["email"]
            result["user_id"] = user_info["id"]
            result["access_key_id"] = user_info["email"]

        except Exception as e:
            result.update({"error": f"Failed to find user {args.username} info: {e}"})
            return _fail(result)

        # Step 3: Create API key
        try:
            user_client = admin_client.login_as(user_info["email"], TEST_PASSWORD)
            try:
                api_key = user_client.post("/key-manager/api-key", {})
                result["secret_access_key"] = api_key["key"]
            except Exception as e:
                if "status 409" not in str(e):
                    raise
                user_client.delete("/key-manager/api-key")
                api_key = user_client.post("/key-manager/api-key", {})
                result["secret_access_key"] = api_key["key"]

        except Exception as e:
            result.update({"error": f"Failed to create API key for user {args.username}: {e}"})
            return _fail(result)

        result["success"] = True

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


class CreateUserDTO:
    def __init__(
        self,
        *,
        username: str,
        email: str,
        first_name: str,
        last_name: str,
        password: str,
        enabled: bool,
        email_verified: bool,
        org_id: str,
        role_ref: dict[str, str],
    ) -> None:
        self.first_name = first_name
        self.last_name = last_name
        self.username = username
        self.email = email
        self.password = password
        self.enabled = enabled
        self.email_verified = email_verified
        self.org_id = org_id
        self.role_ref = role_ref

    def to_dict(self) -> dict[str, Any]:
        return {
            "username": self.username,
            "email": self.email,
            "firstName": self.first_name,
            "lastName": self.last_name,
            "password": self.password,
            "enabled": self.enabled,
            "emailVerified": self.email_verified,
            "organization": self.org_id,
            "roleRef": {
                "scope": self.role_ref["scope"],
                "name": self.role_ref["name"],
                "tenant_id": self.role_ref["tenant_id"],
            },
        }


if __name__ == "__main__":
    sys.exit(main())
