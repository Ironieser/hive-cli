# hive — Issues & Optimization Notes

Engineering review of the `hive` GPU-pool / task-queue CLI, grounded in a long
research session that ran thousands of GPU jobs on a SLURM cluster (≈1973 done,
295 failed, 1311 cancelled, observed in `~/.hive/queue.json`).

The session repeatedly hit a **scheduler-starvation failure mode**: idle GPUs were
labeled `busy`, dispatch stalled, and `running` count drifted to zero while
GPUs sat free. This report locates each problem in the source, diagnoses the root
cause, rates severity, and proposes a concrete fix.

## Architecture recap (how the pieces fit)

- **`libexec/hive-poll`** (one-shot) and **`libexec/hive-daemon`** (loop, `POLL_INTERVAL=900`)
  both probe every running SLURM job via `srun --jobid=X --overlap nvidia-smi`,
  derive a per-job `status` (`cpu` / `idle` / `busy` / `warning` / `unknown`),
  and write `~/.hive/node_monitor.json` (the "node DB").
- **`libexec/hive-sched`** (scheduler daemon, `POLL_INTERVAL=30`) reads the node DB
  + `queue.json` every 30s and dispatches `pending` tasks **only to nodes whose
  `status == "idle"`** (`get_idle_nodes`), launching them with `srun --overlap`.
- **`libexec/hive-queue`** is the user CLI: submit / list / cancel / wait / logs / rm / daemon.
- **`libexec/hive-pool`** submits/cancels SLURM "hold" jobs (`sleep 365d` allocations)
  that form the pool the scheduler dispatches into.
- **`libexec/hive-nodes`** renders the node table and manages the poller daemon.

Status semantics (set in `probe_job`, both poll/daemon):
```
max_gpu_total == 0            → "cpu"     (no GPU allocated)
util >= 5  OR  mem >= 500MiB  → "busy"
else                          → "idle"    (GPU allocated, not in use)
```

---

## A. `hive poll` does not reliably probe GPU util; failed probe → node lost / mislabeled — **CRITICAL**

**Where:** `libexec/hive-poll` — `probe_job()` (L74–188) and `main()` assembly loop (L250–264).

**Root cause.** Two compounding defects in the one-shot poller:

1. **Failed/timed-out probes are silently dropped, not recorded.** `probe_job`
   writes its fragment file only at the very end (L176–187). If
   `timeout 30 srun ... nvidia-smi` returns empty (node busy, srun step contention,
   SLURM slow to grant `--overlap`, `MaxStepCount` pressure), `raw` is empty,
   `max_gpu_total` stays `0`, and the job is written as **`status:"cpu"`** — or, if
   the fragment never gets written because the probe subshell was reaped early, the
   assembly loop at L253–262 does `[[ ! -f "$frag" ]] && continue`, **silently
   omitting the node from the DB entirely**. A node missing from the DB is invisible
   to the scheduler (`get_idle_nodes` can't dispatch to what it can't see) → the
   GPU is stranded.

2. **The "waited 1s, all complete" race.** `main()`'s wait loop (L232–241) breaks as
   soon as every expected fragment file exists. Under concurrent polls (see C) or
   when `srun` fails fast and returns empty, fragments appear almost immediately with
   `gpu:[]`, `util:None`-equivalent, producing the observed "All probes complete
   (waited 1s)" for ~26 nodes — far too fast for 26 real `srun nvidia-smi` calls.
   Ground-truth `srun --jobid=X --overlap nvidia-smi` showed those nodes at 0% / 4 MiB
   (genuinely **idle**), yet the DB recorded them as not-idle.

   Note the **inconsistency with `hive-daemon`**: the daemon already writes a
   `status:"unknown"` fallback fragment for failed probes (L236–250), but
   `hive-poll` does **not**. So `hive poll` and the daemon disagree on the same event.

**Net effect:** probe failure is conflated with "not idle." Idle GPUs are
systematically withheld from the scheduler → starvation.

**Fix (concrete):**
1. In `hive-poll`, port the daemon's failed-probe fallback: always emit a fragment,
   and use **`status:"unknown"`** (never `cpu`/`busy`) when `raw` is empty or `srun`
   exited non-zero. Capture `srun`'s exit code explicitly:
   ```bash
   raw=$(timeout 30 srun ... bash -c "$probe_cmd" 2>/dev/null); rc=$?
   probe_failed=0; { (( rc != 0 )) || [[ -z "$raw" ]]; } && probe_failed=1
   ```
   and branch `status="unknown"` when `probe_failed`.
2. Distinguish "GPU allocated, query succeeded, util≈0" (→ truly `idle`) from
   "query failed" (→ `unknown`) by tracking whether **any** nvidia-smi line parsed.
   If `max_gpu_total==0` AND the probe succeeded → it's genuinely a CPU job; if the
   probe failed → `unknown`.
3. Make the wait loop count **completed** probes against **launched** PIDs (track
   `probe_pids[]` and `wait -n` on them) instead of polling for fragment-file
   existence, removing the "1s" early-exit race.
4. **Retry transient `srun` failures once** (e.g. one immediate re-probe on empty
   `raw`) before declaring `unknown`.

---

## B. Scheduler dispatches ONLY to `status=="idle"` nodes → starvation when (A) mislabels — **CRITICAL**

**Where:** `libexec/hive-sched` — `get_idle_nodes()` (L108–114) and dispatch loop (L262–285).

**Root cause.** `get_idle_nodes` is a strict equality filter:
```python
if info.get("status") == "idle" and jid not in used_jobids
```
This couples dispatch **entirely** to the poller's correctness. Any node the poller
marks `busy`, `unknown`, `cpu`, or omits is unreachable for dispatch. Combined with
(A), when all 26 nodes were wrongly non-`idle`, the dispatch loop's
`zip(idle_nodes, pending)` (L269) iterated **zero** times. Cells stayed `pending`,
GPUs were physically free, and as running cells finished the `running` count drifted
down (12→8→5→0) with **no replacement**, since nothing was re-dispatched.

A single poller hiccup therefore cascades into total throughput collapse — there is
no independent liveness check or fallback.

**Fix (concrete):**
1. **Decouple dispatch from a single status source.** Add a lightweight,
   scheduler-side confirmation: when a node is marked `idle` OR `unknown`, the
   scheduler may directly verify with a fast `srun --jobid=X --overlap -n1 --mem=0
   nvidia-smi --query-gpu=utilization.gpu,memory.used` (short timeout) right before
   dispatch, rather than trusting a possibly-stale DB.
2. **Treat `unknown` as dispatchable-with-verification, not blocked** — never let an
   `unknown` permanently strand a GPU.
3. **Add a starvation watchdog:** if `pending > 0` AND `idle_nodes == 0` for N
   consecutive cycles, log a loud warning and trigger an immediate re-poll
   (`kill -USR1` the poller daemon) instead of waiting for the next 900s cycle.
4. **Staleness guard:** if `node_monitor.json.updated` is older than e.g. 2×
   the poll interval, the scheduler should re-poll (or refuse to trust `busy`
   labels) rather than dispatch against stale data.

---

## C. Concurrent `hive poll` invocations corrupt `node_monitor.json` — **HIGH**

**Where:** `libexec/hive-poll` `main()` (L191–347); same pattern in `hive-daemon` `do_poll()`.

**Root cause.** There is **no lock** guarding the node DB. Both writers compute into
`$DB_TMP="${DB_FILE}.tmp"` then `mv` into place (L207, L270). `DB_TMP` is **not**
PID-scoped (unlike `POLL_TMPDIR=/tmp/hive_poll_$$`), so two concurrent polls write the
**same** `node_monitor.json.tmp` and race on the final `mv`. Worse, each poll only
knows about the jobs **it** enumerated from its own `squeue` call; the last writer
wins and **overwrites** the other's view entirely. Running a keeper loop + a waiter
loop simultaneously (as the session did) produced the observed oscillating /
inconsistent busy↔idle states, amplifying the starvation in (A)/(B).

The scheduler's `queue.json` is correctly protected by `flock` (`QueueLock`,
hive-sched L65–76) — but the **node DB has no equivalent**.

**Fix (concrete):**
1. **Add an exclusive `flock` around the whole poll**, e.g. a `node_monitor.lock`
   acquired non-blocking at the top of `main()`/`do_poll()`. If the lock is held,
   the second poll should **exit immediately** ("poll already in progress") rather
   than queue up — a single fresh poll is what's wanted.
2. **PID-scope the temp file**: `DB_TMP="${DB_FILE}.$$.tmp"` so the atomic `mv` is
   never clobbered mid-write even if the lock is bypassed.
3. The `mv` already gives atomic replacement for *readers*; the lock is what's
   needed to serialize *writers*.

---

## D. Daemon dispatch lag — freed nodes not promptly re-dispatched — **HIGH**

**Where:** `libexec/hive-daemon` `POLL_INTERVAL=900` (L14); `libexec/hive-sched`
`POLL_INTERVAL=30` (L31); `get_idle_nodes` reads the node DB only.

**Root cause — a cadence mismatch.** Per CHANGELOG 0.3.2, the daemon poll interval
was raised **120s → 900s** to avoid exhausting SLURM's `MaxStepCount=40000`
(each probe spawns an `srun` step). But dispatch eligibility is driven **entirely**
by `node_monitor.json`, which the daemon refreshes only every **15 minutes**. So even
though `hive-sched` loops every 30s, it sees the **same stale snapshot** for up to
900s. A node that frees up at second 1 of a poll window stays labeled `busy` (its
old probe result) until the next poll — its GPU idles for up to ~15 minutes before
the scheduler is even *allowed* to consider it. Throughput is capped far below
capacity, exactly as observed (~0–1 dispatches / several minutes).

The poll cost is real (step-count pressure), so the fix is **not** "poll more
often" globally.

**Fix (concrete):**
1. **Event-driven re-poll on task completion.** When `hive-sched` marks a task
   `done`/`failed` (L244–260), that node's GPU just freed. Have the scheduler
   **directly re-probe just that one node** (cheap: one `srun` step for the node it
   already holds the jobid for) and flip it to `idle` in its in-memory view for the
   current dispatch pass — no need to wait for the global poll.
2. **Targeted poll instead of full sweep.** Give the poller a "`--jobid X[,Y]`"
   mode so the scheduler can refresh only the handful of just-freed nodes at high
   cadence while the full sweep stays at 900s. This keeps step consumption bounded
   (you only probe nodes that actually changed).
3. Alternatively, **let the scheduler skip the DB for freshly-freed nodes** entirely:
   on completion, optimistically treat the node as available and verify at dispatch
   (ties into B.1).

---

## E. `hive daemon restart` interrupts in-flight dispatch / kills just-dispatched steps — **HIGH**

**Where:** `libexec/hive-nodes` `daemon_stop()` (L60–100) and the `restart` path
(L455–460); note **`hive daemon restart` routes to the *node poller* daemon**
(hive `daemon` → `hive-nodes`, see `hive` L84–96), **not** `hive-sched`.

**Root cause.** Two issues:
1. The restart was used as a manual "unstick" remedy for the starvation in (A)/(B) —
   i.e. a **workaround for a bug, not a feature**. The stuck state should self-heal
   (see B.3 watchdog) so a restart is never needed.
2. `daemon_stop()` does a broad **`pgrep -f "hive-daemon"` sweep** (L84–99) and
   `kill -TERM`/`-KILL`s every match with elapsed > 60s. The elapsed-time filter is
   meant to spare the <30s probe subshells, but a **`probe_job` subshell that is
   itself running a slow `srun` step** (the timeout is 30s, but `srun` queueing under
   `--overlap` contention can blur this) — or, more importantly, a **dispatched task's
   `srun --overlap` step is NOT a `hive-daemon` process and should be untouched**, yet
   the manual restart of the *scheduler* (`hive queue daemon restart`,
   hive-queue L252–255) does `os.kill(pid, SIGTERM)` on the scheduler, and any
   in-progress `dispatch_task` (hive-sched L269–285) is aborted between
   `subprocess.Popen` and the `save_queue` write — leaving a launched `srun` step with
   **no queue record**, i.e. an orphaned/aborted step.

**Fix (concrete):**
1. **Make stuck-state self-heal** (B.3) so restart is unnecessary.
2. **Never kill the poller mid-poll:** acquire the same poll `flock` (C.1) in
   `daemon_stop` before signaling, or set a "poll in progress" marker and wait for it
   to clear, so a restart can't tear down a probe that's writing the DB.
3. **Make scheduler dispatch crash-safe:** in `dispatch_task`, write the task's
   `state=running` + `srun_pid` to the queue **before** (or atomically with) the
   `srun` launch, and on scheduler startup reconcile any `running` task whose
   `srun_pid` is dead → requeue. Then a restart mid-dispatch can't silently lose a step.

---

## F. Node walltime expiry kills in-flight cells; marked FAILED with no auto-resubmit — **HIGH**

**Where:** `libexec/hive-sched` `check_task_status()` (L189–220) and the running-task
loop (L233–260). The cell dies with
`"STEP ... CANCELLED ... DUE TO SIGNAL Terminated"` when the hold job's `--time`
walltime expires; the heartbeat goes stale → `check_task_status` returns `"dead"` →
task set to **`state="failed"`, `exit_code=-1`** (L254–259), terminal, no retry.

**Root cause.** The scheduler does not distinguish **task-fault failures** (the user's
command exited non-zero) from **infrastructure failures** (the *node* went away:
walltime expiry, `scancel`, preemption). Both collapse to `failed`. Over a long run,
the 295 failures observed include cells that failed **purely because their hold job
expired**, not because the work was wrong. There is no migration/resubmit, so long
runs bleed throughput to node churn.

**Fix (concrete):**
1. **Classify the failure.** On `dead`/non-zero exit, check whether the
   `slurm_jobid` is still alive (`squeue -j <jobid>` or `sacct`). If the **job is
   gone** (walltime/preempt/scancel), tag it `infra_failure` rather than `failed`.
2. **Auto-resubmit infra failures.** Add per-task `attempts` + `max_retries`
   (default e.g. 3). On an infra failure, reset the task to `pending` (clear
   `slurm_jobid`/`node`/`srun_pid`), increment `attempts`, and let the next cycle
   re-dispatch it to a **different** live node. Only mark `failed` permanently when
   `attempts >= max_retries` or when the **task command itself** exited non-zero.
3. Optionally, surface a `--no-retry` submit flag and record `attempt_history` in the
   task so users can see migrations.

---

## G. `hive queue cancel` takes only ONE id; bulk cancel of running tasks unreliable — **MEDIUM**

**Where:** `libexec/hive-queue` `cmd_cancel()` (L447–471) and parser (L610–611:
`pc.add_argument("id", type=int)` — single positional int).

**Root cause.**
1. **No batch interface.** Cancelling 60 cells required a shell `for`/`xargs` loop,
   each invocation re-acquiring `QueueLock` and re-loading/re-writing the whole
   `queue.json` — O(n) lock churn (1311 cancelled tasks in this session made this
   painful).
2. **First-pass bulk cancel of *running* tasks doesn't fully take.** `cmd_cancel`
   sends `SIGTERM` to the recorded **`srun_pid`** (L463) and immediately sets
   `state="cancelled"`. But killing the *local* `srun` parent does **not reliably
   tear down the remote `--overlap` step** (the heartbeat/while-true loop and the
   user process on the compute node can outlive it; SIGTERM to srun may not propagate
   to the step's process group). So a "cancelled" task can keep running on the GPU,
   and a second pass is needed.

**Fix (concrete):**
1. **Add batch cancel:** accept `nargs="+"` ids, plus convenience selectors
   `--ids 1,2,3`, `--state pending`, `--all-pending`, and a range `--range A-B`.
   Do it under a **single** `QueueLock` + single `save_queue`.
2. **Make running-cancel authoritative:** kill the whole process **session/group**
   (`start_new_session=True` is already set in `dispatch_task`, L181, so
   `os.killpg(os.getpgid(srun_pid), SIGTERM)` then `SIGKILL` after a grace period),
   and additionally write the heartbeat **exit file** / remove the heartbeat so the
   monitor loop converges. For belt-and-suspenders, `scancel --signal` the *step* if
   a step id is recorded.
3. Verify termination before declaring `cancelled` (poll once for `srun_pid` gone).

---

## H. `--priority` cannot be changed after submit — **MEDIUM**

**Where:** `libexec/hive-queue` — there is no `reprioritize` subparser (parser
L583–637); `priority` is only set at submit (`cmd_submit` L306–309) and read by the
scheduler's pending sort (`hive-sched` L264–267:
`key=lambda t: (-t.get("priority",0), t["submitted_at"])`).

**Root cause.** Priority is write-once. To raise a pending task's priority the user
had to `cancel` + re-`submit`, which loses the task id, its log, and its submit time
(and re-incurs G's cancel cost).

**Fix (concrete):** Add `hive queue reprioritize <id...> --priority N` (and accept
batch ids / `--state pending`). Under `QueueLock`, update `task["priority"]` for any
task still in `pending` (and optionally `running`, though that only affects future
ordering). It's a trivial, safe mutation since the scheduler reads priority fresh
every cycle. Consider also `--bump`/`--top` to move a task to the front.

---

## I. `hive pool add` default preset is broken (Invalid node name specified) — **MEDIUM**

**Where:** `libexec/hive-pool` `cmd_add()` (L80–118), `get_preset()` default
resolution (L51–62); `pool_config.example.json` (`"default":"normal"`); the shipped
script `~/1_normal_gpu.slurm`.

**Root cause.** The `normal` preset (the **default**) points at a SLURM script with
two defects that make `sbatch` reject it:
```
#SBATCH --time=2-24:00:00     # malformed: 24 in the HH field is out of range
#SBATCH --exclude=evc[1-10],evc[12-20],evc31,...,evc16   # invalid node range → "Invalid node name specified"
```
`cmd_add` runs `sbatch` and only prints `result.stderr` on failure (L114–118) — it
does **not validate** the script or the preset beforehand, so the error surfaces only
at submit time. The `highgpu` preset (`~/1_gpu.slurm`, clean `-p highgpu`,
`--time=24:00:00`, no exclude) works — which is why explicitly passing `highgpu`
succeeded while the default failed.

**Fix (concrete):**
1. **Fix the shipped default**: either make `normal`'s script valid (correct
   `--time` to `3-00:00:00`, fix/remove the malformed `--exclude`) **or** change
   `pool_config.example.json` `"default"` to a known-good preset (`highgpu`).
2. **Validate before submitting.** In `cmd_add`, run `sbatch --test-only <script>`
   (dry run) first; on failure, print the SLURM error and abort **before** looping
   `--count` times (currently a bad default fails `count` times in a row).
3. **`hive pool config` should flag invalid presets** — it already checks script
   existence (L182–183); extend it to `sbatch --test-only` and show a ✗ for presets
   SLURM would reject.

---

## J. Stale/orphaned `busy` state; "allocation held" conflated with "GPU in use" — **HIGH**

**Where:** `probe_job` status logic (`hive-poll` L165–172 / `hive-daemon` L144–151);
node-table accounting (`hive-nodes` `show_table` L318–380).

**Root cause.** The status model has two related flaws:

1. **`busy` means "GPU memory ≥ 500 MiB OR util ≥ 5%", regardless of whether a hive
   task owns it.** A hold job is a `sleep 365d` allocation where the **GPU is free
   until you `srun` into it** — but if *any* resident memory lingers (a previous
   tenant's leaked context, a model left loaded, another user's process on a shared
   multi-tenant node), the node reads `busy` and the scheduler skips it forever.
   This is exactly the "45 busy with 0 running tasks" observation: 45 allocations
   held, the queue shows 0 `running`, yet the DB calls them all busy. The probe
   comment itself (hive-poll L160–164) admits `ps` is node-wide/unreliable on
   multi-GPU nodes — but the **memory threshold has the same multi-tenant problem**.

2. **The node table double-counts.** `show_table` (hive-nodes L318–339) increments
   `n_busy` for both `busy` and `warning`, and `n_idle` for both `idle` and
   `unknown` — so an `unknown` (probe-failed) node is reported as **idle** in the
   summary while the scheduler's `get_idle_nodes` treats it as **not** idle. The
   table and the scheduler disagree about what "idle" means.

**Fix (concrete):**
1. **Define `busy` as "a *hive task* is running here," not "memory is nonzero."**
   Cross-reference `queue.json`: a node is `busy` only if some `running` task's
   `slurm_jobid` matches (the table already builds this `jobid_to_task` map at
   hive-nodes L159–167 — push that logic down into the status decision, or have the
   scheduler own status). A held allocation with no hive task → **`idle`** (it's
   available for dispatch) even if stray memory is resident, with a separate
   `foreign_mem` flag for visibility.
2. **Add a `held`/`free` distinction** separate from `busy`/`idle` so "allocation
   held but GPU free" is a first-class state the scheduler can dispatch into.
3. **Reconcile the table's counters** with the scheduler's `get_idle_nodes`
   definition so `unknown` is never silently counted as idle in the summary.
4. **Expire stale entries:** the parser already computes `is_job_stale` (>600s,
   hive-nodes L192–200) but only annotates it with a `!`. The scheduler should
   **refuse to trust a stale `busy`** and re-probe instead (ties to B.4 / D).

---

## K. No completion signal for a *batch* of tasks — **MEDIUM**

**Where:** `libexec/hive-queue` `cmd_wait()` (L505–559) waits on **one** id only
(parser L624–629, single positional `id`).

**Root cause.** `wait` is single-task. To detect when a *set* of submitted tasks
finished, the session hand-rolled polling loops over `queue.json` — exactly the
boilerplate `wait` was meant to remove for agent workflows (see the docstring,
L506–511).

**Fix (concrete):**
1. **Batch wait:** `hive queue wait <id...>` / `--ids 1,2,3` / `--state pending`
   that blocks until **all** named tasks reach a terminal state, printing a live
   progress line (`done/failed/cancelled/remaining`) and exiting non-zero if **any**
   failed.
2. **Tag + wait-on-tag:** allow `hive submit --tag run42 ...` and
   `hive queue wait --tag run42` to wait on a whole batch without tracking ids.
3. Reuse the existing `TERMINAL_STATES` set (L503) and the single-task poll loop,
   generalized over a list.

---

## Severity summary

| ID | Problem | Severity | Primary file |
|----|---------|----------|--------------|
| A | `hive poll` drops/mislabels failed probes (idle→cpu/missing) | **Critical** | `libexec/hive-poll` |
| B | Scheduler dispatches only to `idle`; no fallback → starvation | **Critical** | `libexec/hive-sched` |
| C | Concurrent polls race/corrupt node DB (no flock) | **High** | `libexec/hive-poll`, `hive-daemon` |
| D | 900s poll cadence → freed GPUs idle up to 15 min | **High** | `libexec/hive-daemon`, `hive-sched` |
| E | `daemon restart` used to unstick; aborts in-flight dispatch | **High** | `libexec/hive-nodes`, `hive-sched` |
| F | Walltime expiry → FAILED, no auto-resubmit | **High** | `libexec/hive-sched` |
| J | `busy` conflates "allocation held" with "GPU in use"; stale `busy` | **High** | `probe_job`, `hive-nodes` |
| G | `cancel` single-id only; running-cancel unreliable | **Medium** | `libexec/hive-queue` |
| H | Priority write-once; no `reprioritize` | **Medium** | `libexec/hive-queue` |
| I | Default pool preset rejected by `sbatch`; no validation | **Medium** | `libexec/hive-pool`, example config |
| K | No batch-completion `wait` | **Medium** | `libexec/hive-queue` |

## Recommended fix ordering

1. **Stop the bleeding (A + B + J):** make probe-failure `unknown` (never
   `busy`/`cpu`), add scheduler-side verify-before-dispatch + starvation watchdog,
   and redefine `busy` as "hive task running here." This directly ends the
   starvation that dominated the session.
2. **Serialize the DB (C):** flock + PID-scoped temp file on the poller.
3. **Close the cadence gap (D + E):** event-driven targeted re-poll of just-freed
   nodes on task completion; remove the need for manual `restart`.
4. **Resilience (F):** classify infra vs task failure, auto-resubmit infra failures.
5. **Ergonomics (G, H, I, K):** batch cancel/wait/reprioritize, validate pool presets.
