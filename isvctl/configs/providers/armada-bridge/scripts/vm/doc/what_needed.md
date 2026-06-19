# Armada Bridge VM suite — what is needed to pass

**Goal:** Pass the VM suite (`config/vm.yaml` + `suites/vm.yaml`) against live Bridge.

**Scale:** 12 scripts/steps → **34 validation checks** (3 steps are `best_effort`, so failures there do not fail the run).

See also: [armada-vm.md](./armada-vm.md) for per-step JSON contracts.

---

## What we have

- Provider config wired (`config/vm.yaml`) — all 12 steps defined
- Validation contract imported from `suites/vm.yaml`
- JSON reference: `armada-vm.md` in this folder
- Shared infra: `BridgeClient`, polling, errors, tenant helpers
- Shared NIM scripts (`deploy_nim`, `teardown_nim`) — already implemented
- All 10 VM Python scripts exist — **demo mode only** (`ISVCTL_DEMO_MODE=1`)
- Reference API flow in `bridge-api-test-automation` (`vm.js`): allocate → poll → get → delete

---

## What we do not have

| Area | Gap |
|------|-----|
| VM scripts | No live Bridge implementation — all raise `NotImplementedError` outside demo |
| HTTP client | No multipart POST for VM allocate |
| SSH helper | `wait_for_ssh()` not implemented |
| Power lifecycle | stop / start / reboot not implemented (API: `power/off`, `on`, `reboot` — not in Postman VM workflows) |
| Config | start / reboot / describe do not pass `key_file` like AWS config does |
| Bridge APIs | Tags, serial console, console RBAC — no workflow match (steps are `best_effort`) |
| Lab VM image | Suite expects Ubuntu GPU VM with SSH, `nvidia-smi`, cloud-init, Docker on the allocated flavour |

---

## What must be added to pass

### Must implement (blocking)

1. **`launch_instance`** — POST allocate (multipart), poll until `running`, save SSH key, emit JSON (`instance_id`, `public_ip`, `key_file`, `state`, `vpc_id`)
2. **`list_instances`** — GET VMs, find target instance
3. **`stop_instance`** — POST `power/off`, poll until stopped
4. **`start_instance`** — POST `power/on`, poll, SSH ready, emit `key_file` + `public_ip`
5. **`reboot_instance`** — POST `power/reboot`, poll, SSH, low uptime, `reboot_confirmed: true`
6. **`describe_instance`** — GET VM, emit `public_ip`, `key_file`, `state: running`
7. **`teardown`** — DELETE VM, poll until gone
8. **`BridgeClient.post_multipart`** + **`wait_for_ssh`**
9. **Config fix** — pass `--key-file` (and IP if needed) into lifecycle / describe steps

### Must be true in the environment (not code)

- Runner can SSH to the VM public IP with the key from launch
- VM is Ubuntu, GPU visible, cloud-init done, Docker installed (for host checks)

### Optional (first pass)

- Tags, serial console, console RBAC — `best_effort`; can leave stubbed
- NIM — omit `NGC_API_KEY` to skip 3 checks, or set it for full NIM pass

---

## Effort (rough)

| Target | Time |
|--------|------|
| allocate + list + describe + delete | ~1 week |
| stop / start / reboot | +2–3 days |
| Full suite green (SSH / GPU / host checks) | +depends on GPU Ubuntu image in lab (~1 week if image ready) |

---

**Summary:** Stubs and docs exist. Need **7 VM scripts + multipart client + SSH wait + minor config**, plus a **GPU Ubuntu VM** in Bridge that SSH checks can reach. Tags / console / NIM are optional for a first pass.
