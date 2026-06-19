# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary

"""Tests for Armada Bridge BM catalog / flavor discovery."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ISVCTL_ROOT = Path(__file__).resolve().parents[1]
BRIDGE_SCRIPTS = ISVCTL_ROOT / "configs" / "providers" / "armada-bridge" / "scripts"

scripts_path = str(BRIDGE_SCRIPTS)
if scripts_path not in sys.path:
    sys.path.insert(0, scripts_path)

from common.catalog import pick_bm_product_type_id

# Single-entry lab where the catalog ID ≠ product type ID; models a typical CSV import lab.
IMPORT_LAB_CATALOG = [
    {
        "id": "dfd23c95-d741-4236-9018-baf9faeb44d6",
        "type": "server",
        "productTypes": [
            {
                "id": "517fc192-bdea-42ff-857a-b53393201c28",
                "name": "Google Compute Engine [e0c96380]",
                "count": 1,
                "gpuType": "Google Compute Engine [e0c96380]",
            }
        ],
    }
]

# Two server product types (H100, L4) plus a storage entry; tests GPU filter and storage-skip logic.
MULTI_FLAVOR_CATALOG = [
    {
        "id": "catalog-a",
        "type": "server",
        "productTypes": [
            {"id": "pt-h100", "name": "H100 node", "count": 2, "gpuType": "H100"},
            {"id": "pt-l4", "name": "L4 node", "count": 1, "gpuType": "L4"},
        ],
    },
    {
        "id": "storage-catalog",
        "type": "storage",
        "productTypes": [{"id": "pt-storage", "name": "storage", "count": 5}],
    },
]


class TestPickBmProductTypeId:
    def test_auto_discover_single_node_lab(self) -> None:
        pt_id, label, auto = pick_bm_product_type_id(IMPORT_LAB_CATALOG)
        assert pt_id == "517fc192-bdea-42ff-857a-b53393201c28"
        assert "Google Compute Engine" in label
        assert auto is True

    def test_explicit_product_type_id(self) -> None:
        pt_id, label, auto = pick_bm_product_type_id(
            IMPORT_LAB_CATALOG,
            explicit_id="517fc192-bdea-42ff-857a-b53393201c28",
        )
        assert pt_id == "517fc192-bdea-42ff-857a-b53393201c28"
        assert auto is False
        assert label != pt_id

    def test_catalog_id_mistake_resolves_to_product_type(self) -> None:
        """User passes the catalog object ID instead of the product type ID; should still resolve correctly."""
        pt_id, _label, auto = pick_bm_product_type_id(
            IMPORT_LAB_CATALOG,
            explicit_id="dfd23c95-d741-4236-9018-baf9faeb44d6",
        )
        assert pt_id == "517fc192-bdea-42ff-857a-b53393201c28"
        assert auto is False

    def test_skips_storage_catalog(self) -> None:
        pt_id, _label, auto = pick_bm_product_type_id(MULTI_FLAVOR_CATALOG)
        assert pt_id == "pt-h100"
        assert auto is True

    def test_gpu_type_filter(self) -> None:
        pt_id, label, auto = pick_bm_product_type_id(
            MULTI_FLAVOR_CATALOG,
            gpu_type_filter="l4",
        )
        assert pt_id == "pt-l4"
        assert label == "L4"
        assert auto is True

    def test_no_capacity_raises(self) -> None:
        empty = [
            {
                "id": "cat-1",
                "type": "server",
                "productTypes": [{"id": "pt-1", "name": "node", "count": 0}],
            }
        ]
        with pytest.raises(RuntimeError, match="count >= 1"):
            pick_bm_product_type_id(empty)

    def test_empty_catalog_raises(self) -> None:
        with pytest.raises(RuntimeError, match="count >= 1"):
            pick_bm_product_type_id([])
