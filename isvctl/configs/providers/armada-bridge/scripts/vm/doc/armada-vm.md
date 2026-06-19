# Armada Bridge VM — JSON output reference

Implementation guide for scripts in the parent directory (`../`). Each script must print **one JSON object to stdout** (logs on stderr). Validations in `isvctl/configs/suites/vm.yaml` read those fields; they do not call Bridge APIs directly.

**Related files**

| File | Role |
|------|------|
| `../../config/vm.yaml` | Step order, CLI args, `best_effort` flags |
| `../../../../suites/vm.yaml` | Canonical validation contract (34 checks) |
| `../../common/bridge_client.py` | HTTP client (`BridgeClient.from_env()`) |
| `../../../shared/deploy_nim.py` | Shared NIM deploy (not in this folder) |
| `automation/bridge-api-test-automation/lib/utils/vm.js` | Reference allocate/poll/delete flow |

---

## Steps vs validations (do not confuse the counts)

| Layer | Count | Meaning |
|-------|-------|---------|
| **Provider steps** | **12** | Scripts you implement (see table below) |
| **Validation groups** | **27** | Blocks under `validations:` in `suites/vm.yaml` |
| **Individual checks** | **34** | Pytest validation classes (some groups have multiple checks) |

The suite runs **12 steps** and then executes **34 validation checks** against their stdout (plus SSH from the runner for host/NIM checks).

### Step execution order (`config/vm.yaml`)

| # | Step | Phase | Script | `best_effort` |
|---|------|-------|--------|---------------|
| 1 | `launch_instance` | setup | `launch_instance.py` | no |
| 2 | `list_instances` | test | `list_instances.py` | no |
| 3 | `verify_tags` | test | `describe_tags.py` | **yes** |
| 4 | `stop_instance` | test | `stop_instance.py` | no |
| 5 | `start_instance` | test | `start_instance.py` | no |
| 6 | `reboot_instance` | test | `reboot_instance.py` | no |
| 7 | `serial_console` | test | `serial_console.py` | **yes** |
| 8 | `console_rbac` | test | `console_rbac.py` | **yes** |
| 9 | `describe_instance` | test | `describe_instance.py` | no |
| 10 | `deploy_nim` | test | `../../shared/deploy_nim.py` | no (skips if no NGC key) |
| 11 | `teardown_nim` | teardown | `../../shared/teardown_nim.py` | no |
| 12 | `teardown` | teardown | `teardown.py` | no |

`best_effort: true` — step failure is logged but does **not** fail the suite. Still implement when Bridge exposes the capability.

---

## Field legend

Every step documents three kinds of JSON keys:

| Tag | Meaning |
|-----|---------|
| **R** | **Required** — validation fails if missing or wrong |
| **W** | **Wiring** — not read by validations, but required by `config/vm.yaml` Jinja (`{{steps.launch_instance.vpc_id}}`, etc.) |
| **O** | **Optional** — safe to omit; useful for debugging |

**Always (every step):** `success` (bool), `platform` (`"vm"`). On failure add `error` (string) and exit non-zero.

**SSH checks** do not need extra JSON beyond `public_ip` + `key_file` (+ optional `ssh_user`, default `ubuntu`). The validation runner SSHs to the VM and runs commands (`nvidia-smi`, `cloud-init status`, etc.).

---

## Bridge API quick reference

Base URL: `BRIDGE_URL` (via `BridgeClient`). Orchestrator paths use the **`/orchestrator`** prefix (same as bare-metal scripts).

| Operation | Method | Path |
|-----------|--------|------|
| Allocate VM | `POST` | `/orchestrator/tenants/{tenant}/vms` |
| Get VM | `GET` | `/orchestrator/tenants/{tenant}/vms/{vmId}` |
| List VMs | `GET` | `/orchestrator/tenants/{tenant}/vms` |
| Delete VM | `DELETE` | `/orchestrator/tenants/{tenant}/vms/{vmId}` |
| Power off | `POST` | `/orchestrator/tenants/{tenant}/vms/{vmId}/power/off` |
| Power on | `POST` | `/orchestrator/tenants/{tenant}/vms/{vmId}/power/on` |
| Reboot | `POST` | `/orchestrator/tenants/{tenant}/vms/{vmId}/power/reboot` |

Power actions accept only **`on`**, **`off`**, **`reboot`** (see `bridge-orchestrator` `ExecuteVMPowerAction`). Stub comments that mention `/power/stop`, `/power/start`, or `/power/reset` are **wrong** — fix when implementing.

### Map Bridge status → ISV `state`

Validations expect EC2-like strings. Translate in your script before printing JSON:

| Bridge / API value | Emit as `state` |
|--------------------|-----------------|
| `running`, `active` | `"running"` |
| `poweredOff`, `stopped`, … | `"stopped"` |
| `processing`, `poweringOn`, … | poll until terminal state |

Allocate body (from `vm.js`): **multipart/form-data** with at least `name`, `vmSSHKey`, `flavor` (flavour **name**, not UUID). Poll `GET` until `status === "running"` (typical: 15s interval, 600s max).

---

## Per-step JSON contract

### 1. `launch_instance.py`

**Bridge:** allocate + poll + SSH key file on disk + `wait_for_ssh`.

**Validations:** `InstanceStateCheck`, `InstanceCreatedCheck`, `CloudInitCheck` (SSH uses **this** step’s output).

| Field | Tag | Value / notes |
|-------|-----|---------------|
| `success` | R | `true` |
| `platform` | R | `"vm"` |
| `instance_id` | R | VM UUID from Bridge (`id`) |
| `state` | R | `"running"` |
| `public_ip` | R | For `CloudInitCheck` |
| `key_file` | R | Path to private key used for SSH |
| `vpc_id` | W | Echo `args.vpc_id` if Bridge response lacks it (`list_instances` needs it) |
| `private_ip` | O | |
| `instance_type` | O | Flavour name (e.g. `gpu.1x`) |
| `security_group_id` | O | Echo from args |
| `ssh_user` | O | Default `ubuntu` if omitted |

**Minimal passing JSON:**

```json
{
  "success": true,
  "platform": "vm",
  "instance_id": "vm-uuid-here",
  "state": "running",
  "public_ip": "203.0.113.10",
  "key_file": "/tmp/isv-test-gpu.pem",
  "vpc_id": "vpc-from-env"
}
```

---

### 2. `list_instances.py`

**Bridge:** `GET /orchestrator/tenants/{tenant}/vms` (filter client-side by vpc / instance id if needed).

**Validation:** `InstanceListCheck` (`min_count: 1`).

| Field | Tag | Notes |
|-------|-----|-------|
| `instances` | R | Array; each item needs `instance_id`, `state`, `vpc_id` |
| `found_target` | R | `true` when config passes `--instance-id` |
| `target_instance` | R | Same ID as launch |
| `count` | O | Defaults to `len(instances)` |
| `total_count` | O | **Not read** by validation (my-isv uses it; optional) |

**Minimal passing JSON:**

```json
{
  "success": true,
  "platform": "vm",
  "instances": [
    {
      "instance_id": "vm-uuid-here",
      "state": "running",
      "vpc_id": "vpc-from-env"
    }
  ],
  "target_instance": "vm-uuid-here",
  "found_target": true
}
```

---

### 3. `describe_tags.py` (`verify_tags` step)

**Validation:** `InstanceTagCheck` — keys **`Name`** and **`CreatedBy`** required.

**Bridge:** likely no tag API today → step is `best_effort`; return honest failure or implement when available.

| Field | Tag |
|-------|-----|
| `instance_id` | R |
| `tags` | R — non-empty dict with `Name`, `CreatedBy` |
| `tag_count` | O |

```json
{
  "success": true,
  "platform": "vm",
  "instance_id": "vm-uuid-here",
  "tags": {
    "Name": "isv-test-gpu",
    "CreatedBy": "isvtest"
  },
  "tag_count": 2
}
```

---

### 4. `stop_instance.py`

**Bridge:** `POST .../power/off`, poll `GET` until stopped.

**Validation:** `InstanceStopCheck`.

| Field | Tag | Value |
|-------|-----|-------|
| `instance_id` | R | |
| `stop_initiated` | R | `true` |
| `state` | R | `"stopped"` (map from Bridge `poweredOff`) |

```json
{
  "success": true,
  "platform": "vm",
  "instance_id": "vm-uuid-here",
  "stop_initiated": true,
  "state": "stopped"
}
```

---

### 5. `start_instance.py`

**Bridge:** `POST .../power/on`, poll running, `wait_for_ssh`.

**Validations:** `InstanceStartCheck`, `StableIdentifierCheck`, `ConnectivityCheck`, `OsCheck`, `GpuCheck`.

| Field | Tag | Notes |
|-------|-----|-------|
| `instance_id` | R | Must match `launch_instance.instance_id` |
| `start_initiated` | R | `true` |
| `state` | R | `"running"` |
| `ssh_ready` | R | `true` after SSH probe |
| `public_ip` | R | For SSH checks on this step |
| `key_file` | R | **Required for SSH checks** — persist from launch or re-read from GET; current demo stub omits this |

```json
{
  "success": true,
  "platform": "vm",
  "instance_id": "vm-uuid-here",
  "start_initiated": true,
  "state": "running",
  "ssh_ready": true,
  "public_ip": "203.0.113.10",
  "key_file": "/tmp/isv-test-gpu.pem"
}
```

---

### 6. `reboot_instance.py`

**Bridge:** `POST .../power/reboot`, poll running, SSH, read uptime.

**Validations:** `InstanceRebootCheck`, `StableIdentifierCheck`, `InstanceStateCheck`, SSH/GPU checks.

| Field | Tag | Notes |
|-------|-----|-------|
| `reboot_initiated` | R | `true` |
| `state` | R | `"running"` |
| `ssh_ready` | R | `true` |
| `reboot_confirmed` | R | Must be **`true`** explicitly (missing fails) |
| `uptime_seconds` | R* | If set, must be ≤ **600** (`max_uptime` in suite) |
| `public_ip`, `key_file` | R | For SSH/GPU checks |

```json
{
  "success": true,
  "platform": "vm",
  "instance_id": "vm-uuid-here",
  "reboot_initiated": true,
  "state": "running",
  "ssh_ready": true,
  "reboot_confirmed": true,
  "uptime_seconds": 45,
  "public_ip": "203.0.113.10",
  "key_file": "/tmp/isv-test-gpu.pem"
}
```

Obtain `uptime_seconds` via SSH: `cat /proc/uptime | cut -d' ' -f1`. Compare pre/post reboot or require low uptime after reboot.

---

### 7. `serial_console.py`

**Validation:** `SerialConsoleCheck` — at least one of `console_available` or `serial_access_enabled` must be `true`.

**Bridge:** no standard workflow in `vm.js`; `best_effort`.

| Field | Tag |
|-------|-----|
| `instance_id` | R |
| `console_available` | R* |
| `serial_access_enabled` | R* |

\* At least one must be `true`.

```json
{
  "success": true,
  "platform": "vm",
  "instance_id": "vm-uuid-here",
  "console_available": false,
  "serial_access_enabled": true,
  "output_length": 0
}
```

---

### 8. `console_rbac.py`

**Validation:** `ConsoleRbacCheck`.

| Field | Tag |
|-------|-----|
| `instance_id` | R |
| `access_restricted` | R — must be **`true`** |
| `restricted_actions` | R — non-empty list |
| `tests.denied_principal_cannot_access_console.passed` | R — `true` |
| `tests.allowed_principal_can_access_console.passed` | R — `true` |
| `tests.allowed_principal_is_resource_scoped.passed` | R — `true` |
| `rbac_model`, `test_name` | O |

```json
{
  "success": true,
  "platform": "vm",
  "instance_id": "vm-uuid-here",
  "access_restricted": true,
  "restricted_actions": ["console:Connect"],
  "tests": {
    "denied_principal_cannot_access_console": {"passed": true},
    "allowed_principal_can_access_console": {"passed": true},
    "allowed_principal_is_resource_scoped": {"passed": true}
  }
}
```

---

### 9. `describe_instance.py`

**Validations (10 checks, all SSH except state):** `InstanceStateCheck`, `ConnectivityCheck`, `OsCheck` (ubuntu), `GpuCheck` (≥1 GPU), `VcpuPinningCheck`, `PciBusCheck`, `HostSoftwareCheck`, `DriverCheck`, `CpuInfoCheck`, `ContainerRuntimeCheck`.

Runs **after** stop/start/reboot — proves host survived full lifecycle.

| Field | Tag |
|-------|-----|
| `instance_id` | R |
| `state` | R — `"running"` |
| `public_ip` | R |
| `key_file` | R |
| `ssh_user` | O — default `ubuntu` |

```json
{
  "success": true,
  "platform": "vm",
  "instance_id": "vm-uuid-here",
  "state": "running",
  "public_ip": "203.0.113.10",
  "key_file": "/tmp/isv-test-gpu.pem"
}
```

No GPU/driver/docker fields in JSON — validations SSH and inspect the host.

---

### 10. `deploy_nim` (shared script)

**Config uses:** `steps.describe_instance.public_ip` and `steps.describe_instance.key_file` (not reboot output).

**Validations:** `NimHealthCheck`, `NimModelCheck`, `NimInferenceCheck` — SSH + curl to `localhost:{port}` on the VM.

| Field | Tag | Notes |
|-------|-----|-------|
| `success` | R | `true` |
| `skipped` | O | If `true`, all three NIM checks **skip** (not fail) |
| `host`, `key_file` | R | When not skipped |
| `port` | O | Default `8000` |
| `container_id`, `model`, `health_ready`, … | O | |

Skipped when `NGC_API_KEY` unset:

```json
{
  "success": true,
  "platform": "vm",
  "skipped": true,
  "skip_reason": "NGC_API_KEY not set"
}
```

---

### 11. `teardown_nim` (shared script)

**Validation:** `StepSuccessCheck` — only needs `success: true`.

```json
{
  "success": true,
  "platform": "vm"
}
```

---

### 12. `teardown.py`

**Bridge:** `DELETE .../vms/{id}`, poll until `GET` returns 404.

**Validation:** `StepSuccessCheck`.

| Field | Tag |
|-------|-----|
| `success` | R |
| `message` | O |
| `resources_deleted` | O |

```json
{
  "success": true,
  "platform": "vm",
  "resources_deleted": ["instance:vm-uuid-here"],
  "message": "VM deleted"
}
```

With `ARMADA_BRIDGE_SKIP_TEARDOWN=true` / `--skip-destroy`:

```json
{
  "success": true,
  "platform": "vm",
  "skipped": true,
  "message": "Teardown skipped"
}
```

---

## Failure JSON (any step)

```json
{
  "success": false,
  "platform": "vm",
  "error": "human-readable message"
}
```

Exit code **non-zero**. Include `instance_id` when known.

---

## Full validation matrix (`suites/vm.yaml`)

| Validation | Step | JSON-driven? |
|------------|------|--------------|
| `InstanceStateCheck` | `launch_instance`, `describe_instance`, `reboot_instance` | yes |
| `InstanceCreatedCheck` | `launch_instance` | yes |
| `InstanceListCheck` | `list_instances` | yes |
| `InstanceTagCheck` | `verify_tags` | yes |
| `SerialConsoleCheck` | `serial_console` | yes |
| `ConsoleRbacCheck` | `console_rbac` | yes |
| `CloudInitCheck` | `launch_instance` | SSH (needs IP/key in launch JSON) |
| `InstanceStopCheck` | `stop_instance` | yes |
| `InstanceStartCheck` | `start_instance` | yes |
| `StableIdentifierCheck` | `start_instance`, `reboot_instance` | yes (`instance_id`) |
| `InstanceRebootCheck` | `reboot_instance` | yes |
| `ConnectivityCheck` | `describe_instance`, `start_instance`, `reboot_instance` | SSH |
| `OsCheck` | same | SSH (expects **ubuntu**) |
| `GpuCheck` | same | SSH (`expected_gpus: 1`) |
| `VcpuPinningCheck` | `describe_instance` | SSH |
| `PciBusCheck` | `describe_instance` | SSH |
| `HostSoftwareCheck` | `describe_instance` | SSH |
| `DriverCheck` | `describe_instance` | SSH |
| `CpuInfoCheck` | `describe_instance` | SSH |
| `ContainerRuntimeCheck` | `describe_instance` | SSH (+ Docker on VM) |
| `NimHealthCheck` / `NimModelCheck` / `NimInferenceCheck` | `deploy_nim` | SSH |
| `StepSuccessCheck` | `teardown_nim`, `teardown` | yes |

---

## Implementation checklist (Bridge-specific gaps)

- [ ] Use `/orchestrator/tenants/...` paths and power actions **`on` / `off` / `reboot`**
- [ ] VM allocate via **multipart** form (extend `BridgeClient` if needed)
- [ ] Map Bridge statuses to ISV `running` / `stopped`
- [ ] Write SSH private key to disk; thread `key_file` through **launch → start → reboot → describe**
- [ ] Implement `wait_for_ssh` (see `../../common/ssh_utils.py` stub)
- [ ] Echo `vpc_id` from launch args for `list_instances` wiring
- [ ] `start_instance` / `reboot_instance` demo stubs: add `key_file` to match validation needs
- [ ] Optional: align `config/vm.yaml` with AWS — pass `--key-file` / `--public-ip` into lifecycle steps
- [ ] Tags / serial console / console RBAC: implement or rely on `best_effort` until Bridge supports them

---

## Run commands

```bash
# Demo JSON only (no Bridge, SSH checks will fail)
ISVCTL_DEMO_MODE=1 uv run isvctl test run -f isvctl/configs/providers/armada-bridge/config/vm.yaml

# Live Bridge (requires GPU VM, SSH, Ubuntu, NVIDIA stack)
export BRIDGE_URL=...
export BRIDGE_EMAIL=...
export BRIDGE_PASSWORD=...
export BRIDGE_TENANT=...
export BRIDGE_VPC_ID=...
export NGC_API_KEY=...   # optional — skips NIM if unset

uv run isvctl test run -f isvctl/configs/providers/armada-bridge/config/vm.yaml
```
