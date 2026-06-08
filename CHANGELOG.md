# Changelog

## [Unreleased]

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
