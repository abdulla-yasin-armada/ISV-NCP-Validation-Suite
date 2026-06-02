# Control Plane Live Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement live (non-demo) Bridge API calls in the 5 non-best-effort control-plane scripts, mirroring the IAM suite pattern.

**Architecture:** Each script uses `BridgeClient.from_env()` for authenticated HTTP, follows the `DEMO_MODE / else` structure from the IAM scripts, and emits structured JSON. The YAML config gains a `teardown_flag` setting and the `get_tenant` / `delete_tenant` steps are updated to use `tenant_id`. Best-effort steps (`create_access_key`, `test_access_key`, `disable_access_key`, `verify_key_rejected`, `delete_access_key`) are not touched.

**Tech Stack:** Python 3.12, `BridgeClient` (stdlib `urllib`, no third-party deps), `uv run isvctl` for smoke tests.

**Spec:** `docs/superpowers/specs/2026-05-14-control-plane-live-impl-design.md`

---

## Orientation

All paths below are relative to `ISV-NCP-Validation-Suite/`.

Key patterns from the IAM scripts (read these before starting):
- `isvctl/configs/providers/armada-bridge/scripts/iam/create_user.py` — live impl with idempotency
- `isvctl/configs/providers/armada-bridge/scripts/iam/delete_user.py` — `--skip-destroy` flag
- `isvctl/configs/providers/armada-bridge/scripts/common/bridge_client.py` — `BridgeClient`
- `isvctl/configs/providers/armada-bridge/scripts/common/iam.py` — `extract_tenant_from_tenants`

Bridge tenant model JSON fields: `ID` (uppercase), `name`, `description`, `status`.

Demo smoke command (run from `ISV-NCP-Validation-Suite/`):
```bash
ISVCTL_DEMO_MODE=1 uv run isvctl test run \
  -f isvctl/configs/providers/armada-bridge/config/control-plane.yaml
```
Expected: all validations pass (`api_health`, `setup_checks`, `access_key_lifecycle`,
`tenant_lifecycle`, `teardown_checks`).

---

## Task 1: YAML — add teardown_flag + update get_tenant/delete_tenant args

**Files:**
- Modify: `isvctl/configs/providers/armada-bridge/config/control-plane.yaml`

- [ ] **Step 1: Verify demo passes before touching anything**

```bash
cd ISV-NCP-Validation-Suite
ISVCTL_DEMO_MODE=1 uv run isvctl test run \
  -f isvctl/configs/providers/armada-bridge/config/control-plane.yaml
```
Expected: all validations pass. If not, stop — something is already broken.

- [ ] **Step 2: Add `teardown_flag` to settings**

In `isvctl/configs/providers/armada-bridge/config/control-plane.yaml`, find the `tests:` → `settings:` block (currently lines 107–112) and add the `teardown_flag` line:

```yaml
tests:
  cluster_name: "armada-bridge-control-plane-validation"
  description: "Armada Bridge control plane lifecycle validation"

  settings:
    bridge_url: "{{env.BRIDGE_URL | default('')}}"
    bridge_tenant: "{{env.BRIDGE_TENANT | default('demo-tenant')}}"
    teardown_flag: "{{(env.ARMADA_BRIDGE_SKIP_TEARDOWN == 'true') | ternary('--skip-destroy', '')}}"
```

- [ ] **Step 3: Update `get_tenant` step — switch arg from tenant_name to tenant_id**

Find the `get_tenant` step (currently lines 79–85) and change it to:

```yaml
      - name: get_tenant
        phase: test
        command: "python ../scripts/control-plane/get_tenant.py"
        args:
          - "--tenant-id"
          - "{{steps.create_tenant.tenant_id}}"
        timeout: 60
```

- [ ] **Step 4: Update `delete_tenant` step — add teardown_flag arg, remove output_schema**

Find the `delete_tenant` step (currently lines 97–104) and change it to:

```yaml
      - name: delete_tenant
        phase: teardown
        command: "python ../scripts/control-plane/delete_tenant.py"
        args:
          - "--tenant-id"
          - "{{steps.create_tenant.tenant_id}}"
          - "{{teardown_flag}}"
        timeout: 60
```

- [ ] **Step 5: Verify demo still passes**

```bash
ISVCTL_DEMO_MODE=1 uv run isvctl test run \
  -f isvctl/configs/providers/armada-bridge/config/control-plane.yaml
```
Expected: all validations pass (demo mode ignores the new arg).

- [ ] **Step 6: Commit**

```bash
git add isvctl/configs/providers/armada-bridge/config/control-plane.yaml
git commit -m "feat(control-plane): add teardown_flag; switch get_tenant/delete_tenant to tenant_id"
```

---

## Task 2: `check_api.py` — implement live health probes

**Files:**
- Modify: `isvctl/configs/providers/armada-bridge/scripts/control-plane/check_api.py`

The live block must probe `GET /health` (auth-gateway) and `GET /orchestrator/health-check`
(orchestrator), then build the `tests` hash the validator expects. Neither endpoint returns
JSON, so we bypass `BridgeClient.get()` and use the client's internal `_opener` directly
(which already holds the authenticated session cookie).

- [ ] **Step 1: Replace the file with the implementation below**

Full file content for `isvctl/configs/providers/armada-bridge/scripts/control-plane/check_api.py`:

```python
#!/usr/bin/env python3
"""check_api — Armada Bridge control-plane suite, setup phase.

Probes two Bridge health endpoints:
  GET /health             → auth-gateway liveness
  GET /orchestrator/health-check → orchestrator liveness

Output: {success, platform, account_id, tests: {auth_gateway_health, orchestrator_health}}
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common.bridge_client import BridgeClient
from common.errors import handle_bridge_errors

DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"


def _probe(client: BridgeClient, path: str) -> tuple[bool, str]:
    """GET a health endpoint using the authenticated session; return (passed, message).

    Uses client._opener so the session cookie is sent. Treats any 2xx as success
    regardless of response body format (health endpoints may return plain text).
    """
    req = urllib.request.Request(client.base_url + path, method="GET")
    try:
        with client._opener.open(req, timeout=10) as resp:
            resp.read()
            return True, f"GET {path} returned OK"
    except urllib.error.HTTPError as e:
        e.read()
        return False, f"GET {path} returned HTTP {e.code}"
    except Exception as e:
        return False, f"GET {path} connection failed: {type(e).__name__}: {e}"


@handle_bridge_errors
def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tenant", required=True)
    args = parser.parse_args()

    result: dict[str, Any] = {"success": False, "platform": "control_plane"}

    if DEMO_MODE:
        result.update(
            {
                "success": True,
                "platform": "control_plane",
                "account_id": "armada-bridge-demo",
                "tests": {
                    "auth_gateway_health": {
                        "passed": True,
                        "message": "GET /health returned OK",
                    },
                    "orchestrator_health": {
                        "passed": True,
                        "message": "GET /orchestrator/health-check returned healthy",
                    },
                },
            }
        )
    else:
        client = BridgeClient.from_env()

        gw_passed, gw_msg = _probe(client, "/health")
        orch_passed, orch_msg = _probe(client, "/orchestrator/health-check")

        result.update(
            {
                "success": gw_passed and orch_passed,
                "account_id": os.environ.get("BRIDGE_USERNAME", ""),
                "tests": {
                    "auth_gateway_health": {"passed": gw_passed, "message": gw_msg},
                    "orchestrator_health": {"passed": orch_passed, "message": orch_msg},
                },
            }
        )

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Verify demo still passes**

```bash
ISVCTL_DEMO_MODE=1 uv run isvctl test run \
  -f isvctl/configs/providers/armada-bridge/config/control-plane.yaml
```
Expected: all validations pass (demo path is unchanged).

- [ ] **Step 3: Commit**

```bash
git add isvctl/configs/providers/armada-bridge/scripts/control-plane/check_api.py
git commit -m "feat(control-plane): implement check_api live health probes"
```

---

## Task 3: `create_tenant.py` — implement live tenant creation

**Files:**
- Modify: `isvctl/configs/providers/armada-bridge/scripts/control-plane/create_tenant.py`

Live flow:
1. `POST /orchestrator/tenants` with `{name, description}`
2. Response: `{ID, name, description, ...}` (Tenant model; `ID` is uppercase)
3. Idempotency: 409 → fall through to `GET /orchestrator/tenants` and find by name

- [ ] **Step 1: Replace the file with the implementation below**

Full file content for `isvctl/configs/providers/armada-bridge/scripts/control-plane/create_tenant.py`:

```python
#!/usr/bin/env python3
"""create_tenant — Armada Bridge control-plane suite, setup phase.

Creates a tenant (resource group / namespace) via:
  POST /orchestrator/tenants
  Body: {name: <tenant_name>, description: "ISV test tenant"}

Response shape (Tenant model): {ID (uppercase), name, description, status, ...}

Idempotency: on 409 (tenant already exists), falls through to
  GET /orchestrator/tenants and locates the tenant by name.

Output: {success, platform, tenant_name, tenant_id, description}
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
from common.iam import extract_tenant_from_tenants

DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"


@handle_bridge_errors
def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tenant-name", required=True)
    args = parser.parse_args()

    result: dict[str, Any] = {"success": False, "platform": "control_plane"}

    if DEMO_MODE:
        result.update(
            {
                "success": True,
                "platform": "control_plane",
                "tenant_name": "isv-test-tenant",
                "tenant_id": "demo-tenant-uuid-0001",
                "description": "ISV test tenant",
            }
        )
    else:
        client = BridgeClient.from_env()

        try:
            tenant = client.post(
                "/orchestrator/tenants",
                {"name": args.tenant_name, "description": "ISV test tenant"},
            )
        except ValueError as e:
            if "status 409" not in str(e):
                raise
            # Tenant already exists from a prior run — find it in the list
            tenants = client.get("/orchestrator/tenants")
            tenant = extract_tenant_from_tenants(tenants, args.tenant_name)
            if tenant is None:
                result["error"] = (
                    f"Tenant '{args.tenant_name}' returned 409 but was not found in list"
                )
                print(json.dumps(result, indent=2))
                return 1

        result.update(
            {
                "success": True,
                "tenant_name": tenant["name"],
                "tenant_id": tenant["ID"],
                "description": tenant.get("description", "ISV test tenant"),
            }
        )

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Verify demo still passes**

```bash
ISVCTL_DEMO_MODE=1 uv run isvctl test run \
  -f isvctl/configs/providers/armada-bridge/config/control-plane.yaml
```
Expected: all validations pass.

- [ ] **Step 3: Commit**

```bash
git add isvctl/configs/providers/armada-bridge/scripts/control-plane/create_tenant.py
git commit -m "feat(control-plane): implement create_tenant live — POST /orchestrator/tenants"
```

---

## Task 4: `list_tenants.py` — implement live tenant list

**Files:**
- Modify: `isvctl/configs/providers/armada-bridge/scripts/control-plane/list_tenants.py`

Live flow: `GET /orchestrator/tenants` → filter list for `args.tenant_name` using
`extract_tenant_from_tenants`. `success` = target tenant found.

- [ ] **Step 1: Replace the file with the implementation below**

Full file content for `isvctl/configs/providers/armada-bridge/scripts/control-plane/list_tenants.py`:

```python
#!/usr/bin/env python3
"""list_tenants — Armada Bridge control-plane suite, test phase.

Lists tenants and verifies the target tenant is present via:
  GET /orchestrator/tenants

Output: {success, platform, found_target, target_tenant, count}
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
from common.iam import extract_tenant_from_tenants

DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"


@handle_bridge_errors
def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tenant-name", required=True)
    args = parser.parse_args()

    result: dict[str, Any] = {"success": False, "platform": "control_plane"}

    if DEMO_MODE:
        result.update(
            {
                "success": True,
                "platform": "control_plane",
                "found_target": True,
                "target_tenant": "isv-test-tenant",
                "count": 1,
            }
        )
    else:
        client = BridgeClient.from_env()
        tenants = client.get("/orchestrator/tenants")

        tenant_info = extract_tenant_from_tenants(tenants, args.tenant_name)
        found = tenant_info is not None
        count = len(tenants) if isinstance(tenants, list) else 0

        if not found:
            result["error"] = f"Tenant '{args.tenant_name}' not found in list of {count} tenants"

        result.update(
            {
                "success": found,
                "found_target": found,
                "target_tenant": args.tenant_name,
                "count": count,
            }
        )

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Verify demo still passes**

```bash
ISVCTL_DEMO_MODE=1 uv run isvctl test run \
  -f isvctl/configs/providers/armada-bridge/config/control-plane.yaml
```
Expected: all validations pass.

- [ ] **Step 3: Commit**

```bash
git add isvctl/configs/providers/armada-bridge/scripts/control-plane/list_tenants.py
git commit -m "feat(control-plane): implement list_tenants live — GET /orchestrator/tenants"
```

---

## Task 5: `get_tenant.py` — switch to --tenant-id + implement live lookup

**Files:**
- Modify: `isvctl/configs/providers/armada-bridge/scripts/control-plane/get_tenant.py`

The YAML (updated in Task 1) now passes `{{steps.create_tenant.tenant_id}}` as `--tenant-id`.
The live call is a direct `GET /orchestrator/tenants/:tenantId` (response = single Tenant object).

- [ ] **Step 1: Replace the file with the implementation below**

Full file content for `isvctl/configs/providers/armada-bridge/scripts/control-plane/get_tenant.py`:

```python
#!/usr/bin/env python3
"""get_tenant — Armada Bridge control-plane suite, test phase.

Retrieves a specific tenant by ID via:
  GET /orchestrator/tenants/:tenantId

Output: {success, platform, tenant_name, tenant_id, description}
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


@handle_bridge_errors
def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tenant-id", required=True)
    args = parser.parse_args()

    result: dict[str, Any] = {"success": False, "platform": "control_plane"}

    if DEMO_MODE:
        result.update(
            {
                "success": True,
                "platform": "control_plane",
                "tenant_name": "isv-test-tenant",
                "tenant_id": "demo-tenant-uuid-0001",
                "description": "ISV test tenant",
            }
        )
    else:
        client = BridgeClient.from_env()
        tenant = client.get(f"/orchestrator/tenants/{args.tenant_id}")

        result.update(
            {
                "success": True,
                "tenant_name": tenant["name"],
                "tenant_id": tenant["ID"],
                "description": tenant.get("description", ""),
            }
        )

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Verify demo still passes**

In demo mode `args.tenant_id` is `"demo-tenant-uuid-0001"` (from the demo output of
`create_tenant`). The demo branch doesn't use `args.tenant_id`, so this is safe.

```bash
ISVCTL_DEMO_MODE=1 uv run isvctl test run \
  -f isvctl/configs/providers/armada-bridge/config/control-plane.yaml
```
Expected: all validations pass.

- [ ] **Step 3: Commit**

```bash
git add isvctl/configs/providers/armada-bridge/scripts/control-plane/get_tenant.py
git commit -m "feat(control-plane): implement get_tenant live — GET /orchestrator/tenants/:id"
```

---

## Task 6: `delete_tenant.py` — add --skip-destroy + implement live deletion

**Files:**
- Modify: `isvctl/configs/providers/armada-bridge/scripts/control-plane/delete_tenant.py`

`--skip-destroy` mirrors `delete_user.py` exactly. `BridgeClient.delete()` already swallows
404 (the server returns 200 even on re-delete, per Bridge teardown coordination), so any
surviving `ValueError` from `delete()` is a real failure.

- [ ] **Step 1: Replace the file with the implementation below**

Full file content for `isvctl/configs/providers/armada-bridge/scripts/control-plane/delete_tenant.py`:

```python
#!/usr/bin/env python3
"""delete_tenant — Armada Bridge control-plane suite, teardown phase.

Deletes the tenant via:
  DELETE /orchestrator/tenants/<tenant_id>

auth-gateway runs teardown coordination before proxying. BridgeClient.delete()
already swallows 404 (already deleted = success).

Output: {success, platform}
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


@handle_bridge_errors
def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--skip-destroy", action="store_true")
    args = parser.parse_args()

    result: dict[str, Any] = {"success": False, "platform": "control_plane"}

    if args.skip_destroy:
        result["success"] = True
        result["skipped"] = True
    elif DEMO_MODE:
        result["success"] = True
    else:
        client = BridgeClient.from_env()
        try:
            client.delete(f"/orchestrator/tenants/{args.tenant_id}")
        except ValueError as e:
            result["error"] = str(e)
            print(json.dumps(result, indent=2))
            return 1
        result["success"] = True

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Verify demo still passes**

```bash
ISVCTL_DEMO_MODE=1 uv run isvctl test run \
  -f isvctl/configs/providers/armada-bridge/config/control-plane.yaml
```
Expected: all validations pass.

- [ ] **Step 3: Verify skip-destroy flag works**

```bash
ARMADA_BRIDGE_SKIP_TEARDOWN=true ISVCTL_DEMO_MODE=1 uv run isvctl test run \
  -f isvctl/configs/providers/armada-bridge/config/control-plane.yaml
```
Expected: teardown phase runs, `delete_tenant` emits `{success: true, skipped: true}`,
`teardown_checks` validation passes (it only checks `success: true`).

- [ ] **Step 4: Commit**

```bash
git add isvctl/configs/providers/armada-bridge/scripts/control-plane/delete_tenant.py
git commit -m "feat(control-plane): implement delete_tenant live — DELETE /orchestrator/tenants/:id"
```

---

## Final Verification

- [ ] **Full demo run**

```bash
ISVCTL_DEMO_MODE=1 uv run isvctl test run \
  -f isvctl/configs/providers/armada-bridge/config/control-plane.yaml
```
Expected: all 5 validation groups pass (`api_health`, `setup_checks`,
`access_key_lifecycle`, `tenant_lifecycle`, `teardown_checks`).

- [ ] **Confirm best-effort scripts are untouched**

```bash
git diff HEAD~6 -- \
  isvctl/configs/providers/armada-bridge/scripts/control-plane/create_access_key.py \
  isvctl/configs/providers/armada-bridge/scripts/control-plane/test_access_key.py \
  isvctl/configs/providers/armada-bridge/scripts/control-plane/disable_access_key.py \
  isvctl/configs/providers/armada-bridge/scripts/control-plane/verify_key_rejected.py \
  isvctl/configs/providers/armada-bridge/scripts/control-plane/delete_access_key.py
```
Expected: no output (none of these files changed).
