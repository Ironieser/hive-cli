# Changelog

## [Unreleased]

### Added
- **Self-maintained bad-node list (`hive health`)** — feedback #13/#15/#16/#20/#23/#28/
  #29/#30: evc43/evc50 read IDLE (free memory, no processes) yet every task placed there
  died at CUDA init within a minute. nvidia-smi cannot see that; only creating a real CUDA
  context can. hive now keeps `~/.hive/node_health.json`, keyed by physical node:
  - **Verify-before-dispatch creates a CUDA context** (stdlib `ctypes` → `libcuda`, no
    torch needed) inside the same `srun --overlap` step as the memory read. A failure
    quarantines the node immediately (`pending_reason=cuda_unavailable_on_verify`);
    `unknown` (no python / libcuda) never counts as a fault. Verified live: healthy node
    → ok, evc43 → `cuCtxCreate=999`.
  - **Every dispatch is now live-verified**, not only uncertain nodes, so a stale poll can
    no longer place a task on a card a co-tenant filled since (feedback #14).
  - **Auto-quarantine from task outcomes**: a task that fails within 180 s with a
    CUDA-init signature in its log (`CUDA-capable device(s) is/are busy or unavailable`,
    `CUDA unknown error`, …) is a *strike* against the node; two strikes quarantine it and
    the task that tripped it is requeued elsewhere. A successful task clears strikes.
    Ordinary crashes and slow failures never count.
  - **Agents can seed it**: `hive health report <node> --reason …` quarantines at once.
  - **It heals itself**: the scheduler re-probes each quarantined node every 10 min
    through one of its hold jobs and releases it after 2 consecutive healthy probes
    (after a 1 h minimum hold); a failed probe re-arms it. `hive health check <node>`
    runs the probe now, `hive health clear <node>` releases manually.
  - `hive nodes` / `hive top` show quarantined nodes as **QUAR**; pending tasks that have
    only quarantined nodes left report `node_quarantined`.

### Fixed
- **Feedback filed through the installed copy was invisible and about to be deleted.**
  `hive feedback` resolved its storage relative to the script, so agents running
  `~/.local/share/hive-cli` wrote reports into *that* tree; 23 reports (#11–#33) piled up
  there unseen by the maintainer, and the next `install.sh` (`rsync --delete`) would have
  removed them. `install.sh` now records the source checkout in
  `<install-dir>/.source_checkout`, `hive feedback` files into it, the installer rescues
  any inbox entries left in the install dir, and `feedback/inbox/` is excluded from the
  delete sweep.
- **Scheduler: one unplaceable head-of-queue task starved everything behind it**
  (feedback #24/#25/#26). Candidate nodes were popped off the shared per-cycle list even
  when the rejection was *task-specific* (`insufficient_gpus`, `waiting_for_mem`,
  `insufficient_walltime`), so a `gpus=2` or 6-hour task at the front consumed every
  idle node and the rest reported `no_dispatchable_node` against a free pool. Task-level
  rejections now leave the node for the next task; only node-level ones (probe failed,
  busy on verify, dirty GPU) remove it. A node live-verified clean is also cached for the
  rest of the cycle instead of re-probed per task.
- **Timestamps were naive local strings mixed across time zones** (feedback #31). The
  scheduler, the poller and each agent's shell can run under different `TZ` (observed:
  EDT / Asia/Shanghai / CST), so `hive list` showed RUNNING tasks 12 h old seconds after
  dispatch and `queued_secs` went negative → `None`. Every queue timestamp now also gets
  a tz-independent epoch twin (`submitted_ts` / `started_ts` / `dispatched_ts` /
  `finished_ts`; events carry `t`) and all arithmetic prefers it. The `*_at` strings are
  kept for old readers; records without `*_ts` fall back to the old behaviour.
- **`hive cancel` of a running task only worked from the scheduler's own host.** It
  `os.kill`ed the srun PID locally — from another node the step kept the GPU while the
  task read "cancelled", and a same-number PID on the local node could be hit instead.
  Cancel now signals directly only when the scheduler is on this host; otherwise it sets
  `cancel_requested` and the scheduler (which owns the srun child) kills the step within
  one cycle, shown as `CANCELLING` in `hive list` meanwhile.
- **A single quote in the command broke the dispatch wrapper** (task #9128). The log
  header embedded `cmd`/`workdir` unquoted inside `echo '…'`; with a `'` plus any shell
  metacharacter bash refused the whole wrapper, no heartbeat ever appeared, and the task
  was retried as a crash orphan until declared dead. All user-controlled strings and
  paths in the wrapper are now `shlex`-quoted (the command itself still runs verbatim).
- **A dead task's hold job was assumed alive if the (up to 15-min-stale) node DB still
  listed it**, turning a node reclaim into a "self-crash" (`failed`, no retry) instead of
  an infra-failure requeue. `hold_job_alive` now always confirms with `squeue -j`; the DB
  is only the fallback when squeue can't run.
- **`hive poll` wrote one more job than the daemon**: its `squeue` format lacked the job
  name column, so the `cursor_ssh_proxy` filter never matched. Format and stdin handling
  now mirror `hive-daemon`.
- **`hive list | head` printed a BrokenPipeError traceback** (feedback #12). SIGPIPE is
  restored to its default so the command exits quietly.
- **`hive nodes` / `hive top` showed IDLE for a hold job that had a RUNNING hive task**
  whose GPU wasn't hot yet (cold import / model load / poll older than the dispatch), and
  hid the task name — read as "scheduler ignores idle nodes" and cost several false-alarm
  debug sessions (feedback #4/#7/#17/#32). Such rows now show **CLAIM** with the task,
  and the summary counts `claimed:` separately. Display-only: `node_monitor.json` is
  unchanged.
- **`warning` was a terminal node state → hold jobs permanently evicted from dispatch
  (feedback #10, dups #9/#6/#5).** `hive-dbpost` armed the busy→idle grace timer but
  never cleared `gpu_idle_since`, so the same branch re-entered every poll and pinned
  the job at `warning` for good; `hive-sched.get_candidates()` skipped `warning`, so any
  hold job that finished one task and sat idle >180 s stopped receiving work — surviving
  daemon restarts, because the state lives in `node_monitor.json`. Observed live as 7
  hold jobs with completely free GPUs while 20 tasks waited on `no_dispatchable_node`.
  The grace now expires back to `idle` with the timer cleared, and the scheduler treats
  `warning` as *uncertain* (verify-before-dispatch) rather than busy — which also lets a
  DB written by an older hive self-heal without hand-editing.
- **`hive nodes` counted `warning` as `busy`**, so a wedged pool still reported a
  plausible `busy: 18` when only 11 nodes were working. It's now its own `warn:` count.

### Added
- **`#HIVE gpus=N` / `--gpus N`** — how many GPUs a task may see, default **1**
  (feedback #8). A hold job can own several GPUs; handing all of them to a task that
  never asked made HF Trainer wrap the model in `nn.DataParallel` and die at step 0
  (`Expected all tensors to be on the same device, cuda:1 vs cuda:0`) after ~40 min of
  setup. The dispatch wrapper now keeps only the first N entries of the
  `CUDA_VISIBLE_DEVICES` SLURM handed the step — narrowing, never widening, so it can't
  name a device the hold job doesn't own. Multi-GPU is opt-in and gated: a task asking
  for N only places on a hold job that owns ≥N GPUs (new reason `insufficient_gpus`).
  Note this is a **behaviour change** for workloads that relied on seeing every GPU of a
  multi-GPU hold job — declare `#HIVE gpus=2` for those.
- **Task logs record actual GPU visibility** —
  `=== gpus: requested 1, visible=[0] (hold job provided [0,1]) ===` — turning a
  device-count surprise into one line instead of a mid-run mystery.

### Notes
- Investigated and **disproved** feedback #8's stated root cause (that `srun --overlap`
  steps don't inherit the hold-job's per-GPU cgroup and see the whole node). Measured on
  7 hold jobs across 2 partitions and 3 multi-tenant nodes: every step saw exactly its
  own card, with `SLURM_STEP_GPUS` matching `scontrol`'s allocated `IDX`. The real cause
  was a hold job submitted with `--gres=gpu:2`. No `--gres`/`--exact`/`--gpu-bind` flag
  is needed on the dispatch step.

## [0.4.0] - 2026-06-09

### Added (walltime-aware scheduling + checkpoint-loss notification — feedback C4)
- **Node remaining walltime tracked end-to-end**: the poller now records each hold
  job's `time_left_secs` (from `squeue %L`) into `node_monitor.json`, and `hive nodes`
  shows it as a new **LEFT** column (red when a node is within the hour of expiry).
- **Walltime-aware dispatch**: a task that carries a runtime estimate is never placed
  on a node whose remaining walltime is below `estimate + 10 min` — it's held PENDING
  with reason `insufficient_walltime` instead of being dispatched onto a node that will
  be reclaimed mid-run. Tasks *without* an estimate stay walltime-blind (no behaviour
  change). Unlimited (`-1`) and unknown (older poller) walltimes never block.
- **`--est-runtime` / `#HIVE est_runtime=`** — per-task runtime estimate. Accepts
  `2h`, `90m`, `1-12:00:00`, raw seconds, or `auto` (resolves to the **P90** of this
  task name's completed-run history). Logged in the task header at dispatch.
- **Durable event log `~/.hive/events.jsonl`** (`libexec/hive_events.py`) — an
  append-only JSONL record of every task lifecycle event (`submit` / `dispatch` /
  `finish` / `requeue` / `cancel`) with timestamps and **measured** durations. This is
  the authoritative timing history: it survives `queue.json` growth/pruning, appends are
  O(1), and writes are serialized by the existing `queue.lock`. The log is **bounded**
  (auto-trimmed to the recent tail past ~4 MB, like `roll_log`), so it can't grow without
  limit and reads stay cheap; `hive stats` / `--est-runtime auto` therefore reflect
  *recent* history. The `auto` history scan was also moved **out of** `queue.lock` so a
  large-history submit no longer blocks other queue operations.
- **Explicit timing feedback returned to the agent**: `hive wait` now prints the real
  measured numbers on completion — `✓ task #5 done · queued 3m12s · ran 1h12m · on evc23`
  (and `re-disp xN` if it was re-dispatched) — so agents use ground-truth durations
  rather than a guess. Stored per-task as `queued_secs` / `duration_secs`.
- **`hive stats [NAME]`** — completed-task runtime history (count / min / median / P90 /
  max) grouped by name, read from the durable event log (merged with any `done` tasks
  still in the queue), so an agent can pick an `--est-runtime` value (or pass `auto`,
  which uses the P90 of this name's **real** history).
- **`hive prune`** — trim terminal (done/failed/cancelled) tasks from `queue.json` to
  keep it small now that timing history is durable in `events.jsonl`. Never touches
  pending/running. `--older-than 24h|7d|0` (default 7d), `--keep N`, `--state`,
  `--dry-run`, and `--logs` (also delete the task log files; off by default). Cleans each
  pruned task's heartbeat/exit files; `hive logs <id>` still works on a pruned task by
  falling back to the deterministic log path.
- **Checkpoint-loss notification on requeue**: when a node is reclaimed mid-run, the
  task is now auto-requeued onto another live node (infra-failure path, distinct from a
  task's own crash, which still fails without retry). Because the new run does **not**
  resume from where it stopped, the loss is surfaced three ways: a durable banner in the
  task log, a `⚠` line in `hive wait`, and a `(re-disp xN)` marker in `hive list`.
  Tracked via new task fields `requeue_count` / `checkpoint_warning`.
- The task log header now records the node's **remaining walltime at dispatch** and the
  task's runtime estimate.
- **`hive wait` now surfaces *why* a task is held**, so a blocked task is perceived
  instead of waited-on blindly: it prints the `pending_reason` when it changes, and a
  prominent ⚠ for `insufficient_walltime` (which may never clear on its own — it tells
  the agent to add a longer-walltime node or lower `--est-runtime`). New
  `--pending-timeout SEC` stops waiting and exits **75** if the task never dispatches,
  so an agent can't block forever on an unschedulable task.
- **`hive list` redesign**: a scope line at the top makes the current view explicit
  (`queue · 2 running · 1 pending · since 06-01 (last 7d) · N older hidden (--all)`),
  and terminal tasks are **grouped by calendar date** (`Today 06-08` / `Yesterday` /
  `MM-DD`). Default window is now **last 7 days** (was 24h, done-only); `--days N`
  adjusts it, `--all` shows everything grouped, `--state` filters. The window now covers
  all terminal states (done/failed/cancelled), not just done.

### Added
- **`hive feedback`** — structured issue intake so other agents can file bugs/feature
  requests into `<repo>/feedback/inbox/` for the repo-owning agent to triage. Quick
  (`hive feedback "text"`) or structured (`submit --title/--severity/--tags/--task`);
  `list` / `show` / `triage` / `reindex`. Auto-captures hive version + queue/node
  snapshots + task-log tails. New `feedback/{TRIAGE,ROADMAP,INDEX}.md`. Prior session
  reports moved to `feedback/archive/`. (`libexec/hive-feedback`)
- **`#HIVE need_mb=` / `--need-mb`** — per-task minimum free GPU memory; the scheduler
  holds the task PENDING (`waiting_for_mem`) until a node has it free.
- **`docs/status_model.md`** — authoritative node-status & dispatch-gating reference.

### Safety
- **`hive pool release` now requires interactive human confirmation.** Releasing
  allocated nodes is irreversible (you re-enter the SLURM queue), so it **refuses to run
  non-interactively** (no TTY → an agent/script can never release nodes) and otherwise
  requires typing `release`. There is intentionally **no `--yes`/`--force`** and it must
  never be blanket-/"always"-authorized.

### Fixed (correctness — zombie srun masking finished tasks, found during the live soak)
- **A reclaimed task is no longer stuck `running` forever.** `check_task_status` /
  `_is_crash_orphan` used `os.kill(srun_pid, 0)` for liveness, but an exited-and-unreaped
  `srun` becomes a `<defunct>` zombie that still answers kill-0 — so when a node was
  reclaimed, the zombie kept the task "alive" and the infra-failure requeue never fired
  (observed: 3 tasks stuck after their evc104 hold jobs ended). The scheduler now reaps
  exited children each cycle (`os.waitpid(WNOHANG)`) and uses a zombie-aware `_pid_alive`
  (reads `/proc/<pid>/stat`, treats state `Z` as dead).

### Fixed (multi-instance safety — found via live multi-node test)
- **`daemon stop`/`restart` sweep is now `HIVE_DIR`-scoped**: the stale-daemon cleanup
  (`pgrep -f hive-daemon`, kill >60s old) previously killed **all** hive-daemon
  processes on the host regardless of `HIVE_DIR`, so stopping one instance could kill a
  different instance's poller (e.g. a test instance vs the production one). It now reads
  each candidate's `/proc/<pid>/environ` and only sweeps daemons whose `HIVE_DIR`
  matches this instance (defaulting to `~/.hive` when unset).

### Fixed (robustness — faster death detection / self-heal)
- **Same-host liveness is PID-authoritative**: `daemon_running` (poller),
  `is_sched_running` (scheduler), and `hive top`'s status now treat a recorded
  same-host daemon's live PID as the source of truth, so a killed daemon is detected
  immediately instead of looking alive for up to the heartbeat timeout (≤180s poller /
  ≤90s scheduler). Cross-node still uses the shared-FS heartbeat. This also unblocks an
  immediate restart after an unexpected death (the next `hive nodes`/`submit` re-spawns).

### Fixed (concurrency — daemon cold-start race, found via multi-agent test)
- **Atomic start mutex for both daemons**: simultaneous `hive submit` from many agents
  could each spawn a `hive-sched` (TOCTOU in `cmd_daemon start`). Added an flock on
  `sched.start.lock` / `node_monitor.start.lock` around check+start, and publish
  pid+heartbeat immediately after launch so racers bail. Verified: 8 concurrent agents
  → exactly 1 poller + 1 scheduler (was 1 poller + 4 schedulers).

### Fixed (feedback C5 — cluster-singleton daemons / multi-node)
- **Node poller is now a cluster-wide singleton** (feedback #1). Liveness no longer uses a
  host-local `kill -0`: `node_monitor.pid` is hostname-tagged (`<pid>\n<host>`) and the
  daemon writes a shared-FS `node_monitor.heartbeat`, so a poller started on any node is
  seen as alive from every other node — no more duplicate pollers each running
  `squeue` + `srun --overlap` (the "squeue chaos" with multiple agents/nodes).
- **Cross-node force-repoll / stop via request files**: `node_monitor.poll-request` (an
  immediate poll) and `node_monitor.stop-request` (stop a remote poller), honored in the
  daemon's 1s tick; SIGUSR1 kept as a same-host fast path. `hive-sched`'s starvation
  watchdog and `hive top`'s refresh now reach a poller on another node. `hive daemon
  status` shows the poller's host; `hive top` parses the new two-line PID file.

### Fixed (feedback Phase 1 — "stop the starvation")
- **Probe failure is now a distinct `probe_failed` status, never `cpu`/`busy`/dropped**
  (the dominant starvation bug — OPT-A/SESSION#1). Probes retry once; `hive-poll` now
  emits the same fallback record the daemon did.
- **Carry-forward of last-good state**: a transient probe miss no longer strands a node
  — it keeps its last good `idle`/`busy` for up to 30 min (`carried_forward`).
- **Shared post-processing `hive-dbpost`** — the warning-grace timer + carry-forward now
  run identically for `hive poll` and the daemon, ending their divergence.
- **Node-DB `flock` + PID-scoped temp file** — concurrent pollers no longer corrupt
  `node_monitor.json` (OPT-C).
- **Scheduler no longer dispatch-blind** (OPT-B/C2): dispatchability = SLURM-allocated +
  GPU-clean (≤5 GB used) + enough free mem; uncertain/stale/just-freed nodes get a live
  verify-probe first; a starvation watchdog forces a re-poll; freed nodes are reconsidered
  the same cycle; dispatch is crash-safe (claim saved before launch, orphans requeued).
- **`pending_reason`** surfaced in the `hive list` NODE column for pending tasks.
- **Crash-orphan age uses local time** (`_age_local`): `dispatched_at`/`started_at` are
  written by `now_iso()` in local time, so the orphan-grace check must not compare them
  as UTC (a tz-offset would inflate the age). Node-DB fields stay UTC. (Caught in testing.)
- **`hive nodes`** renders `probe_failed` as `PFAIL` and counts it separately (no longer
  miscounted as idle — OPT-J).

### Fixed (feedback Phase 0)
- **`hive pool add` validates the preset** with `sbatch --test-only` before submitting
  (`--no-validate` to skip), and redirects hold-job stdout to `~/.hive/pool-logs/`
  instead of dumping `slurm-<id>.out` into the cwd (OPT-I, SESSION#9). `hive pool config`
  flags SLURM-rejected presets.
- **Docs drift**: corrected the daemon poll interval from 120s → 900s across docs and the
  `hive top` / `hive nodes` display constants.

## [0.3.2] - 2026-04-23

### Fixed
- **daemon: stale process sweep on stop/restart** — `daemon_stop` now kills all
  `hive-daemon` main processes (elapsed > 60s) beyond the PID-file entry. Previously,
  repeated `hive daemon restart` left zombie daemons running from prior sessions;
  10 simultaneous daemons at 120s poll interval consumed ~5.9 srun steps/min per
  job, exhausting SLURM's MaxStepCount=40000 on long-running hold jobs.
- **nodes: read actual poll interval from daemon log** — `show_daemon_footer` no
  longer hard-codes 120s; it reads the `interval=N` value from the daemon's startup
  log line so "next poll in Xs" is accurate after the interval is changed.
- **daemon: increase default POLL_INTERVAL to 900s** — reduces srun step consumption
  from ~0.5/min (120s) to ~0.067/min (900s) per job, keeping well under MaxStepCount.

---

## [0.3.1] - 2026-04-15

### Added
- `hive pool add` now accepts a direct `.slurm` / `.sh` script path in addition to preset names
  - e.g. `hive pool add ~/1_normal_gpu.slurm --time 12:00:00`
  - Detection: path starting with `~`/`/`/`./`, ending in `.slurm`/`.sh`, or file exists → treated as script
- `docs/agent_guide.md` — step-by-step guide for AI agents using hive-cli

---

## [0.3.0] - 2026-04-15

### Added
- `hive pool` — node pool management subcommand
  - `add [preset] [--count N]` — submit hold jobs via user-configured presets
  - `release JOBID / --idle` — scancel specific or all idle hold jobs
  - `init` — create `~/.hive/pool_config.json` from example template
  - `config` — inspect presets and verify script paths
- `pool_config.example.json` — template for cluster-specific sbatch scripts
- `.gitignore` rule for `pool_config.json` (never committed)

### Changed
- Removed cluster-specific details from `docs/hive.md` (node names, QOS, partitions)

---

## [0.2.0] - 2026-04-15

### Added
- `hive queue` — SLURM-inspired task queue system
  - `submit "cmd"` or `submit job.hive` — two submission formats
  - `.hive` script format with `#HIVE` directives (workdir, priority, name)
  - `list [--state]` — queue table with state, node, elapsed, command
  - `cancel`, `logs [-f]`, `rm` — task lifecycle management
  - `daemon start|stop|status|logs` — manage `hive-sched`
- `hive-sched` — scheduler daemon (30s loop, flock-protected dispatch)
  - Reads `node_monitor.json` for idle nodes, dispatches via `srun --overlap`
  - Heartbeat monitoring: detects crashed tasks (5 min timeout → FAILED)
  - Cross-node daemon status via `sched.heartbeat` on shared filesystem
- Task logs at `~/.hive/logs/task-<id>.log` with header/footer and srun stderr capture

---

## [0.1.0] - 2026-04-15

### Added
- `hive` — unified dispatcher replacing scattered `myjob` / `mynode` / `jobtop` scripts
- `hive jobs [-a/-r/-w/-p]` — enhanced SLURM queue dashboard
- `hive nodes` — one-shot node status table (auto-starts daemon)
- `hive top` — interactive htop-style TUI with expandable process details
- `hive daemon start|stop|restart|status|logs` — background node poller (120s cycle)
- `hive poll` — immediate on-demand node probe (SIGUSR1)
- `~/.hive/node_monitor.json` — shared node state DB (readable from all nodes)
- Backward-compat symlinks: `myjob`, `mynode`, `jobtop` → `hive`
