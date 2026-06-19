# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary

"""Tests for bare_metal/launch_instance.py and bare_metal/teardown.py."""

from __future__ import annotations

import importlib
import json
import sys
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

BRIDGE_SCRIPTS = (
    Path(__file__).resolve().parents[1]
    / "configs" / "providers" / "armada-bridge" / "scripts"
)
BARE_METAL_DIR = BRIDGE_SCRIPTS / "bare_metal"


def _add_path() -> None:
    for p in (str(BRIDGE_SCRIPTS), str(BARE_METAL_DIR)):
        if p not in sys.path:
            sys.path.insert(0, p)


# ---------------------------------------------------------------------------
# Lazy module fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def launch_mod():
    _add_path()
    # Force fresh load so DEMO_MODE is re-evaluated in each test via monkeypatch.
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "bare_metal.launch_instance", BARE_METAL_DIR / "launch_instance.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="session")
def teardown_mod():
    _add_path()
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "bare_metal.teardown", BARE_METAL_DIR / "teardown.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_UUID_NODE = "aaaaaaaa-1111-2222-3333-444444444444"
_COMPUTE_VPC = "cccccccc-1111-2222-3333-000000000000"
_COMPUTE_SUB = "dddddddd-1111-2222-3333-000000000000"
_CONVERGED_VPC = "eeeeeeee-1111-2222-3333-000000000000"
_CONVERGED_SUB = "ffffffff-1111-2222-3333-000000000000"

_IMPORT_TOPOLOGIES = []
_DISCOVERY_TOPOLOGIES = [{"topology": "eth0", "networkType": "ethernet"}]


def _run_launch(launch_mod, argv, *, env_overrides=None):
    """Run launch_instance.main() capturing stdout; returns parsed JSON."""
    with (
        patch.object(sys, "argv", ["launch_instance.py"] + argv),
        patch.dict("os.environ", env_overrides or {}, clear=False),
        patch("sys.stdout", new_callable=StringIO) as mock_out,
    ):
        rc = launch_mod.main()
    return rc, json.loads(mock_out.getvalue())


def _run_teardown(teardown_mod, argv):
    """Run teardown.main() capturing stdout; returns parsed JSON."""
    with (
        patch.object(sys, "argv", ["teardown.py"] + argv),
        patch("sys.stdout", new_callable=StringIO) as mock_out,
    ):
        rc = teardown_mod.main()
    return rc, json.loads(mock_out.getvalue())


# ---------------------------------------------------------------------------
# launch_instance — DEMO_MODE
# ---------------------------------------------------------------------------

class TestLaunchInstanceDemoMode:
    def test_demo_mode_returns_success(self, launch_mod):
        with patch.object(launch_mod, "DEMO_MODE", True):
            rc, result = _run_launch(launch_mod, ["--tenant", "t1", "--name", "test"])
        assert rc == 0
        assert result["success"] is True
        assert result["instance_id"] == "demo-bm-node01"
        assert result["vpc_id"] == "n/a"
        assert result["converged_vpc_id"] == "n/a"

    def test_demo_mode_no_api_calls(self, launch_mod):
        with (
            patch.object(launch_mod, "DEMO_MODE", True),
            patch.object(launch_mod, "BridgeClient") as mock_client,
        ):
            _run_launch(launch_mod, ["--tenant", "t1", "--name", "test"])
        mock_client.from_env.assert_not_called()


# ---------------------------------------------------------------------------
# launch_instance — import flow (no VPCs)
# ---------------------------------------------------------------------------

class TestLaunchInstanceImportFlow:
    def test_import_flow_no_vpcs(self, launch_mod):
        with (
            patch.object(launch_mod, "DEMO_MODE", False),
            patch.object(launch_mod, "BridgeClient"),
            patch.object(launch_mod, "resolve_tenant_id", return_value="t1"),
            patch.object(launch_mod, "list_topologies", return_value=_IMPORT_TOPOLOGIES),
            patch.object(launch_mod, "is_discovery_flow", return_value=False),
            patch.object(
                launch_mod, "provision_bm_node",
                return_value=([_UUID_NODE], "n/a", "", "n/a", ""),
            ),
        ):
            rc, result = _run_launch(launch_mod, ["--tenant", "t1", "--name", "test"])

        assert rc == 0
        assert result["success"] is True
        assert result["discovery_flow"] is False
        assert result["instance_id"] == _UUID_NODE
        assert result["vpc_id"] == "n/a"
        assert result["subnet_id"] == ""
        assert result["converged_vpc_id"] == "n/a"
        assert result["converged_subnet_id"] == ""

    def test_import_flow_provision_bm_node_called_with_correct_args(self, launch_mod):
        mock_provision = MagicMock(return_value=([_UUID_NODE], "n/a", "", "n/a", ""))

        with (
            patch.object(launch_mod, "DEMO_MODE", False),
            patch.object(launch_mod, "BridgeClient"),
            patch.object(launch_mod, "resolve_tenant_id", return_value="t1"),
            patch.object(launch_mod, "list_topologies", return_value=_IMPORT_TOPOLOGIES),
            patch.object(launch_mod, "is_discovery_flow", return_value=False),
            patch.object(launch_mod, "provision_bm_node", mock_provision),
        ):
            _run_launch(launch_mod, ["--tenant", "t1", "--name", "test"])

        call_kwargs = mock_provision.call_args.kwargs
        assert call_kwargs["discovery_flow"] is False
        assert call_kwargs["count"] == 1
        assert call_kwargs["prefix"] == "isv-bm"


# ---------------------------------------------------------------------------
# launch_instance — discovery flow (2 VPCs)
# ---------------------------------------------------------------------------

class TestLaunchInstanceDiscoveryFlow:
    def test_discovery_flow_emits_both_vpc_ids(self, launch_mod):
        with (
            patch.object(launch_mod, "DEMO_MODE", False),
            patch.object(launch_mod, "BridgeClient"),
            patch.object(launch_mod, "resolve_tenant_id", return_value="t1"),
            patch.object(launch_mod, "list_topologies", return_value=_DISCOVERY_TOPOLOGIES),
            patch.object(launch_mod, "is_discovery_flow", return_value=True),
            patch.object(
                launch_mod, "provision_bm_node",
                return_value=(
                    [_UUID_NODE],
                    _COMPUTE_VPC, _COMPUTE_SUB,
                    _CONVERGED_VPC, _CONVERGED_SUB,
                ),
            ),
        ):
            rc, result = _run_launch(launch_mod, ["--tenant", "t1", "--name", "test"])

        assert rc == 0
        assert result["discovery_flow"] is True
        assert result["vpc_id"] == _COMPUTE_VPC
        assert result["subnet_id"] == _COMPUTE_SUB
        assert result["converged_vpc_id"] == _CONVERGED_VPC
        assert result["converged_subnet_id"] == _CONVERGED_SUB

    def test_flavor_arg_forwarded_to_env(self, launch_mod):
        captured_env: dict = {}

        def fake_provision(client, tenant, *, epoch, discovery_flow, count, prefix):
            captured_env["BRIDGE_BM_FLAVOR"] = __import__("os").environ.get("BRIDGE_BM_FLAVOR", "")
            return ([_UUID_NODE], "n/a", "", "n/a", "")

        with (
            patch.object(launch_mod, "DEMO_MODE", False),
            patch.object(launch_mod, "BridgeClient"),
            patch.object(launch_mod, "resolve_tenant_id", return_value="t1"),
            patch.object(launch_mod, "list_topologies", return_value=_IMPORT_TOPOLOGIES),
            patch.object(launch_mod, "is_discovery_flow", return_value=False),
            patch.object(launch_mod, "provision_bm_node", side_effect=fake_provision),
        ):
            _run_launch(
                launch_mod,
                ["--tenant", "t1", "--name", "test", "--flavor", "pt-custom-uuid"],
            )

        assert captured_env["BRIDGE_BM_FLAVOR"] == "pt-custom-uuid"


# ---------------------------------------------------------------------------
# teardown — skip / demo
# ---------------------------------------------------------------------------

class TestTeardownSkipAndDemo:
    def test_skip_destroy_flag(self, teardown_mod):
        rc, result = _run_teardown(
            teardown_mod,
            ["--tenant", "t1", "--compute-node-id", _UUID_NODE, "--skip-destroy"],
        )
        assert rc == 0
        assert result["success"] is True
        assert result.get("skipped") is True

    def test_demo_mode_no_api(self, teardown_mod):
        with (
            patch.object(teardown_mod, "DEMO_MODE", True),
            patch.object(teardown_mod, "BridgeClient") as mock_client,
        ):
            rc, result = _run_teardown(
                teardown_mod,
                ["--tenant", "t1", "--compute-node-id", _UUID_NODE],
            )
        assert rc == 0
        assert result["success"] is True
        mock_client.from_env.assert_not_called()


# ---------------------------------------------------------------------------
# teardown — live (import flow: no VPC deletion)
# ---------------------------------------------------------------------------

class TestTeardownImportFlow:
    def test_import_flow_deallocates_node_only(self, teardown_mod):
        mock_deallocate = MagicMock()
        mock_deprovision = MagicMock()

        with (
            patch.object(teardown_mod, "DEMO_MODE", False),
            patch.object(teardown_mod, "BridgeClient"),
            patch.object(teardown_mod, "resolve_tenant_id", return_value="t1"),
            patch.object(teardown_mod, "deallocate_bm", mock_deallocate),
            patch.object(teardown_mod, "deprovision_discovery_vpcs", mock_deprovision),
        ):
            rc, result = _run_teardown(
                teardown_mod,
                ["--tenant", "t1", "--compute-node-id", _UUID_NODE,
                 "--vpc-id", "n/a"],
            )

        assert rc == 0
        assert result["success"] is True
        mock_deallocate.assert_called_once()
        # deprovision_discovery_vpcs is still called but skips n/a IDs internally
        mock_deprovision.assert_called_once_with(
            mock_deallocate.call_args.args[0],  # client
            "t1",
            "n/a",
            "",
        )


# ---------------------------------------------------------------------------
# teardown — live (discovery flow: both VPCs deleted)
# ---------------------------------------------------------------------------

class TestTeardownDiscoveryFlow:
    def test_discovery_flow_deletes_both_vpcs(self, teardown_mod):
        mock_deallocate = MagicMock()
        mock_deprovision = MagicMock()

        with (
            patch.object(teardown_mod, "DEMO_MODE", False),
            patch.object(teardown_mod, "BridgeClient"),
            patch.object(teardown_mod, "resolve_tenant_id", return_value="t1"),
            patch.object(teardown_mod, "deallocate_bm", mock_deallocate),
            patch.object(teardown_mod, "deprovision_discovery_vpcs", mock_deprovision),
        ):
            rc, result = _run_teardown(
                teardown_mod,
                [
                    "--tenant", "t1",
                    "--compute-node-id", _UUID_NODE,
                    f"--vpc-id={_COMPUTE_VPC}",
                    f"--subnet-id={_COMPUTE_SUB}",
                    f"--converged-vpc-id={_CONVERGED_VPC}",
                ],
            )

        assert rc == 0
        assert result["success"] is True
        mock_deprovision.assert_called_once_with(
            mock_deallocate.call_args.args[0],  # client
            "t1",
            _COMPUTE_VPC,
            _CONVERGED_VPC,
        )
