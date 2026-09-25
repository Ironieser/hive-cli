# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

hive-cli pre-allocates a pool of GPU nodes on a SLURM cluster as long-lived "hold"
jobs, then dispatches experiments onto those already-running allocations via
`srun --overlap` — eliminating SLURM queue latency for tight agentic iterate-debug
loops. There is no build step; it is a set of executable scripts symlinked onto `PATH`.

## Commands

```bash
bash install.sh                 # install to ~/.local/share/hive-cli, symlink hive→~/bin,
                                # copy Claude skill to ~/.claude/commands/hive.md
./hive <subcommand>             # run directly from the checkout (no install needed)
./hive help                     # full subcommand list
```

There is a **deterministic offline test suite** (`bash tests/run.sh`) — mock SLURM
binaries + a shadow `HIVE_DIR`, so it never touches `~/.hive`, a real cluster, or the
running daemons. It covers the pollers, scheduler dispatch/walltime-gate/reclaim, the
event log, and the queue CLI. Run it before `install.sh` after changes. There is **no
linter or CI**, so `tests/run.sh` is the regression net; also validate cluster-facing
changes against a live cluster (SLURM + `nvidia-smi` + running hold jobs). The `.out`
files and `*_NOTES.md` / `*_ISSUES.md` in the repo root are scratch, not part of the product.

## Architecture

`hive` (bash dispatcher) resolves its real path through symlinks, exports shared env
vars (`HIVE_DIR`, `DB_FILE`, `PID_FILE`, etc.), then `exec`s into one `libexec/hive-*`
script per subcommand. argv[0] compat routing maps the old `myjob`/`mynode`/`jobtop`
names to `jobs`/`nodes`/`top`. Bash scripts run directly; Python scripts (`top`,
`queue`, `pool`) are launched through a resolved interpreter (`HIVE_PYTHON`, else a
hard-coded conda fallback, else `python3`).

Two **independent background daemons**, each reading/writing JSON state under `~/.hive/`:

1. **Node poller** — `libexec/hive-daemon` (persistent, 900s loop) and
   `libexec/hive-poll` (one-shot, same probe logic). For each of the user's running
   SLURM jobs, runs `srun --jobid=<id> --overlap` to exec `nvidia-smi` + `ps` on the
   node, assembles a per-job JSON fragment, and atomically writes `node_monitor.json`
   (serialized by a `flock` on `node_monitor.json.lock` + PID-scoped temp file).
   Both pollers then call the shared **`libexec/hive-dbpost`** to apply the
   busy→idle warning timer and carry-forward of last-good state on probe failure —
   factored out so the daemon and `hive poll` can't drift apart. `hive nodes`/`hive
   top` only *read* the DB; they auto-start the daemon if absent. `hive poll`
   triggers an immediate refresh via SIGUSR1.

2. **Task scheduler** — `libexec/hive-sched` (30s loop). Reads `queue.json` +
   `node_monitor.json`, dispatches `pending` tasks via `srun --overlap`, and tracks
   liveness via heartbeat files under `~/.hive/heartbeat/`. Managed through
   `hive queue daemon` / `hive-queue` (the user-facing CLI).

The two daemons communicate **only** through `node_monitor.json` — the scheduler never
polls SLURM directly. It trusts the poller's DB for *fresh, idle* nodes, but for
uncertain ones (`probe_failed`/`unknown`/`carried_forward`/stale/just-freed) it does a
**live verify probe before dispatching**, so a single stale/failed poll can't strand a
node.

**Both daemons are cluster-wide singletons.** State lives in shared-FS `~/.hive`, and
agents may invoke hive from *different* nodes, so liveness must not use a host-local
`kill -0`. Both write a hostname-tagged PID file (`<pid>\n<host>`) plus a shared-FS
heartbeat (`sched.heartbeat`, `node_monitor.heartbeat`); "is it running" = heartbeat
fresh (any node) or same-host PID alive. Cross-node control uses request files
(`node_monitor.poll-request` to force a re-poll, `node_monitor.stop-request` to stop a
remote poller); SIGUSR1 is only a same-host fast path. Without this, every node that
ran `hive nodes` spawned its own poller → N× `squeue`/`srun` step blowup (feedback C5).

A third user-facing tool, **`libexec/hive-feedback`**, lets other agents file issues
into `<repo>/feedback/inbox/` for the repo-owning agent to triage (see `feedback/`).
Agents run the *installed* copy, so `install.sh` writes `<install-dir>/.source_checkout`
and `hive-feedback` files into that checkout; the installer also rescues inbox entries
left in the install dir and excludes `feedback/inbox/` from its `rsync --delete`. Check
`hive feedback list --status open` from the checkout at the start of a maintenance pass.

### Key invariants when editing

- **Status is GPU-based, not process-based.** A job is `busy` iff GPU util ≥ 5 or GPU
  mem ≥ 500 MB; `cpu` if no GPU; `probe_failed` if the `srun --overlap` probe couldn't
  run (empty / non-zero / missing `---PS---` marker — retried once first); else `idle`.
  **Never collapse a probe failure into `cpu`/`busy`** — that strands idle GPUs. Process
  probing filters PIDs by `/proc/<pid>/cgroup` containing `/job_<jobid>/`. The probe
  body is duplicated in `hive-daemon` and `hive-poll` — keep them in sync; the
  *post-processing* (warning timer + carry-forward) is shared via `hive-dbpost`.
- `hive-dbpost` carries a `probe_failed` job forward to its last-good state (flagged
  `carried_forward`) for up to 30 min, so one transient probe miss doesn't drop a node.
- A `busy→idle` transition first becomes `warning` (held in `gpu_idle_since`) before
  flipping to `idle`, so brief GPU dips don't churn the table. **`warning` is transient,
  never terminal** — when the grace expires, `hive-dbpost` must set `idle` *and clear
  `gpu_idle_since`*, or the job re-enters that branch every poll and is pinned at
  `warning` for good. The scheduler treats `warning` as *uncertain* (verify-before-
  dispatch), not as `busy`, so a stale DB self-heals and the grace window never costs
  throughput. Never make `warning` a hard skip in `get_candidates()` (feedback #10).
- **Scheduler dispatch is gated, not status-blind.** `hive-sched` only dispatches onto a
  GPU with ≤ 5 GB used (`GPU_CLEAN_MB`) and ≥ the task's `need_mb` free; uncertain nodes
  get a live verify-probe first; a starvation watchdog SIGUSR1s the poller after 3
  starved cycles; tasks that can't place get a `pending_reason`. Dispatch is crash-safe
  (claim saved before the `srun` launch; orphans without heartbeat/exit are requeued).
- **Only node-level rejections consume a dispatch candidate.** `candidates` is shared by
  every pending task in a cycle. `probe_unverifiable` / `node_busy_on_verify` /
  `gpu_dirty` describe the node → pop it; `insufficient_gpus` / `waiting_for_mem` /
  `insufficient_walltime` describe the task → leave it (`i += 1`). Popping on task-level
  rejections let one unplaceable head-of-queue task starve the whole queue with
  `no_dispatchable_node` (feedback #24–#26).
- **Time arithmetic uses the `*_ts` epoch fields, never the `*_at` strings.** The
  scheduler, poller and each agent's shell run under different `TZ`s; `*_at` is naive
  local for display only. Write both on every state change (`now_iso()` + `now_ts()`);
  read via `_task_epoch` / `task_epoch`, which fall back to `*_at` for pre-0.4.1 records.
- **Never `os.kill` an srun PID from a node other than the scheduler's.** `hive cancel`
  sets `cancel_requested` and the scheduler (owner of the srun child) kills the step; the
  CLI only signals directly when `sched.pid`'s host is the local host.
- **Everything user-controlled in the dispatch wrapper is `shlex.quote`d** (cmd in the
  header, workdir, log/heartbeat paths). The command line itself runs verbatim.
- **Walltime-aware placement is opt-in per task.** The poller records each hold-job's
  `time_left_secs` (`squeue %L`) — measured every cycle even on probe failure, so its
  basis is the DB's top-level `updated`, **not** per-job `polled_at` (which carry-forward
  pins). A task with an `est_runtime_secs` is held `insufficient_walltime` rather than
  placed on a node expiring within `est + WALLTIME_MARGIN_SECS`; a task *without* an
  estimate stays walltime-blind, so existing behaviour is unchanged. `-1`=unlimited and
  `null`=unknown never block.
- **GPU visibility is narrowed, never widened.** SLURM's cgroup already scopes an
  `srun --overlap` step to the hold-job's own GPUs, renumbered `0..N-1` — verified on
  multi-tenant nodes, so hive never needs `--gres`/`--gpu-bind` on the step. But a hold
  job may own *more* GPUs than the task wants, and a framework that auto-parallelizes
  over every visible device then crashes (feedback #8). So the dispatch wrapper keeps
  only the first `gpus` entries of whatever SLURM handed it (`cut -d, -f1-N` on
  `CUDA_VISIBLE_DEVICES`) — subsetting the existing value, so it can never name a device
  the job doesn't own, and it works for index *or* UUID form. Default `gpus`=1; a task
  asking for N only places on a hold job with ≥N GPUs (`insufficient_gpus`).
  `DEFAULT_TASK_GPUS` is duplicated in `hive-queue` and `hive-sched` — keep them equal,
  since tasks queued by an older hive carry no `gpus` key and are read with the default.
- **Infra failure ≠ self-crash.** A dead `running` task whose hold-job is **gone**
  (confirmed by `squeue -j`) is requeued to another node (`requeue_count`++,
  `checkpoint_warning`, log banner, `hive wait` ⚠) — the new run starts fresh. A dead
  task whose hold-job is **still alive** is a self-crash → `failed`, no retry (don't loop
  a buggy command). Both capped at `MAX_ATTEMPTS`. Never auto-retry the alive-hold-job case.
- All queue mutations go through `flock` on `queue.lock`; the node DB has its own `flock`
  on `node_monitor.json.lock`. `nodes`/`queue`/`top` treat `queue.json` as authoritative
  for the TASK column.
- Daemon stop/restart sweeps **all** stale `hive-daemon` processes (>60s old), not just
  the PID-file entry — multiple zombie daemons each burn `srun` steps and can exhaust
  SLURM's `MaxStepCount`. This is why the poll interval is 900s, not seconds.
- **Schema changes must be additive + default-safe** (read with `.get(...)` defaults):
  the live daemons run the *installed* copy and write files the checkout's newer code
  must still parse, and vice-versa. Roll out via `install.sh`, then restart daemons.

### Configuration (`~/.hive/`, all gitignored)

| File | Written by | Read by |
|---|---|---|
| `pool_config.json` | user (`pool init`) | `hive-pool` — cluster-specific sbatch presets |
| `node_monitor.json` | poller daemon (+`hive-dbpost`) | `nodes`, `top`, `sched` |
| `queue.json` | `hive-queue` / `hive-sched` | `sched`, `nodes`, `top` |
| `events.jsonl` | `hive-queue` / `hive-sched` (via `hive_events.py`) | `hive stats`, `--est-runtime auto` |
| `pool-logs/slurm-<id>.out` | SLURM (via `pool add --output`) | hold-job stdout/stderr |

`events.jsonl` is an **append-only** durable lifecycle/timing log (submit / dispatch /
finish / requeue / cancel, with measured `run_secs`/`queued_secs`). It's the history
source for `hive stats` and `--est-runtime auto`, survives `queue.json` pruning, and its
appends are serialized by `queue.lock` (every emitter already holds it) — so do **not**
add a separate lock, and only emit events from inside a `QueueLock` section. Because this
log is the durable record, `hive prune` can safely drop terminal tasks from
`queue.json` (it never touches pending/running; keeps task logs unless `--logs`).

Feedback (filed via `hive feedback`) lives in `<repo>/feedback/` — tracked in git, not
in `~/.hive`. Inbox entries are untracked-friendly (`install.sh` never runs `git clean`,
so they survive `git reset --hard` on upgrade). See `feedback/TRIAGE.md` + `ROADMAP.md`.

`pool_config.json` holds the only cluster-specific details (node names, partitions, QOS,
sbatch scripts) and is **never committed** — `pool_config.example.json` is the template.
Keep the rest of the repo cluster-agnostic.

## Task model

`hive queue submit` accepts either a raw command string or a `.hive` script. `.hive`
files use `#HIVE key=value` directives (`workdir`, `priority`, `name`, `need_mb`, `gpus`)
analogous to `#SBATCH`. Task states: `pending → running → done|failed|cancelled`.
Pending tasks carry a scheduler-set `pending_reason` (surfaced in the `hive list` NODE
column) explaining why they haven't dispatched. Logs land in
`~/.hive/logs/task-<id>.log` (header/footer + captured srun stderr). `hive submit`/
`wait`/`list`/`logs`/`cancel` are dispatcher shortcuts for the `queue` subcommands.

## Conventions

- New subcommand: add a `case` arm in `hive`, create `libexec/hive-<name>`, `chmod +x`,
  and add it to the `chmod` loop / help text in `install.sh` and `hive`.
- User-facing strings mix English and Simplified Chinese (`hive-jobs` help is zh-CN);
  match the surrounding file. README has a `README_zh.md` counterpart.
- `docs/architecture.md` documents the data flow and JSON schemas; update it when the
  daemon/DB contract changes. The bundled Claude skill lives at `.claude/commands/hive.md`.
