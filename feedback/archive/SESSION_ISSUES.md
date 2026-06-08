# hive-cli — Session Issues & Optimization Recommendations

Context: heavy multi-model vLLM workload, multiple GPUs per physical node via the
pre-allocated `highgpu_sleep` hold-job model (`~/1_gpu.slurm`, `--gres=gpu:1`,
24h walltime). Each hold-job = 1 GPU; physical nodes (e.g. `evc101`, `evc102`)
carried **7 distinct hold-jobs each** this session (confirmed in `queue.json`:
`running node->#distinct jobids: {evc101:7, evc102:7, evc103:3}`).

All issues below are grounded in the source at `/home/si384883/hive-cli/` and the
runtime state in `~/.hive/`. File:line references are to the source tree.

Code-vs-docs drift note up front: `docs/architecture.md` claims the daemon polls
"every 120s", but `libexec/hive-daemon:14` sets `POLL_INTERVAL=900` (15 min). The
15-minute reality makes every probe/state staleness problem below far worse than
the docs imply.

---

## Issue 1 — Node-monitor probe unreliable → dispatch stalls (MOST FREQUENT)

**Observed.** `hive nodes` / `node_monitor.json` diverged from real SLURM state.
Under cluster contention the GPU probe returned empty/None, the node was recorded
as `busy` or `unknown`, and the scheduler then saw ~0 idle nodes and refused to
dispatch while ~20 nodes were genuinely free. `hive poll` rebuilt the DB but still
marked everything not-idle. Single most frequent failure; dispatch stalled for
many minutes.

**Root cause.**
- The probe is a `srun --jobid=<hold> --overlap` running `nvidia-smi` inside the
  job (`hive-daemon:93-97`, `hive-poll:95-99`), with a 30s `timeout`. Under
  contention `srun --overlap` is slow to schedule the step or `nvidia-smi` returns
  nothing, so `raw` is empty.
- Status is derived purely from GPU numbers: `hive-daemon:144-151` /
  `hive-poll:165-172`:
  ```
  if   (( max_gpu_total == 0 )); then status="cpu"
  elif (( max_gpu_util >= 5 || max_gpu_mem >= 500 )); then status="busy"
  else status="idle"
  ```
  When the probe returns nothing, `max_gpu_total`/`util`/`mem` all stay 0 →
  `status="cpu"`, **not** `idle`. A CPU-classified node is never dispatchable.
- When the probe *frag file is missing entirely* (probe subshell killed / timed
  out), `hive-daemon:236-249` writes a fallback record with `status="unknown"`.
  `hive-poll` has **no** such fallback at all (`grep unknown hive-poll` → none); it
  silently drops the job from the DB.
- The scheduler only ever dispatches to `status == "idle"`:
  `hive-sched.py:108-114` `get_idle_nodes()` filters `info.get("status")=="idle"`.
  So `busy`, `cpu`, **and** `unknown` (the three values a failed probe produces)
  are all treated as non-dispatchable. A transient probe miss removes a free node
  from the dispatch pool for a **full 900s poll cycle**.

**Severity.** Critical — this is the core throughput killer.

**Fix.**
1. Distinguish *probe failure* from *measured state*. Never collapse "no data" to
   `cpu`/`busy`. Add an explicit `status="probe_failed"` when `raw` is empty, and
   in `hive-poll` add the same `unknown`/`probe_failed` fallback the daemon has
   (`hive-daemon:236-249`).
2. Make `get_idle_nodes()` (`hive-sched.py:108`) treat a hold-job as dispatchable
   when SLURM says it is allocated+idle, using `squeue`/cgroup as the source of
   truth and nvidia-smi only as a *zombie/occupancy* gate (see Issue 4). A hold-job
   with no running hive task and <5 GB GPU used is dispatchable regardless of
   whether the GPU-util probe succeeded.
3. Add retry/backoff on the probe (1 immediate re-probe before classifying), and
   carry-forward the last good state with an age stamp instead of overwriting a
   known-good `idle` with a `cpu`/`unknown` produced by a single failed `srun`.
4. Drop `POLL_INTERVAL` from 900s to ~60-120s (`hive-daemon:14`) so a transient
   misclassification self-heals in a minute, not 15.

---

## Issue 2 — GPU isolation broken for `srun --jobid=<hold> --overlap` (BIGGEST STRUCTURAL BLOCKER)

**Observed.** Each `highgpu_sleep` hold-job is `gres=gpu:1` with its own GPU UUID
(nvidia-smi inside the job shows a distinct GPU). But the `--overlap` step's actual
process landed on **physical GPU 0** of the node. Launching >1 vLLM per physical
node collided on GPU 0 and OOM'd, even though the jobids mapped to different GPUs.
This blocked multi-GPU-per-node work via the hold-job model. The only thing that
worked: an exclusive whole-node `sbatch --exclusive` + manual
`CUDA_VISIBLE_DEVICES` per process (whose epilog also reset the GPUs clean).

**Root cause.**
- `dispatch_task()` launches the task with
  `srun --jobid=<slurm_jobid> --overlap -n1 --mem=0 bash -c <wrapper>`
  (`hive-sched.py:175-182`). `--overlap` joins an existing allocation's step but,
  on this cluster, the overlap step does **not** inherit the hold-job's per-GPU
  cgroup device binding — so inside the wrapper CUDA enumerates from physical
  device 0. The wrapper (`hive-sched.py:143-169`) sets **no** `CUDA_VISIBLE_DEVICES`
  and passes no `--gres`/`--gpus` to the overlap step.
- The probe code already documents that it cannot trust per-step GPU scoping —
  `hive-poll:88-93` explicitly notes "CUDA_VISIBLE_DEVICES is unreliable under
  concurrent srun --overlap steps" and works around it for *reads* via
  `/proc/<pid>/cgroup`. The dispatcher never applies the equivalent fix for the
  *write* (workload launch) path, so every overlap task targets GPU 0.
- The hold-job model fundamentally cannot place N independent GPU jobs on N GPUs of
  one node via `--overlap` unless each step is pinned. The 7-hold-jobs-on-evc101
  layout (confirmed in `queue.json`) is exactly the case that collides.

**Severity.** Critical / structural.

**Fix (pick one, ideally both):**
1. **Pin the overlap step to the hold-job's GPU.** When the hold-job holds a single
   GPU, resolve that GPU's index/UUID from the job (e.g. probe
   `nvidia-smi --query-gpu=index,uuid` *inside the hold-job once* and cache it in
   `node_monitor.json`), then in the wrapper export
   `CUDA_VISIBLE_DEVICES=<that-uuid>` (UUID is collision-proof; index is not).
   Add `export CUDA_VISIBLE_DEVICES=...` to the wrapper at `hive-sched.py:143`.
   Prefer `--gpus=1`/`--gres=gpu:1` on the overlap srun if the site's SLURM honors
   it on overlap steps.
2. **Adopt the exclusive-node model that actually worked** as a first-class pool
   preset: a `--exclusive` whole-node hold-job, with hive assigning
   `CUDA_VISIBLE_DEVICES` per task round-robin across the node's GPUs and tracking
   per-GPU occupancy. This also gets the epilog GPU reset for free (see Issues 3/7).

---

## Issue 3 — Zombie processes survive task cancel and poison GPUs

**Observed.** Killed/crashed vLLM (OOM or scancel) left D-state processes holding
~76 GB GPU memory that survived `scancel`, `pkill -9 -u $USER`, and
`nvidia-smi --query-compute-apps=pid | xargs kill -9`. New tasks dispatched onto
those hold-jobs OOM'd immediately. Only full node deallocation (an exclusive job's
epilog) cleared them — and hold-jobs never deallocate, so the reset never fires.

**Root cause.**
- `cmd_cancel()` (`hive-queue.py:459-466`) only sends `SIGTERM` to the **local srun
  PID** on the login/sched host. It never reaches the compute-node process group,
  and never `scancel`s anything (the hold-job must survive). A vLLM in uninterruptible
  D-state ignores signals entirely; the GPU memory is only reclaimed on process
  teardown by the driver, which here only happens at job-epilog.
- The hold-job model is the structural cause: because the node never fully
  deallocates, the SLURM GPU-reset epilog that would clear leaked memory never runs.
  This is the flip side of Issue 2's "exclusive worked because the epilog reset GPUs".
- The wrapper's cleanup (`hive-sched.py:166-168`) only `kill $_HB_PID` + writes an
  exit file; it does not kill the workload's child process tree on the node.

**Severity.** High.

**Fix.**
1. On cancel/dead, kill the **whole process group on the compute node**, not the
   login-side srun PID: run a cleanup `srun --jobid=<id> --overlap` that does
   `pkill -9 -g <pgid>` / kills the recorded workload PID tree, and verify GPU mem
   dropped below threshold afterward.
2. Record the remote workload PID (the `$!` of `{cmd}` in the wrapper,
   `hive-sched.py:161`) into the heartbeat/exit metadata so cancel has a concrete
   target on the node, not just the local srun PID.
3. Treat D-state-survivors as a node-health failure: if GPU mem stays high after
   cleanup, mark the hold-job `poisoned` and stop dispatching to it (ties into
   Issue 4) until it's recycled.

---

## Issue 4 — No GPU-clean gate before dispatch

**Observed.** The scheduler dispatches a task onto a hold-job without checking the
GPU is actually free; if a zombie (Issue 3) occupies it, the task OOMs on startup.

**Root cause.** The dispatch loop `hive-sched.py:269-285` zips idle hold-jobs with
pending tasks and immediately calls `dispatch_task()`. The only precondition is
`status == "idle"` from `get_idle_nodes()` (`hive-sched.py:108-114`). There is **no
free-memory check** at dispatch time. The DB *does* carry per-GPU `mem_used`
(`gpu[].mem_used`, written at `hive-daemon:117` / `hive-poll:124`), but the
scheduler reads only `status`, never the memory number.

**Severity.** High (cheap to fix, prevents a whole class of OOM-on-startup).

**Fix.** In the dispatch loop, before launching, gate on the most recent
`gpu[].mem_used` for that hold-job: skip (and log a per-task reason) if
`mem_used > ~5 GB`. Optionally do a fast live re-probe of just that one job right
before dispatch instead of trusting the up-to-900s-old DB. This is a ~10-line guard
in `run_one_cycle()` around `hive-sched.py:269`.

---

## Issue 5 — Pool maintenance fights node-freeing

**Observed.** Cancelling a node's holds to free it for an exclusive allocation raced
against holds being re-added to maintain a target count, making it nearly impossible
to free a whole node on demand.

**Root cause / clarification from code.** There is currently **no auto-maintenance
daemon** in `hive-pool` — `cmd_add` (`hive-pool.py:80-118`) submits N holds once,
`cmd_release`/`--idle` (`hive-pool.py:121-142`) scancels them. The "fight" observed
is therefore between (a) the user/agent re-running `hive pool add` (or a wrapper/cron
maintaining a target count) and (b) the manual node-freeing — there is no
coordination or "drain" concept. Additionally `release --idle`
(`hive-pool.py:122-134`) keys off `node_monitor.json` `status=="idle"`, which is
exactly the field corrupted by Issue 1, so it frequently releases the wrong set or
nothing.

**Severity.** Medium.

**Fix.**
1. Add a **drain/reserve** state: `hive pool drain <node>` marks every hold on a
   physical node as "do-not-redispatch + scheduled for release", persisted in
   `pool_config.json`/queue so any maintenance logic respects it and won't re-add.
2. Make pool target-count maintenance (if/when added as a daemon) node-aware and
   honor the drain flag; never re-add holds to a draining node.
3. Fix `release --idle` to not depend on the unreliable `idle` status — cross-check
   against `squeue` + the GPU-clean gate (Issue 4).

---

## Issue 6 — FIFO queue clogging + dead-task churn

**Observed.** A backlog of 326 pending tasks (priority 0) from another experiment sat
ahead; the daemon repeatedly retried dead tasks (heartbeat timeout) and was slow to
backfill free nodes. `--priority N` let tasks jump the queue but dispatch still
stalled (Issue 1). Restarting the daemon did not reliably resume dispatch.

**Root cause.**
- Ordering is correct in principle: `hive-sched.py:264-267` sorts pending by
  `(-priority, submitted_at)`. So `--priority` *does* re-order. The stall was Issue 1
  (no node ever became `idle`), not the sort.
- Throughput is capped per cycle: dispatch only fills `min(len(idle_nodes),
  len(pending))` via `zip()` (`hive-sched.py:269`), once per 30s scheduler loop
  (`hive-sched.py:31 POLL_INTERVAL=30`). With most nodes mis-flagged, idle_nodes≈0.
- Dead-task churn: heartbeat timeout is 300s (`hive-sched.py:32`); a task with no
  heartbeat but a still-alive local srun PID is repeatedly returned `alive`
  (`check_task_status`, `hive-sched.py:213-218`), so genuinely-hung tasks can hold a
  slot for a long time before being declared dead.
- `queue.json` is never pruned: it currently holds **3606 tasks** (`done:1973
  cancelled:1311 failed:295`), a 3.2 MB file rewritten in full under flock on every
  state change (`save_queue`, `hive-sched.py:89-93`). This adds latency to every
  cycle and every `hive queue` command.
- Daemon restart not resuming: `is_sched_running()` (`hive-queue.py:200-219`) trusts
  the shared-FS heartbeat file for up to `HEARTBEAT_DAEMON_TIMEOUT=90s`; a restart
  can see a stale-but-recent heartbeat from the dying instance and decline to start.

**Severity.** Medium-High.

**Fix.**
1. Dispatch more aggressively per cycle and re-poll node state right after a
   completion rather than waiting the full 30s.
2. Tighten dead detection: don't treat "local srun PID alive" as proof of liveness
   when the heartbeat is stale past timeout; verify the remote workload via cgroup.
3. **Prune/archive** terminal tasks (e.g. keep last 24h in `queue.json`, move the
   rest to `queue.archive.jsonl`). A 3.2 MB hot file under a global flock is a
   self-inflicted latency tax.
4. Make daemon `restart` authoritative: on `start`, if the PID's host==self and the
   process is gone, ignore the stale heartbeat instead of deferring to it.

---

## Issue 7 — 24h walltime expiry mid-run

**Observed.** Hold-jobs hit walltime and were killed ("CANCELLED DUE TO TIME LIMIT"),
killing the running task; the monitor then idle-waited on the now-dead node. No
proactive replace-before-expiry.

**Root cause.** Hold-job walltime is `--time=24:00:00` (`~/1_gpu.slurm`). Nothing in
hive tracks remaining walltime or pre-empts it. The daemon reads `job_elapsed`
(`hive-daemon:90`, displayed only) but never computes *remaining* time, and there is
no replace-before-expiry logic anywhere in `hive-pool`/`hive-sched`. When the job
dies, the next poll simply drops it from `squeue -t R` output
(`hive-daemon:174`) and the scheduler quietly loses a node.

**Severity.** Medium.

**Fix.**
1. Bump hold-job walltime substantially and/or use `sleep infinity` if the site
   allows longer limits.
2. Add walltime awareness: store each hold-job's `TimeLimit`/`EndTime` (from
   `scontrol show job` / `squeue %L`), and when remaining < threshold, submit a
   replacement hold *before* expiry and **stop dispatching** new tasks onto the
   expiring one. This is the proactive-replace the maintenance daemon should own.
3. On detecting a hold-job that vanished from `squeue` while it owned a running hive
   task, mark that task `failed` with a distinct reason ("hold walltime expiry") so
   it can be auto-resubmitted rather than silently lost.

---

## Issue 8 — Observability gaps

**Observed.** Could not tell from `hive` *why* a task was PENDING (no per-task
reason); had to drop to `squeue`/`sacct`/`scontrol` repeatedly. `node_monitor`
`busy`/`unknown` hides probe-failure vs genuinely-busy. `hive nodes` lagged real
state.

**Root cause.**
- No per-task reason field: a pending task is just `state="pending"`
  (`hive-queue.py:319-335`); the scheduler logs dispatch decisions but never records
  *why a task was skipped* (no idle node / GPU dirty / priority). `hive queue list`
  (`hive-queue.py:351-442`) has no reason column.
- Probe-failure is indistinguishable from real state: `unknown`/`cpu`/`busy` all
  arise from a failed probe (Issue 1), and `hive nodes` renders `unknown` as `?????`
  (`hive-nodes:333-335`) without saying "probe failed N times".
- Lag is structural: `hive nodes` reads a DB refreshed only every 900s
  (`hive-daemon:14`); the STALE banner only triggers after **600s**
  (`hive-nodes:184`, `is_job_stale` at `:198`), so up to 10 min of staleness shows
  as fresh.

**Severity.** Medium (multiplies the cost of every other issue — you can't see what's
wrong).

**Fix.**
1. Add a `pending_reason` to each task, set by the scheduler each cycle
   ("no idle node", "all idle GPUs dirty", "blocked behind N higher-priority"), and
   surface it in `hive queue list`.
2. Add a `probe_ok`/`probe_attempts` field to each node record and render
   "PROBE-FAIL" distinctly from BUSY/IDLE in `hive-nodes`.
3. Lower poll interval (Issue 1) and the STALE threshold so the table reflects
   reality within ~1 min.

---

## Issue 9 — Hold-jobs write `slurm-<jobid>.out` to submission cwd

**Observed.** 75 `slurm-<jobid>.out` files cluttered the project root.

**Root cause.** The hold-job scripts (`~/1_gpu.slurm`, `~/1_normal_gpu.slurm`,
`~/1_cpu.slurm`) declare **no** `#SBATCH --output=`, so SLURM defaults to
`slurm-%j.out` in the directory where `sbatch` ran. `cmd_add`
(`hive-pool.py:109-118`) invokes `sbatch <script>` from the current cwd and never
injects an `--output`. Confirmed: 26 such files currently sit in
`/home/si384883/hive-cli/`.

**Severity.** Low (cosmetic, but real clutter; hold-job stdout is also where the
"CANCELLED DUE TO TIME LIMIT" of Issue 7 lands, so it shouldn't just be scattered).

**Fix.** Have `cmd_add` pass `--output=$HIVE_DIR/pool-logs/slurm-%j.out` (and
`--error=...`) on the `sbatch` line (`hive-pool.py:110-114`), and/or add
`#SBATCH --output=%x-%j.out` pointing at a logs dir in the example hold-job scripts.
Create the dir first.

---

## Prioritized Top Fixes

1. **Fix dispatch-blocking probe misclassification (Issue 1).** Never map "no probe
   data" to `cpu`/`busy`/`unknown`-and-skip; make SLURM allocation + a GPU-clean
   gate the dispatch criterion, add a probe retry, carry-forward last-good state, and
   cut `POLL_INTERVAL` 900→~90s. This alone recovers most of the lost throughput.

2. **Pin the overlap workload to its hold-job's GPU (Issue 2).** Export
   `CUDA_VISIBLE_DEVICES=<GPU-UUID>` in the dispatch wrapper (`hive-sched.py:143`),
   or promote the proven `--exclusive` whole-node + per-task `CUDA_VISIBLE_DEVICES`
   model to a first-class pool preset. Without this, multi-GPU-per-node is impossible.

3. **Add a pre-dispatch GPU-clean gate (Issue 4) + real remote cleanup on cancel
   (Issue 3).** Skip hold-jobs whose `gpu[].mem_used > ~5 GB`; on cancel/dead kill the
   compute-node process group and verify GPU mem dropped. Eliminates OOM-on-startup
   from zombies.

4. **Queue hygiene + dead-task detection (Issue 6).** Prune terminal tasks out of the
   3.2 MB hot `queue.json`, stop trusting a live local srun PID as liveness proof, and
   make daemon `restart` ignore stale heartbeats.

5. **Walltime-aware replace-before-expiry + drain state (Issues 7 & 5), plus
   observability (Issue 8) and log redirection (Issue 9).** Track hold-job EndTime and
   replace proactively; add a drain flag so freeing a node doesn't race re-adds; add
   `pending_reason` + `probe_ok` so the next stall is diagnosable from `hive` alone;
   send `slurm-%j.out` to a logs dir.
