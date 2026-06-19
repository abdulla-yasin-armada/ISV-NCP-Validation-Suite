# Armada Bridge Kubernetes — JSON output reference

Implementation guide for scripts in the parent directory (`../`). Each script must print **one JSON object to stdout** (logs on stderr). Validations in `isvctl/configs/suites/k8s.yaml` read setup fields via Jinja; test-phase checks run **`kubectl`** locally via `LocalRunner` (or `KUBECTL` / `microk8s kubectl` when configured).

**Related files**

| File | Role |
|------|------|
| [validation-coverage.md](./validation-coverage.md) | Category taxonomy, 33-check matrix, lab sign-off summary |
| [failure-analysis.md](./failure-analysis.md) | Bug vs skip vs config triage guide |
| `../../config/k8s.yaml` | Step order, CLI args, Bridge env wiring |
| `../../../../suites/k8s.yaml` | Canonical validation contract (33 checks) |
| `../../common/bridge_client.py` | HTTP client (`BridgeClient.from_env()`) |
| `../../../my-isv/scripts/k8s/setup.sh` + `_common.sh` | Inventory JSON shape (`kubectl` queries) |
| `../../../aws/config/eks.yaml` | Reference provider with node-pool steps |
| `automation/bridge-api-test-automation/lib/utils/cluster.js` | Reference create/poll/scale/delete flow |
| `automation/bridge-api-test-automation/docs/orchestrator-api.yaml` | OpenAPI (`/clusters`, `/kubeconfig`) |
| `automation/bridge-api-test-automation/wfs/vm/vm_based_none_template_cluster.postman_collection.json` | VM → cluster workflow |

---

## Steps vs validations (do not confuse the counts)

| Layer | Count | Meaning |
|-------|-------|---------|
| **Provider steps (armada today)** | **2** | `setup.sh`, `teardown.sh` |
| **Provider steps (full suite)** | **up to 4+** | EKS adds `create_test_node_pool`, `update_test_node_pool`, `destroy_test_node_pool` |
| **Validation groups** | **8** | Blocks under `validations:` in `suites/k8s.yaml` |
| **Individual checks** | **33** | Pytest validation / workload classes |

The canonical suite defines **node-pool checks** that bind to `steps.create_test_node_pool` and `steps.update_test_node_pool`. Armada config does not define those steps yet — either implement them (Bridge scale / node-pool API) or override `validations.k8s_node_pools` in `config/k8s.yaml` to skip.

### Step execution order (`config/k8s.yaml` — intended)

| # | Step | Phase | Script | Notes |
|---|------|-------|--------|-------|
| 1 | `setup` | setup | `setup.sh` | Create cluster, fetch kubeconfig, inventory |
| 2 | `teardown` | teardown | `teardown.sh` | DELETE cluster, poll gone |

**Config fix required:** `config/k8s.yaml` currently uses `commands: k8s:` but the suite platform key is **`kubernetes`**. Change to `commands: kubernetes:` or setup/teardown will not run (merged config keeps suite’s `my-isv` paths).

Compare AWS EKS (`config/eks.yaml`): adds node-pool steps in setup/test/teardown phases before cluster destroy.

---

## Field legend

| Tag | Meaning |
|-----|---------|
| **R** | **Required** — schema / validations fail if missing or wrong |
| **W** | **Wiring** — Jinja in `suites/k8s.yaml` or teardown persistence |
| **O** | **Optional** — safe to omit; may cause checks to **skip** rather than fail |

**Always (every step):** `success` (bool), `platform` (`"kubernetes"`). On failure add `error` (string) and exit non-zero.

---

## Runner access (kubectl)

Unlike Slurm (CLI on host), K8s validations use **`kubectl`** (or `KUBECTL` env) on the **isvctl runner**. After Bridge creates the cluster:

1. `GET .../clusters/{clusterID}/kubeconfig` → write file (e.g. `$KUBECONFIG_PATH` or `~/.cache/isvctl/bridge-{cluster_id}.kubeconfig`).
2. Export **`KUBECONFIG`** for the remainder of setup and for the test phase (isvctl subprocess inherits env from the orchestrator — persist path in setup JSON and ensure test phase sees it).
3. Run inventory logic from `my-isv/scripts/k8s/_common.sh` with `KUBECTL="kubectl --kubeconfig=..."`.

Bridge Postman workflows often use **mTLS client certs** on `:6443` (`scripts/kubeconfig_extract_newman_tls.sh`). Standard `kubectl` with the fetched kubeconfig is the ISV path.

---

## Bridge API quick reference

Base URL: `BRIDGE_URL` (via `BridgeClient`). Orchestrator paths use the **`/orchestrator`** prefix.

### Orchestrator — cluster lifecycle

| Operation | Method | Path |
|-----------|--------|------|
| Create cluster | `POST` | `/orchestrator/tenants/{tenant}/clusters` |
| List clusters | `GET` | `/orchestrator/tenants/{tenant}/clusters` |
| Get cluster | `GET` | `/orchestrator/tenants/{tenant}/clusters/{clusterID}` |
| Get kubeconfig | `GET` | `/orchestrator/tenants/{tenant}/clusters/{clusterID}/kubeconfig` |
| Scale cluster | `PUT` or `PATCH`* | `/orchestrator/tenants/{tenant}/clusters/{clusterID}/scale` |
| Delete cluster | `DELETE` | `/orchestrator/tenants/{tenant}/clusters/{clusterID}` |
| Deploy CSI | `POST`* | `/orchestrator/tenants/{tenant}/clusters/{clusterID}/csi` |

\* Confirm exact method/path in `orchestrator-api.yaml` / `cluster_repository.go` when implementing.

**Create body** (from `cluster.js` / `K8sManagerClusterRequest`):

```json
{
  "name": "cluster-<timestamp>",
  "description": "",
  "version": "1.31",
  "installGpuTools": true,
  "enableNetworkAcceleration": false,
  "cni": "flannel",
  "distribution": "kubernetes",
  "deployLocalProvisioner": false,
  "autoScaling": false,
  "nodes": [
    { "id": "<vm-or-bm-uuid>", "isAllocated": true, "nodeType": "vm" }
  ]
}
```

- Set **`installGpuTools: true`** for GPU operator / `nvidia.com/gpu` checks.
- Set **`deployLocalProvisioner: true`** if lab relies on local-path / in-cluster CSI for storage checks.
- Optional **`workerVmId`** → second node `{ id, nodeType: "vm" }` (multi-node NCCL).
- BM workflow: use `computeNodeId` and `nodeType: "bareMetal"`.

**Poll `GET .../clusters/{clusterID}`** until `status === "running"` (automation default max ~900s). Fail on `failed`.

**Kubeconfig response:** `{ "kubeconfig": "<yaml string>" }` (`K8sManagerKubeConfigResponse`).

**Delete:** `DELETE .../clusters/{clusterID}` → poll until **404** or absent from list (automation deletion poll max ~180s).

### VM-based workflow (typical lab path)

Reference: `vm_based_none_template_cluster.postman_collection.json` + `cluster.js`

1. Allocate VM(s) (`POST .../vms`, poll `running`).
2. `POST .../clusters` with node VM UUID(s).
3. Poll cluster `running`.
4. Fetch kubeconfig → configure local `kubectl`.
5. (Optional) deploy workloads / CSI via Bridge API — ISV suite does not require Bridge workload API for core checks.
6. Teardown: delete cluster → delete VMs (extend teardown if needed).

---

## Per-step JSON contract

### 1. `setup.sh`

**Bridge:** provision nodes if needed → `POST .../clusters` → poll → `GET .../kubeconfig` → write kubeconfig → **`source _common.sh` inventory** → print JSON.

**Validations:** all 33 checks consume **`steps.setup.*`** via Jinja (node counts, GPU fields, CSI class names, API endpoint, etc.).

#### Top-level fields

| Field | Tag | Value / notes |
|-------|-----|---------------|
| `success` | R | `true` |
| `platform` | R | `"kubernetes"` |
| `cluster_name` | R | Bridge cluster `name` or kubectl context |
| `cluster_id` | W | Bridge UUID — **teardown** must read this |
| `kubeconfig_path` | W | Path written on disk; test phase needs `KUBECONFIG` |
| `endpoint` | O | API server URL (legacy; some providers emit top-level) |

#### `kubernetes` object

| Field | Tag | Used by |
|-------|-----|---------|
| `kubernetes.node_count` | W | `K8sNodeCountCheck` |
| `kubernetes.nodes` | O | `K8sExpectedNodesCheck` (names list often empty in suite) |
| `kubernetes.gpu_node_count` | W | GPU pod access, NCCL multi-node |
| `kubernetes.gpu_per_node` | W | `K8sGpuCapacityCheck`, NIM Helm 3b |
| `kubernetes.total_gpus` | W | `K8sGpuCapacityCheck` |
| `kubernetes.driver_version` | W | `K8sDriverVersionCheck` (default suite: `580.82.07`) |
| `kubernetes.gpu_operator_namespace` | W | GPU operator checks |
| `kubernetes.control_plane_namespace` | W | `K8sControlPlaneLogsCheck` |
| `kubernetes.runtime_class` | W | GPU pod / smi checks (default `nvidia`) |
| `kubernetes.gpu_resource_name` | W | Capacity check (default `nvidia.com/gpu`) |
| `kubernetes.api_endpoint` | W | `K8sApiNetworkAclCheck` |
| `kubernetes.unauthorized_probe_cmd` | W | Network ACL check (empty → subtest skipped) |

Populate via `_common.sh`: node/GPU counts from labels and capacity, driver from `nvidia.com/cuda.driver.*` labels, GPU operator namespace auto-detect, CSI StorageClasses auto-detect.

#### `csi` object

| Field | Tag | Notes |
|-------|-----|-------|
| `csi.block_storage_class` | W | Empty → **CSI type/quota/provisioning checks skip** |
| `csi.shared_fs_storage_class` | O | Shared-fs CSI subtest |
| `csi.nfs_storage_class` | O | NFS subtest |
| `csi.static_volume_handle` | O | Static provisioning subtest |
| `csi.static_driver_name` | O | Paired with static volume |

Env overrides: `K8S_CSI_BLOCK_SC`, `K8S_CSI_SHARED_FS_SC`, `K8S_CSI_NFS_SC`.

**Minimal passing JSON** (GPU cluster with CSI block class detected):

```json
{
  "success": true,
  "platform": "kubernetes",
  "cluster_name": "cluster-1717780000",
  "cluster_id": "cluster-uuid-here",
  "kubeconfig_path": "/tmp/isv-bridge.kubeconfig",
  "kubernetes": {
    "driver_version": "580.82.07",
    "node_count": 2,
    "nodes": ["worker-1", "worker-2"],
    "gpu_node_count": 2,
    "gpu_per_node": 4,
    "total_gpus": 8,
    "gpu_operator_namespace": "nvidia-gpu-operator",
    "control_plane_namespace": "kube-system",
    "runtime_class": "nvidia",
    "gpu_resource_name": "nvidia.com/gpu",
    "api_endpoint": "https://203.0.113.10:6443"
  },
  "csi": {
    "block_storage_class": "local-path",
    "shared_fs_storage_class": "",
    "nfs_storage_class": "",
    "static_volume_handle": "",
    "static_driver_name": ""
  }
}
```

**Side effect:** export `KUBECONFIG=<kubeconfig_path>` before `_common.sh` runs (or pass `--kubeconfig` into kubectl).

---

### 2. `teardown.sh`

**Bridge:** `DELETE /orchestrator/tenants/{tenant}/clusters/{cluster_id}`, poll until gone.

| Field | Tag | Notes |
|-------|-----|-------|
| `success` | R | `true` when delete confirmed |
| `cluster_id` | O | Echo deleted id |
| `resources_deleted` | O | e.g. `["cluster:uuid"]` |
| `skipped` | O | Skip-teardown env |

```json
{
  "success": true,
  "platform": "kubernetes",
  "cluster_id": "cluster-uuid-here",
  "resources_deleted": ["cluster:cluster-uuid-here"],
  "message": "Cluster deleted"
}
```

Persist `cluster_id` between setup and teardown (file under `~/.cache/isvctl/` or CLI arg — extend `config/k8s.yaml` when implementing).

---

## Optional node-pool steps (suite default, not in armada yet)

The canonical suite includes **`k8s_node_pools`** (2× `K8sNodePoolCheck`) bound to:

| Step | Phase | Purpose |
|------|-------|---------|
| `create_test_node_pool` | setup | Pool reaches `expected_replicas: 1` |
| `update_test_node_pool` | test | Same pool scales to `expected_replicas: 2` |

AWS EKS implements these with Terraform (`create_node_pool.sh`). For Bridge, map to **`ScaleCluster`** / node-pool APIs in `cluster.js` (`workerNodeCount`, `nodeType`) and emit **`node_pool`** schema JSON (`label_selector`, `expected_replicas`, `expected_labels_json`, …).

Until implemented, override in armada `config/k8s.yaml`:

```yaml
tests:
  validations:
    k8s_node_pools: []   # or exclude checks via tests.exclude
```

---

## Failure JSON (any step)

```json
{
  "success": false,
  "platform": "kubernetes",
  "error": "human-readable message"
}
```

Exit code **non-zero**. Include `cluster_id` when known.

---

## Full validation matrix (`suites/k8s.yaml`)

All checks use **local kubectl** unless noted.

| Group | Check | Setup JSON / notes |
|-------|-------|-------------------|
| **k8s_node_pools** | `K8sNodePoolCheck` (×2) | Needs `create_test_node_pool` + `update_test_node_pool` step output |
| **kubernetes** | `K8sNodeCountCheck` | `kubernetes.node_count` |
| | `K8sExpectedNodesCheck` | `names: []` (passes if empty) |
| | `K8sNodeReadyCheck` | kubectl |
| | `K8sNvidiaSmiCheck` | GPU RuntimeClass |
| | `K8sDriverVersionCheck` | `driver_version` |
| | `K8sGpuPodAccessCheck` | GPU nodes, schedules GPU pod |
| | `K8sGpuCapacityCheck` | `gpu_per_node`, `total_gpus` |
| | `K8sGpuOperatorNamespaceCheck` | `gpu_operator_namespace` |
| | `K8sGpuOperatorPodsCheck` | operator pods healthy |
| | `K8sGpuLabelsCheck` | `nvidia.com/gpu.present=true` |
| | `K8sPodHealthCheck` | cluster-wide pod scan |
| | `K8sNoPendingPodsCheck` | kubectl |
| | `K8sNoErrorPodsCheck` | kubectl |
| | `K8sMigConfigCheck` | `require_mig: false` (labels only) |
| | `K8sDualStackNodeCheck` | `require_dual_stack: auto` |
| | `K8sNetworkPolicyCheck` | creates test namespaces/pods |
| **k8s_storage** | `K8sCsiStorageTypesCheck` | `csi.*_storage_class` — skips empty types |
| | `K8sCsiStorageQuotaApiCheck` | needs `block_storage_class` |
| | `K8sCsiTenantScopedCredentialsCheck` | read-only RBAC/Secret scan |
| | `K8sCsiProvisioningModesCheck` | dynamic + optional static PV |
| **k8s_identity** | `K8sOidcIssuerCheck` | kubectl / API discovery |
| **k8s_conformance** | `K8sCncfConformanceCheck` | `mode: quick` (smoke, not full cert) |
| **k8s_workloads** | `K8sNcclWorkload` | single-node NCCL |
| | `K8sNcclMultiNodeWorkload` | `gpu_node_count` ≥ 2 |
| | `K8sGpuStressWorkload` | GPU stress pod |
| | `K8sNimInferenceWorkload` | needs **`NGC_API_KEY`** |
| | `K8sNimHelmWorkload-1b` | NGC + Helm |
| | `K8sNimHelmWorkload-3b` | multi-GPU NIM |
| **k8s_observability** | `K8sApiServerMetricsCheck` | metrics API |
| | `K8sControlPlaneLogsCheck` | control-plane pod logs |
| **k8s_network** | `K8sApiNetworkAclCheck` | needs `unauthorized_probe_cmd` or skips unauth leg |

---

## Cluster requirements vs suite defaults

| Requirement | Suite default / check |
|-------------|----------------------|
| GPU operator + `nvidia` RuntimeClass | GPU smi, driver, capacity, labels |
| ≥2 GPU nodes | NCCL multi-node (`gpu_node_count` default 2) |
| 4 GPUs per node (typical) | Capacity + NIM 3b Helm |
| CSI block StorageClass | Storage group (else skips) |
| Healthy workload namespace | NIM / NCCL / stress |
| **`NGC_API_KEY`** | NIM workloads (skip or fail without) |
| Network ACL probe command | `K8sApiNetworkAclCheck` unauth leg |

---

## Implementation checklist (Bridge-specific)

- [ ] Fix **`commands: kubernetes:`** in `config/k8s.yaml` (not `k8s`)
- [ ] VM/BM allocate before `POST .../clusters` (reuse VM suite / Postman flow)
- [ ] `installGpuTools: true` on create when running full GPU suite
- [ ] Poll until `status: running`
- [ ] Fetch kubeconfig, write file, set **`KUBECONFIG`** for inventory + test phase
- [ ] Reuse **`my-isv/scripts/k8s/_common.sh`** for inventory JSON
- [ ] Persist **`cluster_id`** for teardown
- [ ] Decide node-pool strategy: implement Bridge scale steps or exclude `k8s_node_pools`
- [ ] CSI: enable provisioner / `DeployCSI` or accept storage-check skips
- [ ] Optional: `ISVCTL_DEMO_MODE=1` dummy JSON (kubectl checks still fail)
- [ ] Optional: exclude heavy checks (NIM, conformance, network ACL) in armada config for first pass

---

## Run commands

```bash
# Live Bridge (requires GPU K8s cluster + kubeconfig on runner)
export BRIDGE_URL=...
export BRIDGE_EMAIL=...
export BRIDGE_PASSWORD=...
export BRIDGE_TENANT=...
export NGC_API_KEY=...   # optional — NIM workloads need it

uv run isvctl test run -f isvctl/configs/providers/armada-bridge/config/k8s.yaml

# Test phase only (cluster already up, KUBECONFIG set)
uv run isvctl test run -f isvctl/configs/providers/armada-bridge/config/k8s.yaml --phase test
```
