# Armada Bridge Slurm suite — what is needed to pass

**Goal:** Pass the Slurm suite (`config/slurm.yaml` + `suites/slurm.yaml`) against live Bridge.

**Scale:** **2 scripts** (`setup.sh`, `teardown.sh`) → **13 validation checks** (8 core Slurm + 5 workloads).

See also: [armada-slurm.md](./armada-slurm.md) for per-step JSON contracts and Bridge API mapping.

---

## What we have

- Provider config wired (`config/slurm.yaml`) — setup + teardown steps, `--tenant` arg
- Validation contract imported from `suites/slurm.yaml`
- Reference inventory script: `my-isv/scripts/slurm/setup.sh` (`sinfo` → JSON)
- Shared infra: `BridgeClient`, polling, session auth (`../../common/`)
- Bridge QA reference: `bridge-api-test-automation/lib/utils/slurm.js` (create / poll / delete / Job API)
- Postman workflows: VM-based and BM-based Slurm cluster paths
- Armada `setup.sh` / `teardown.sh` exist — **exit 1, not implemented**

---

## What we do not have

| Area | Gap |
|------|-----|
| Slurm scripts | No live Bridge implementation — stubs return `Not implemented` |
| Node provisioning | Setup does not allocate VMs/BMs required for `POST .../slurm` |
| **Runner / CLI gap** | Validations run `sinfo`/`srun`/`sbatch` **locally**; Bridge QA uses **HTTP Job API** — different surface |
| Local Slurm client | No logic to configure `SLURM_CONF` or run `isvctl` on head node |
| Partition naming | Suite hard-codes **`cpu`** and **`gpu`** — must match cluster or change suite |
| Topology | NCCL + 2-GPU checks need multi-node / multi-GPU cluster; single-VM may be insufficient |
| Config | Only `--tenant` passed; no VM flavour, worker count, `cluster_id` persistence, or skip-teardown |
| Teardown scope | Deletes Slurm only — backing VMs/BMs not torn down unless extended |
| Demo mode | No `ISVCTL_DEMO_MODE=1` path in armada slurm scripts (unlike VM Python stubs) |

---

## What must be added to pass

### Must implement (blocking)

1. **`setup.sh`** (or Python equivalent)
   - Provision backing **VM(s)** and/or **BM node(s)** (reuse VM allocate flow from VM suite / Postman).
   - `POST /orchestrator/tenants/{tenant}/slurm` with `nodes: [{id, role: master|worker}, ...]`.
   - Poll until `status` is `running` or `success`.
   - Make Slurm CLI available where **`isvctl` runs** (SSH to head + client config, or run tests on head).
   - Run **`sinfo` inventory** (copy from `my-isv/setup.sh`).
   - Emit JSON: `cluster_name`, `cluster_id`, `slurm_host`, `slurm.partitions.cpu/gpu`, `storage_path`, `cuda_arch`, etc.

2. **`teardown.sh`**
   - Load persisted `cluster_id`.
   - `DELETE .../slurm/{id}`, poll until 404 / absent from list.
   - Optional: delete backing VMs/BMs.

3. **Bridge ↔ ISV alignment**
   - Do **not** substitute Job API for CLI validations.
   - Ensure partitions **`cpu`** / **`gpu`** exist with correct GRES.

4. **Config / persistence**
   - Pass provisioning params (flavour, worker count, SSH key).
   - Persist `cluster_id` between setup and teardown invocations.

### Must be true in the environment (not code)

- Runner can execute **`sinfo`**, **`srun`**, **`sbatch`** against the new cluster.
- GPU nodes expose **`nvidia-smi`** via Slurm jobs.
- Cluster has enough **GPU nodes and GPUs** for 2-GPU allocation and NCCL multi-node checks (or accept skips/failures on smaller topologies).
- Writable scratch at `storage_path` on compute nodes.

### Optional (first pass)

- `ISVCTL_DEMO_MODE=1` dummy JSON for pipeline testing (validations will still fail without real Slurm).
- Skip teardown env flag.
- Relax or skip NCCL / 2-GPU checks via suite edits for minimal topology labs.

---

## Effort (rough)

| Target | Time |
|--------|------|
| Slurm create + poll + delete (orchestrator only) | ~2–3 days |
| VM allocate + Slurm create (Postman parity) | ~1 week (depends on VM suite / multipart client) |
| Local Slurm CLI / head-node test execution | ~2–4 days (environment-dependent) |
| Full 13-check green (multi-node GPU, NCCL) | +1–2 weeks (topology + drivers + network) |

---

## Summary

Docs and config scaffolding exist. Blockers are **(1)** implementing setup/teardown against Bridge orchestrator, **(2)** provisioning nodes before Slurm create, and **(3)** bridging the **CLI vs Job API** gap so `isvctl` can run `sinfo`/`srun`/`sbatch` during the test phase. VM suite work (allocate, SSH) overlaps heavily with the VM-based Slurm Postman workflow.
