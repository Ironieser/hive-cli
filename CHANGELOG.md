# Changelog

## [Unreleased]

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
