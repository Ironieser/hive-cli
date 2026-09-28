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
                                # install Claude skill to ~/.claude/skills/hive/
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
   `hive queue daemon` / `hive-queue` (the user-facing CLI). It also owns the node
   health list (`libexec/hive_health.py`, user-facing `hive health`): verify-probe CUDA
   failures and fast CUDA-signature task failures quarantine a node; periodic probes
   release it.

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
- **A probe that ran and got no answer from the GPU is a verdict, not a miss.** `nvidia-smi`
  runs in the background under its own deadline (`gpu_query_shell` in `hive_health.py`,
  duplicated in the two bash pollers) because a wedged driver blocks it in uninterruptible
  sleep where `timeout` can't end it. The deadline is 60 s, not less: healthy normal-
  partition nodes answer in up to ~25 s, wedged ones took 116 s+. `gpu_unresponsive` / `no_gpu_devices` — only when
  SLURM granted a GPU (`CUDA_VISIBLE_DEVICES` set), so a CPU-only hold job is never at
  fault — strike the node and are never carried forward; an srun that couldn't run stays
  `probe_unverifiable`, never strikes, and backs off per hold job. Without this, three
  hold jobs sat 24 h with pending tasks (2026-09-26) while every probe timed out.
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
- **Node health is a separate, self-maintained list** (`libexec/hive_health.py`,
  `~/.hive/node_health.json`, keyed by physical node). Every dispatch is live-verified
  (`VERIFY_EVERY_DISPATCH`) with memory *and* a real CUDA-context probe; `fail`
  quarantines the node, `unknown` (no python/libcuda) **never** counts as a fault. Fast
  task failures count as strikes only with a CUDA-init signature in the log
  (`CUDA_FAULT_PATTERNS`) — keep those specific so a buggy command can't blacklist a
  node. The scheduler re-probes quarantined nodes and releases them itself; don't add a
  static blacklist. Offline tests stub the probe via `HIVE_CUDA_PROBE_CMD` (the mock
  cluster has no GPU) and the live reading via `MOCK_LIVE_GPU`.
- **New hold jobs stay off quarantined nodes.** `hive pool add` passes `--exclude` with
  every node on the health list (SLURM favours broken nodes — their GPUs are always
  free). A command-line `--exclude` *replaces* the script's `#SBATCH --exclude`, so
  `build_exclude()` re-reads the script's list and merges it; never pass a bare one.
  Task-level `exclude_nodes` (`hive submit --exclude`) is a task-level rejection
  (`node_excluded`, `i += 1`).
- **A quarantined node must have a way back that needs no hold job.** Since `pool add`
  excludes it, the through-a-hold-job check never runs there again. The scheduler's
  health step then calls `hh.check_without_hold_job`: release if `scontrol` reports a
  `BootTime` after the quarantine, else a `hive_canary` batch job pinned to the node
  (two probes; one outstanding per node; every `CANARY_INTERVAL_SECS`; opt out with
  `"health_canary": false` in `pool_config.json`). The pollers filter `hive_canary` out
  by job name, or a canary would show up as a hold job. The offline suite mocks
  `scontrol`/`sbatch`/`scancel` — keep it that way, the real ones are on `PATH`.
- **`--timeout` is enforced, `--est-runtime` is a hint.** Over `timeout_secs` of run time
  the scheduler SIGTERMs the step: `failed`, exit 124, `fail_reason: timeout`. It is the
  command's doing — never retried, never a strike against the node.
- **The notify hook runs in the scheduler, detached, after the outcome is recorded.**
  `notify(task, event)` is called on every terminal transition and on requeue; it must
  stay non-blocking (it runs inside the `queue.lock` cycle) and must never be able to
  change a task's state. Pending tasks cancelled by the CLI do not fire it.
- **GPU usage is sampled by the wrapper, reduced by the scheduler.** The dispatch wrapper
  keeps `heartbeat/<id>.usage` (`peak_mb max_util sum_util samples`, first `gpus` cards
  only); `collect_usage()` moves it onto the task at every terminal transition and the
  `finish` event carries it — that event is the history behind `hive stats` and
  `--need-mb auto`. Read `gpus` through `task_gpus()`: `task.get("gpus") or DEFAULT`
  turned an explicit 0 into 1.
- **Health probes of quarantined nodes run in background threads** (`start_health_probe`
  / `finished_health_probes`); the cycle starts one and applies its verdict on a later
  cycle. They target nodes known to be slow or wedged (60–150 s each) and, run inline,
  delayed dispatch by minutes on an idle pool.
  The tests set `hs.HEALTH_ASYNC = False` so one cycle yields one verdict.
- **No node is probed under `queue.lock`.** A cycle is two passes of `_cycle()`: pass 1
  reaps, applies the gates and notes which hold job each dispatchable task would take
  (`wanted`); `probe_nodes()` probes those in parallel with the lock released; pass 2 is
  a full pass again, on the queue as reloaded, and dispatches from `probe_cache`. Pass 2
  never probes: a task whose node is rejected there shows `verifying_node` and is tried
  next cycle. Everything in `_cycle` must therefore be safe to run twice in a row.
- **A task's finish time is its exit file's mtime**, not the cycle that noticed it.
- **Dependencies and concurrency caps are gates that need no node.** They are checked
  before the candidate loop (`waiting_for_dependency`, `array_limit`, `owner_limit`),
  never consume a candidate, and do not count towards the starvation watchdog. A
  failed/cancelled `--after` dependency fails the dependant without running it (exit
  125, `dependency_failed`); a dependency that was pruned is looked up in
  `events.jsonl`, and one hive knows nothing about counts as `done`. An array is N
  ordinary tasks sharing `array_id` — same `name`, so history and `auto` still group.
- **The user's command runs in a subshell** in the dispatch wrapper. Without it a
  command containing `exit N` ended the wrapper before the footer and the exit file
  were written, and the task surfaced as a crash orphan / "declared dead" (-1).
- **Queued hold jobs follow the quarantine list.** When the list grows the scheduler
  (background thread) adds the new nodes to `ExcNodeList` of the user's PENDING hold
  jobs via `scontrol update`. A hold job is recognised by its stdout being under
  `pool-logs/`; jobs submitted any other way are never modified. Add-only.
- **`slow` is a third node state, between ok and quarantined.** A node whose CUDA
  context is created, but in more than `CUDA_INIT_DEADLINE`, is `slow`
  (`hh.mark_slow`): it takes only tasks for which `hh.task_accepts_slow()` holds
  (`allow_slow`, else `est_runtime_secs` ≥ 1 h) — a task-level rejection, `node_slow` —
  and candidates are sorted so slow nodes come last, then by `prefer_partitions`.
  Verify-before-dispatch has a 60 s CUDA deadline and therefore quarantines such a node
  first; it is the periodic check, which waits `SLOW_CUDA_DEADLINE`, that finds the
  context does get created and reclassifies it. Verify on a slow node skips the CUDA
  probe. `pool add` does not exclude slow nodes.
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
  daemon/DB contract changes. The bundled Claude skill is `.claude/skills/hive/SKILL.md`
  (short, always loaded when it triggers) + `references/{cli,troubleshooting,state}.md`
  (read on demand). Keep SKILL.md under ~150 lines; put detail in references. Every
  user-facing behaviour change must be reflected there — other agents learn hive from it.
- **CLI output is sized for agent context.** `hive list` caps finished rows
  (`LIST_LIMIT_DEFAULT`), `hive wait` prints a log *tail* (`--log-lines`, default 40),
  `hive logs` tails logs over `LOG_FULL_MAX_LINES` (`-n` / `--full`). A 7-day history
  was 250+ lines and a `hive wait` once dumped 2,500 lines into an agent's context.
  Don't add unbounded output paths.
