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
  JOBID   NODE   PART     ELAPSED  STATUS  GPU%  MEM       TASK
  ─────────────────────────────────────────────────────────────────
  582228  n1     highgpu  3d13h    BUSY    87%   42G/80G   python train.py ...
  584954  n2     normal   2h20m    IDLE     0%    0G/80G   —
```

### Task queue

```bash
hive queue submit "python train.py --config exp/v1.yaml"   # submit command
hive queue submit job.hive                                   # submit .hive script
hive queue list                                              # show queue
hive queue logs 3 -f                                         # follow task log
hive queue wait 3                                            # block until done, then print log + exit $?
hive queue cancel 3                                          # cancel task
hive queue daemon start|stop|status|logs
```

`.hive` script format (like `#SBATCH` directives):

```bash
#!/bin/bash
#HIVE workdir=/path/to/project
#HIVE priority=5
#HIVE name=my-experiment

python train.py --config exp/v1.yaml
```

### SLURM queue dashboard

```bash
hive jobs           # your jobs
hive jobs -a        # all users
hive jobs -r        # running only
hive jobs -p gpu    # filter by partition
```

## Configuration

| File | Location | Purpose |
|---|---|---|
| `pool_config.json` | `~/.hive/` | Preset sbatch scripts (local only, not in git) |
| `node_monitor.json` | `~/.hive/` | Node state DB (written by daemon) |
| `queue.json` | `~/.hive/` | Task queue DB (written by hive-sched) |

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

## Requirements

- SLURM with `srun --overlap` support
- NVIDIA GPUs + `nvidia-smi`
- Python 3.6+

## License

MIT
