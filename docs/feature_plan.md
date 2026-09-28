# Feature plan (2026-09-28)

Scheduling features hive lacks compared with plain SLURM, ordered by what they unblock.
Each phase ships on its own: additive schema, offline tests, skill docs, `install.sh`,
daemon restart. Status is updated here as phases land.

| Phase | Feature | User-facing surface | Status |
|---|---|---|---|
| 1 | Task timeout | `hive submit --timeout 2h` / `#HIVE timeout=` | done |
| 1 | Completion notification | `--notify CMD` / `#HIVE notify=` / `$HIVE_NOTIFY` | done |
| 2 | Resource usage accounting | peak GPU memory / utilisation per task in `hive stats`, `hive wait`; `--need-mb auto` | done |
| 3 | Dependencies | `hive submit --after ID[,ID]` (`afterok`), `--after-any` | done |
| 3 | Sweeps | `hive submit --array 0-9%4` with `$HIVE_ARRAY_INDEX`; `%N` caps concurrency; `hive wait/cancel --array` | done |
| 3 | Per-owner concurrency cap | `--max-running N` / `$HIVE_MAX_RUNNING` per owner, so one sweep cannot take the pool | done |
| — | Slow-node tier, partition preference | `SLOW` node state, `--allow-slow` / `--no-slow`, `"prefer_partitions"` in pool_config.json | done |
| 4 | GPU-slot scheduling | several tasks on one multi-GPU hold job, one set of cards each (`gpu_slots`, `waiting_for_gpu`) | done — validated live 2026-09-28 on a 2×V100 hold job (evc12): two 1-GPU tasks at once on slots 0 and 1, then a 2-GPU task |
| 5 | Queue control | `hive hold` / `unhold` / `priority N` (pending tasks, ids or `--array`); `hive wait ID ID …` | done |
| 5 | Probes outside `queue.lock` | `hive submit` / `cancel` never wait on a node probe | done: health checks in background threads, verify probes between two passes of the cycle, in parallel |
| 5 | Reap after scheduler death | feedback #18/#19: `hive list` / `hive wait` restart a dead scheduler | done |

Not planned: multi-node tasks (a step cannot span hold jobs; needs a per-node launcher
and group failure handling) and preemption (without a checkpoint contract the evicted
task loses its work).

## Phase 1 — design notes

**Timeout.** `timeout_secs` on the task; the scheduler compares it with the run time
(`started_ts`) each cycle, SIGTERMs the step, and records `failed`, `exit_code` 124,
`fail_reason: timeout`. A timeout is the command's doing, so it is never retried and
never a strike against the node. Independent of `est_runtime` (a placement hint).

**Notification.** A shell command stored on the task and run by the scheduler — on the
scheduler's host, not the submit host or the compute node — when the task reaches a
terminal state or is requeued after an infra failure. It gets the task as `HIVE_TASK_*`
environment variables, runs detached under a 60 s limit, and its output goes to
`~/.hive/logs/notify.log`. A failing hook never affects the task.

## Phase 2 — design notes

Sampled inside the dispatch wrapper (it already runs a heartbeat loop on the node):
`nvidia-smi` every 10 s for the first minute, then every 30 s, kept as four running
numbers in `heartbeat/<id>.usage`, moved onto the task at finish and written to the
`finish` event. `--need-mb auto` = P90 peak of the name's
history × 1.1, the same shape as `--est-runtime auto`. The sampler is its own
background loop, so a node that answers `nvidia-smi` slowly (up to 25 s measured) or
never only costs samples, not the task.

## Phase 3 — design notes

`depends_on: [ids]` checked in the dispatch loop as a task-level rejection
(`waiting_for_dependency`); a failed or cancelled dependency fails the dependant
(`dependency_failed`) unless `--after-any`. An array is N ordinary tasks sharing an
`array_id`; the concurrency cap is counted per `array_id` (and per owner) among running
tasks. Dependencies must survive `hive prune`: a pruned dependency counts as its
recorded final state from `events.jsonl`.

## Phase 4 — design notes

Track occupancy per (hold job, GPU index) in `queue.json` rather than per hold job; the
wrapper picks the assigned entries of `CUDA_VISIBLE_DEVICES` instead of the first N. The
live probe has to report per-GPU readings. Needs the Phase 2 sampler to tell which
card a running task actually uses.

---

# Checklist, round 2 (2026-09-28)

Everything that was still open after the five phases: weak spots of what exists, the
SLURM features that had been left out, and housekeeping. One row per item; the status
column is updated as each lands. "Live" = tried on the real cluster, not only offline.

## A. What exists, made better

| # | Item | Status |
|---|---|---|
| A1 | Dispatch latency: wake the scheduler on submit and on a task's exit instead of waiting out the 30 s cycle | done — sched.wake + exit files end the wait; at least 3 s between cycles |
| A2 | `hive top`: SLOW state, several tasks on one hold job, array index | done |
| A3 | `hive nodes`: GPU% / MEM over all cards of a hold job, not only card 0 | done — busiest card, memory summed, `xN` |
| A4 | Canary jobs wait hours in a busy partition | not fixable in hive: the canary is already the smallest job SLURM accepts (1 CPU, 4 GB, 1 GPU, 15 min); in a busy partition it waits for priority like any job. Reboot detection does not depend on it |
| A5 | "healthy probes → node released" never seen live | open until C5 |
| A6 | Steady-state speed of slow nodes has no healthy same-model reference | cannot be verified: every H100-PCIe node tried was slow or wedged, so there is no healthy reference |
| A7 | `--timeout` counts the CUDA init time of a slow node | done |
| A8 | Every array member is a full task record | not changed: members stay full task records (1000 members ≈ 15 MB of queue.json). Bounded by the 1000-member limit and by A9 |
| A9 | `queue.json` and task logs grow without bound unless `hive prune` is run by hand | done — `auto_prune_days` (default 14), `log_keep_days` (default off) |
| A10 | `--need-mb auto` / `--est-runtime auto` need one finished run | by design: a first run has no history; `auto` then submits without the constraint and says so |

## B. SLURM features that were left out

| # | Item | Status |
|---|---|---|
| B1 | Pool autoscale: replace hold jobs before they expire, keep N nodes | done — `"autoscale"` in pool_config.json, `hive pool autoscale` |
| B2 | Warn a task before its node expires (`--signal`-like) | done — `--warn-before` |
| B3 | Delayed start (`--begin`) | done — `--begin` |
| B4 | CPU / memory per task | done — `--cpus`, `--mem` |
| B5 | Multi-node tasks | done — `--nodes N` (all members or none; no partial restart) |
| B6 | Preemption | done — `--preempt` / `--preemptible`, opt-in on both sides |
| B7 | Fair share between owners | done — `"fair_share": true` |

## C. Housekeeping

| # | Item | Status |
|---|---|---|
| C1 | README.md / README_zh.md describe none of the new features | done |
| C2 | docs/architecture.md: two-pass cycle, GPU slots, node health states | done |
| C3 | Feedback #18, #19, #27, #33 | open |
| C4 | Red-team round 3 on everything in this checklist | open |
| C5 | Install, live validation | open |
| C6 | Push the branch, open the pull request | open |
