# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary

"""Tests for common/vpc.py — VPC/subnet CRUD and discovery-flow provisioning."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, call, patch

import pytest

BRIDGE_SCRIPTS = (
    Path(__file__).resolve().parents[1]
    / "configs" / "providers" / "armada-bridge" / "scripts"
)


def _import_vpc():
    """Lazy import common.vpc to avoid caching armada-bridge common.* at collection time."""
    if str(BRIDGE_SCRIPTS) not in sys.path:
        sys.path.insert(0, str(BRIDGE_SCRIPTS))
    import importlib
    return importlib.import_module("common.vpc")


# ---------------------------------------------------------------------------
# Lazy-import helpers — called once per test session via module-level fixture
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session")
def vpc_mod():
    return _import_vpc()


@pytest.fixture(scope="session")
def pick_compute_topology(vpc_mod):
    return vpc_mod.pick_compute_topology


@pytest.fixture(scope="session")
def pick_converged_topology(vpc_mod):
    return vpc_mod.pick_converged_topology


@pytest.fixture(scope="session")
def create_vpc_fn(vpc_mod):
    return vpc_mod.create_vpc


@pytest.fixture(scope="session")
def create_subnet_fn(vpc_mod):
    return vpc_mod.create_subnet


@pytest.fixture(scope="session")
def delete_subnet_fn(vpc_mod):
    return vpc_mod.delete_subnet


@pytest.fixture(scope="session")
def delete_vpc_fn(vpc_mod):
    return vpc_mod.delete_vpc


@pytest.fixture(scope="session")
def provision_discovery_vpcs_fn(vpc_mod):
    return vpc_mod.provision_discovery_vpcs


@pytest.fixture(scope="session")
def deprovision_discovery_vpcs_fn(vpc_mod):
    return vpc_mod.deprovision_discovery_vpcs

# ---------------------------------------------------------------------------
# Topology pickers
# ---------------------------------------------------------------------------

TOPOLOGIES_BOTH = [
    {"topology": "compute", "networkType": "ethernet", "id": "topo-compute"},
    {"topology": "converged", "networkType": "ethernet", "id": "topo-converged"},
    {"topology": "other", "networkType": "infiniband", "id": "topo-other"},
]

TOPOLOGIES_COMPUTE_ONLY = [
    {"topology": "compute", "networkType": "ethernet", "id": "topo-compute"},
]

TOPOLOGIES_ETHERNET_ONLY = [
    {"topology": "custom", "networkType": "ethernet", "id": "topo-eth"},
]

TOPOLOGIES_EMPTY_NAMES = [
    {"topology": "", "networkType": "ethernet", "id": "topo-a"},
    {"topology": "", "networkType": "ethernet", "id": "topo-b"},
]


def test_pick_compute_topology_exact_match(pick_compute_topology):
    result = pick_compute_topology(TOPOLOGIES_BOTH)
    assert result["id"] == "topo-compute"


def test_pick_converged_topology_exact_match(pick_converged_topology):
    result = pick_converged_topology(TOPOLOGIES_BOTH)
    assert result["id"] == "topo-converged"


def test_pick_compute_topology_falls_back_to_ethernet(pick_compute_topology):
    result = pick_compute_topology(TOPOLOGIES_ETHERNET_ONLY)
    assert result["id"] == "topo-eth"


def test_pick_converged_topology_falls_back_to_ethernet(pick_converged_topology):
    result = pick_converged_topology(TOPOLOGIES_ETHERNET_ONLY)
    assert result["id"] == "topo-eth"


def test_pick_compute_topology_falls_back_to_first(pick_compute_topology):
    result = pick_compute_topology(TOPOLOGIES_EMPTY_NAMES)
    assert result["id"] == "topo-a"


def test_pick_converged_topology_falls_back_to_first(pick_converged_topology):
    result = pick_converged_topology(TOPOLOGIES_EMPTY_NAMES)
    assert result["id"] == "topo-a"


# ---------------------------------------------------------------------------
# create_vpc / create_subnet
# ---------------------------------------------------------------------------

def _mock_client(post_return: dict[str, Any] | None = None, get_return: Any = None) -> MagicMock:
    client = MagicMock()
    client.post.return_value = post_return or {}
    client.get.return_value = get_return or []
    return client


def test_create_vpc_returns_id(create_vpc_fn):
    client = _mock_client(post_return={"id": "vpc-123"})
    result = create_vpc_fn(client, "t1", "compute", "my-vpc")
    assert result == "vpc-123"
    client.post.assert_called_once_with(
        "/orchestrator/tenants/t1/vpcs",
        {"name": "my-vpc", "topologyID": "compute", "description": "", "capabilities": []},
    )


def test_create_subnet_returns_id(create_subnet_fn):
    client = _mock_client(post_return={"id": "sub-456"})
    result = create_subnet_fn(client, "t1", "vpc-123", "compute", "my-subnet", "10.0.0.0/24")
    assert result == "sub-456"
    client.post.assert_called_once_with(
        "/orchestrator/tenants/t1/subnets",
        {
            "name": "my-subnet",
            "subnetCIDR": "10.0.0.0/24",
            "topology": "compute",
            "parentVpcID": "vpc-123",
        },
    )


# ---------------------------------------------------------------------------
# delete_subnet
# ---------------------------------------------------------------------------

def test_delete_subnet_skips_non_uuid(delete_subnet_fn):
    client = _mock_client()
    delete_subnet_fn(client, "t1", "n/a")
    client.delete.assert_not_called()


def test_delete_subnet_skips_empty(delete_subnet_fn):
    client = _mock_client()
    delete_subnet_fn(client, "t1", "")
    client.delete.assert_not_called()


def test_delete_subnet_calls_api_for_uuid(delete_subnet_fn):
    client = _mock_client()
    uid = "550e8400-e29b-41d4-a716-446655440000"
    delete_subnet_fn(client, "t1", uid)
    client.delete.assert_called_once_with(f"/orchestrator/tenants/t1/subnets/{uid}")


def test_delete_subnet_ignores_404(delete_subnet_fn):
    client = MagicMock()
    client.delete.side_effect = ValueError("status 404 not found")
    uid = "550e8400-e29b-41d4-a716-446655440000"
    delete_subnet_fn(client, "t1", uid)


def test_delete_subnet_reraises_non_404(delete_subnet_fn):
    client = MagicMock()
    client.delete.side_effect = ValueError("status 500 server error")
    uid = "550e8400-e29b-41d4-a716-446655440000"
    with pytest.raises(ValueError, match="500"):
        delete_subnet_fn(client, "t1", uid)


# ---------------------------------------------------------------------------
# delete_vpc
# ---------------------------------------------------------------------------

def test_delete_vpc_skips_non_uuid(delete_vpc_fn):
    client = _mock_client()
    delete_vpc_fn(client, "t1", "n/a")
    client.get.assert_not_called()
    client.delete.assert_not_called()


def test_delete_vpc_deletes_subnets_then_vpc(delete_vpc_fn):
    vpc_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    subnet_id = "11111111-2222-3333-4444-555555555555"
    client = MagicMock()
    client.get.return_value = [{"id": subnet_id, "parentVpcID": vpc_id}]

    delete_vpc_fn(client, "t1", vpc_id)

    client.delete.assert_any_call(f"/orchestrator/tenants/t1/subnets/{subnet_id}")
    client.delete.assert_any_call(f"/orchestrator/tenants/t1/vpcs/{vpc_id}")


def test_delete_vpc_skips_subnets_from_other_vpcs(delete_vpc_fn):
    vpc_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    other_vpc_id = "ffffffff-0000-1111-2222-333333333333"
    subnet_id = "11111111-2222-3333-4444-555555555555"
    client = MagicMock()
    client.get.return_value = [{"id": subnet_id, "parentVpcID": other_vpc_id}]

    delete_vpc_fn(client, "t1", vpc_id)

    calls = [str(c) for c in client.delete.call_args_list]
    assert not any("subnets" in c for c in calls)
    client.delete.assert_called_once_with(f"/orchestrator/tenants/t1/vpcs/{vpc_id}")


# ---------------------------------------------------------------------------
# provision_discovery_vpcs
# ---------------------------------------------------------------------------

def test_provision_discovery_vpcs_creates_two_vpcs(provision_discovery_vpcs_fn, vpc_mod):
    topologies = [
        {"topology": "compute", "networkType": "ethernet", "id": "topo-c"},
        {"topology": "converged", "networkType": "ethernet", "id": "topo-v"},
    ]
    client = MagicMock()
    client.post.side_effect = [
        {"id": "compute-vpc-id"},
        {"id": "compute-sub-id"},
        {"id": "converged-vpc-id"},
        {"id": "converged-sub-id"},
    ]

    with patch.object(vpc_mod, "list_topologies", return_value=topologies):
        result = provision_discovery_vpcs_fn(client, "t1", epoch=1234, prefix="test")

    assert result == ("compute-vpc-id", "compute-sub-id", "converged-vpc-id", "converged-sub-id")


def test_provision_discovery_vpcs_raises_when_no_topologies(provision_discovery_vpcs_fn, vpc_mod):
    client = _mock_client()
    with patch.object(vpc_mod, "list_topologies", return_value=[]):
        with pytest.raises(RuntimeError, match="no topologies"):
            provision_discovery_vpcs_fn(client, "t1")


def test_provision_discovery_vpcs_uses_epoch_in_names(provision_discovery_vpcs_fn, vpc_mod):
    topologies = [{"topology": "compute", "networkType": "ethernet", "id": "topo-c"}]
    client = MagicMock()
    client.post.return_value = {"id": "some-id"}

    with patch.object(vpc_mod, "list_topologies", return_value=topologies):
        provision_discovery_vpcs_fn(client, "t1", epoch=9999, prefix="p")

    names_used = [call_args[0][1]["name"] for call_args in client.post.call_args_list
                  if "name" in call_args[0][1]]
    assert any("9999" in n for n in names_used)
    assert any("p-compute-vpc" in n for n in names_used)
    assert any("p-converged-vpc" in n for n in names_used)


# ---------------------------------------------------------------------------
# deprovision_discovery_vpcs
# ---------------------------------------------------------------------------

def test_deprovision_discovery_vpcs_skips_non_uuids(deprovision_discovery_vpcs_fn):
    client = _mock_client()
    deprovision_discovery_vpcs_fn(client, "t1", "n/a", "")
    client.delete.assert_not_called()


def test_deprovision_discovery_vpcs_deletes_both_vpcs(deprovision_discovery_vpcs_fn):
    compute_vpc = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    converged_vpc = "ffffffff-1111-2222-3333-444444444444"
    client = MagicMock()
    client.get.return_value = []

    deprovision_discovery_vpcs_fn(client, "t1", compute_vpc, converged_vpc)

    vpc_deletes = [
        c for c in client.delete.call_args_list
        if "vpcs" in str(c) and "subnets" not in str(c)
    ]
    deleted_vpcs = {str(c) for c in vpc_deletes}
    assert any(compute_vpc in s for s in deleted_vpcs)
    assert any(converged_vpc in s for s in deleted_vpcs)
