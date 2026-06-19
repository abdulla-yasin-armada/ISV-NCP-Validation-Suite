# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary

"""Tests for network/provision_nodes.py and network/deprovision_nodes.py."""

from __future__ import annotations

import importlib.util
import json
import sys
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

BRIDGE_SCRIPTS = (
    Path(__file__).resolve().parents[1]
    / "configs" / "providers" / "armada-bridge" / "scripts"
)
NETWORK_DIR = BRIDGE_SCRIPTS / "network"


def _load(name: str, filename: str):
    """Load a script module by filename into sys.modules under `name`."""
    if str(BRIDGE_SCRIPTS) not in sys.path:
        sys.path.insert(0, str(BRIDGE_SCRIPTS))
    spec = importlib.util.spec_from_file_location(name, NETWORK_DIR / filename)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="session")
def provision_mod():
    return _load("network.provision_nodes", "provision_nodes.py")


@pytest.fixture(scope="session")
def deprovision_mod():
    return _load("network.deprovision_nodes", "deprovision_nodes.py")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_UUID1 = "aaaaaaaa-1111-2222-3333-444444444444"
_UUID2 = "bbbbbbbb-5555-6666-7777-888888888888"
_COMPUTE_SUB = "dddddddd-1111-2222-3333-000000000000"
_CONVERGED_SUB = "ffffffff-1111-2222-3333-000000000000"

_IMPORT_TOPOLOGIES = []
_DISCOVERY_TOPOLOGIES = [{"topology": "eth0", "networkType": "ethernet"}]


def _ready_node(node_id: str, product_type_id: str = "pt-1") -> dict:
    return {"id": node_id, "productTypeId": product_type_id, "allocateStatus": "done",
            "inBandIP": "10.0.0.1"}


def _run(mod, argv, *, env_overrides=None):
    with (
        patch.object(sys, "argv", [mod.__file__] + argv),
        patch.dict("os.environ", env_overrides or {}, clear=False),
        patch("sys.stdout", new_callable=StringIO) as mock_out,
    ):
        rc = mod.main()
    return rc, json.loads(mock_out.getvalue())


# ===========================================================================
# provision_nodes.py
# ===========================================================================

class TestProvisionNodesDemoMode:
    def test_demo_returns_two_nodes(self, provision_mod):
        with patch.object(provision_mod, "DEMO_MODE", True):
            rc, result = _run(provision_mod, ["--tenant", "t1"])
        assert rc == 0
        assert result["success"] is True
        assert result["node_count"] == 2
        assert result["provisioned"] is True

    def test_demo_no_api_calls(self, provision_mod):
        with (
            patch.object(provision_mod, "DEMO_MODE", True),
            patch.object(provision_mod, "BridgeClient") as mock_client,
        ):
            _run(provision_mod, ["--tenant", "t1"])
        mock_client.from_env.assert_not_called()


class TestProvisionNodesExplicitIds:
    def test_reuse_explicit_ids_skips_allocate(self, provision_mod):
        node = _ready_node(_UUID1)
        with (
            patch.object(provision_mod, "DEMO_MODE", False),
            patch.object(provision_mod, "BridgeClient"),
            patch.object(provision_mod, "resolve_tenant_id", return_value="t1"),
            patch.object(provision_mod, "list_computes", return_value=[node]),
            patch.object(provision_mod, "allocate_bm") as mock_allocate,
            patch.dict("os.environ", {"BRIDGE_NETWORK_NODE_IDS": _UUID1}, clear=False),
        ):
            rc, result = _run(provision_mod, ["--tenant", "t1"])
        assert rc == 0
        assert result["instance_ids"] == [_UUID1]
        assert result["provisioned"] is False
        mock_allocate.assert_not_called()

    def test_reuse_multiple_ids_deduped(self, provision_mod):
        nodes = [_ready_node(_UUID1), _ready_node(_UUID2)]
        with (
            patch.object(provision_mod, "DEMO_MODE", False),
            patch.object(provision_mod, "BridgeClient"),
            patch.object(provision_mod, "resolve_tenant_id", return_value="t1"),
            patch.object(provision_mod, "list_computes", return_value=nodes),
            patch.dict("os.environ",
                       {"BRIDGE_NETWORK_NODE_IDS": f"{_UUID1},{_UUID2},{_UUID1}"},
                       clear=False),
        ):
            rc, result = _run(provision_mod, ["--tenant", "t1"])
        assert result["instance_ids"] == [_UUID1, _UUID2]
        assert result["node_count"] == 2


class TestProvisionNodesImportFlow:
    def test_import_flow_no_subnet_ids(self, provision_mod):
        """Import flow: allocate_bm called with subnet_ids=None."""
        nodes = [_ready_node(_UUID1), _ready_node(_UUID2)]
        mock_allocate = MagicMock()

        with (
            patch.object(provision_mod, "DEMO_MODE", False),
            patch.dict("os.environ", {"BRIDGE_NETWORK_NODE_IDS": ""}, clear=False),
            patch.object(provision_mod, "BridgeClient"),
            patch.object(provision_mod, "resolve_tenant_id", return_value="t1"),
            patch.object(provision_mod, "list_computes", return_value=[]),
            patch.object(provision_mod, "discover_bm_product_type_id",
                         return_value=("pt-1", "flavor-1", True)),
            patch.object(provision_mod, "list_topologies", return_value=_IMPORT_TOPOLOGIES),
            patch.object(provision_mod, "is_import_flow", return_value=True),
            patch.object(provision_mod, "allocate_bm", mock_allocate),
            patch.object(provision_mod, "poll_until_bm_ready", return_value=nodes),
        ):
            rc, result = _run(provision_mod, ["--tenant", "t1", "--count", "2"])

        assert rc == 0
        assert result["node_count"] == 2
        assert result["provisioned"] is True
        # No subnet IDs → allocate_bm called with subnet_ids=None
        mock_allocate.assert_called_once()
        assert mock_allocate.call_args.kwargs["subnet_ids"] is None


class TestProvisionNodesDiscoveryFlow:
    def test_discovery_flow_passes_both_subnet_ids(self, provision_mod):
        """Discovery flow: both compute and converged subnet IDs passed to allocate_bm."""
        nodes = [_ready_node(_UUID1), _ready_node(_UUID2)]
        mock_allocate = MagicMock()

        with (
            patch.object(provision_mod, "DEMO_MODE", False),
            patch.dict("os.environ", {"BRIDGE_NETWORK_NODE_IDS": ""}, clear=False),
            patch.object(provision_mod, "BridgeClient"),
            patch.object(provision_mod, "resolve_tenant_id", return_value="t1"),
            patch.object(provision_mod, "list_computes", return_value=[]),
            patch.object(provision_mod, "discover_bm_product_type_id",
                         return_value=("pt-1", "flavor-1", False)),
            patch.object(provision_mod, "list_topologies", return_value=_DISCOVERY_TOPOLOGIES),
            patch.object(provision_mod, "is_import_flow", return_value=False),
            patch.object(provision_mod, "allocate_bm", mock_allocate),
            patch.object(provision_mod, "poll_until_bm_ready", return_value=nodes),
        ):
            rc, result = _run(provision_mod, [
                "--tenant", "t1", "--count", "2",
                f"--subnet-id={_COMPUTE_SUB}",
                f"--converged-subnet-id={_CONVERGED_SUB}",
            ])

        assert rc == 0
        assert result["node_count"] == 2
        assert mock_allocate.call_args.kwargs["subnet_ids"] == [_COMPUTE_SUB, _CONVERGED_SUB]

    def test_discovery_flow_instance_ids_csv(self, provision_mod):
        nodes = [_ready_node(_UUID1), _ready_node(_UUID2)]

        with (
            patch.object(provision_mod, "DEMO_MODE", False),
            patch.dict("os.environ", {"BRIDGE_NETWORK_NODE_IDS": ""}, clear=False),
            patch.object(provision_mod, "BridgeClient"),
            patch.object(provision_mod, "resolve_tenant_id", return_value="t1"),
            patch.object(provision_mod, "list_computes", return_value=[]),
            patch.object(provision_mod, "discover_bm_product_type_id",
                         return_value=("pt-1", "f", False)),
            patch.object(provision_mod, "list_topologies", return_value=_DISCOVERY_TOPOLOGIES),
            patch.object(provision_mod, "is_import_flow", return_value=False),
            patch.object(provision_mod, "allocate_bm"),
            patch.object(provision_mod, "poll_until_bm_ready", return_value=nodes),
        ):
            rc, result = _run(provision_mod, [
                "--tenant", "t1",
                f"--subnet-id={_COMPUTE_SUB}",
                f"--converged-subnet-id={_CONVERGED_SUB}",
            ])

        assert result["instance_ids"] == [_UUID1, _UUID2]
        assert result["instance_ids_csv"] == f"{_UUID1},{_UUID2}"


# ===========================================================================
# deprovision_nodes.py
# ===========================================================================

class TestDeprovisionNodesSkipAndDemo:
    def test_skip_destroy(self, deprovision_mod):
        rc, result = _run(
            deprovision_mod,
            ["--tenant", "t1", "--instance-ids", _UUID1, "--skip-destroy"],
        )
        assert rc == 0
        assert result["success"] is True
        assert result.get("skipped") is True

    def test_pre_existing_nodes_not_deallocated(self, deprovision_mod):
        with patch.dict("os.environ", {"BRIDGE_NETWORK_NODE_IDS": _UUID1}, clear=False):
            rc, result = _run(deprovision_mod, ["--tenant", "t1"])
        assert rc == 0
        assert result.get("skipped") is True
        assert "pre_existing" in result.get("reason", "")

    def test_demo_mode_no_api(self, deprovision_mod):
        with (
            patch.object(deprovision_mod, "DEMO_MODE", True),
            patch.object(deprovision_mod, "BridgeClient") as mock_client,
        ):
            rc, result = _run(deprovision_mod, ["--tenant", "t1"])
        assert rc == 0
        assert result["success"] is True
        mock_client.from_env.assert_not_called()

    def test_no_instance_ids_skips(self, deprovision_mod):
        with (
            patch.object(deprovision_mod, "DEMO_MODE", False),
            patch.dict("os.environ", {"BRIDGE_NETWORK_NODE_IDS": ""}, clear=False),
        ):
            rc, result = _run(deprovision_mod, ["--tenant", "t1", "--instance-ids", ""])
        assert rc == 0
        assert result.get("skipped") is True


class TestDeprovisionNodesLive:
    def test_calls_deallocate_bm_for_each_node(self, deprovision_mod):
        mock_deallocate = MagicMock()

        with (
            patch.object(deprovision_mod, "DEMO_MODE", False),
            patch.dict("os.environ", {"BRIDGE_NETWORK_NODE_IDS": ""}, clear=False),
            patch.object(deprovision_mod, "BridgeClient"),
            patch.object(deprovision_mod, "resolve_tenant_id", return_value="t1"),
            patch.object(deprovision_mod, "deallocate_bm", mock_deallocate),
        ):
            rc, result = _run(
                deprovision_mod,
                ["--tenant", "t1", f"--instance-ids={_UUID1},{_UUID2}"],
            )

        assert rc == 0
        assert result["deallocated"] == [_UUID1, _UUID2]
        assert mock_deallocate.call_count == 2
        called_ids = [c.args[2] for c in mock_deallocate.call_args_list]
        assert called_ids == [_UUID1, _UUID2]

    def test_json_array_instance_ids_parsed(self, deprovision_mod):
        mock_deallocate = MagicMock()
        ids_json = json.dumps([_UUID1, _UUID2])

        with (
            patch.object(deprovision_mod, "DEMO_MODE", False),
            patch.dict("os.environ", {"BRIDGE_NETWORK_NODE_IDS": ""}, clear=False),
            patch.object(deprovision_mod, "BridgeClient"),
            patch.object(deprovision_mod, "resolve_tenant_id", return_value="t1"),
            patch.object(deprovision_mod, "deallocate_bm", mock_deallocate),
        ):
            rc, result = _run(
                deprovision_mod,
                ["--tenant", "t1", f"--instance-ids={ids_json}"],
            )

        assert result["deallocated"] == [_UUID1, _UUID2]
        assert mock_deallocate.call_count == 2
