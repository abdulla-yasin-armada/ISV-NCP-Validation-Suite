# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary

"""Tests for Armada Bridge tenant create helpers (control-plane)."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ISVCTL_ROOT = Path(__file__).resolve().parents[1]
BRIDGE_SCRIPTS = ISVCTL_ROOT / "configs" / "providers" / "armada-bridge" / "scripts"

scripts_path = str(BRIDGE_SCRIPTS)
if scripts_path not in sys.path:
    sys.path.insert(0, scripts_path)

from common.tenant import (  # noqa: E402
    billing_contact_email,
    billing_tenant_create_enabled,
    build_tenant_create_body,
    create_or_resolve_tenant,
    default_test_tenant_name,
    tenant_record_id,
)


class TestBillingTenantCreateEnabled:
    def test_disabled_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("BRIDGE_BILLING", raising=False)
        assert billing_tenant_create_enabled() is False

    @pytest.mark.parametrize("value", ["1", "true", "yes", "TRUE"])
    def test_enabled_values(self, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
        monkeypatch.setenv("BRIDGE_BILLING", value)
        assert billing_tenant_create_enabled() is True


class TestBillingContactEmail:
    def test_explicit_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BRIDGE_BILLING_CONTACT_EMAIL", "billing@example.com")
        assert billing_contact_email() == "billing@example.com"

    def test_username_email_used_when_valid(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("BRIDGE_BILLING_CONTACT_EMAIL", raising=False)
        monkeypatch.setenv("BRIDGE_USERNAME", "admin@bridge.example.com")
        assert billing_contact_email() == "admin@bridge.example.com"

    def test_short_username_falls_back_to_test_email(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("BRIDGE_BILLING_CONTACT_EMAIL", raising=False)
        monkeypatch.setenv("BRIDGE_USERNAME", "admin")
        assert billing_contact_email() == "test@test.com"


class TestDefaultTestTenantName:
    def test_auto_suffix(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("BRIDGE_TEST_TENANT_NAME", raising=False)
        name = default_test_tenant_name("isv-test-tenant")
        assert name.startswith("isv-test-tenant-")
        assert len(name) == len("isv-test-tenant-") + 8

    def test_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BRIDGE_TEST_TENANT_NAME", "fixed-tenant")
        assert default_test_tenant_name() == "fixed-tenant"


class TestBuildTenantCreateBody:
    def test_minimal_payload(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("BRIDGE_BILLING", raising=False)
        body = build_tenant_create_body("isv-test-tenant", "desc")
        assert body == {"name": "isv-test-tenant", "description": "desc"}

    def test_billing_payload(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BRIDGE_BILLING", "true")
        monkeypatch.setenv("BRIDGE_USERNAME", "admin@example.com")
        body = build_tenant_create_body("isv-test-tenant", "desc")
        assert body["name"] == "isv-test-tenant"
        assert body["currency"] == "US Dollar"
        assert body["taxStatus"] == "Tax Exempt"
        assert body["address"]["addressType"] == "Bill To"
        assert body["contactInfo"][0]["emailTo"] == "admin@example.com"
        assert body["contactInfo"][0]["uid"]

    def test_force_billing_without_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("BRIDGE_BILLING", raising=False)
        monkeypatch.setenv("BRIDGE_USERNAME", "admin")
        body = build_tenant_create_body("t1", "desc", force_billing=True)
        assert body["contactInfo"][0]["emailTo"] == "test@test.com"
        assert body["address"]["line1"] == "111"


class TestCreateOrResolveTenant:
    def test_uses_post_response_when_id_present(self) -> None:
        client = MagicMock()
        client.post.return_value = {
            "ID": "tid-1",
            "name": "isv-test-tenant",
            "description": "ISV test tenant",
        }
        tenant = create_or_resolve_tenant(client, "isv-test-tenant", "ISV test tenant")
        assert tenant_record_id(tenant) == "tid-1"
        client.get.assert_not_called()

    def test_resolves_from_list_when_post_body_empty(self) -> None:
        client = MagicMock()
        client.post.return_value = {}
        client.get.return_value = [
            {"ID": "tid-2", "name": "isv-test-tenant", "description": "ISV test tenant"}
        ]
        tenant = create_or_resolve_tenant(client, "isv-test-tenant", "ISV test tenant")
        assert tenant_record_id(tenant) == "tid-2"
        client.get.assert_called_once_with("/orchestrator/tenants")

    def test_conflict_falls_back_to_list(self) -> None:
        client = MagicMock()
        client.post.side_effect = ValueError("POST /orchestrator/tenants failed with status 409: exists")
        client.get.return_value = [{"ID": "tid-3", "name": "isv-test-tenant"}]
        tenant = create_or_resolve_tenant(client, "isv-test-tenant", "ISV test tenant")
        assert tenant_record_id(tenant) == "tid-3"

    def test_403_raises_clear_runtime_error(self) -> None:
        client = MagicMock()
        client.post.side_effect = ValueError(
            'POST /orchestrator/tenants failed with status 403: {"error":"Forbidden"}'
        )
        with pytest.raises(RuntimeError, match="platform \\(super\\) admin"):
            create_or_resolve_tenant(client, "isv-test-tenant", "ISV test tenant")

    def test_400_billing_required_retries_with_billing_payload(self) -> None:
        client = MagicMock()
        client.post.side_effect = [
            ValueError(
                'POST /orchestrator/tenants failed with status 400: '
                '{"message":"billingAddress is required for tenant creation when bridge billing is enabled"}'
            ),
            {"ID": "tid-bill", "name": "isv-test-tenant"},
        ]
        tenant = create_or_resolve_tenant(client, "isv-test-tenant", "ISV test tenant")
        assert tenant_record_id(tenant) == "tid-bill"
        assert client.post.call_count == 2
        second_body = client.post.call_args_list[1].args[1]
        assert second_body["contactInfo"][0]["emailTo"] == "test@test.com"

    def test_500_billing_raises_clear_runtime_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BRIDGE_BILLING", "1")
        client = MagicMock()
        client.post.side_effect = ValueError(
            'POST /orchestrator/tenants failed with status 500: '
            '{"message":"Failed to provision tenant billing account"}'
        )
        with pytest.raises(RuntimeError, match="M360 billing account provisioning failed"):
            create_or_resolve_tenant(client, "isv-test-tenant", "ISV test tenant")
