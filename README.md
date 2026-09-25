# hive-cli

> Personal GPU node manager for agentic workflows on SLURM clusters. Pre-allocate a pool of nodes, then submit experiments with zero queue latency.

[中文文档](README_zh.md)

## ⚠️ Responsible Use on Shared Clusters

**hive-cli is for active, short-term debug sessions — not permanent resource reservation.**

Pre-allocating nodes on a shared HPC cluster affects everyone in the queue. Please follow these norms:

- **Release idle nodes promptly.** If `hive nodes` shows `IDLE` for 30–60 min and you're not actively iterating, run `hive pool release --idle` and return them.
- **Keep sessions short.** Hold jobs should last hours, not days.
- **Don't hoard during peak hours.** If the queue is long, reduce your pool size. One or two nodes is enough for most debug workflows.
- **Be transparent.** Your hold jobs are visible in `squeue` to all users.

> The goal is flow, not ownership. If you're not actively iterating, let the nodes go.

---

## Why

AI coding agents (Claude Code, Cursor, etc.) need tight iterate-debug-rerun loops. SLURM's queue latency (seconds to hours) breaks this. hive-cli pre-allocates GPU nodes as persistent sessions and provides a lightweight interface to schedule, monitor, and manage experiments — no queue wait between runs.

## Install

```bash
git clone git@github.com:Ironieser/hive-cli.git
cd hive-cli && bash install.sh
```

Installs to `~/.local/share/hive-cli/`, symlinks `hive` into `~/bin/`.

## Commands

### Pool management

```bash
hive pool init              # first-time setup: create ~/.hive/pool_config.json
hive pool add               # sbatch a new hold job (default preset)
hive pool add highgpu       # use a named preset
hive pool add ~/my.slurm    # pass a script path directly
hive pool add --count 3 --time 12:00:00   # 3 nodes, override wall time
hive health                 # bad-node quarantine list (self-maintained; see docs/status_model.md)
hive health report evc43    # quarantine a node now; hive re-probes it and releases it when healthy
hive pool release 584954    # scancel a specific hold job
hive pool release --idle    # scancel all idle hold jobs
hive pool config            # verify preset scripts exist
```

### Node monitoring

```bash
hive nodes                  # one-shot node status table (auto-starts daemon)
hive top                    # interactive live monitor (htop-style, q to quit)
hive poll                   # force immediate refresh
hive daemon start|stop|status|logs
```

```
  JOBID   NODE   PART     ELAPSED  STATUS  GPU%  MEM       LEFT    TASK
  ──────────────────────────────────────────────────────────────────────
  582228  n1     highgpu  3d13h    BUSY    87%   42G/80G   8h12m   python train.py ...
  584954  n2     normal   2h20m    IDLE     0%    0G/80G   21h40m  —
```

`LEFT` is the node's remaining walltime before SLURM reclaims it (red under an hour).

### Task queue

```bash
hive submit "python train.py --config exp/v1.yaml"      # submit a command (alias for `queue submit`)
hive submit job.hive                                     # submit a .hive script
hive submit --priority 10 --name train "python train.py"  # higher priority dispatches first (default 0)
hive submit --need-mb 40000 "python train_big.py"       # hold until ≥40 GB GPU mem is free
hive submit --est-runtime 2h "python train.py"          # runtime estimate → walltime-aware (or 'auto')
hive list                                               # queue: active tasks + last 10 finished
hive list --limit 50  |  --all  |  --state failed       # more history / everything / filter
hive logs 3 -n 100    |  --full  |  -f                  # tail / whole log / follow
hive wait 3                                             # block until done → print log TAIL, exit task's code
hive wait 3 --pending-timeout 600                       # give up (exit 75) if it never dispatches
hive stats [NAME]                                       # completed-run durations (min/median/P90/max)
hive cancel 3                                           # cancel a pending/running task
hive prune --older-than 7d                              # drop old terminal tasks (history kept in events.jsonl)
hive queue daemon start|stop|status|logs               # manage the scheduler (auto-started by submit)
```

`hive wait` exit codes for agents: **0** done · **1** failed · **75** never dispatched
(`--pending-timeout`) · **130** cancelled.

`.hive` script format (like `#SBATCH` directives):

```bash
#!/bin/bash
#HIVE workdir=/path/to/project
#HIVE priority=5           # higher = dispatched first (default 0)
#HIVE name=my-experiment   # also the key for runtime history (hive stats / --est-runtime auto)
#HIVE need_mb=25000        # optional: min free GPU MiB before dispatch
#HIVE est_runtime=2h       # optional: runtime estimate → won't place on a soon-expiring node
#HIVE gpus=1               # optional: GPUs the task may see (default 1; extras are hidden)

python train.py --config exp/v1.yaml
```

### SLURM queue dashboard

```bash
hive jobs           # your jobs
hive jobs -a        # all users
hive jobs -r        # running only
hive jobs -p gpu    # filter by partition
```

### Feedback

Hit a bug or rough edge in hive itself? File it for the repo maintainer to triage:

```bash
hive feedback "list 的 CMD 列被截断"                      # quick one-liner
hive feedback submit --title "OOM-blind dispatch" \
    --severity high --tags scheduler,oom --task 42       # structured
hive feedback list                                       # see what's filed
```

Each submit auto-captures the hive version + a queue/node snapshot. Reports land in
`feedback/inbox/`; see [`feedback/TRIAGE.md`](feedback/TRIAGE.md).

### Dispatch safety (free-mem gate)

The scheduler will **not** dispatch onto a GPU that already has >5 GB in use (a zombie
process or an out-of-band co-tenant). For large models, declare a minimum free-memory
requirement so a task waits for a genuinely free card instead of OOM'ing on startup:

```bash
hive submit --need-mb 25000 "python serve_big_model.py"   # or  #HIVE need_mb=25000
```

Pending tasks show *why* they haven't dispatched (e.g. `waiting_for_mem`, `gpu_dirty`)
in the NODE column of `hive list`. See [`docs/status_model.md`](docs/status_model.md).

### Walltime-aware scheduling & runtime history

Hold jobs expire. If a task carries a runtime estimate, the scheduler won't place it on
a node whose remaining walltime is below `estimate + 10 min` — it's held with reason
`insufficient_walltime` instead of being evicted mid-run:

```bash
hive submit --name train --est-runtime 4h "python train.py"   # 2h / 90m / 1-12:00:00 / 14400
hive submit --name train --est-runtime auto "python train.py" # P90 of this name's real history
hive stats train                                              # see that history (count/min/median/P90/max)
```

Estimates are optional — without one, scheduling is walltime-blind (unchanged). Each
finished task records its real **queued** and **run** durations to a durable append-only
log (`~/.hive/events.jsonl`), which is what `hive stats` and `--est-runtime auto` read —
so history survives `hive prune`.

### Resilience: node reclaim & checkpoint-loss

If a node is reclaimed **while your task is running**, hive auto-requeues the task onto
another live node. But SLURM cannot resume the process — it restarts from scratch — so
hive surfaces this loudly:

- `hive wait` prints a `⚠` line (and, for a held task, the blocking reason);
- `hive list` tags it `(re-disp xN)`;
- the task log gets a banner.

**Design every long job to be resumable**, and pass `--est-runtime` so the scheduler
avoids placing it on a soon-expiring node in the first place:

- **Training** — checkpoint periodically; load the latest checkpoint on start (`--resume`).
- **Inference / batch** — write outputs incrementally and make the run **idempotent**:
  on start, skip inputs that already have outputs, so a re-run only does what's missing.

A task whose *own* command crashes (node still alive) is marked `failed` and **not** retried.

### Keeping the queue tidy

```bash
hive prune --dry-run            # preview what would be removed
hive prune --older-than 7d      # drop terminal tasks finished >7d ago (default)
hive prune --keep 50            # or keep the 50 most-recent terminal tasks
```

`prune` never touches pending/running tasks; task logs are kept unless `--logs` is given.
Runtime history stays in `events.jsonl` regardless.

## Configuration

| File | Location | Purpose |
|---|---|---|
| `pool_config.json` | `~/.hive/` | Preset sbatch scripts (local only, not in git) |
| `node_monitor.json` | `~/.hive/` | Node state DB (written by daemon + hive-dbpost) |
| `queue.json` | `~/.hive/` | Live task queue (written by hive-sched; trimmed by `hive prune`) |
| `events.jsonl` | `~/.hive/` | Durable append-only task/timing history (runtime stats source) |
| `pool-logs/` | `~/.hive/` | Hold-job stdout (redirected by `hive pool add`) |
| `feedback/` | `<repo>/` | Feedback inbox + triage (tracked in git) |

| Variable | Default | |
|---|---|---|
| `HIVE_DIR` | `~/.hive/` | Config/DB directory |
| `HIVE_PYTHON` | auto-detected | Python interpreter |

## ⚠️ Responsible Use

hive-cli is for **active, short-term debug sessions** — not permanent resource reservation.

- Release idle nodes when you're done: `hive pool release --idle`
- Reduce pool size during peak hours
- Hold jobs are visible in `squeue` to everyone

> Clusters work best when shared resources are borrowed, not owned.

## For AI Agents

See **[docs/agent_guide.md](docs/agent_guide.md)** for a step-by-step guide on how to install, configure, and use hive-cli inside an AI agent session.

A **Claude Code skill** is included and installed automatically. Once installed, invoke `/hive status`, `/hive submit "cmd"`, `/hive wait <id>` etc. directly in Claude Code.

## Tests

A deterministic **offline** test suite (mock SLURM + a shadow `HIVE_DIR`; never touches
`~/.hive` or a real cluster) covers the pollers, scheduler, queue CLI, walltime gate,
node-reclaim requeue, event log, and prune:

```bash
bash tests/run.sh        # exit 0 = all passed
```

There is no CI; run this before installing after changes.

## Requirements

- SLURM with `srun --overlap` support
- NVIDIA GPUs + `nvidia-smi`
- Python 3.6+

## License

MIT
