# hive-cli node status model

Authoritative reference for the per-hold-job `status` field in
`~/.hive/node_monitor.json` and how the scheduler acts on it. Added in v0.4.0 to
resolve the long-standing confusion where probe failures, held-but-free
allocations, and out-of-band contention all collapsed into `idle`/`busy`
(feedback OPT-A/OPT-B/OPT-J, SESSION#1).

## Status values (set by `hive-poll` / `hive-daemon` → `hive-dbpost`)

| Status | Meaning | GPU reading | Dispatchable? |
|--------|---------|-------------|---------------|
| `idle` | GPU allocated, not in use | util < 5% **and** mem < 500 MiB | **yes** (subject to the GPU-clean / free-mem gate) |
| `busy` | GPU actively used by a job | util ≥ 5% **or** mem ≥ 500 MiB | no |
| `warning` | was busy, GPU went quiet < grace window | mem dropped to ~0 after a model was loaded | only after a live verify-probe |
| `cpu` | no GPU allocated to this job | `mem_total == 0`, probe succeeded | no (no GPU to run on) |
| `probe_failed` | the `srun --overlap` probe could **not** run | none — empty / non-zero / no `---PS---` marker, after one retry | only after a live verify-probe |
| `unknown` | legacy alias for a failed probe (pre-v0.4.0 DBs) | none | treated like `probe_failed` |

`hive nodes` / `hive top` additionally render **CLAIM** (display-only, never written to
the DB) when `queue.json` has a `running` task on a hold job whose GPU reading is
`idle`/`warning`/`probe_failed`: the slot is taken (cold import, model load, or a poll
older than the dispatch) even though the card isn't hot yet. Dispatch already excluded
such jobs (`used_jobids`); only the table was misleading (feedback #4/#7/#17/#32).

Extra fields a record may carry:

- `gpu_idle_since` — epoch when the busy→idle grace timer started (drives `warning`).
  **`warning` is transient, never terminal**: once `WARN_SECS` elapses and the GPU is
  still quiet, the job returns to `idle` and this field is cleared so the next
  busy→idle transition can re-arm it. (Before v0.4.1 the timer was carried forward but
  never cleared, pinning the job at `warning` for good; since the scheduler skipped
  `warning`, any hold job that finished one task was permanently evicted from the
  dispatch pool — see feedback #10.)
- `carried_forward: true` — this record's `status`/`gpu` were **reused from the last
  good poll** because the latest probe failed; `polled_at` keeps the last-good stamp
  and `probe_failed_at` records the failed attempt. Held up to `CARRY_MAX_AGE`=1800s.
- `time_left_secs` — remaining hold-job walltime from `squeue %L`, captured **every**
  cycle (even on probe failure / carry-forward), so its measurement basis is the DB's
  top-level `updated`, not per-job `polled_at`. `-1` = unlimited, `null`/absent =
  unknown (older poller). Drives the walltime gate below and the `hive nodes` LEFT col.

## Node health / quarantine (`hive health`, `libexec/hive_health.py`)

Separate from per-hold-job `status`, hive keeps a per-**physical-node** health list in
`~/.hive/node_health.json`. A node is `quarantined` when creating a real CUDA context on
it fails — the failure mode nvidia-smi cannot see (co-tenant outside our cgroup, broken
driver state; feedback #13/#15/#16/#20/#28/#29/#30). Hold jobs on a quarantined node are
never dispatch candidates. Entry points:

| Trigger | Effect |
|---|---|
| verify-before-dispatch CUDA probe returns `fail` | quarantine now (`source=verify`) |
| task fails < `FAST_FAIL_SECS`=180 s with a CUDA-init signature in its log | +1 strike; `STRIKES_TO_QUARANTINE`=2 → quarantine (`source=auto`), tripping task requeued (`node_quarantined_redispatch`) |
| `hive health report NODE` | quarantine now (`source=agent`) |
| successful task on the node | strikes reset |
| periodic probe every `HEALTH_CHECK_SECS`=600 s (through a hold job with no running task) | `HEALTH_OK_STREAK`=2 consecutive `ok` after `QUARANTINE_MIN_SECS`=3600 s → released; `fail` → re-armed; `unknown` → ignored |
| `hive health clear NODE` | released (`source=manual`) |

The probe is stdlib-only (`ctypes` → `libcuda.so.1`: `cuInit`, `cuCtxCreate`,
`cuMemAlloc`) and runs inside `srun --overlap` under the hold job's cgroup, so it sees
exactly what a task would. `unknown` (no python / no libcuda) is never a fault.
`HIVE_CUDA_PROBE_CMD` overrides the probe snippet (offline tests).

## Key rule

**A probe failure is never reported as `cpu` or `busy`.** Doing so silently removed
idle GPUs from the dispatch pool for a whole 900s poll cycle — the dominant
starvation bug. Probe failure → `probe_failed` (or carried-forward last-good), which
the scheduler treats as *verify before trusting*, not *blocked*.

## Scheduler dispatch (`hive-sched`)

Dispatchability is **decoupled from a single `status=="idle"` check**:

1. **Candidates** = `idle` nodes + uncertain ones (`probe_failed`/`unknown`/`warning`/
   `carried_forward`/stale) + just-freed nodes. `busy`/`cpu` excluded. `warning` counts
   as *uncertain*, not busy — it always carries `needs_verify`, so the live probe below
   decides. This also lets a DB written by an older hive (stuck at `warning`) self-heal
   without hand-editing `node_monitor.json`.
2. **Verify-before-dispatch — every dispatch** (`VERIFY_EVERY_DISPATCH`): one
   `srun --overlap` step reads `nvidia-smi` memory/util *and* creates a CUDA context
   right before placing a task. The live reading overrides the DB (a stale-dirty DB no
   longer blocks a now-clean card, and a stale-idle DB no longer places onto a card a
   co-tenant filled since — feedback #14). Verified-clean nodes are cached for the rest
   of the cycle. `cuda=fail` → `cuda_unavailable_on_verify` + node quarantined.
3. **GPU-clean / free-mem gate**: skip any candidate with > `GPU_CLEAN_MB`=5000 MiB
   resident (zombie process or out-of-band co-tenant), or with less than the task's
   `need_mb` free. The task stays PENDING with a `pending_reason`.
3a. **Candidates are shared across the pending list, and only NODE-level rejections
   consume them.** `probe_unverifiable`, `node_busy_on_verify` and `gpu_dirty` describe
   the node and remove it for every task this cycle; `insufficient_gpus`,
   `waiting_for_mem` and `insufficient_walltime` describe *this task* and leave the node
   for the next one. (Before v0.4.1 every rejection popped the node, so one unplaceable
   head-of-queue task starved the whole queue with `no_dispatchable_node` — feedback
   #24/#25/#26.) A node live-verified clean is cached for the rest of the cycle.
3b. **Walltime gate** (only when the task carries `est_runtime_secs`): skip any node
   whose live remaining walltime (`time_left_secs` − DB age) is below
   `est_runtime_secs + WALLTIME_MARGIN_SECS`=600s, so a long task is never placed on a
   node that will be reclaimed mid-run (`pending_reason=insufficient_walltime`).
   Estimate-less tasks stay walltime-blind; `-1`/unknown walltimes never block.
4. **Starvation watchdog**: if there are pending tasks but nothing dispatched for
   `STARVATION_CYCLES`=3 consecutive cycles, SIGUSR1 the poller for an immediate
   re-poll instead of waiting 900s. A stale node DB triggers the same.
5. **Event-driven**: when a task finishes, its node is reconsidered the same cycle
   (live-verified), not after the next full poll.
6. **Crash-safe**: a task's running-claim is saved to `queue.json` *before* the `srun`
   launch; on restart a `running` task with no heartbeat/exit and a dead srun PID is
   requeued (`pending_reason=redispatched_after_crash`, capped at `MAX_ATTEMPTS`=3).
7. **Infra-failure requeue + checkpoint-loss notice**: a `running` task that *did* run
   (heartbeat existed) but whose hold-job/node is gone (confirmed by `squeue -j`) is an
   **infra failure**, not the task's own crash. It's requeued onto another live node
   (`pending_reason=infra_failure_redispatch`, `requeue_count`++, `checkpoint_warning`),
   but the new run starts fresh — so the loss is surfaced in the task log, in
   `hive wait` (`⚠`), and as `(re-disp xN)` in `hive list`. A task whose hold-job is
   **still alive** when its heartbeat dies is treated as a self-crash → `failed`, no
   retry (so a buggy command can't loop forever). "Alive" is always confirmed with a
   bounded `squeue -j`, never read from the node DB alone — the DB can be up to a poll
   cycle stale and still list a reclaimed job.
8. **Cancel is executed by the scheduler.** `hive cancel` on a running task signals the
   srun step directly only when the scheduler runs on the caller's host (the step is the
   scheduler's child); otherwise it sets `cancel_requested` on the task and the scheduler
   SIGTERMs the step in its next cycle, marks the task `cancelled`, and live-verifies the
   node before reusing it. `hive list` shows `CANCELLING` in between.

## Timestamps

Every queue timestamp is stored twice: a human-readable naive-local `*_at` string
(`submitted_at`, `started_at`, `dispatched_at`, `finished_at`) and a tz-independent epoch
`*_ts` twin. **Arithmetic must use `*_ts`** (`_task_epoch` in `hive-sched`, `task_epoch`
in `hive-queue`): the scheduler, the poller and each agent's shell may run under
different `TZ` settings, so subtracting two `*_at` strings written by different
processes was off by hours (feedback #31). Records written before v0.4.1 have no `*_ts`
and fall back to parsing `*_at` in the reader's zone. `events.jsonl` lines carry `t`.

## `pending_reason` values

`no_dispatchable_node`, `waiting_for_mem`, `gpu_dirty`, `node_busy_on_verify`,
`probe_unverifiable`, `cuda_unavailable_on_verify`, `node_quarantined`,
`node_quarantined_redispatch`, `insufficient_walltime`, `insufficient_gpus`,
`redispatched_after_crash`, `infra_failure_redispatch`, `dispatch_error`. Shown in the
NODE column of `hive list` for pending tasks.

## Runtime estimate, history & timing feedback

A task's `est_runtime_secs` (from `--est-runtime 2h|auto` / `#HIVE est_runtime=`) drives
the walltime gate. `auto` resolves to the **P90** of the task name's completed-run
history.

History and timing feedback come from the **durable append-only event log**
`~/.hive/events.jsonl` (`libexec/hive_events.py`): one JSON line per `submit` /
`dispatch` / `finish` / `requeue` / `cancel`, with **measured** `run_secs` and
`queued_secs`. Because it's append-only and separate from `queue.json`, it survives
queue pruning and keeps history cheap to record. The log is **bounded** — auto-trimmed
to the recent tail once it passes ~4 MB (`EVENTS_MAX_BYTES`/`EVENTS_KEEP_LINES` in
`hive_events.py`), so stats/estimates reflect *recent* history and reads stay cheap.
`hive stats [NAME]` aggregates it (merged with any `done` tasks still in the queue) into
per-name count/min/median/P90/max;
`hive wait` returns the measured `queued`/`ran` durations on completion so an agent can
base its own estimate on ground truth rather than a guess. Writes are serialized by the
existing `queue.lock` (every emitter already holds it), so no extra lock is needed.

## Tuning

Constants live at the top of `libexec/hive-sched` (`GPU_CLEAN_MB`, `NEED_MB_DEFAULT`,
`NODE_DB_STALE_SECS`, `ORPHAN_GRACE_SECS`, `MAX_ATTEMPTS`, `STARVATION_CYCLES`,
`LIVE_PROBE_TIMEOUT`, `WALLTIME_MARGIN_SECS`) and `libexec/hive-dbpost` (`WARN_SECS`,
`CARRY_MAX_AGE`).
