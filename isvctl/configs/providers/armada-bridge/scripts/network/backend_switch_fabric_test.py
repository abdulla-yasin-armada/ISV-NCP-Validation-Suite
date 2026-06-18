#!/usr/bin/env python3
"""backend_switch_fabric_test — Armada Bridge network suite, test phase.

Validates backend switch fabric topology via GET /orchestrator/device-discovery.

BackendSwitchFabricCheck requires:
  tests: {node_resolved, leaf_switch_ids_present, spine_switch_ids_present,
          core_switch_ids_present}
  node_id: non-empty string
  fabric: {leaf_switch_ids: [...], spine_switch_ids: [...], core_switch_ids: [...]}
  (each list must be non-empty list of non-empty strings)

Output: {success, platform, node_id, fabric, tests}
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

DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"

_DISCOVERY_PATH = "/orchestrator/device-discovery"


def _extract_ids(items: list[dict[str, Any]]) -> list[str]:
    """Return non-empty id strings from a list of switch/node dicts."""
    return [str(item["id"]) for item in items if item.get("id")]


@handle_bridge_errors
def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tenant", required=True)
    parser.parse_args()

    result: dict[str, Any] = {"success": False, "platform": "network"}

    if DEMO_MODE:
        result.update(
            {
                "success": True,
                "platform": "network",
                "node_id": "demo-node-0001",
                "fabric": {
                    "leaf_switch_ids": ["demo-leaf-sw-0001"],
                    "spine_switch_ids": ["demo-spine-sw-0001"],
                    "core_switch_ids": ["demo-core-sw-0001"],
                },
                "tests": {
                    "node_resolved": {"passed": True},
                    "leaf_switch_ids_present": {"passed": True},
                    "spine_switch_ids_present": {"passed": True},
                    "core_switch_ids_present": {"passed": True},
                },
            }
        )
        print(json.dumps(result, indent=2))
        return 0

    client = BridgeClient.from_env()
    discovery = client.get(_DISCOVERY_PATH)

    if not isinstance(discovery, dict):
        result["error"] = f"Unexpected response type from {_DISCOVERY_PATH}: {type(discovery).__name__}"
        print(json.dumps(result, indent=2))
        return 1

    server_nodes: list[dict[str, Any]] = discovery.get("serverNodes") or []
    leaf_switches: list[dict[str, Any]] = discovery.get("leafSwitches") or []
    spine_switches: list[dict[str, Any]] = discovery.get("spineSwitches") or []
    core_switches: list[dict[str, Any]] = discovery.get("coreSwitches") or []

    # Use the first compute serverNode as the representative node.
    node_id: str = ""
    if server_nodes:
        first = server_nodes[0]
        node_id = str(first.get("id") or first.get("name") or "")

    leaf_ids = _extract_ids(leaf_switches)
    spine_ids = _extract_ids(spine_switches)
    core_ids = _extract_ids(core_switches)

    tests = {
        "node_resolved": {
            "passed": bool(node_id),
            **({"message": "No serverNodes found in device-discovery response"} if not node_id else {}),
        },
        "leaf_switch_ids_present": {
            "passed": bool(leaf_ids),
            **({"message": "No leafSwitches found in device-discovery response"} if not leaf_ids else {}),
        },
        "spine_switch_ids_present": {
            "passed": bool(spine_ids),
            **({"message": "No spineSwitches found in device-discovery response"} if not spine_ids else {}),
        },
        "core_switch_ids_present": {
            "passed": bool(core_ids),
            **({"message": "No coreSwitches found in device-discovery response"} if not core_ids else {}),
        },
    }

    all_passed = all(t["passed"] for t in tests.values())

    result.update(
        {
            "success": all_passed,
            "platform": "network",
            "node_id": node_id,
            "fabric": {
                "leaf_switch_ids": leaf_ids,
                "spine_switch_ids": spine_ids,
                "core_switch_ids": core_ids,
            },
            "tests": tests,
        }
    )

    print(json.dumps(result, indent=2))
    return 0 if all_passed else 1


if __name__ == "__main__":
    sys.exit(main())
