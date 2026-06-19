# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary

"""Tests for Armada Bridge discovery vs import flow detection."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ISVCTL_ROOT = Path(__file__).resolve().parents[1]
BRIDGE_SCRIPTS = ISVCTL_ROOT / "configs" / "providers" / "armada-bridge" / "scripts"

scripts_path = str(BRIDGE_SCRIPTS)
if scripts_path not in sys.path:
    sys.path.insert(0, scripts_path)

from common.network import (
    ipalloc_to_subnet,
    is_discovery_flow,
    is_import_flow,
    is_managed_network_id,
    is_orchestrator_resource_id,
    pick_primary_ipalloc,
)

IMPORT_LAB_TOPOLOGIES = [
    {
        "id": "a457e14e-8c34-49e2-b365-c84bce58f698",
        "networkType": "nonetwork",
        "topology": "compute",
        "tier": "tier1",
        "underlayStatus": "notStarted",
    },
    {
        "id": "3c9d11d1-f546-4267-bcf9-390168f8bd07",
        "networkType": "nonetwork",
        "topology": "storage",
        "tier": "tier1",
        "underlayStatus": "notStarted",
    },
]

DISCOVERY_TOPOLOGIES = [
    {
        "id": "eth-compute",
        "networkType": "ethernet",
        "topology": "compute",
        "tier": "tier1",
        "underlayStatus": "ready",
    },
]


class TestImportVsDiscoveryFlow:
    @pytest.mark.parametrize(
        ("topologies", "expect_import", "expect_discovery"),
        [
            ([], True, False),
            (IMPORT_LAB_TOPOLOGIES, True, False),
            (DISCOVERY_TOPOLOGIES, False, True),
            (
                [
                    {"networkType": "nonetwork", "topology": "compute"},
                    {"networkType": "ethernet", "topology": "storage"},
                ],
                False,
                True,
            ),
            ([{"networkType": "ethernet", "topology": "converged"}], False, True),
            ([{"topology": "compute"}], False, True),
        ],
        ids=[
            "empty-list",
            "import-lab-nonetwork",
            "ethernet-discovery",
            "mixed-nonetwork-ethernet",
            "single-ethernet",
            "missing-network-type",
        ],
    )
    def test_flow_detection(
        self,
        topologies: list[dict[str, str]],
        expect_import: bool,
        expect_discovery: bool,
    ) -> None:
        assert is_import_flow(topologies) is expect_import
        assert is_discovery_flow(topologies) is expect_discovery


SAMPLE_IPALLOC = {
    "metadata": {"name": "test-tenant001-inband-subnet-amcop-0"},
    "spec": {"subnet": "10.75.0.0/16", "topology": "inband"},
    "status": {"main_status": "success", "gateway": "10.75.0.1"},
}


class TestIpAllocationMapping:
    def test_tenant_ipalloc_name_prefix_filter(self) -> None:
        items = [
            SAMPLE_IPALLOC,
            {"metadata": {"name": "other-tenant-inband-subnet-amcop-0"}, "spec": {}, "status": {}},
        ]
        filtered = [
            item
            for item in items
            if str(item.get("metadata", {}).get("name", "")).lower().startswith("test-tenant001")
        ]
        assert len(filtered) == 1
        assert filtered[0]["metadata"]["name"] == SAMPLE_IPALLOC["metadata"]["name"]

    def test_ipalloc_to_subnet_maps_fields(self) -> None:
        subnet = ipalloc_to_subnet(SAMPLE_IPALLOC)
        assert subnet["subnet_id"] == "test-tenant001-inband-subnet-amcop-0"
        assert subnet["cidr"] == "10.75.0.0/16"
        assert subnet["az"] == "amcop-0"
        assert subnet["auto_assign_public_ip"] is False
        assert subnet["available_ips"] > 0

    def test_pick_primary_ipalloc_prefers_inband(self) -> None:
        converged = {
            "metadata": {"name": "test-tenant001-converged-subnet-amcop-0"},
            "spec": {"subnet": "10.76.0.0/16"},
            "status": {"main_status": "success"},
        }
        picked = pick_primary_ipalloc([converged, SAMPLE_IPALLOC])
        assert picked["metadata"]["name"] == SAMPLE_IPALLOC["metadata"]["name"]


class TestOrchestratorResourceId:
    @pytest.mark.parametrize(
        ("resource_id", "expect_orchestrator"),
        [
            ("a457e14e-8c34-49e2-b365-c84bce58f698", True),
            ("test-tenant001-inband-subnet-amcop-0", False),
            ("n/a", False),
        ],
    )
    def test_is_orchestrator_resource_id(self, resource_id: str, expect_orchestrator: bool) -> None:
        assert is_orchestrator_resource_id(resource_id) is expect_orchestrator


class TestManagedNetworkId:
    @pytest.mark.parametrize(
        ("resource_id", "expect_managed"),
        [
            ("", False),
            ("n/a", False),
            ("N/A", False),
            ("a457e14e-8c34-49e2-b365-c84bce58f698", True),
        ],
    )
    def test_is_managed_network_id(self, resource_id: str, expect_managed: bool) -> None:
        assert is_managed_network_id(resource_id) is expect_managed
