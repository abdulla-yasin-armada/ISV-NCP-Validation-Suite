# Armada Bridge Slurm — manual validation runbook

Step-by-step commands to replay each ISV Slurm check against a live cluster. Use this to continue validation where isvctl left off.

**Related:** [validation-coverage.md](./validation-coverage.md) · [armada-slurm.md](./armada-slurm.md)

---

## Lab context (test-tenant001 import)

| Item | Value |
|------|--------|
| Bridge VM | `35.255.165.115` (user `pavans`) — isvctl runs here |
| Slurm master | **`gpu-a100-vm-2`** (`10.138.0.17`) — run manual `sinfo`/`srun`/`sbatch` here |
| Worker | `gpu-vm-1` (`10.162.0.4`) — `sinfo` failed here (not controller / no slurmctld) |
| Partition | **`debug`** only (default `debug*`) — ISV suite expects `cpu` and `gpu` |
| Nodes in `debug` | `gpu-a100-vm-2`, `gpu-vm-1` (2 nodes) |
| GRES | `gpu:nvidia:1` (A100), `gpu:l4:1` (L4) — 1 GPU per node |

```bash
# SSH to master (recommended for all manual checks)
ssh -i ~/.ssh/id_rsa pavans@10.138.0.17
export PART=debug
```

**Alternative (same as isvctl):** from bridge VM, `export PATH="$HOME/.cache/isvctl/slurm-bin:$PATH"` then run the same commands.

---

## Execution model

| Step | Where |
|------|--------|
| `sinfo` / `srun` / `sbatch` **client** | Master (`gpu-a100-vm-2`) or bridge VM → SSH → master |
| **Job body** (`nvidia-smi`, docker, …) | Compute node Slurm schedules (`gpu-a100-vm-2` or `gpu-vm-1`) |

Harmless warnings on every command: `Ignoring ControlMachine since SlurmctldHost is set`.

---

## Progress tracker — all 13 tests

Status key: **DONE** = recorded below · **TODO** = run command and fill in · **N/A** = expected skip on this lab

| # | ISV test | Run on | Command(s) | Pass criteria | Status | Your result / notes |
|---|----------|--------|------------|---------------|--------|---------------------|
| 1 | **SlurmInfoAvailable** | Master | `sinfo -o '%P %a %l %D %N'` | Exit 0, ≥1 partition | **DONE** | `debug* up infinite 2 gpu-a100-vm-2,gpu-vm-1` → **PASS** |
| 2 | **SlurmPartition-cpu** | Master | `sinfo -h -o '%P' \| tr -d '*' \| sort -u` | Partition **`cpu`** exists, up | **DONE** | Only `debug` → **SKIP** (no `cpu`) |
| 2b | | Master | `sinfo -p cpu -h -o '%N'` | Node list non-empty | **DONE** | Empty output |
| 3 | **SlurmPartition-gpu** | Master | `sinfo -h -o '%P' \| tr -d '*' \| sort -u` | Partition **`gpu`** exists | **DONE** | Only `debug` → **SKIP** (no `gpu`) |
| 3b | | Master | `sinfo -p gpu -h -o '%N'` | (ISV expects `gpu`) | **DONE** | Empty |
| 3c | | Master | `sinfo -p debug -h -o '%N'` | Lab equivalent | **DONE** | `gpu-a100-vm-2,gpu-vm-1` |
| 3d | | Master | `sinfo -p debug -h -o '%G'` | GPU GRES present | **DONE** | `gpu:nvidia:1` / `gpu:l4:1` |
| 4 | **SlurmJobSubmission** | Master | `srun --partition=gpu --gres=gpu:1 nvidia-smi -L` | GPU UUIDs in output | **TODO** | ISV cmd → expect **FAIL** (no `gpu` partition) |
| 4b | | Master | `srun --partition=debug --gres=gpu:1 nvidia-smi -L` | Lab equivalent | **TODO** | Fill in output below |
| 5 | **SlurmGpuAllocation-1gpu** | Master | `srun --partition=gpu --gres=gpu:1 nvidia-smi --list-gpus` | Exactly 1 GPU | **TODO** | ISV → **FAIL**; use `debug` for manual |
| 5b | | Master | `srun --partition=debug --gres=gpu:1 nvidia-smi --list-gpus` | Lab equivalent | **TODO** | |
| 6 | **SlurmGpuAllocation-2gpu** | Master | `srun --partition=debug --gres=gpu:2 nvidia-smi --list-gpus` | 2 GPUs on **one** node | **TODO** | Likely **FAIL** (1 GPU/node) |
| 7 | **SlurmNodeJobExecution-cpu** | Master | `sinfo -p cpu -h -o '%N'` | Per-node jobs on `cpu` | **N/A** | **SKIP** — no `cpu` partition |
| 8 | **SlurmNodeJobExecution-gpu** | Master | See [§8 per-node GPU jobs](#8-slurmnodejobexecution-gpu) | Each node: nvidia-smi + STORAGE_OK | **TODO** | Use `partition=debug` |
| 9 | **SlurmGpuStressWorkload** | Master | See [§9 GPU stress](#9-slurmgpustressworkload) | SUCCESS marker per node | **TODO** | Needs docker + NGC on compute nodes |
| 10 | **SlurmNcclMultiNodeWorkload** | Master | See [§10 NCCL](#10-slurmncclmultinodeworkload) | NCCL job completes | **TODO** | Docker = intra-node only |
| 11 | **SlurmSbatchWorkload-gpu** | Master | See [§11 sbatch GPU](#11-slurmsbatchworkload-gpu) | Job COMPLETED | **TODO** | Use `PARTITION=debug` |
| 12 | **SlurmSbatchWorkload-cpu** | Master | See [§12 sbatch CPU](#12-slurmsbatchworkload-cpu) | Job COMPLETED | **N/A** | **SKIP** — no `cpu` |
| 13 | **SlurmSbatchWorkload-inline** | Master | See [§13 inline sbatch](#13-slurmsbatchworkload-inline) | Job COMPLETED + nvidia-smi | **TODO** | Use `partition=debug` |

---

## Recorded output (paste zone)

### Test 1 — SlurmInfoAvailable

```
PARTITION AVAIL TIMELIMIT NODES NODELIST
debug* up infinite 2 gpu-a100-vm-2,gpu-vm-1
```

### Test 2 — SlurmPartition-cpu

```
$ sinfo -h -o '%P' | tr -d '*' | sort -u
debug

$ sinfo -p cpu -h -o '%N'
(empty)
```

### Test 3 — SlurmPartition-gpu

```
$ sinfo -p gpu -h -o '%N'
(empty)

$ sinfo -p debug -h -o '%N'
gpu-a100-vm-2,gpu-vm-1

$ sinfo -p debug -h -o '%G'
gpu:nvidia:1
gpu:l4:1
```

### Test 4 — SlurmJobSubmission

```
(paste after: srun --partition=debug --gres=gpu:1 nvidia-smi -L)
```

---

## Detailed commands (tests 4–13)

Replace `gpu` with `debug` on this lab. Run on **master** unless noted.

### 4 — SlurmJobSubmission

```bash
# ISV (fails here — wrong partition name)
srun --partition=gpu --gres=gpu:1 nvidia-smi -L

# Lab equivalent
srun --partition=debug --gres=gpu:1 nvidia-smi -L
```

### 5 — SlurmGpuAllocation-1gpu

```bash
srun --partition=debug --gres=gpu:1 nvidia-smi --list-gpus
# Expect: one GPU line
```

### 6 — SlurmGpuAllocation-2gpu

```bash
srun --partition=debug --gres=gpu:2 nvidia-smi --list-gpus
# Expect on this lab: fail or only 1 GPU (one GPU per node)
```

### 8 — SlurmNodeJobExecution-gpu

```bash
sinfo -p debug -h -o '%N'

# Repeat for each node:
srun --nodelist=gpu-vm-1 --partition=debug --gres=gpu:1 --chdir=/tmp -N1 bash -c \
  'hostname; nvidia-smi -L; echo isvtest > /tmp/.isvtest && rm /tmp/.isvtest && echo STORAGE_OK'

srun --nodelist=gpu-a100-vm-2 --partition=debug --gres=gpu:1 --chdir=/tmp -N1 bash -c \
  'hostname; nvidia-smi -L; echo isvtest > /tmp/.isvtest && rm /tmp/.isvtest && echo STORAGE_OK'
```

Pass: each node prints hostname, GPU list, `STORAGE_OK`.

### 9 — SlurmGpuStressWorkload

```bash
sinfo -p debug -h -o '%G'
sinfo -p debug -h -o '%N'

srun --job-name=isvtest-gpu-stress-manual \
  --partition=debug --nodes=2 --nodelist=gpu-a100-vm-2,gpu-vm-1 \
  --ntasks=2 --ntasks-per-node=1 --gres=gpu --chdir=/tmp --label \
  docker run --rm --gpus all \
  -e GPU_STRESS_RUNTIME=30 -e GPU_MEMORY_GB=16 \
  nvcr.io/nvidia/pytorch:25.04-py3 python3 -c "import torch; print('CUDA', torch.cuda.is_available())"
```

If this fails: check `docker`, `nvidia-container-toolkit`, NGC pull on **compute** nodes.

### 10 — SlurmNcclMultiNodeWorkload

```bash
sinfo -p debug -h -o '%N'

cat > /tmp/nccl-test.sbatch <<'EOF'
#!/bin/bash
#SBATCH --job-name=isvtest-nccl-manual
#SBATCH --partition=debug
#SBATCH --nodes=2
#SBATCH --ntasks-per-node=1
#SBATCH --gpus-per-node=1
#SBATCH --time=00:30:00
#SBATCH --output=slurm-%j.out
srun --ntasks-per-node=1 docker run --rm --gpus all --network=host --ipc=host \
  -e NCCL_DEBUG=INFO nvcr.io/nvidia/hpc-benchmarks:25.04 \
  all_reduce_perf -b 8 -e 4G -f 2 -g 1
EOF

sbatch /tmp/nccl-test.sbatch
squeue
# When done: cat slurm-<JOBID>.out
```

### 11 — SlurmSbatchWorkload-gpu

```bash
cat > /tmp/example_gpu_job.sbatch <<'EOF'
#!/bin/bash
#SBATCH --job-name=demo-gpu-test
#SBATCH --partition=debug
#SBATCH --nodes=2
#SBATCH --ntasks-per-node=1
#SBATCH --gres=gpu:1
#SBATCH --time=00:10:00
#SBATCH --output=slurm-%j.out
#SBATCH --error=slurm-%j.err
srun nvidia-smi --query-gpu=index,name --format=csv
EOF

sbatch /tmp/example_gpu_job.sbatch
squeue
cat slurm-*.out
```

### 12 — SlurmSbatchWorkload-cpu

Skip on this lab (no `cpu` partition). If added later:

```bash
sbatch --partition=cpu --nodes=1 --cpus-per-task=4 --wrap="hostname; echo SUCCESS"
```

### 13 — SlurmSbatchWorkload-inline

```bash
cat > /tmp/inline-debug.sbatch <<'EOF'
#!/bin/bash
#SBATCH --job-name=inline-test
#SBATCH --partition=debug
#SBATCH --gres=gpu:1
#SBATCH --nodes=1
#SBATCH --time=00:01:00
#SBATCH --output=slurm-%j.out
echo "Hello from inline script"
nvidia-smi
EOF

sbatch /tmp/inline-debug.sbatch
cat slurm-*.out
```

---

## isvctl vs manual — partition naming

| Layer | Partition used |
|-------|----------------|
| Bridge Slurm cluster | **`debug`** |
| Canonical ISV suite | **`cpu`**, **`gpu`** |
| isvctl run (your lab) | Setup OK; partition checks **SKIP**; job tests **FAIL** on `--partition=gpu` |

Env remap (setup JSON only, does **not** fix hardcoded `gpu` in isvtest):

```bash
BRIDGE_SLURM_GPU_PARTITION_SOURCE=debug
BRIDGE_SLURM_GPU_PARTITION=gpu
```

---

## Next steps checklist

- [ ] Test **4b** — `srun --partition=debug --gres=gpu:1 nvidia-smi -L` on master
- [ ] Test **5b** — `--list-gpus` (1 GPU)
- [ ] Test **6** — 2 GPU allocation (expect fail on 1-GPU nodes)
- [ ] Test **8** — per-node job + `/tmp` on both BMs
- [ ] Test **9** — docker GPU stress
- [ ] Test **10–11, 13** — sbatch jobs with `partition=debug`
- [ ] Paste results into **Recorded output** section above
- [ ] Archive: `mkdir -p run-results/slurm && cp _output/* run-results/slurm/` (after isvctl re-run)

---

## Troubleshooting

| Symptom | Likely cause |
|---------|----------------|
| `slurm_load_partitions: Zero Bytes…` on `gpu-vm-1` | Not master or `slurmctld` down — use **`gpu-a100-vm-2`** |
| `invalid partition specified: gpu` | Expected — use **`debug`** |
| `grep -w gpu` matches sinfo line | False positive from hostname `gpu-vm-1` — use `sinfo -h -o '%P'` |
| GPU stress / NCCL fail | Docker, NGC pull, or nvidia-container-toolkit on compute nodes |
