# Control Plane Live Implementation Design

**Date:** 2026-05-14
**Scope:** `ISV-NCP-Validation-Suite/isvctl/configs/providers/armada-bridge/`

## Goal

Implement live (non-demo) code paths for the Armada Bridge control-plane suite,
mirroring the pattern already established in the IAM suite (`config/iam.yaml` +
`scripts/iam/`). Best-effort steps are explicitly excluded.

---

## What Changes

### 1. `config/control-plane.yaml` — three YAML changes

**Add `teardown_flag` to settings** (mirrors `iam.yaml`):
```yaml
settings:
  bridge_url: "{{env.BRIDGE_URL | default('')}}"
  bridge_tenant: "{{env.BRIDGE_TENANT | default('demo-tenant')}}"
  teardown_flag: "{{(env.ARMADA_BRIDGE_SKIP_TEARDOWN == 'true') | ternary('--skip-destroy', '')}}"
```

**Update `delete_tenant` step** — pass `teardown_flag` and remove `output_schema: teardown`
(the script emits `{success, platform}` which is sufficient):
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

**Update `get_tenant` step** — switch from `--tenant-name` to `--tenant-id` so
the script can do a direct `GET /orchestrator/tenants/:tenantId`:
```yaml
- name: get_tenant
  phase: test
  command: "python ../scripts/control-plane/get_tenant.py"
  args:
    - "--tenant-id"
    - "{{steps.create_tenant.tenant_id}}"
  timeout: 60
```

---

### 2. `scripts/control-plane/` — five scripts get live implementations

Best-effort steps (`create_access_key`, `test_access_key`, `disable_access_key`,
`verify_key_rejected`, `delete_access_key`) are **not implemented** — they remain
`raise NotImplementedError` and pass because `best_effort: true`.

#### `check_api.py`

- `GET /health` — auth-gateway liveness
- `GET /orchestrator/health-check` — orchestrator liveness
- Neither endpoint returns the structured `tests` hash the validator expects;
  the script builds it from HTTP status codes.
- `account_id` is derived from `os.environ["BRIDGE_USERNAME"]`.
- Both probes wrapped in try/except; per-probe `passed`/`message` set accordingly.
- Overall `success` = both probes passed.

**Output:**
```json
{
  "success": true,
  "platform": "control_plane",
  "account_id": "<BRIDGE_USERNAME>",
  "tests": {
    "auth_gateway_health":   {"passed": true,  "message": "GET /health returned OK"},
    "orchestrator_health":   {"passed": true,  "message": "GET /orchestrator/health-check returned healthy"}
  }
}
```

#### `create_tenant.py`

- `POST /orchestrator/tenants` with body `{name, description: "ISV test tenant"}`
- Response shape (from orchestrator Tenant model): `{ID (uppercase), name, description, ...}`
- Idempotency: on 409, fall through to `GET /orchestrator/tenants` and find by name.
- Outputs `tenant_id` ← response `.ID`, `tenant_name` ← response `.name`.

**Output:**
```json
{"success": true, "platform": "control_plane",
 "tenant_name": "isv-test-tenant", "tenant_id": "<uuid>", "description": "ISV test tenant"}
```

#### `list_tenants.py`

- `GET /orchestrator/tenants` — returns list of Tenant objects.
- Filter list for the target tenant name (arg `--tenant-name`).
- Outputs `found_target`, `target_tenant`, `count` (total list length).

**Output:**
```json
{"success": true, "platform": "control_plane",
 "found_target": true, "target_tenant": "isv-test-tenant", "count": 3}
```

#### `get_tenant.py`

- Arg changes from `--tenant-name` to `--tenant-id`.
- `GET /orchestrator/tenants/:tenantId` — direct lookup by ID.
- Outputs `tenant_name`, `tenant_id`, `description`.

**Output:**
```json
{"success": true, "platform": "control_plane",
 "tenant_name": "isv-test-tenant", "tenant_id": "<uuid>", "description": "ISV test tenant"}
```

#### `delete_tenant.py`

- Adds `--skip-destroy` flag (same as `delete_user.py`).
- When `--skip-destroy` is set → `{success: true, skipped: true}`, no API call.
- `DELETE /orchestrator/tenants/:tenantId`
- 404 on DELETE is swallowed (already deleted = success), consistent with
  `BridgeClient.delete()` 404-ignore behavior.

**Output:**
```json
{"success": true, "platform": "control_plane"}
```

---

## IAM Pattern Reference

All implementations follow this established pattern from the IAM suite:

```
@handle_bridge_errors
def main() -> int:
    # parse args
    result = {"success": False, "platform": "..."}

    if DEMO_MODE:
        result.update({...demo output...})
    else:
        client = BridgeClient.from_env()
        # live API calls
        # idempotency guards (409/404 handling)
        result["success"] = True

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1
```

---

## Files Touched

| File | Change |
|------|--------|
| `config/control-plane.yaml` | Add `teardown_flag` setting; update `get_tenant` args; update `delete_tenant` args |
| `scripts/control-plane/check_api.py` | Add live implementation |
| `scripts/control-plane/create_tenant.py` | Add live implementation |
| `scripts/control-plane/list_tenants.py` | Add live implementation |
| `scripts/control-plane/get_tenant.py` | Add live implementation; change arg `--tenant-name` → `--tenant-id` |
| `scripts/control-plane/delete_tenant.py` | Add live implementation + `--skip-destroy` flag |

**Not touched:** `create_access_key.py`, `test_access_key.py`, `disable_access_key.py`,
`verify_key_rejected.py`, `delete_access_key.py` (all `best_effort: true`).

---

## Validation

Demo run must continue to pass:
```bash
ISVCTL_DEMO_MODE=1 uv run isvctl test run \
  -f isvctl/configs/providers/armada-bridge/config/control-plane.yaml
```

Live run requires: `BRIDGE_URL`, `BRIDGE_USERNAME`, `BRIDGE_PASSWORD`.
