#!/usr/bin/env python3
"""teardown — Armada Bridge VM suite, teardown phase.

Terminates a VM instance via:
  DELETE /orchestrator/tenants/<tenant>/vms/<vm_id>

Polls GET until Bridge returns 404 (delete complete).

Idempotent: if the VM is already gone, skips DELETE and returns success.

Pass --skip-destroy when ARMADA_BRIDGE_SKIP_TEARDOWN=true (wired via vm.yaml
teardown_flag).

Validation: StepSuccessCheck (success: true).

Output: {success, platform, resources_deleted, message} or
        {success, platform, skipped: true, message} when --skip-destroy
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
from common.tenant import resolve_tenant_id
from common.vm import get_vm, vm_path, wait_for_vm_deleted

DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"
_POLL_TIMEOUT = 105  # vm.yaml teardown step timeout is 120s


@handle_bridge_errors
def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--vm-id", required=True)
    parser.add_argument("--skip-destroy", action="store_true")
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "vm",
        "resources_deleted": [],
        "message": "",
    }

    if args.skip_destroy:
        result.update(
            {
                "success": True,
                "skipped": True,
                "message": "Teardown skipped",
            }
        )
    elif DEMO_MODE:
        result.update(
            {
                "success": True,
                "resources_deleted": ["instance:demo-vm-abc123"],
                "message": "VM deleted",
            }
        )
    else:
        client = BridgeClient.from_env()
        tenant_id = resolve_tenant_id(client, args.tenant)

        already_gone = False
        try:
            get_vm(client, tenant_id, args.vm_id)
        except ValueError as exc:
            if "404" in str(exc):
                already_gone = True
            else:
                raise

        if already_gone:
            result.update(
                {
                    "success": True,
                    "message": "VM not found (already deleted)",
                }
            )
        else:
            client.delete(vm_path(tenant_id, args.vm_id))
            wait_for_vm_deleted(
                client,
                tenant_id,
                args.vm_id,
                timeout=_POLL_TIMEOUT,
            )
            result.update(
                {
                    "success": True,
                    "resources_deleted": [f"instance:{args.vm_id}"],
                    "message": "VM deleted",
                }
            )

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
