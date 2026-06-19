# Armada Bridge Slurm — JSON output reference

Implementation guide for scripts in the parent directory (`../`). Each script must print **one JSON object to stdout** (logs on stderr). Validations in `isvctl/configs/suites/slurm.yaml` read setup fields via Jinja; they run **Slurm CLI locally** on the machine executing `isvctl` — they do **not** call Bridge APIs or the Bridge Job HTTP API.

**Related files**

| File | Role |
|------|------|
| `../../config/slurm.yaml` | Step order, CLI args, Bridge env wiring |
| `../../../../suites/slurm.yaml` | Canonical validation contract (13 checks) |
| `../../common/bridge_client.py` | HTTP client (`BridgeClient.from_env()`) |
| `../../../my-isv/scripts/slurm/setup.sh` | Inventory JSON shape (`sinfo` / `scontrol`) |
| `automation/bridge-api-test-automation/lib/utils/slurm.js` | Reference create/poll/delete + Job API flow |
| `automation/bridge-api-test-automation/wfs/vm/vm_based_slurm_cluster_workflow.postman_collection.json` | VM → Slurm end-to-end |
| `automation/bridge-api-test-automation/wfs/slurm-cluster-workflow.postman_collection.json` | BM-based Slurm workflow |

---

## Steps vs validations (do not confuse the counts)

| Layer | Count | Meaning |
|-------|-------|---------|
| **Provider steps** | **2** | `setup.sh`, `teardown.sh` |
| **Validation groups** | **2** | `slurm`, `slurm_workloads` in `suites/slurm.yaml` |
| **Individual checks** | **13** | Pytest validation / workload classes |

The suite runs **2 lifecycle steps**, then executes **13 checks** that invoke **`sinfo`**, **`srun`**, and **`sbatch`** via `LocalRunner` on the **isvctl host** (see [Runner access](#runner-access-critical)).

### Step execution order (`config/slurm.yaml`)

| # | Step | Phase | Script | Timeout |
|---|------|-------|--------|---------|
| 1 | `setup` | setup | `setup.sh` | 600s |
| 2 | `teardown` | teardown | `teardown.sh` | 300s |

---

## Field legend

| Tag | Meaning |
|-----|---------|
| **R** | **Required** — schema / validations fail if missing or wrong |
| **W** | **Wiring** — required for teardown or Jinja in `suites/slurm.yaml` |
| **O** | **Optional** — safe to omit; useful for debugging or ops |

**Always (every step):** `success` (bool), `platform` (`"slurm"`). On failure add `error` (string) and exit non-zero.

---

## Runner access (critical)

ISV Slurm validations use **`LocalRunner`** — commands run on the **same machine as `isvctl`**, not over SSH and not via Bridge’s Job REST API.

| What Bridge automation tests | What ISV suite tests |
|------------------------------|----------------------|
| `POST {auth}/slurm-{tenant}-{slurmId}/api/...` (HTTP Job API) | `sinfo`, `srun`, `sbatch` (Slurm CLI) |
| Proven in `slurm.js` | Proven in `isvtest/validations/slurm_*.py` and `workloads/slurm_*.py` |

**Implication for `setup.sh`:** after Bridge creates the cluster, you must make Slurm CLI work **locally** before the test phase. Typical options:

1. **Run `isvctl` on the cluster head / login node** (SSH deploy or manual) — matches `my-isv` assumption.
2. **Configure a local Slurm client** during setup: SSH to head, copy `/etc/slurm/slurm.conf` (or equivalent), set `SLURM_CONF` / install `slurm-client` on the runner.
3. **Future framework change** — add `SlurmRunner` (SSH wrapper); not in ISV-NCP today.

Bridge Job API success does **not** satisfy ISV checks. Do not substitute HTTP job submit for `srun`/`sbatch` without changing the suite.

---

## Bridge API quick reference

Base URL: `BRIDGE_URL` (via `BridgeClient`). Orchestrator paths use the **`/orchestrator`** prefix (same as VM / bare-metal).

### Orchestrator — cluster lifecycle

| Operation | Method | Path |
|-----------|--------|------|
| Create Slurm cluster | `POST` | `/orchestrator/tenants/{tenant}/slurm` |
| List clusters | `GET` | `/orchestrator/tenants/{tenant}/slurm` |
| Get cluster | `GET` | `/orchestrator/tenants/{tenant}/slurm/{slurmId}` |
| Delete cluster | `DELETE` | `/orchestrator/tenants/{tenant}/slurm/{slurmId}` |

**Create body** (from `slurm.js` / `bridge-orchestrator` `SlurmRequest`):

```json
{
  "name": "slurm-vm-<timestamp>",
  "description": "...",
  "version": "24.05.6",
  "nodes": [
    { "id": "<vm-or-bm-uuid>", "role": "master" },
    { "id": "<worker-uuid>", "role": "worker" }
  ]
}
```

- **`nodes[].id`** — VM UUID (VM workflow) or bare-metal server UUID (BM workflow).
- **`nodes[].role`** — `"master"` or `"worker"`.
- Response **`201`**: extract `id` / `slurmId` → store as `cluster_id` for teardown.

**Poll `GET .../slurm/{slurmId}`** (or list + filter) until `status` is **`running`** or **`success`** (typical max wait in automation: ~3300s). Fail on `failed` / `error`.

**Delete:** `DELETE .../slurm/{slurmId}` → poll until **`GET` returns 404** or cluster absent from list (automation max ~1500s).

### Auth-gateway — Job API (Bridge QA only, not ISV validations)

| Operation | Method | Path |
|-----------|--------|------|
| Anonymous JWT | `GET` | `{authBaseUrl}/slurm-{tenant}-{slurmId}/api/anonymous` |
| List jobs | `GET` | `{authBaseUrl}/slurm-{tenant}-{slurmId}/api/jobs` |
| Submit job | `POST` | `{authBaseUrl}/slurm-{tenant}-{slurmId}/api/jobs` |

Requires refreshed tenant session cookie before Job API calls (`slurm.js`: `prepareReloginTenantAdminForSlurm`).

### VM-based workflow (prerequisite for many lab setups)

Reference: `vm_based_slurm_cluster_workflow.postman_collection.json`

1. Allocate one or more VMs (`POST .../vms`, poll `running`).
2. Optionally allocate a **worker** VM (`workerVmId`).
3. `POST .../slurm` with master (+ workers).
4. Poll cluster ready → Job API smoke test (Bridge QA).
5. ISV path: SSH to head → run inventory (`sinfo`) → emit setup JSON → run validations locally (see above).
6. Teardown: delete Slurm cluster → delete VMs (order per lab policy; ISV teardown step only deletes Slurm unless you extend it).

---

## Per-step JSON contract

### 1. `setup.sh`

**Bridge:** allocate backing nodes (VM and/or BM) if needed → `POST .../slurm` → poll ready → SSH to head (if runner is remote) **or** configure local Slurm client → run **`sinfo` / `scontrol`** inventory (same logic as `my-isv/scripts/slurm/setup.sh`) → print JSON.

**Validations:** all 13 checks consume **`steps.setup.*`** via Jinja (partition node lists, `storage_path`, `cuda_arch`, `default_partition`). Checks themselves call Slurm CLI locally.

#### Top-level fields

| Field | Tag | Value / notes |
|-------|-----|---------------|
| `success` | R | `true` |
| `platform` | R | `"slurm"` |
| `cluster_name` | R | Slurm cluster name (`scontrol` / Bridge `name`) |
| `cluster_id` | W | Bridge Slurm UUID — **teardown** must read this (persist to file if teardown is a separate process) |
| `slurm_host` | W | Head / login IP or hostname for ops and SSH (not read by validations directly) |
| `slurm_id` | O | Alias for `cluster_id` (use one consistently) |
| `vm_ids` | O | List of VM UUIDs if VM-based (for extended teardown) |
| `key_file` | O | SSH private key path if setup used SSH to gather inventory |
| `ssh_user` | O | Default `ubuntu` |

#### `slurm` object (inventory — drives Jinja in suite)

| Field | Tag | Notes |
|-------|-----|-------|
| `slurm.partitions` | R | Map partition name → `{ "nodes": ["node1", ...] }` |
| `slurm.partitions.cpu` | W | Suite expects partition named **`cpu`** |
| `slurm.partitions.gpu` | W | Suite expects partition named **`gpu`** |
| `slurm.storage_path` | W | Writable scratch path (default `/tmp` in suite) |
| `slurm.default_partition` | W | Used by GPU stress / NCCL configs (default `gpu`) |
| `slurm.cuda_arch` | W | e.g. `"90"` for H100 — GPU stress workload |
| `slurm.driver_version` | O | From `nvidia-smi` on GPU node |
| `slurm.gpu_per_node` | O | Integer — informs expectations |
| `slurm.total_gpus` | O | Integer |

Partition names **must** include `cpu` and `gpu` unless you change `suites/slurm.yaml`. If Bridge provisioning uses different names, remap in setup output and/or reconfigure Slurm on the cluster before inventory.

**Minimal passing JSON** (single-node VM cluster — some workload checks may skip or fail without multi-GPU / multi-node):

```json
{
  "success": true,
  "platform": "slurm",
  "cluster_name": "slurm-vm-1717780000",
  "cluster_id": "slurm-uuid-here",
  "slurm_host": "203.0.113.20",
  "slurm": {
    "partitions": {
      "cpu": { "nodes": ["cpu-node-1"] },
      "gpu": { "nodes": ["gpu-node-1", "gpu-node-2"] }
    },
    "cuda_arch": "90",
    "storage_path": "/tmp",
    "default_partition": "gpu",
    "driver_version": "560.35.03",
    "gpu_per_node": 4,
    "total_gpus": 8
  }
}
```

**Inventory collection:** reuse the loop from `my-isv/scripts/slurm/setup.sh` (`sinfo -h -o "%P"`, per-partition node lists, GRES for GPU counts, `nvidia-smi` for `cuda_arch`). Run that block **where `sinfo` works** (head node via SSH, or locally after client config).

---

### 2. `teardown.sh`

**Bridge:** `DELETE /orchestrator/tenants/{tenant}/slurm/{cluster_id}`, poll until gone.

**Validation:** no explicit teardown check in suite; failure leaves cluster billing / quota impact.

| Field | Tag | Notes |
|-------|-----|-------|
| `success` | R | `true` when delete confirmed |
| `cluster_id` | O | Echo deleted id |
| `resources_deleted` | O | e.g. `["slurm:uuid"]` |
| `message` | O | Human-readable status |
| `skipped` | O | If teardown disabled via env |

```json
{
  "success": true,
  "platform": "slurm",
  "cluster_id": "slurm-uuid-here",
  "resources_deleted": ["slurm:slurm-uuid-here"],
  "message": "Slurm cluster deleted"
}
```

Skip pattern (analogous to VM `ARMADA_BRIDGE_SKIP_TEARDOWN`):

```json
{
  "success": true,
  "platform": "slurm",
  "skipped": true,
  "message": "Teardown skipped"
}
```

**Persist `cluster_id`:** setup and teardown are separate invocations. Write `cluster_id` to a well-known path under `~/.cache/isvctl/` or require env `BRIDGE_SLURM_ID` if not passed as CLI arg (extend `config/slurm.yaml` args when implementing).

---

## Failure JSON (any step)

```json
{
  "success": false,
  "platform": "slurm",
  "error": "human-readable message"
}
```

Exit code **non-zero**. Include `cluster_id` when known (partial teardown).

---

## Full validation matrix (`suites/slurm.yaml`)

All checks use **local Slurm CLI** unless noted.

| Check | Setup JSON used? | CLI / notes |
|-------|------------------|-------------|
| `SlurmInfoAvailable` | no | `sinfo` — `min_partitions: 1` |
| `SlurmPartition-cpu` | yes — `partitions.cpu.nodes` | `sinfo` — partition **`cpu`**, inventory match |
| `SlurmPartition-gpu` | yes — `partitions.gpu.nodes` | `sinfo` — partition **`gpu`**, inventory match |
| `SlurmJobSubmission` | no | `srun --partition=gpu --gres=gpu:1 nvidia-smi -L` |
| `SlurmGpuAllocation-1gpu` | no | `srun --partition=gpu --gres=gpu:1 nvidia-smi --list-gpus` |
| `SlurmGpuAllocation-2gpu` | no | `srun --partition=gpu --gres=gpu:2 ...` — needs ≥2 GPUs allocatable |
| `SlurmNodeJobExecution-cpu` | yes — `storage_path` | Per-node `srun` on **cpu** partition |
| `SlurmNodeJobExecution-gpu` | yes — `storage_path` | Per-node `srun` on **gpu** partition + GPU/storage tests |
| `SlurmGpuStressWorkload` | yes — `default_partition`, `cuda_arch` | Long GPU stress (~30s runtime, 900s timeout) |
| `SlurmNcclMultiNodeWorkload` | yes — `partitions.gpu.nodes` length | Multi-node NCCL — needs **≥2 GPU nodes** |
| `SlurmSbatchWorkload-gpu` | yes — node counts in variables | `sbatch` from manifest `example_gpu_job.sbatch` |
| `SlurmSbatchWorkload-cpu` | yes — node counts | `sbatch` `example_cpu_job.sbatch` |
| `SlurmSbatchWorkload-inline` | no | Inline `#SBATCH` script via `sbatch` |

---

## Cluster topology vs suite expectations

| Requirement | Why |
|-------------|-----|
| Partitions **`cpu`** and **`gpu`** | Hard-coded in suite validation names / configs |
| GPU nodes with **`gres=gpu:N`** | Job submission, GPU allocation, stress, sbatch GPU |
| **≥2 GPUs** on a node (or cluster) | `SlurmGpuAllocation-2gpu` |
| **≥2 GPU nodes** | `SlurmNcclMultiNodeWorkload`, multi-node sbatch variables |
| Writable **`storage_path`** on compute nodes | `SlurmNodeJobExecution-*` |
| NVIDIA driver + CUDA on GPU nodes | `nvidia-smi` in srun/sbatch paths |
| NCCL + multi-node network | NCCL workload bandwidth threshold (`min_bus_bw_gbps: 100`) |

A minimal single-VM Slurm cluster may pass partition/info checks but fail NCCL, 2-GPU allocation, or multi-node sbatch unless topology matches.

---

## Implementation checklist (Bridge-specific)

- [ ] Implement VM and/or BM allocation before `POST .../slurm` (see Postman workflows)
- [ ] Use `/orchestrator/tenants/...` paths; poll `running` / `success`
- [ ] Persist `cluster_id` for teardown
- [ ] Solve **local Slurm CLI access** (head-node deploy, client config, or SSH wrapper)
- [ ] Emit inventory JSON matching **`my-isv/setup.sh`** shape + Bridge ids
- [ ] Ensure partitions are named **`cpu`** / **`gpu`** (or customize suite)
- [ ] Do **not** rely on Job HTTP API for ISV pass/fail
- [ ] Extend `config/slurm.yaml` args (VM flavour, node count, `--cluster-id` file, skip-teardown env)
- [ ] Optional: `ISVCTL_DEMO_MODE=1` dummy inventory for JSON/schema testing (CLI checks will still fail)
- [ ] Optional: teardown also deletes backing VMs if suite should not leak VMs

---

## Run commands

```bash
# Schema / orchestration only (no Bridge) — add demo branch to setup.sh first
ISVCTL_DEMO_MODE=1 uv run isvctl test run -f isvctl/configs/providers/armada-bridge/config/slurm.yaml

# Live Bridge (requires cluster + local Slurm CLI on runner)
export BRIDGE_URL=...
export BRIDGE_EMAIL=...
export BRIDGE_PASSWORD=...
export BRIDGE_TENANT=...

uv run isvctl test run -f isvctl/configs/providers/armada-bridge/config/slurm.yaml
```

For a full pass, run from a host where **`sinfo`** targets the Bridge-provisioned cluster (typically the Slurm head after setup configures access).
