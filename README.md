# hive-cli

> A lightweight GPU node manager designed for **agentic coding workflows** on SLURM clusters.

## Motivation

Modern AI coding agents (Claude Code, Cursor, etc.) operate in tight iterate-debug-rerun loops. Traditional SLURM job submission introduces significant friction in this workflow:

- Each experiment requires a new `sbatch` submission and queue wait
- Queue latency (seconds to hours) completely breaks an agent's continuous testing flow
- Agents lose context between job submissions, degrading multi-step debugging quality
- `squeue` / `sacct` output is too verbose for agents to quickly assess GPU availability

**hive-cli** solves this by pre-allocating a pool of GPU nodes as persistent sessions, then providing agents with a lightweight interface to submit, monitor, and manage experiments on those nodes — with zero queue latency between runs.

The result: agents can iterate on code the same way a developer would on a local GPU workstation, while still running on a shared HPC cluster.

## Architecture

```
SLURM cluster
  └── Pre-allocated node pool (long-running hold jobs)
        └── hive daemon  (background monitor, polls every 2 min)
              └── ~/.hive/node_monitor.json  (real-time node state DB)
                    └── hive top / hive nodes  (agent-readable status)
```

Agents use `hive nodes` to find a free slot, run their experiment via `srun --jobid=<id>`, and check status via `hive top` — all without touching the SLURM queue.

## Installation

```bash
git clone git@github.com:Ironieser/hive-cli.git
cd hive-cli
bash install.sh
```

`install.sh` will:
- Install to `~/.local/share/hive-cli/`
- Symlink `hive` into `~/bin/`
- Migrate any existing monitor data to `~/.hive/`

## Usage

```
hive <subcommand> [options]
```

### `hive jobs` — SLURM queue dashboard

```bash
hive jobs           # your jobs only
hive jobs -a        # all users
hive jobs -r        # running only
hive jobs -w        # pending only
hive jobs -p highgpu
```

Enhanced `squeue` view with computed time remaining, idle H100 node detection, and QOS quota blocking status.

### `hive nodes` — node pool status

```bash
hive nodes          # show node table (auto-starts daemon)
hive nodes -w       # watch mode, refresh every 60s
```

Shows each pre-allocated node's real-time status: GPU utilization, memory usage, running processes, and elapsed time — by actively probing each node via `srun --overlap`.

```
  JOBID     NODE     PART       ELAPSED    STATUS   GPU%  MEM         TASK
  ──────────────────────────────────────────────────────────────────────────
  582228    evc104   highgpu    3d13h      BUSY     87%   42G/80G     python tools/train.py --config ...
  584954    evc23    normal     2h20m      IDLE      0%    0G/80G     —
  584956    evc39    normal     2h20m      BUSY      2%    9G/80G     python tools/eval.py --model ...
```

### `hive top` — interactive TUI

```bash
hive top
```

htop-style live monitor. Navigate with `↑↓`, press `Enter` to expand a node and see full process list and commands, `r` to refresh, `q` to quit.

### `hive daemon` — background poller

```bash
hive daemon start
hive daemon stop
hive daemon restart
hive daemon status
hive daemon logs
```

The daemon probes all active nodes every 120 seconds and writes results to `~/.hive/node_monitor.json`. `hive nodes` and `hive top` read from this file instantly.

### `hive poll` — immediate refresh

```bash
hive poll
```

Triggers the daemon to re-probe all nodes right now (sends SIGUSR1). Useful after starting a new experiment.

## Agent Integration

Agents can use `hive-cli` to manage their own experiment queue:

```bash
# Find a free node
hive nodes | grep IDLE

# Enter the node and run experiment
srun --jobid=<job_id> --pty bash
# ... run your experiment ...

# Check status from outside
hive nodes
hive top
```

The `~/.hive/node_monitor.json` DB is machine-readable and can be parsed directly by agents to make scheduling decisions.

## Node Registry

For multi-agent environments, a shared `node_registry.json` prevents conflicts when multiple agent sessions compete for the same GPU slots. Each agent marks a slot as `in_use` before running and resets it to `free` on completion.

## Configuration

| Environment variable | Default | Description |
|---|---|---|
| `HIVE_DIR` | `~/.hive/` | Config and DB directory |
| `HIVE_PYTHON` | auto-detected | Python interpreter for `hive top` |

## Cluster Support

Currently configured for clusters with:
- SLURM workload manager
- NVIDIA GPUs with `nvidia-smi`
- `srun --overlap` support for non-exclusive job probing

Tested on evc cluster with H100 SXM/PCIe nodes.

## Author

[Ironieser](https://github.com/Ironieser) — ironieser@gmail.com

## License

MIT
