# hive-cli Feedback Roadmap (synthesized 2026-06-03)

Synthesis of 4 long-form session reports (now in `archive/`) filed by different
agents across different workloads. ~36 raw issues de-duplicated into **3 common
root causes** + **6 special themes**. Fix common roots first; specials build on
them.

Source reports:
- `archive/OPTIMIZATION_NOTES.md` — scheduler-starvation deep dive (A–K)
- `archive/SESSION_ISSUES.md` — multi-GPU-per-node hold-job session (Issues 1–9)
- `archive/HIVE_ISSUES_AND_IMPROVEMENTS.md` — big-model TP workload (问题 1–7)
- `archive/observed_issues_2026_06_03.md` — ma-egoqa w/ out-of-band co-tenant (1–10)

---

## Common roots (shared infrastructure — highest leverage)

### C1 — Status model conflates 4 meanings; scheduler blindly trusts a stale, unlocked, 900s DB
Probe failure → mislabeled `cpu`/`busy`/dropped; scheduler dispatches only on
`status=="idle"`; no node-DB lock; 900s vs 30s cadence gap; `busy` can't tell
"held" from "GPU in use" from "out-of-band tenant".
Covers: OPT-A, OPT-B, OPT-C, OPT-D, OPT-J, SESSION#1, SESSION#8.
**Status: Phase 1 — IN PROGRESS (2026-06-03).**

### C2 — No pre-dispatch GPU-clean / free-mem gate; dispatch couples to a single status source
Zombie/foreign memory → instant OOM on startup; co-tenant vLLM occupancy invisible.
Covers: SESSION#3, SESSION#4, OBSERVED#2, OPT-B.
**Status: Phase 1 — IN PROGRESS.**

### C3 — Dispatch wrapper sets no `CUDA_VISIBLE_DEVICES` / no GPU binding
All same-node overlap tasks land on physical GPU0 → collide/OOM; also blocks
multi-GPU. Monitoring `probe_job` hard-codes `--id=0`, hiding it.
Covers: SESSION#2, 问题4, 问题1(partial).
**Status: Phase 2 — PLANNED.**

### C4 — Failures not classified; no auto-retry
Walltime expiry / OOM / node-loss all collapse to FAILED with no retry.
Covers: OPT-F, SESSION#7, OBSERVED#4.
**Status: Phase 2 — PLANNED.**

### C5 — Node poller is not a cluster-wide singleton (cross-node duplicate daemons)
State lives in shared-FS `~/.hive`, but `hive-daemon` liveness uses a host-local
`kill -0` on an un-hostnamed `node_monitor.pid`, so each node that touches hive spawns
its OWN poller → N× `squeue`+`srun --overlap` → step-count blowup, probe contention,
DB churn ("squeue chaos"). The same-host sweep can't reap cross-node copies;
cross-node force-repoll (SIGUSR1) is also host-local. The SCHEDULER is already a correct
cluster singleton (hostname + shared-FS heartbeat) — the poller just needs the same.
Covers: inbox #1 (newly reported — not in the original 4 archive reports).
**Status: Phase 2 — PLANNED.** Fix = mirror the scheduler's heartbeat/host pattern onto
the poller + a `poll.request` file for cross-node re-poll.

---

## Special themes (build on the common roots)

| Theme | Summary | Source issues | Status |
|-------|---------|---------------|--------|
| S1 Multi-GPU | `--gpus N`, GPU-slot scheduling, whole-node `--exclusive` preset | 问题1 | planned |
| S2 Fairness | fair-share / round-robin, high-prio lane, `reprioritize` | 问题2, OBSERVED#1, OPT-H | planned |
| S3 Pool elasticity | burst auto-scale, `drain` state, walltime replace-before-expiry | 问题3, SESSION#5, SESSION#7 | planned |
| S4 Cancel correctness | compute-node process-group kill, batch cancel | SESSION#3, OPT-G | planned |
| S5 Observability/ergonomics | `stats`, `tail`/`inspect`, `--group`/`--prefix`, ETA/pos, CMD column, dedupe, batch `wait` | OBSERVED#5/6/7/8/10, OPT-K | planned |
| S6 Config & safety | pool preset `--test-only` validation, hold-job log redirect, nested-srun guard, crash-safe dispatch + authoritative restart, docs drift | OPT-I/E, SESSION#9, 问题7, drift | partial (Phase 0) |

---

## Delivery phases

- **Phase 0 (done/landing):** feedback system; pool preset validation + log
  redirect (S6); docs-drift fix; additive queue/node schema fields; `.gitignore`.
- **Phase 1 (in progress):** C1 + C2 — probe-failure as distinct state, node-DB
  flock, dispatchable = SLURM-allocated + GPU-clean, free-mem gate (`need_mb`),
  `pending_reason`, starvation watchdog, staleness guard, event-driven re-probe,
  crash-safe dispatch.
- **Phase 2 (planned):** C3 + C4 — GPU UUID pinning + per-GPU monitoring; infra-
  vs-task failure classification + auto-retry.
- **Phase 3 (planned):** S4 → S5 → S2 → S3 → S1.

Per-report tracking lives in `INDEX.md`; this file tracks themes.
