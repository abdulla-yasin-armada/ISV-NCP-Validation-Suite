"""Bridge catalog helpers for bare-metal product type discovery."""
from __future__ import annotations

from typing import Any

from .bridge_client import BridgeClient

_SERVER_CATALOG_TYPES = frozenset({"server", ""})


def _product_type_count(product_type: dict[str, Any]) -> int:
    try:
        return int(product_type.get("count", 0))
    except (TypeError, ValueError):
        return 0


def _product_type_label(product_type: dict[str, Any]) -> str:
    for key in ("gpuType", "gpu_type", "name"):
        value = product_type.get(key)
        if value:
            return str(value)
    return str(product_type.get("id", ""))


def _is_server_catalog(catalog: dict[str, Any]) -> bool:
    catalog_type = str(catalog.get("type") or "").lower()
    return catalog_type in _SERVER_CATALOG_TYPES


def _iter_server_product_types(
    catalogs: list[dict[str, Any]],
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Return (catalog, productType) pairs from server catalogs only."""
    pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for catalog in catalogs:
        if not _is_server_catalog(catalog):
            continue
        for product_type in catalog.get("productTypes", []) or []:
            if isinstance(product_type, dict):
                pairs.append((catalog, product_type))
    return pairs


def pick_bm_product_type_id(
    catalogs: list[dict[str, Any]],
    *,
    explicit_id: str = "",
    gpu_type_filter: str = "",
) -> tuple[str, str, bool]:
    """Pick a bare-metal productTypeId from catalog data.

    Returns (product_type_id, flavor_label, auto_discovered).
    explicit_id may be a productTypeId or, if it matches a catalog id, the first
    available product type in that catalog (common operator mistake).
    """
    explicit_id = explicit_id.strip()
    gpu_type_filter = gpu_type_filter.strip().lower()

    if explicit_id:
        for _catalog, product_type in _iter_server_product_types(catalogs):
            pt_id = str(product_type.get("id") or "")
            if pt_id == explicit_id:
                return pt_id, _product_type_label(product_type), False

        for catalog, product_type in _iter_server_product_types(catalogs):
            if str(catalog.get("id", "")) != explicit_id:
                continue
            for candidate in catalog.get("productTypes", []) or []:
                if not isinstance(candidate, dict):
                    continue
                count = _product_type_count(candidate)
                pt_id = str(candidate.get("id") or "")
                if count >= 1 and pt_id:
                    return pt_id, _product_type_label(candidate), False
            raise RuntimeError(
                f"BRIDGE_BM_FLAVOR '{explicit_id}' is a catalog ID, not a product type ID, "
                "and that catalog has no product types with count >= 1."
            )
        return explicit_id, explicit_id, False

    for _catalog, product_type in _iter_server_product_types(catalogs):
        count = _product_type_count(product_type)
        pt_id = str(product_type.get("id") or "")
        if count < 1 or not pt_id:
            continue
        label = _product_type_label(product_type)
        if gpu_type_filter and gpu_type_filter not in label.lower():
            continue
        return pt_id, label, True

    if gpu_type_filter:
        raise RuntimeError(
            f"No server product type with count >= 1 matches BRIDGE_BM_GPU_TYPE='{gpu_type_filter}'. "
            "Set BRIDGE_BM_FLAVOR to a productTypeId explicitly."
        )
    raise RuntimeError(
        "No server product type with count >= 1 found in GET /orchestrator/catalog. "
        "Set BRIDGE_BM_FLAVOR to a productTypeId explicitly."
    )


def discover_bm_product_type_id(
    client: BridgeClient,
    *,
    explicit_id: str = "",
    gpu_type_filter: str = "",
) -> tuple[str, str, bool]:
    """Resolve productTypeId from catalog; explicit_id overrides auto-discovery."""
    catalogs = client.get("/orchestrator/catalog")
    if not catalogs:
        raise RuntimeError(
            "Catalog is empty — no BM flavors available. "
            "Set BRIDGE_BM_FLAVOR to a productTypeId explicitly."
        )
    if not isinstance(catalogs, list):
        raise RuntimeError("Unexpected catalog response — expected a JSON array.")
    return pick_bm_product_type_id(
        catalogs,
        explicit_id=explicit_id,
        gpu_type_filter=gpu_type_filter,
    )
