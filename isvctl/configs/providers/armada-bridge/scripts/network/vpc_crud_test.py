#!/usr/bin/env python3
"""vpc_crud_test — Armada Bridge network suite, test phase.

Discovery flow: exercise orchestrator VPC create/read/delete lifecycle.
Import flow: returns structured failure — IPAllocation CRs are not CRUD via VPC API.

Output: {success, platform, tests: {create_vpc, read_vpc, update_tags,
         update_dns, delete_vpc}}
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common.bridge_client import BridgeClient
from common.errors import handle_bridge_errors
from common.network import (
    get_tenant_vpc,
    import_flow_block,
    is_import_flow,
    is_orchestrator_resource_id,
    list_topologies,
    pick_ethernet_topology,
)
from common.tenant import resolve_tenant_id

DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"


def _failed_test(error: str) -> dict[str, Any]:
    return {"passed": False, "error": error}


@handle_bridge_errors
def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--vpc-id", required=True)
    args = parser.parse_args()

    result: dict[str, Any] = {"success": False, "platform": "network", "tests": {}}

    if DEMO_MODE:
        result.update(
            {
                "success": True,
                "platform": "network",
                "tests": {
                    "create_vpc": {"passed": True},
                    "read_vpc": {"passed": True},
                    "update_tags": {"passed": True},
                    "update_dns": {"passed": True},
                    "delete_vpc": {"passed": True},
                },
            }
        )
    else:
        client = BridgeClient.from_env()
        tenant_id = resolve_tenant_id(client, args.tenant)
        topologies = list_topologies(client)

        if is_import_flow(topologies) or not is_orchestrator_resource_id(args.vpc_id):
            result = import_flow_block("vpc_crud")
            result["tests"] = {
                "create_vpc": _failed_test("import flow"),
                "read_vpc": _failed_test("import flow"),
                "update_tags": _failed_test("import flow"),
                "update_dns": _failed_test("import flow"),
                "delete_vpc": _failed_test("import flow"),
            }
        else:
            tests: dict[str, Any] = {}
            temp_vpc_id = ""
            epoch = int(time.time())

            try:
                topology_name = str(pick_ethernet_topology(topologies).get("topology", ""))
                created = client.post(
                    f"/orchestrator/tenants/{tenant_id}/vpcs",
                    {
                        "name": f"isv-crud-vpc-{epoch}",
                        "topologyID": topology_name,
                        "description": "isvctl vpc crud test",
                        "capabilities": [],
                    },
                )
                temp_vpc_id = str(created.get("id", "") or "")
                tests["create_vpc"] = {
                    "passed": bool(temp_vpc_id),
                    "vpc_id": temp_vpc_id,
                }

                read_resp = get_tenant_vpc(client, tenant_id, temp_vpc_id)
                tests["read_vpc"] = {
                    "passed": str(read_resp.get("id", "")) == temp_vpc_id,
                    "state": "available",
                    "cidr": "",
                }

                # Bridge orchestrator has no VPC tag/DNS mutation endpoints yet.
                tests["update_tags"] = {
                    "passed": False,
                    "error": "Bridge VPC API does not support tag mutation (no tag endpoint)",
                }
                tests["update_dns"] = {
                    "passed": False,
                    "error": "Bridge VPC API does not support DNS hostname configuration",
                }

                client.delete(f"/orchestrator/tenants/{tenant_id}/vpcs/{temp_vpc_id}")
                tests["delete_vpc"] = {"passed": True}
            except Exception as exc:
                if temp_vpc_id and is_orchestrator_resource_id(temp_vpc_id):
                    try:
                        client.delete(f"/orchestrator/tenants/{tenant_id}/vpcs/{temp_vpc_id}")
                    except ValueError:
                        pass
                raise exc

            result.update(
                {
                    "success": all(test.get("passed") for test in tests.values()),
                    "platform": "network",
                    "tests": tests,
                }
            )

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
