# Armada Bridge — Kubernetes Validation Tests

All tests run via `isvctl test run -f isvctl/configs/providers/armada-bridge/config/k8s.yaml`.

| # | Test Group | Test | What it validates |
|---|---|---|---|
| 1 | Setup | `K8sNodeCountCheck` | Expected number of nodes are joined to the cluster |
| 2 | Setup | `K8sNodePoolCheck` (create) | Node pool create converges to desired replica count, labels, taints, and instance type *(SKIP — Bridge has no node pool API yet)* |
| 3 | Setup | `K8sNodePoolCheck` (update/scale) | Node pool scale-up converges to new replica count *(SKIP — Bridge has no node pool API yet)* |
| 4 | K8s core health | `K8sExpectedNodesCheck` | Specific node names are present in the cluster |
| 5 | K8s core health | `K8sNodeReadyCheck` | All nodes are in Ready state |
| 6 | K8s core health | `K8sPodHealthCheck` | No pods in failed state (Pending ignored) |
| 7 | K8s core health | `K8sNoPendingPodsCheck` | No pods stuck in Pending state |
| 8 | K8s core health | `K8sNoErrorPodsCheck` | No pods in Error or CrashLoopBackOff state |
| 9 | GPU node + driver | `K8sNvidiaSmiCheck` | nvidia-smi runs successfully inside a pod |
| 10 | GPU node + driver | `K8sDriverVersionCheck` | NVIDIA driver version matches expected |
| 11 | GPU node + driver | `K8sGpuPodAccessCheck` | Pod requesting a GPU can see it via nvidia-smi |
| 12 | GPU node + driver | `K8sGpuCapacityCheck` | Node reports correct GPU count as a Kubernetes resource |
| 13 | GPU node + driver | `K8sGpuLabelsCheck` | GPU nodes carry expected NVIDIA labels |
| 14 | GPU Operator | `K8sGpuOperatorNamespaceCheck` | GPU operator namespace exists in the cluster |
| 15 | GPU Operator | `K8sGpuOperatorPodsCheck` | All GPU operator pods are running |
| 16 | Network policy + dual stack | `K8sNetworkPolicyCheck` | NetworkPolicy correctly blocks and allows pod-to-pod traffic |
| 17 | Network policy + dual stack | `K8sDualStackNodeCheck` | Nodes have both IPv4 and IPv6 addresses (if required) |
| 18 | CSI storage | `K8sCsiStorageTypesCheck` | Block, shared-FS, and NFS storage classes can bind PVCs |
| 19 | CSI storage | `K8sCsiStorageQuotaApiCheck` | Over-quota PVC requests are rejected by the API |
| 20 | CSI storage | `K8sCsiTenantScopedCredentialsCheck` | CSI credentials are scoped to the tenant and not shared |
| 21 | CSI storage | `K8sCsiProvisioningModesCheck` | Dynamic and static CSI provisioning work end-to-end |
| 22 | MIG config | `K8sMigConfigCheck` | MIG labels are present and strategy is correct on GPU nodes |
| 23 | OIDC identity | `K8sOidcIssuerCheck` | Cluster OIDC issuer URL is configured and reachable |
| 24 | CNCF conformance | `K8sCncfConformanceCheck` | Runs CNCF conformance test suite (quick / full mode) |
| 25 | GPU workloads | `K8sNcclWorkload` | NCCL all-reduce bandwidth meets minimum threshold on a single node |
| 26 | GPU workloads | `K8sNcclMultiNodeWorkload` | NCCL bandwidth meets minimum threshold across multiple nodes |
| 27 | GPU workloads | `K8sGpuStressWorkload` | GPU stress test runs without errors |
| 28 | GPU workloads | `K8sNimInferenceWorkload` | NIM inference server deploys and responds to requests |
| 29 | GPU workloads | `K8sNimHelmWorkload-1b` | NIM Helm deploy + inference with llama-3.2-1b model |
| 30 | GPU workloads | `K8sNimHelmWorkload-3b` | NIM Helm deploy + inference with llama-3.2-3b model |
| 31 | Observability | `K8sApiServerMetricsCheck` | Kubernetes API server metrics endpoint is accessible |
| 32 | Observability | `K8sControlPlaneLogsCheck` | apiserver, scheduler, and controller-manager produce logs |
| 33 | API network ACL | `K8sApiNetworkAclCheck` | Kubernetes API endpoint refuses unauthorized connections |

## Notes

- **32 unique test classes**, 33 rows (`K8sNodePoolCheck` appears twice for create and scale operations).
- Tests marked **SKIP** will not fail the suite — they are skipped gracefully when the required step output is absent.
- `K8sNcclMultiNodeWorkload` requires 2+ GPU nodes. It will skip or fail on a single-node cluster.
- GPU workload tests (`K8sNimHelmWorkload-*`, `K8sNimInferenceWorkload`) require `NGC_API_KEY` to be set.
- Setup timeout is 1800s (30 min) to accommodate BM allocation + cluster creation polling.
