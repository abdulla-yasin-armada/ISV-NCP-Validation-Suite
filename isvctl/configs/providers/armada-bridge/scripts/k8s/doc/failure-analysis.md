# Armada Bridge Kubernetes — failure analysis (bug vs skip vs config)

Discussion guide for interpreting K8s suite results on Bridge labs. Use this when a check **skips**, **fails**, or shows **partial** coverage — to decide whether to fix Bridge/product, isvctl config, or accept as expected for the lab.

**Related:** [validation-coverage.md](./validation-coverage.md) (category matrix + reference run) · [armada-k8s.md](./armada-k8s.md) (JSON contracts, Bridge API)

---

## One-line conclusion (import BM lab reference)

**Core lab validation** (setup + K8s health + GPU + operator + CSI block + OIDC + metrics): **SOLID**

**Extended / cert / workloads / security** (netpol, NGC, NCCL, ACL, full conformance, CP logs): **OPEN** — mix of config, infra, and hardware limits

---

## How to read results

| Symbol | Meaning |
|--------|---------|
| **PASS** | Check ran and assertion succeeded |
| **PASS\*** | Pytest green, but the real assertion was **auto-skipped** inside the check (e.g. NCCL with 1 GPU) |
| **SKIP** | Check did not run — missing config, missing step, or explicit `pytest.skip` |
| **FAIL** | Check ran and assertion failed — investigate |
| **Partial** | Check **passed**, but some **subtests** or modes were skipped (CSI shared-fs, conformance `quick`, etc.) — not a failure |

A **failed orchestration exit code** means at least one check **FAIL**ed. Skips and partial passes do not fail the suite by themselves.

---

## Classification framework

When triaging, assign each item to one bucket:

| Bucket | Meaning | Who fixes |
|--------|---------|-----------|
| **Expected skip** | Lab hardware/env cannot satisfy the check by design | None — or different lab (A100, 2+ GPUs, NGC key) |
| **Config gap** | Suite supports it; armada Bridge wiring or env not set | isvctl `k8s.yaml`, setup JSON, `bridge-isv.env` |
| **Product / infra** | Bridge cluster or platform missing capability | Bridge team — CNI, GPU runtime, etc. |
| **Test assumption** | Cluster is healthy; check expects a different layout (EKS vs RKE2) | Provider config override or exclude check |

---

## Summary table (reference run: 1× L4 import BM, Flannel CNI)

| Check / area | Run result | Bucket | Verdict |
|--------------|------------|--------|---------|
| **Setup** (lifecycle) | PASS | — | Working |
| **Teardown** (lifecycle) | PASS | — | Working (`ARMADA_BRIDGE_SKIP_TEARDOWN`) |
| **K8s core health** (6 checks) | PASS | — | Working |
| **GPU node + driver** (4 checks) | PASS | — | Working |
| **GPU Operator** (3 checks) | PASS | — | Working |
| **MIG** (`K8sMigConfigCheck`) | SKIP | Expected skip | L4 — not MIG-capable; A100+MIG would run |
| **Dual stack** (`K8sDualStackNodeCheck`) | PASS\* | Expected skip | Single-stack cluster — auto-skipped inside |
| **CSI storage types** | PASS (partial) | Expected skip | Block/Longhorn OK; shared-fs & nfs not configured |
| **CSI provisioning** | PASS (partial) | Expected skip | Dynamic OK; static PV not configured |
| **OIDC** | PASS | — | Working |
| **CNCF conformance** | PASS (partial) | Config / scope | `quick` smoke only — not full cert suite |
| **API network ACL** | SKIP | Config gap | `unauthorized_probe` not wired — test never ran |
| **Node pools** (×2) | SKIP | Config gap | No `create/update_test_node_pool` steps in armada config |
| **NetworkPolicy** | **FAIL** | Product / infra | Flannel does not enforce NetworkPolicy — use Cilium/Calico at cluster create |
| **Control plane logs** | **FAIL** | Test assumption | No CP pods in `kube-system` on RKE2/Bridge — need custom `commands` or exclude |
| **GPU stress** | **FAIL** | Investigate | Pod failed on `gpu-vm-1` — debug cluster (may be product or image) |
| **NCCL / NIM workloads** | SKIP / PASS\* | Expected skip / config | 1 GPU, no MPI operator, no `NGC_API_KEY` |

---

## Per-check discussion

### 1. MIG (`K8sMigConfigCheck`)

**What it does:** Reads node labels (`nvidia.com/mig.capable`, MIG strategy) and verifies expected values when MIG nodes exist.

**Reference run:** Skipped — *No MIG-capable nodes (`require_mig: false`)*.

**Bug?** No. **L4 is not MIG-capable.** On A100/H100 with MIG enabled and correct labels, the check runs and can pass.

**Fix:** Use MIG hardware, or accept skip on non-MIG labs.

---

### 2. API network ACL (`K8sApiNetworkAclCheck`)

**What it does:**

1. **Authorized probe** (default): `kubectl get --raw /readyz` — must succeed from the runner (allowed network).
2. **Unauthorized probe** (required in config): Shell command from a **blocked** network that must **fail or timeout** against the K8s API.

Validates: API reachable from inside the lab, not from an unauthorized vantage point.

**Reference run:** **Skipped** — not failed. `unauthorized_probe_cmd` was empty in setup output / config.

**Bug?** Unknown — the ACL test **never executed**.

**Fix (config):** Add e.g. a Mac-side or external-host curl to the private API URL that should be blocked, in `K8sApiNetworkAclCheck.commands.unauthorized_probe` or setup JSON field `kubernetes.unauthorized_probe_cmd`.

**Fix (product):** If Bridge only exposes API on private IP, document that as the ACL model; prove with the unauthorized probe command.

---

### 3. Control plane logs (`K8sControlPlaneLogsCheck`)

**What it does:** For `kube-apiserver`, `kube-scheduler`, `kube-controller-manager`:

- **Mode `auto`:** Find pods in `kube-system` with matching component label → `kubectl logs`.
- Else use per-component **`commands`** (e.g. `aws logs tail`, `journalctl` on RKE2 nodes).

**Reference run:** **Failed** — *No control-plane pods in `kube-system` and no `commands` configured*.

**Bug?** Usually **no** — cluster may be fine. RKE2/Bridge often runs control plane as **static pods on the host**, not as discoverable pods the test expects (EKS-style).

**Fix (config):** Add RKE2-specific `commands` in armada `k8s.yaml`, or exclude this check (AWS EKS provider excludes it).

**Fix (product):** Only if CP logs are genuinely unavailable anywhere.

---

### 4. CNCF conformance (`K8sCncfConformanceCheck`) — “quick mode only”

**What it does:** Runs upstream conformance e2e image in a pod; parses JUnit results.

| Mode | Coverage |
|------|----------|
| **`quick`** (suite default) | One ConfigMap smoke test — verifies harness works (~seconds) |
| **`non-disruptive-conformance`** | Full `[Conformance]` minus disruptive/serial — hours |
| **`certified-conformance`** | Full CNCF certification — many hours |

**Reference run:** **Passed** — `2/6610` (rest skipped by `quick` focus filter).

**Bug?** No. **Partial** means shallow coverage by **design**, not failure.

**Fix:** Change `mode` in config for real cert runs; expect long runtime and cluster load.

---

### 5. CSI provisioning (`K8sCsiProvisioningModesCheck`) — “dynamic only”

**What it does:**

| Subtest | Requires |
|---------|----------|
| **dynamic** | `dynamic_storage_class` (e.g. Longhorn from setup) — PVC bind, mount, canary R/W |
| **static** | Pre-provisioned `static_pv.volume_handle` + `csi_driver` in setup JSON |

**Reference run:** **Passed** — dynamic OK; static subtest **skipped** (fields unset).

**Bug?** No — static provisioning is optional unless the product claims pre-provisioned volumes.

**Fix:** Populate setup `csi.static_volume_handle` and `csi.static_driver_name` if Bridge provides static CSI volumes.

---

### 6. CSI storage types (`K8sCsiStorageTypesCheck`) — “block only”

**What it does:** For each configured type — **block**, **shared-fs**, **nfs** — verifies StorageClass exists and PVC binds.

**Reference run:** **Passed** — block (Longhorn) OK; shared-fs and nfs subtests **skipped** (empty in setup).

**Bug?** No — empty types skip by design.

**Fix:** If Bridge offers RWX shared-fs or NFS StorageClasses, emit them in setup `csi.*_storage_class` fields.

---

### 7. NetworkPolicy (`K8sNetworkPolicyCheck`) + CNI

**What it does:** Creates test pods, applies NetworkPolicy, verifies **denied-client** cannot reach **server** (and allowed-client can).

**Reference run:** **Failed** — denied pod still connected after 30s.

**Root cause:** **CNI does not enforce NetworkPolicy.** Default cluster create uses **`cni: flannel`** (`cluster.py` default; `setup.py` does not override). Flannel provides networking but **does not filter** policy rules.

**Bug?** **Product/infra** — need enforcing CNI (**Cilium**, **Calico**, etc.) at **cluster create**:

```json
POST .../clusters  { "cni": "cilium", ... }
```

The ISV test applies its own NetworkPolicy objects — you do **not** configure NetPol YAML for the suite.

**Note:** Cilium fixes **NetPol** only, not dual-stack (separate), GPU stress, or CP logs.

**isvctl gap:** `create_cluster()` accepts `cni` but `setup.py` always defaults to `flannel`. Future: `BRIDGE_K8S_CNI=cilium` env wired into setup.

**Existing cluster:** Cannot fix by editing NetPol — recreate cluster with enforcing CNI or migrate CNI (non-trivial).

---

### 8. Node pools (`K8sNodePoolCheck` ×2)

**What it does:** **Outcome-only** — after provider steps create/scale a node pool, verifies node count, labels, taints, instance types via kubectl. Does **not** create pools itself.

**Suite expects:**

| Step | Phase |
|------|-------|
| `create_test_node_pool` | setup |
| `update_test_node_pool` | test |

**Reference run:** **Skipped** — steps not defined in armada `config/k8s.yaml`.

**Bug?** No — import BM lab uses fixed `BRIDGE_K8S_NODE_IDS`, not elastic pools.

**Fix:** Implement Bridge scale/node-pool steps (like AWS EKS Terraform), **or** set `validations.k8s_node_pools: []` in armada config for BM import flows.

---

### 9. GPU workloads

| Check | Reference run | Analysis |
|-------|---------------|----------|
| **K8sNcclWorkload** | PASS\* | Skipped inside — needs ≥2 GPUs on one node for allreduce |
| **K8sNcclMultiNodeWorkload** | SKIP | MPI Operator not installed |
| **K8sGpuStressWorkload** | **FAIL** | Stress pod failed (~5 min), empty logs — **investigate** on cluster |
| **K8sNimInferenceWorkload** | PASS\* | Skipped — no `NGC_API_KEY` |
| **K8sNimHelmWorkload-1b/3b** | SKIP | No `NGC_API_KEY` |

**GPU stress:** Only real workload failure. Debug with kubeconfig on bridge VM:

```bash
kubectl get pods -A | grep -i stress
kubectl describe pod <name> -n <ns>
kubectl logs <name> -n <ns>
```

May be CUDA/image/scheduling on L4 — product or workload env, not necessarily isvctl.

**NIM / NCCL:** Expected skips until `NGC_API_KEY`, second GPU node, and/or MPI operator.

---

## What isvctl can fix vs Bridge/lab must fix

| Item | isvctl / config | Bridge / lab |
|------|-----------------|--------------|
| API ACL skip | Wire `unauthorized_probe` | Prove network isolation model |
| CP logs fail | `commands` for RKE2 or exclude check | Expose logs if required |
| Node pool skip | Exclude group or add scale steps | Node pool API if supported |
| NetPol fail | Wire `BRIDGE_K8S_CNI=cilium` (future) | Install enforcing CNI at create |
| CSI partial | Emit extra SC names in setup JSON | Deploy shared-fs/NFS CSI if offered |
| Conformance partial | Override `mode` in yaml | Cluster resources for long run |
| MIG / NCCL / NIM skip | Document env vars | Hardware, NGC, MPI operator |
| GPU stress fail | Tune workload timeout/params if needed | Fix GPU runtime / debug pod |

---

## Quick decision tree

```text
Did the check SKIP?
  └─ Missing step/env in config?     → Config gap (armada yaml / env)
  └─ Hardware/env (MIG, 1 GPU, NGC)? → Expected skip

Did the check FAIL?
  └─ NetworkPolicy + Flannel?          → Product/infra (CNI at cluster create)
  └─ CP logs + no kube-system pods?    → Test assumption (add commands / exclude)
  └─ GPU stress?                       → Investigate pod on cluster first

Did the check PASS but say "partial" or "quick"?
  └─ Subtests skipped or shallow mode → By design; deepen config if needed
```

---

## Updating this doc

After each lab run, update the **Summary table** with new pass/fail/skip results. Keep [validation-coverage.md](./validation-coverage.md) in sync for category-level sign-off.
