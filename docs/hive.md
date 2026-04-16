# hive-cli Skill

Personal GPU node pool manager for SLURM clusters. Designed for agentic coding workflows where zero-latency experiment iteration is critical.

## Quick Reference

```
hive jobs            # SLURM queue dashboard (your jobs)
hive jobs -a         # all users' jobs
hive nodes           # node pool status (one-shot, auto-starts daemon)
hive top             # interactive live monitor (curses, q to quit)
hive daemon start    # start background poller
hive daemon stop     # stop background poller
hive daemon status   # check if daemon is running
hive poll            # force immediate poll of all nodes
```

## Core Concept

hive pre-allocates GPU nodes as persistent SLURM sessions ("hold jobs"), then manages them as a personal pool. The background daemon (`hive-daemon`) probes each node every 120s via `srun --overlap`, collecting GPU utilization and running processes, writing results to `~/.hive/node_monitor.json`.

```
SLURM hold jobs (sleep 365d)
  └── hive-daemon polls every 120s
        └── ~/.hive/node_monitor.json
              ├── hive nodes  (one-shot table)
              └── hive top    (live TUI)
```

## Typical Agent Workflow

### 1. Check available nodes

```bash
hive nodes
# Output:
#   JOBID   NODE    PART     ELAPSED  STATUS  GPU%  MEM       TASK
#   582228  evc104  highgpu  3d13h    BUSY    87%   42G/80G   python tools/train.py ...
#   584954  evc23   normal   2h20m    IDLE     0%    0G/80G   —
#   584956  evc39   normal   2h20m    BUSY     2%    9G/80G   python tools/eval.py ...
```

### 2. Find a free slot

```bash
hive nodes | grep IDLE
# → 584954  evc23  normal  2h20m  IDLE  ...
```

### 3. Run experiment on that node

```bash
srun --jobid=584954 --pty bash
# now inside the node's cgroup (isolated GPU)
cd /your/project && python train.py --config ...
# or run in background:
nohup python train.py > logs/run.log 2>&1 &
```

### 4. Monitor progress

```bash
hive nodes          # quick status check
hive poll           # force refresh if just started something
hive top            # live TUI with expandable process details
```

### 5. Check what's running on a specific node

```bash
# In hive top: select the row, press Enter to expand
# Shows: GPU util, mem, all PIDs, full command, elapsed time
```

## `hive jobs` flags

```
hive jobs           # your jobs only
hive jobs -a        # all users
hive jobs -r        # running only
hive jobs -w        # pending only
hive jobs -p highgpu   # filter by partition
```

Output includes: job ID, partition, user, name, status, CPU, mem, elapsed, time remaining, node/reason.
Bottom section shows: idle H100 nodes, QOS CPU quota blocks, your pending job reasons.

## `hive nodes` output fields

| Field | Description |
|---|---|
| JOBID | SLURM job ID of the hold job |
| NODE | Hostname (evcXX) |
| PART | Partition (highgpu=H100 SXM, normal=H100 PCIe) |
| ELAPSED | How long the hold job has been running |
| STATUS | `BUSY` (experiment running) / `IDLE` (free to use) / `?????` (probe failed) |
| GPU% | GPU compute utilization |
| MEM | GPU memory used / total |
| TASK | Top process command (truncated) |

## `hive top` keyboard controls

| Key | Action |
|---|---|
| `↑` `↓` / `j` `k` | Navigate rows |
| `Enter` / `Space` | Expand row: full command + all processes |
| `Esc` | Collapse expanded row |
| `r` | Force poll now (sends SIGUSR1 to daemon) |
| `p` | Pause/resume auto-refresh |
| `q` | Quit |

## Daemon management

The daemon runs as a background bash process, polls every 120s, handles SIGUSR1 for on-demand polls, and rolls logs to 500 lines.

```bash
hive daemon start    # start (auto-started by hive nodes if not running)
hive daemon stop     # graceful SIGTERM
hive daemon restart  # stop + start
hive daemon status   # PID, uptime, last poll time
hive daemon logs     # tail -n 40 of daemon log
```

PID file: `~/.hive/node_monitor.pid`
Log file: `~/.hive/node_monitor.log`

## Node DB format (`~/.hive/node_monitor.json`)

Machine-readable, updated every poll cycle:

```json
{
  "updated": "2026-04-15T08:13:31",
  "jobs": {
    "582228": {
      "node": "evc104",
      "partition": "highgpu",
      "job_elapsed": "3d13h",
      "gpu": [{"index": 0, "util": 87, "mem_used": 42301, "mem_total": 81920}],
      "processes": [
        {"pid": 2868397, "cpu": 242.0, "mem": 5.4, "elapsed": "1d14h",
         "cmd": "python tools/train.py --config exp/v3.yaml"}
      ],
      "status": "busy",
      "polled_at": "2026-04-15T08:13:31"
    }
  }
}
```

Agents can parse this directly to make scheduling decisions:

```python
import json, os
db = json.load(open(os.path.expanduser("~/.hive/node_monitor.json")))
free_slots = [(jid, v) for jid, v in db["jobs"].items() if v["status"] == "idle"]
```

## Entering a node

Each hold job reserves one GPU via SLURM cgroups. Enter with:

```bash
srun --jobid=<job_id> --pty bash
```

Multiple sessions can attach to the same job (each sees the same GPU). Different jobs on the same node are fully isolated — each sees only its own GPU.

## Installation

```bash
git clone git@github.com:Ironieser/hive-cli.git
cd hive-cli && bash install.sh
```

Installs to `~/.local/share/hive-cli/`, symlinks `hive` to `~/bin/hive`.
Backward-compat symlinks: `myjob` → `hive jobs`, `mynode` → `hive nodes`, `jobtop` → `hive top`.

## `hive pool` — node pool management

```bash
hive pool init                          # create ~/.hive/pool_config.json (first-time setup)
hive pool config                        # show presets and verify script paths
hive pool add                           # sbatch one hold job (default preset)
hive pool add highgpu                   # use a named preset
hive pool add ~/hold.slurm              # pass a script path directly
hive pool add --count 3 --time 12:00:00 # submit 3 hold jobs, override wall time
hive pool release 584954                # scancel a specific hold job
hive pool release --idle                # scancel all currently IDLE hold jobs
```

### First-time setup

```bash
hive pool init
# then edit ~/.hive/pool_config.json:
```

```json
{
  "default": "normal",
  "presets": {
    "normal":  { "script": "~/1_normal_gpu.slurm", "description": "..." },
    "highgpu": { "script": "~/1_gpu.slurm",        "description": "..." }
  }
}
```

`~/.hive/pool_config.json` is **local only** — never committed to git.
The repo ships `pool_config.example.json` as a template.
