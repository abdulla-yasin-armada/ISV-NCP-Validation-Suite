#!/usr/bin/env python3
"""teardown — Armada Bridge VM suite, teardown phase.

Terminates one or more VM instances via:
  DELETE /orchestrator/tenants/<tenant>/vms/<vm_id>

Polls GET until Bridge returns 404 (delete complete) for each VM.

Idempotent: if a VM is already gone, skips DELETE and continues.

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
_POLL_TIMEOUT = 270  # vm.yaml teardown step timeout is 300s


def _parse_vm_ids(vm_id: str, vm_ids: str) -> list[str]:
    if vm_ids.strip():
        ids = [part.strip() for part in vm_ids.split(",") if part.strip()]
    elif vm_id.strip():
        ids = [vm_id.strip()]
    else:
        raise RuntimeError("teardown requires --vm-id or --vm-ids")
    # Preserve order while dropping duplicates.
    seen: set[str] = set()
    unique: list[str] = []
    for vm in ids:
        if vm not in seen:
            seen.add(vm)
            unique.append(vm)
    return unique


def _delete_vm(client: BridgeClient, tenant_id: str, vm_id: str) -> str | None:
    """Delete one VM. Returns resource label when deleted, None if already gone."""
    try:
        get_vm(client, tenant_id, vm_id)
    except ValueError as exc:
        if "404" in str(exc):
            return None
        raise

    client.delete(vm_path(tenant_id, vm_id))
    wait_for_vm_deleted(
        client,
        tenant_id,
        vm_id,
        timeout=_POLL_TIMEOUT,
    )
    return f"instance:{vm_id}"


@handle_bridge_errors
def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--vm-id", default="", help="Single VM id (legacy)")
    parser.add_argument("--vm-ids", default="", help="Comma-separated VM ids")
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
        demo_ids = _parse_vm_ids(args.vm_id, args.vm_ids) or ["demo-vm-abc0"]
        result.update(
            {
                "success": True,
                "resources_deleted": [f"instance:{vm_id}" for vm_id in demo_ids],
                "message": f"Deleted {len(demo_ids)} VM(s)",
            }
        )
    else:
        client = BridgeClient.from_env()
        tenant_id = resolve_tenant_id(client, args.tenant)
        vm_ids = _parse_vm_ids(args.vm_id, args.vm_ids)

        deleted: list[str] = []
        already_gone = 0
        for vm_id in vm_ids:
            label = _delete_vm(client, tenant_id, vm_id)
            if label:
                deleted.append(label)
            else:
                already_gone += 1

        if deleted:
            message = f"Deleted {len(deleted)} VM(s)"
        elif already_gone:
            message = f"All {already_gone} VM(s) already deleted"
        else:
            message = "No VMs to delete"

        result.update(
            {
                "success": True,
                "resources_deleted": deleted,
                "message": message,
            }
        )

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
