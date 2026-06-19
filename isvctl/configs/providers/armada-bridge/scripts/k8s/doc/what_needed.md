# Armada Bridge Kubernetes suite — what is needed to pass

**Goal:** Pass the K8s suite (`config/k8s.yaml` + `suites/k8s.yaml`) against live Bridge.

**Scale:** **2 scripts** today (`setup.sh`, `teardown.sh`) → **33 validation checks** across 8 groups (2 node-pool checks need extra steps).

See also: [armada-k8s.md](./armada-k8s.md) for per-step JSON contracts and Bridge API mapping.

---

## What we have

- Provider config scaffold (`config/k8s.yaml`) — setup + teardown steps, `--tenant` arg
- Validation contract imported from `suites/k8s.yaml` (full 33-check matrix)
- JSON reference: `armada-k8s.md` in this folder
- Inventory reference: `my-isv/scripts/k8s/setup.sh` + `_common.sh` (kubectl → JSON)
- Shared infra: `BridgeClient`, session auth, polling helpers
- Bridge QA reference: `bridge-api-test-automation/lib/utils/cluster.js` (create / poll / scale / delete)
- Postman workflows: VM-based and BM-based cluster creation, template clusters (NIM/Jupyter/LLM)
- OpenAPI: `GET .../clusters/{id}/kubeconfig` documented
- Armada `setup.sh` / `teardown.sh` exist — **exit 1, not implemented**

---

## What we do not have

| Area | Gap |
|------|-----|
| K8s scripts | No live Bridge implementation — stubs return `Not implemented` |
| **Config bug** | `commands: k8s:` should be **`commands: kubernetes:`** — steps may not override suite today |
| Node provisioning | Setup does not allocate VM/BM nodes before `POST .../clusters` |
| Kubeconfig handling | No fetch/write/`KUBECONFIG` export for test phase |
| Node-pool steps | Suite expects `create_test_node_pool` / `update_test_node_pool` — only on AWS EKS today |
| GPU stack | Bridge `installGpuTools` must be set; cluster needs GPU operator + RuntimeClass |
| CSI | Storage checks skip without `csi.block_storage_class` — may need `deployLocalProvisioner` or `DeployCSI` |
| NIM workloads | Require **`NGC_API_KEY`** + pullable NIM charts/images |
| Network ACL | `K8sApiNetworkAclCheck` needs `kubernetes.unauthorized_probe_cmd` from setup |
| Demo mode | No `ISVCTL_DEMO_MODE=1` in armada k8s scripts |
| Teardown scope | Deletes cluster only — backing VMs/BMs not torn down unless extended |

---

## What must be added to pass

### Must implement (blocking)

1. **`setup.sh`**
   - Provision **VM(s)** or **BM node(s)** (Postman / VM suite parity).
   - `POST /orchestrator/tenants/{tenant}/clusters` with `installGpuTools: true` (for GPU checks).
   - Poll until `status: running`.
   - `GET .../clusters/{id}/kubeconfig` → write file, export **`KUBECONFIG`**.
   - Source **`_common.sh`** inventory → emit JSON (`kubernetes`, `csi`, `cluster_id`, `kubeconfig_path`).

2. **`teardown.sh`**
   - Load persisted `cluster_id`.
   - `DELETE .../clusters/{id}`, poll until 404 / absent.
   - Optional: delete backing VMs/BMs.

3. **Config fix**
   - Rename `commands: k8s:` → **`commands: kubernetes:`**.
   - Pass kubeconfig path / cluster id args; env for `KUBECONFIG` in test phase.

4. **Environment**
   - Runner must reach API server with fetched kubeconfig (may need lab routing / VPN).

### Node pools (suite default — choose one)

- **Implement** Bridge scale / node-pool steps + `node_pool` JSON (like AWS EKS), **or**
- **Exclude** `k8s_node_pools` validations in armada `config/k8s.yaml` for first pass.

### Must be true in the environment (not code)

- GPU nodes with NVIDIA driver, GPU operator, `runtime_class: nvidia`
- Enough GPU nodes/GPUs for capacity + NCCL multi-node defaults
- CSI block StorageClass if storage group should pass (else those checks skip)
- `NGC_API_KEY` for NIM workload checks (or exclude them)

### Optional (first pass)

- Exclude: `K8sNimHelmWorkload-*`, `K8sNimInferenceWorkload`, `K8sCncfConformanceCheck`, `K8sApiNetworkAclCheck`, `K8sControlPlaneLogsCheck` (AWS EKS excludes control-plane logs)
- `ISVCTL_DEMO_MODE=1` for JSON/schema smoke tests
- Skip teardown env flag

---

## Effort (rough)

| Target | Time |
|--------|------|
| Cluster create + kubeconfig + delete (orchestrator) | ~3–5 days |
| VM allocate + cluster (Postman parity) | ~1 week (overlaps VM suite) |
| Inventory + core kubernetes checks (16) | ~3–5 days after cluster reachable |
| CSI + storage group | +depends on Bridge CSI / local-path (~3–5 days) |
| Node-pool checks (Bridge scale) | ~1 week |
| Full 33-check green (NCCL, NIM, conformance quick) | +1–2 weeks (GPU topology + NGC) |

---

## Summary

Stubs and docs exist. Blockers are **(1)** fix **`kubernetes` commands key**, **(2)** implement setup/teardown against Bridge cluster + kubeconfig APIs, **(3)** provision nodes and GPU tooling before create, **(4)** wire **`KUBECONFIG`** for kubectl validations, and **(5)** either implement node-pool steps or exclude that group. VM suite work overlaps for VM-based cluster workflows.
