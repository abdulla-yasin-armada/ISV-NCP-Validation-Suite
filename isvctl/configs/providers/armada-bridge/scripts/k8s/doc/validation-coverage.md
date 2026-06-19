# Armada Bridge Kubernetes — validation coverage

Maps the canonical K8s suite (`isvctl/configs/suites/k8s.yaml`) to **validation categories** used for Armada Bridge lab sign-off. Provider config: `config/k8s.yaml`.

**Related:** [armada-k8s.md](./armada-k8s.md) (JSON contracts, Bridge API) · [failure-analysis.md](./failure-analysis.md) (bug vs skip vs config triage) · [what_needed.md](./what_needed.md) (implementation checklist)

---

## One-line conclusion

**Core lab validation** (setup + K8s health + GPU + operator + CSI block + OIDC + metrics): **SOLID**

**Extended / cert / workloads / security** (netpol, NGC, NCCL, ACL, full conformance, CP logs): **OPEN** — depends on lab infra, env, and optional config

---

## Category taxonomy

These categories cover the full suite. **Setup** and **Teardown** are lifecycle **phases** (Bridge API scripts), not pytest validations. Everything else is a **test-phase** check.

| Category | Suite location | Count |
|----------|----------------|-------|
| Setup | `setup.py` phase | 1 phase |
| K8s core health | `validations.kubernetes` | 6 checks |
| GPU node + driver | `validations.kubernetes` | 4 checks |
| GPU Operator | `validations.kubernetes` | 3 checks |
| Network policy + dual stack | `validations.kubernetes` | 2 checks |
| CSI storage | `validations.k8s_storage` | 4 checks |
| MIG config | `validations.kubernetes` | 1 check |
| OIDC identity | `validations.k8s_identity` | 1 check |
| CNCF conformance | `validations.k8s_conformance` | 1 check |
| GPU workloads | `validations.k8s_workloads` (+ 1 armada override in `kubernetes`) | 6 checks |
| Observability | `validations.k8s_observability` | 2 checks |
| API network ACL | `validations.k8s_network` | 1 check |
| Teardown | `teardown.py` phase | 1 phase |
| Node pools (optional) † | `validations.k8s_node_pools` | 2 checks |

† Not in the core 13 categories above; setup-adjacent. Armada Bridge config does **not** wire node-pool steps today — checks skip.

**Total test-phase validations:** 33 (canonical suite) + 1 duplicate `K8sNimHelmWorkload-3b` entry in armada `kubernetes` overrides → **34 pytest items** when armada config is merged.

---

## Full validation matrix

| # | Check | Category | What it does |
|---|--------|----------|--------------|
| 1 | K8sNodePoolCheck (create) | Node pools † | After create-node-pool step: nodes match labels/replicas |
| 2 | K8sNodePoolCheck (update) | Node pools † | After scale step: pool converges to new replica count |
| 3 | K8sNodeCountCheck | K8s core health | `kubectl get nodes` — count matches setup |
| 4 | K8sExpectedNodesCheck | K8s core health | Named nodes exist (skips if names list empty) |
| 5 | K8sNodeReadyCheck | K8s core health | All nodes `Ready` |
| 6 | K8sPodHealthCheck | K8s core health | All pods Running or Succeeded |
| 7 | K8sNoPendingPodsCheck | K8s core health | No Pending pods |
| 8 | K8sNoErrorPodsCheck | K8s core health | No Error / CrashLoopBackOff pods |
| 9 | K8sNvidiaSmiCheck | GPU node + driver | `nvidia-smi` on GPU nodes |
| 10 | K8sDriverVersionCheck | GPU node + driver | Driver version matches setup |
| 11 | K8sGpuPodAccessCheck | GPU node + driver | GPU pod schedules and sees GPU |
| 12 | K8sGpuCapacityCheck | GPU node + driver | `nvidia.com/gpu` capacity on nodes |
| 13 | K8sGpuLabelsCheck | GPU Operator | Nodes labeled `nvidia.com/gpu.present=true` |
| 14 | K8sGpuOperatorNamespaceCheck | GPU Operator | GPU Operator namespace exists |
| 15 | K8sGpuOperatorPodsCheck | GPU Operator | Operator pods healthy |
| 16 | K8sNetworkPolicyCheck | Network policy + dual stack | Denied pod blocked by NetworkPolicy |
| 17 | K8sDualStackNodeCheck | Network policy + dual stack | IPv4+IPv6 on nodes (`auto` skips single-stack) |
| 18 | K8sCsiStorageTypesCheck | CSI storage | PVC bind per StorageClass type (block/shared-fs/nfs) |
| 19 | K8sCsiStorageQuotaApiCheck | CSI storage | ResourceQuota APIs and enforcement |
| 20 | K8sCsiTenantScopedCredentialsCheck | CSI storage | CSI secrets/RBAC tenant-scoped |
| 21 | K8sCsiProvisioningModesCheck | CSI storage | Dynamic (+ optional static) provision and mount |
| 22 | K8sMigConfigCheck | MIG config | MIG labels on capable nodes |
| 23 | K8sOidcIssuerCheck | OIDC identity | OIDC discovery endpoint valid |
| 24 | K8sCncfConformanceCheck | CNCF conformance | Conformance harness (`quick` = smoke only) |
| 25 | K8sNcclWorkload | GPU workloads | Single-node NCCL bandwidth |
| 26 | K8sNcclMultiNodeWorkload | GPU workloads | Multi-node NCCL (MPI Operator) |
| 27 | K8sGpuStressWorkload | GPU workloads | CUDA GPU memory stress |
| 28 | K8sNimInferenceWorkload | GPU workloads | NIM deploy + inference |
| 29 | K8sNimHelmWorkload-1b | GPU workloads | Helm NIM Llama 1B + genai-perf |
| 30 | K8sNimHelmWorkload-3b (kubernetes) | GPU workloads | Helm NIM Llama 3B (armada override) |
| 31 | K8sNimHelmWorkload-3b (workloads) | GPU workloads | Same class, workloads category |
| 32 | K8sApiServerMetricsCheck | Observability | API server Prometheus metrics |
| 33 | K8sControlPlaneLogsCheck | Observability | Control-plane component logs |
| 34 | K8sApiNetworkAclCheck | API network ACL | Unauthorized API probe must fail |

---

## Category coverage (suite design)

| Category | In suite? | Validations | Notes |
|----------|-----------|-------------|-------|
| Setup | Yes (phase) | — | Bridge cluster create, kubeconfig, inventory |
| K8s core health | Yes | #3–8 | |
| GPU node + driver | Yes | #9–12 | |
| GPU Operator | Yes | #13–15 | |
| Network policy + dual stack | Yes | #16–17 | |
| CSI storage | Yes | #18–21 | |
| MIG config | Yes | #22 | Skips when no MIG hardware |
| OIDC identity | Yes | #23 | |
| CNCF conformance | Yes | #24 | Default `mode: quick` |
| GPU workloads | Yes | #25–31 | Needs NGC key for NIM |
| Observability | Yes | #32–33 | CP logs may need custom commands on RKE2 |
| API network ACL | Yes | #34 | Skips without `unauthorized_probe_cmd` |
| Teardown | Yes (phase) | — | Cluster destroy; `ARMADA_BRIDGE_SKIP_TEARDOWN` supported |
| Node pools † | Yes (optional) | #1–2 | Not wired in armada config |

**All categories are represented in suite design.** Gaps on a given lab are **pass/fail/skip** outcomes, not missing test definitions.

---

## Reference run — import BM lab (1× L4, `gpu-vm-1`)

Example from `isvctl deploy run` on bridge VM with internal Bridge ingress. Update this table after each lab run.

| Category | Result | Detail |
|----------|--------|--------|
| Setup | ✅ PASS | Cluster running ~4 min; kubeconfig + inventory OK |
| K8s core health | ✅ PASS | 1 node Ready; pods healthy |
| GPU node + driver | ✅ PASS | Driver 580.95.05; 1× L4 |
| GPU Operator | ✅ PASS | `gpu-operator` namespace, 6 pods |
| Network policy + dual stack | ⚠️ PARTIAL | Dual-stack N/A (single-stack); **NetworkPolicy FAIL** (CNI not enforcing) |
| CSI storage | ⚠️ PARTIAL | **Block/Longhorn PASS**; shared-fs/nfs/static subtests skipped |
| MIG config | ⏭️ N/A | L4 — no MIG |
| OIDC identity | ✅ PASS | Discovery OK |
| CNCF conformance | ⚠️ PARTIAL | `quick` smoke only (2/6610) |
| GPU workloads | ⚠️ OPEN | **Stress FAIL**; NCCL N/A (1 GPU); NIM skipped (no NGC); MPI missing |
| Observability | ⚠️ PARTIAL | Metrics ✅; **CP logs FAIL** (no CP pods in kube-system) |
| API network ACL | ⏭️ SKIP | Probe not configured |
| Teardown | ✅ PASS | `--skip-destroy` (cluster retained) |
| Node pools † | ⏭️ SKIP | Steps not configured |

**Executed checks:** 23 passed · 3 failed · 6 skipped (pytest summary)

| Result | Checks |
|--------|--------|
| ❌ Failed | K8sNetworkPolicyCheck, K8sGpuStressWorkload, K8sControlPlaneLogsCheck |
| ⏭️ Skipped | Node pools (×2), MIG, NIM helm (×3), NCCL multi-node, API ACL |

---

## Closing extended gaps (lab / config, not suite code)

| Gap | Category | Typical fix |
|-----|----------|-------------|
| NetworkPolicy not enforced | Network policy + dual stack | CNI with netpol enforcement (Calico/Cilium) |
| GPU stress pod fails | GPU workloads | Debug stress pod on cluster (`kubectl` events/logs) |
| No control-plane pods in kube-system | Observability | Add `commands` in config for RKE2/Bridge CP logs |
| NIM workloads skipped | GPU workloads | Set `NGC_API_KEY` in env |
| NCCL multi-node skipped | GPU workloads | Install MPI Operator; add GPU nodes |
| NCCL single-node skipped | GPU workloads | ≥2 GPUs on node |
| API ACL skipped | API network ACL | Wire `kubernetes.unauthorized_probe_cmd` in setup output / config |
| Full CNCF cert | CNCF conformance | Change `mode` to `certified-conformance` or `non-disruptive-conformance` |
| Node pool checks skipped | Node pools † | Add Bridge scale / node-pool steps (like AWS EKS) |

---

## Run commands

```bash
# From Mac — remote on bridge VM (kubectl + internal Bridge URL on VM)
uv run isvctl deploy run <bridge-vm-ip> -u pavans \
  -f isvctl/configs/providers/armada-bridge/config/k8s.yaml --no-upload

# Direct on bridge VM
source ~/bridge-isv.env
uv run isvctl test run -f isvctl/configs/providers/armada-bridge/config/k8s.yaml

# Test phase only (cluster already up, KUBECONFIG set)
uv run isvctl test run -f isvctl/configs/providers/armada-bridge/config/k8s.yaml --phase test
```

Bridge VM env for internal ingress: see `BRIDGE_URL`, `BRIDGE_HOST`, `BRIDGE_INSECURE` in [armada-k8s.md](./armada-k8s.md) runner section.
