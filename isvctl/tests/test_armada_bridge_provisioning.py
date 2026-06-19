# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary

"""Tests for provision_bm_node (common/metal.py) and provision_vm_nodes (common/vm.py)."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch, call

import pytest

BRIDGE_SCRIPTS = (
    Path(__file__).resolve().parents[1]
    / "configs" / "providers" / "armada-bridge" / "scripts"
)


def _add_path() -> None:
    if str(BRIDGE_SCRIPTS) not in sys.path:
        sys.path.insert(0, str(BRIDGE_SCRIPTS))


# ---------------------------------------------------------------------------
# Session-scoped lazy imports (avoids caching common.* at collection time)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def metal_mod():
    _add_path()
    import importlib
    return importlib.import_module("common.metal")


@pytest.fixture(scope="session")
def vm_mod():
    _add_path()
    import importlib
    return importlib.import_module("common.vm")


@pytest.fixture(scope="session")
def provision_bm_node_fn(metal_mod):
    return metal_mod.provision_bm_node


@pytest.fixture(scope="session")
def provision_vm_nodes_fn(vm_mod):
    return vm_mod.provision_vm_nodes


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_UUID1 = "aaaaaaaa-1111-2222-3333-444444444444"
_UUID2 = "bbbbbbbb-5555-6666-7777-888888888888"
_COMPUTE_VPC = "cccccccc-1111-2222-3333-000000000000"
_COMPUTE_SUB = "dddddddd-1111-2222-3333-000000000000"
_CONVERGED_VPC = "eeeeeeee-1111-2222-3333-000000000000"
_CONVERGED_SUB = "ffffffff-1111-2222-3333-000000000000"


def _ready_bm_node(node_id: str, product_type_id: str = "pt-1") -> dict[str, Any]:
    return {"id": node_id, "productTypeId": product_type_id, "allocateStatus": "done"}


# ---------------------------------------------------------------------------
# provision_bm_node — import flow (no VPC creation)
# ---------------------------------------------------------------------------

class TestProvisionBmNodeImportFlow:
    def test_import_flow_no_vpc_provisioned(self, provision_bm_node_fn, metal_mod):
        with (
            patch.object(metal_mod, "list_computes", return_value=[]),
            patch.object(metal_mod, "poll_until_bm_ready", return_value=[_ready_bm_node(_UUID1)]),
            patch("common.catalog.discover_bm_product_type_id", return_value=("pt-1", "flavor-1", True)),
        ):
            result = provision_bm_node_fn(
                MagicMock(), "t1", epoch=1000, discovery_flow=False, count=1, prefix="test"
            )

        node_ids, vpc_id, subnet_id, cvpc, csub = result
        assert node_ids == [_UUID1]
        assert vpc_id == "n/a"
        assert subnet_id == ""
        assert cvpc == "n/a"
        assert csub == ""

    def test_import_flow_allocate_called_without_subnet_ids(self, provision_bm_node_fn, metal_mod):
        allocate_calls: list[Any] = []

        def fake_allocate(client, tenant_id, product_type_id, *, count, subnet_ids):
            allocate_calls.append(subnet_ids)

        with (
            patch.object(metal_mod, "list_computes", return_value=[]),
            patch.object(metal_mod, "poll_until_bm_ready", return_value=[_ready_bm_node(_UUID1)]),
            patch("common.catalog.discover_bm_product_type_id", return_value=("pt-1", "f", True)),
            patch.object(metal_mod, "allocate_bm", side_effect=fake_allocate),
        ):
            provision_bm_node_fn(MagicMock(), "t1", epoch=1, discovery_flow=False)

        assert allocate_calls == [None]  # subnet_ids=None for import flow


# ---------------------------------------------------------------------------
# provision_bm_node — discovery flow (2 VPCs created)
# ---------------------------------------------------------------------------

class TestProvisionBmNodeDiscoveryFlow:
    def test_discovery_flow_provisions_two_vpcs(self, provision_bm_node_fn, metal_mod):
        with (
            patch.object(metal_mod, "list_computes", return_value=[]),
            patch.object(metal_mod, "poll_until_bm_ready", return_value=[_ready_bm_node(_UUID1)]),
            patch("common.catalog.discover_bm_product_type_id", return_value=("pt-1", "f", True)),
            patch.object(metal_mod, "allocate_bm"),
            patch("common.vpc.provision_discovery_vpcs",
                  return_value=(_COMPUTE_VPC, _COMPUTE_SUB, _CONVERGED_VPC, _CONVERGED_SUB)),
        ):
            result = provision_bm_node_fn(
                MagicMock(), "t1", epoch=2000, discovery_flow=True, prefix="my"
            )

        node_ids, vpc_id, subnet_id, cvpc, csub = result
        assert vpc_id == _COMPUTE_VPC
        assert subnet_id == _COMPUTE_SUB
        assert cvpc == _CONVERGED_VPC
        assert csub == _CONVERGED_SUB

    def test_discovery_flow_passes_both_subnet_ids_to_allocate(self, provision_bm_node_fn, metal_mod):
        allocate_calls: list[Any] = []

        def fake_allocate(client, tenant_id, product_type_id, *, count, subnet_ids):
            allocate_calls.append(subnet_ids)

        with (
            patch.object(metal_mod, "list_computes", return_value=[]),
            patch.object(metal_mod, "poll_until_bm_ready", return_value=[_ready_bm_node(_UUID1)]),
            patch("common.catalog.discover_bm_product_type_id", return_value=("pt-1", "f", True)),
            patch.object(metal_mod, "allocate_bm", side_effect=fake_allocate),
            patch("common.vpc.provision_discovery_vpcs",
                  return_value=(_COMPUTE_VPC, _COMPUTE_SUB, _CONVERGED_VPC, _CONVERGED_SUB)),
        ):
            provision_bm_node_fn(MagicMock(), "t1", epoch=1, discovery_flow=True)

        assert allocate_calls == [[_COMPUTE_SUB, _CONVERGED_SUB]]

    def test_discovery_flow_count_multiple(self, provision_bm_node_fn, metal_mod):
        node1 = _ready_bm_node(_UUID1)
        node2 = _ready_bm_node(_UUID2)

        with (
            patch.object(metal_mod, "list_computes", return_value=[]),
            patch.object(metal_mod, "poll_until_bm_ready", return_value=[node1, node2]),
            patch("common.catalog.discover_bm_product_type_id", return_value=("pt-1", "f", True)),
            patch.object(metal_mod, "allocate_bm"),
            patch("common.vpc.provision_discovery_vpcs",
                  return_value=(_COMPUTE_VPC, _COMPUTE_SUB, _CONVERGED_VPC, _CONVERGED_SUB)),
        ):
            result = provision_bm_node_fn(
                MagicMock(), "t1", epoch=1, discovery_flow=True, count=2
            )

        node_ids, *_ = result
        assert node_ids == [_UUID1, _UUID2]

    def test_missing_node_id_raises(self, provision_bm_node_fn, metal_mod):
        with (
            patch.object(metal_mod, "list_computes", return_value=[]),
            patch.object(metal_mod, "poll_until_bm_ready", return_value=[{"allocateStatus": "done"}]),
            patch("common.catalog.discover_bm_product_type_id", return_value=("pt-1", "f", True)),
            patch.object(metal_mod, "allocate_bm"),
            patch("common.vpc.provision_discovery_vpcs",
                  return_value=(_COMPUTE_VPC, _COMPUTE_SUB, _CONVERGED_VPC, _CONVERGED_SUB)),
        ):
            with pytest.raises(RuntimeError, match="node id"):
                provision_bm_node_fn(MagicMock(), "t1", epoch=1, discovery_flow=True)


# ---------------------------------------------------------------------------
# provision_vm_nodes
# ---------------------------------------------------------------------------

class TestProvisionVmNodes:
    def test_import_flow_no_vpc_created(self, provision_vm_nodes_fn, vm_mod):
        with (
            patch.object(vm_mod, "resolve_ssh_key", return_value=("pub-key", "/key.pem")),
            patch.object(vm_mod, "allocate_vm", return_value={"id": _UUID1}),
            patch.object(vm_mod, "extract_vm_id", return_value=_UUID1),
            patch.object(vm_mod, "wait_for_vm_status", return_value={}),
        ):
            node_ids, vpc_id, subnet_id = provision_vm_nodes_fn(
                MagicMock(), "t1", epoch=1000, discovery_flow=False,
                vm_flavor="gpu.1x", count=1, name_prefix="test",
            )

        assert node_ids == [_UUID1]
        assert vpc_id == "n/a"
        assert subnet_id == ""

    def test_discovery_flow_creates_compute_vpc(self, provision_vm_nodes_fn, vm_mod):
        topologies = [{"topology": "compute", "networkType": "ethernet", "id": "t1"}]

        with (
            patch("common.network.list_topologies", return_value=topologies),
            patch("common.vpc.pick_compute_topology", return_value=topologies[0]),
            patch("common.vpc.create_vpc", return_value="vpc-x"),
            patch("common.vpc.create_subnet", return_value="sub-x"),
            patch.object(vm_mod, "resolve_ssh_key", return_value=("pub-key", "/key.pem")),
            patch.object(vm_mod, "allocate_vm", return_value={"id": _UUID1}),
            patch.object(vm_mod, "extract_vm_id", return_value=_UUID1),
            patch.object(vm_mod, "wait_for_vm_status", return_value={}),
        ):
            node_ids, vpc_id, subnet_id = provision_vm_nodes_fn(
                MagicMock(), "t1", epoch=1000, discovery_flow=True,
                vm_flavor="gpu.1x", count=1, name_prefix="isv-k8s-node",
            )

        assert node_ids == [_UUID1]
        assert vpc_id == "vpc-x"
        assert subnet_id == "sub-x"

    def test_multiple_vms_single_vpc(self, provision_vm_nodes_fn, vm_mod):
        topologies = [{"topology": "compute", "networkType": "ethernet", "id": "t1"}]
        vm_ids = [_UUID1, _UUID2]

        with (
            patch("common.network.list_topologies", return_value=topologies),
            patch("common.vpc.pick_compute_topology", return_value=topologies[0]),
            patch("common.vpc.create_vpc", return_value="vpc-shared") as mock_create_vpc,
            patch("common.vpc.create_subnet", return_value="sub-shared"),
            patch.object(vm_mod, "resolve_ssh_key", return_value=("pub", "/key")),
            patch.object(vm_mod, "allocate_vm", side_effect=[{"id": v} for v in vm_ids]),
            patch.object(vm_mod, "extract_vm_id", side_effect=vm_ids),
            patch.object(vm_mod, "wait_for_vm_status", return_value={}),
        ):
            node_ids, vpc_id, _ = provision_vm_nodes_fn(
                MagicMock(), "t1", epoch=1000, discovery_flow=True,
                vm_flavor="gpu.1x", count=2, name_prefix="isv-k8s-node",
            )

        assert node_ids == vm_ids
        assert vpc_id == "vpc-shared"
        mock_create_vpc.assert_called_once()

    def test_409_on_vm_allocate_falls_back_to_existing(self, provision_vm_nodes_fn, vm_mod):
        existing_vm = {"id": _UUID1, "name": "isv-vm-node-1000"}

        with (
            patch.object(vm_mod, "resolve_ssh_key", return_value=("pub", "/key")),
            patch.object(vm_mod, "allocate_vm", side_effect=ValueError("status 409 conflict")),
            patch.object(vm_mod, "list_vms", return_value=[existing_vm]),
            patch.object(vm_mod, "find_vm_by_name", return_value=existing_vm),
            patch.object(vm_mod, "extract_vm_id", return_value=_UUID1),
            patch.object(vm_mod, "wait_for_vm_status", return_value={}),
        ):
            node_ids, *_ = provision_vm_nodes_fn(
                MagicMock(), "t1", epoch=1000, discovery_flow=False,
                vm_flavor="gpu.1x", count=1, name_prefix="isv-vm-node",
            )

        assert node_ids == [_UUID1]
