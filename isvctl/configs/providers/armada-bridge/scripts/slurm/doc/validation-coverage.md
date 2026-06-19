# Armada Bridge Slurm — validation coverage

Maps the canonical Slurm suite (`isvctl/configs/suites/slurm.yaml`) to **validation categories** for Armada Bridge lab sign-off. Provider config: `config/slurm.yaml`.

**Related:** [armada-slurm.md](./armada-slurm.md) (JSON contracts, Bridge API) · [what_needed.md](./what_needed.md) (implementation checklist)

---

## One-line conclusion

**Core question:** Did Bridge build a **working Slurm cluster** where jobs can be submitted via **`sinfo` / `srun` / `sbatch`**, GPU allocation works, and heavier GPU workloads can run?

**Not tested:** Bridge Slurm **Job HTTP API** — the ISV suite validates **Slurm CLI** only.

---

## Lifecycle (not pytest checks)

| Phase | Script | What it does |
|-------|--------|--------------|
| **Setup** | `scripts/slurm/setup.py` | Allocate/reuse BMs → `POST .../slurm` → poll running → SSH to master → `sinfo` inventory → JSON for tests |
| **Test** | pytest / isvtest | 13 validations below |
| **Teardown** | `scripts/slurm/teardown.py` | `DELETE .../slurm/{id}` (optional skip via `ARMADA_BRIDGE_SKIP_TEARDOWN`) |

---

## Category taxonomy

| Category | Suite location | Count |
|----------|----------------|-------|
| Slurm core health | `validations.slurm` | 8 checks |
| Slurm workloads | `validations.slurm_workloads` | 5 checks |

**Total test-phase validations:** 13

---

## Full validation matrix

| # | Check | Category | What it verifies | Pass means |
|---|--------|----------|------------------|------------|
| 1 | SlurmInfoAvailable | Slurm core | Slurm responds to `sinfo` | At least `min_partitions` (default 1) partition listed |
| 2 | SlurmPartition-cpu | Slurm core | **`cpu`** partition exists and is healthy | Partition is **up**; node count and names match setup inventory |
| 3 | SlurmPartition-gpu | Slurm core | **`gpu`** partition exists and is healthy | Partition is **up**; node count and names match setup inventory |
| 4 | SlurmJobSubmission | Slurm core | Basic job submission | A simple Slurm job can be submitted and completes |
| 5 | SlurmGpuAllocation-1gpu | Slurm core | 1-GPU allocation | `srun --partition=gpu --gres=gpu:1 nvidia-smi --list-gpus` sees **1 GPU** |
| 6 | SlurmGpuAllocation-2gpu | Slurm core | 2-GPU allocation on one job | Same command with `--gres=gpu:2` sees **2 GPUs** on **one node** |
| 7 | SlurmNodeJobExecution-cpu | Slurm core | Per-node CPU jobs + storage | Job runs on **each cpu partition node**; can write/read under `storage_path` (default `/tmp`) |
| 8 | SlurmNodeJobExecution-gpu | Slurm core | Per-node GPU jobs + storage | Job runs on **each gpu partition node**; GPU + scratch path work |
| 9 | SlurmGpuStressWorkload | Workloads | GPU stress (~30s) | CUDA GPU stress job succeeds on **gpu** partition (`cuda_arch` from setup, default 90 in armada config) |
| 10 | SlurmNcclMultiNodeWorkload | Workloads | Multi-node NCCL | GPU communication across **all gpu partition nodes**; bandwidth vs `min_bus_bw_gbps` (default 100) |
| 11 | SlurmSbatchWorkload-gpu | Workloads | Batch GPU job (manifest) | `sbatch` from `example_gpu_job.sbatch` with substituted vars succeeds |
| 12 | SlurmSbatchWorkload-cpu | Workloads | Batch CPU job (manifest) | `sbatch` from `example_cpu_job.sbatch` succeeds |
| 13 | SlurmSbatchWorkload-inline | Workloads | Inline GPU batch job | Inline `#SBATCH` script runs on **gpu** with 1 GPU; **`nvidia-smi`** in job output |

---

## What setup must discover (drives test expectations)

Setup runs `sinfo` / inventory on the Slurm master and emits JSON used by Jinja in the suite:

| Setup field | Used by |
|-------------|---------|
| `slurm.partitions.cpu.nodes` | SlurmPartition-cpu, SlurmNodeJobExecution-cpu, SlurmSbatchWorkload-cpu |
| `slurm.partitions.gpu.nodes` | SlurmPartition-gpu, SlurmNodeJobExecution-gpu, NCCL, sbatch GPU vars |
| `slurm.storage_path` | SlurmNodeJobExecution-cpu/gpu (default `/tmp`) |
| `slurm.default_partition` | GPU stress, NCCL (default `gpu`) |
| `slurm.cuda_arch` | SlurmGpuStressWorkload (armada default `90`) |
| `slurm.gpu_per_node` | SlurmSbatchWorkload-gpu `GPUS_PER_NODE` (armada default `1`) |

Partition names **`cpu`** and **`gpu`** are hard-coded in the suite unless remapped via:

- `BRIDGE_SLURM_CPU_PARTITION` / `BRIDGE_SLURM_GPU_PARTITION`
- `BRIDGE_SLURM_CPU_PARTITION_SOURCE` / `BRIDGE_SLURM_GPU_PARTITION_SOURCE`

---

## Cluster prerequisites for success

| Requirement | Why |
|-------------|-----|
| Slurm cluster **running** after Bridge create | Setup polls until ready |
| Partitions named **`cpu`** and **`gpu`** (or remapped) | Partition checks + sbatch scripts |
| GPU nodes with working **`gres=gpu`** | GPU allocation and GPU jobs |
| **`nvidia-smi`** in GPU jobs | Allocation checks, inline sbatch |
| Writable **`storage_path`** on compute nodes | Node job execution checks |
| **SSH (key)** from bridge VM → Slurm **master** | All CLI runs via SSH wrappers or local client + copied `slurm.conf` |
| **≥2 GPUs on one node** | SlurmGpuAllocation-2gpu |
| **≥2 GPU nodes** + network | SlurmNcclMultiNodeWorkload, multi-node sbatch |
| NVIDIA driver + CUDA stack | GPU stress, NCCL |

---

## Armada Bridge lab notes (typical import BM)

| Topic | Detail |
|-------|--------|
| **Runner** | Tests run on **bridge VM**; Slurm CLI reaches master via **SSH** (`BRIDGE_SSH_KEY_FILE`, `BRIDGE_SLURM_SSH_USER`) |
| **Nodes** | Default: 1 **master** + 1+ **worker** BM UUIDs (`BRIDGE_SLURM_MASTER_NODE_ID`, `BRIDGE_SLURM_WORKER_NODE_IDS`) |
| **IPs** | Resolved from `GET .../metal/computes/{id}` (`inBandIP` / `externalIPAddress`); override with `BRIDGE_SLURM_HEAD_HOST` if needed |
| **2×1-GPU BMs** | Many core checks can pass; **2gpu**, **cpu partition**, and **NCCL bandwidth** often fail without matching topology |
| **K8s conflict** | Same BMs cannot be in an active K8s cluster and Slurm cluster simultaneously — tear down K8s first |
| **Setup time** | Slurm install often **30–55+ minutes**; provider setup timeout **3600s** |

---

## Run commands

```bash
# From Mac (same as K8s deploy pattern)
uv run isvctl deploy run <bridge-vm-ip> -u pavans \
  -f isvctl/configs/providers/armada-bridge/config/slurm.yaml -- \
  -v -s

# On bridge VM (avoids Mac SSH timeout on long setup)
source ~/bridge-isv.env
cd ~/isv-ncp-validation-suite
uv run isvctl test run -f isvctl/configs/providers/armada-bridge/config/slurm.yaml -- -v -s

# Archive results (manual — _output/ is overwritten each run)
mkdir -p run-results/slurm
cp _output/junit-validation.xml _output/pytest-output.log run-results/slurm/
```

---

## Results and debugging

| Artifact | Location | Contents |
|----------|----------|----------|
| Full log | `_output/pytest-output.log` | Setup, every check, command output (use `-s`) |
| Report | `_output/junit-validation.xml` | Pass/fail per check with messages |
| Console | End of run | `ORCHESTRATION RESULTS` summary only |

To run a subset: `-- -k SlurmPartition-gpu` or `-m "not workload"`.
