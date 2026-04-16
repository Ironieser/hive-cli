# hive-cli Reference

Personal GPU node pool manager for SLURM clusters. Designed for agentic workflows where zero-latency experiment iteration is critical.

---

## Architecture Overview

```
                        ┌─────────────────────────────┐
                        │   ~/.hive/ (shared Lustre)   │
                        │                              │
  hive-daemon  ─poll──▶ │  node_monitor.json           │
  (120s loop)           │                              │
                        │  queue.json  ◀─ flock ─┐    │
  hive-sched   ─read──▶ │  queue.lock            │    │
  (30s loop)            │  sched.heartbeat       │    │
      │                 │  sched.pid             │    │
      └──dispatch──▶  srun --overlap ──▶ node    │    │
                                          │       │    │
                        │  heartbeat/<id>◀─┘     │    │
                        │  logs/task-<id>.log ◀──┘    │
                        └─────────────────────────────┘
                                    ▲
                        hive-queue  │  hive nodes/top
                        (CLI)       │  (read-only display)
```

**Key design points:**

- `~/.hive/` lives on the shared filesystem (Lustre/NFS) — readable/writable from any node
- `node_monitor.json` is updated every 120s by `hive-daemon` (one per user, on any node)
- `queue.json` is protected by `fcntl.flock` so concurrent agents can submit safely
- `hive-sched` dispatches tasks via `srun --overlap`, which enters the node's existing cgroup
- Heartbeat file is written every 30s inside the running task wrapper; scheduler detects crashes at 5 min silence

---

## Installation

```bash
git clone git@github.com:Ironieser/hive-cli.git
cd hive-cli && bash install.sh
```

- Installs to `~/.local/share/hive-cli/`
- Symlinks `~/bin/hive`
- Creates `~/.hive/`
- Backward-compat: `myjob` → `hive jobs`, `mynode` → `hive nodes`, `jobtop` → `hive top`

**Update:**

```bash
bash ~/.local/share/hive-cli/install.sh   # fetch + hard reset to origin/main
```

**Environment variables:**

| Variable | Default | Description |
|---|---|---|
| `HIVE_DIR` | `~/.hive/` | All state files live here |
| `HIVE_PYTHON` | auto (vlm → python3) | Python interpreter |

---

## `hive pool` — Node Pool Management

Manages the set of pre-allocated SLURM "hold jobs" that form your personal GPU pool.

### First-time setup

```bash
hive pool init
# creates ~/.hive/pool_config.json from pool_config.example.json
# then edit it to point to your cluster's sbatch scripts
```

`~/.hive/pool_config.json` format:

```json
{
  "default": "normal",
  "presets": {
    "normal":  { "script": "~/hold_normal.slurm",  "description": "H100 PCIe, 4 CPU 16G, 3d" },
    "highgpu": { "script": "~/hold_highgpu.slurm", "description": "H100 SXM, 4 CPU 64G, 24h" },
    "cpu":     { "script": "~/hold_cpu.slurm",     "description": "CPU only, 4 CPU 16G" }
  }
}
```

> This file is **local only** — `.gitignore`'d, never committed. The repo ships `pool_config.example.json` as a template.

A hold script is a regular sbatch script that sleeps indefinitely:

```bash
#!/bin/bash
#SBATCH --job-name=hold_node
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=4
#SBATCH --mem=16G
#SBATCH --time=3-00:00:00
#SBATCH -p normal
#SBATCH --gres=gpu:1

echo "Allocated Node: $SLURM_JOB_NODELIST"
sleep 365d
```

### Commands

```bash
hive pool init                           # create ~/.hive/pool_config.json
hive pool config                         # show all presets, verify script paths exist
hive pool add                            # sbatch one hold job (default preset)
hive pool add highgpu                    # use named preset
hive pool add ~/custom.slurm             # pass a script path directly (no preset needed)
hive pool add --count 3                  # submit 3 hold jobs
hive pool add --time 12:00:00            # override wall time in the script
hive pool add highgpu --count 2 --time 6:00:00
hive pool release 584954                 # scancel one hold job by SLURM job ID
hive pool release --idle                 # scancel all nodes currently showing IDLE
```

**`hive pool config` output:**

```
Pool config:  /home/user/.hive/pool_config.json

  normal  (default)
    script: /home/user/hold_normal.slurm  ✓
    desc:   H100 PCIe, normal partition, 4 CPU 16G, 3d
    time:   2-24:00:00  (override with --time)

  highgpu
    script: /home/user/hold_highgpu.slurm  ✓
    desc:   H100 SXM, highgpu partition, 4 CPU 64G, 24h
    time:   24:00:00  (override with --time)
```

> **Warning:** `hive pool release --idle` runs `scancel` on IDLE hold jobs. Only use it when you genuinely want to return nodes to the cluster queue. Never run it while experiments are being scheduled — a node shows IDLE only briefly between tasks.

---

## `hive nodes` / `hive top` / `hive poll` — Node Monitoring

### `hive nodes`

One-shot status table. Auto-starts `hive-daemon` if not running.

```bash
hive nodes
```

```
  JOBID   NODE    PART     ELAPSED  STATUS  GPU%  MEM        TASK
  ─────────────────────────────────────────────────────────────────
  582228  evc104  highgpu  3d13h    BUSY    87%   42G/80G    python train.py --config exp/v3.yaml
  584954  evc23   normal   2h20m    IDLE     0%    0G/80G    —
  584956  evc39   normal   5h10m    BUSY     2%    9G/80G    python eval.py --model ...
```

| Field | Description |
|---|---|
| JOBID | SLURM job ID of the hold job |
| NODE | Hostname |
| PART | Partition |
| ELAPSED | Hold job uptime |
| STATUS | `BUSY` / `IDLE` / `?????` (probe failed) |
| GPU% | GPU compute utilization (from nvidia-smi) |
| MEM | GPU memory used / total |
| TASK | Top process command (truncated) |

**STATUS logic:** `IDLE` = GPU util < 5% and no significant processes. `BUSY` = GPU active or experiment process detected. `?????` = srun probe timed out (node may be down or overloaded).

### `hive top`

Interactive curses TUI. Updates from `node_monitor.json` every 5s.

```bash
hive top
```

| Key | Action |
|---|---|
| `↑` `↓` / `j` `k` | Navigate rows |
| `Enter` / `Space` | Expand: full command + all PIDs + per-process GPU/mem |
| `Esc` | Collapse |
| `r` | Force immediate poll (SIGUSR1 to daemon) |
| `p` | Pause/resume auto-refresh |
| `q` | Quit |

### `hive poll`

Force one immediate probe of all nodes (same as pressing `r` in `hive top`):

```bash
hive poll
```

Sends SIGUSR1 to the running daemon. Results appear in `node_monitor.json` within a few seconds.

---

## `hive daemon` — Node Monitor Daemon

Background bash process that probes all hold jobs every 120s via `srun --overlap`.

```bash
hive daemon start      # start (auto-started by hive nodes if not running)
hive daemon stop       # graceful SIGTERM
hive daemon restart    # stop + start
hive daemon status     # PID, uptime, last poll time
hive daemon logs       # last 40 lines of daemon log
```

- PID file: `~/.hive/node_monitor.pid`
- Log file: `~/.hive/node_monitor.log` (rolling 500 lines)
- State DB: `~/.hive/node_monitor.json`

**Each probe** (per hold job):
1. `srun --jobid=<id> --overlap -n1 --mem=0 nvidia-smi ...` → GPU stats
2. `srun --jobid=<id> --overlap -n1 --mem=0 ps ...` → process list
3. Write results to `node_monitor.json` under a tmp+rename (atomic)

### `node_monitor.json` schema

```json
{
  "updated": "2026-04-15T08:13:31",
  "jobs": {
    "582228": {
      "node": "evc104",
      "partition": "highgpu",
      "job_elapsed": "3d13h",
      "gpu": [
        {"index": 0, "util": 87, "mem_used": 42301, "mem_total": 81920}
      ],
      "processes": [
        {"pid": 2868397, "cpu": 242.0, "mem": 5.4, "elapsed": "1d14h",
         "cmd": "python train.py --config exp/v3.yaml"}
      ],
      "status": "busy",
      "polled_at": "2026-04-15T08:13:31"
    }
  }
}
```

Read from Python:

```python
import json, os
db = json.load(open(os.path.expanduser("~/.hive/node_monitor.json")))
idle = [(jid, v["node"]) for jid, v in db["jobs"].items() if v["status"] == "idle"]
```

---

## `hive queue` — Task Queue

Automatic experiment dispatcher. Submit a command; the scheduler finds an IDLE node and runs it. No manual node hunting.

### Concepts

**Task state machine:**

```
pending → running → done
                 ↘ failed
       ↘ cancelled
```

- `pending`: waiting for an IDLE node
- `running`: dispatched to a node via `srun --overlap`, heartbeat active
- `done`: exited 0
- `failed`: exited non-zero, heartbeat timeout (>5 min), or srun error
- `cancelled`: cancelled by user before or during execution

**Scheduling loop (hive-sched, every 30s):**

1. Read `node_monitor.json` → find nodes with `status=idle`
2. Subtract jobids already occupied by `running` tasks in `queue.json`
3. Sort `pending` tasks by `priority DESC, submitted_at ASC`
4. Pair idle nodes with pending tasks; dispatch each under `flock`
5. For each `running` task: check heartbeat (>5 min stale → FAILED), check `.exit` file (task finished)

**Dispatch mechanism:** `srun --jobid=<slurm_id> --overlap -n1 --mem=0 bash -c <wrapper>`

The wrapper:
- Writes a log header (node, workdir, cmd, timestamp)
- `cd` to workdir (fails immediately if missing)
- Starts heartbeat subprocess (writes `~/.hive/heartbeat/<task_id>` every 30s)
- Runs `<cmd> >> task.log 2>&1`
- On exit: kills heartbeat, writes exit code to `~/.hive/heartbeat/<task_id>.exit`
- Writes log footer with exit code and timestamp

srun's own stderr (job expired, node unavailable, etc.) is also captured into the task log.

### Submit

**Inline command:**

```bash
hive queue submit "python train.py --config exp/v1.yaml"
# → Submitted task #4
```

**From a `.hive` script:**

```bash
# experiment.hive
#!/bin/bash
#HIVE workdir=/lustre/home/user/project
#HIVE priority=5
#HIVE name=train-v1

python train.py \
  --config exp/v1.yaml \
  --output results/v1
```

```bash
hive queue submit experiment.hive
# → Submitted task #5
```

`#HIVE` directives:

| Directive | Type | Default | Description |
|---|---|---|---|
| `workdir` | path | `$PWD` at submit time | Working directory on the node |
| `priority` | int | 0 | Higher = dispatched first |
| `name` | string | — | Label shown in `hive queue list` |

**CLI flags (override .hive directives):**

```bash
hive queue submit --workdir /path/to/project "cmd"
hive queue submit --priority 10 "urgent.sh"
hive queue submit --name my-run "cmd"
```

### List

```bash
hive queue list            # all tasks
hive queue list --state pending
hive queue list --state running
```

```
  ID  NAME      STATE    NODE    ELAPSED    CMD
  ────────────────────────────────────────────────────────────────
   5  train-v1  RUNNING  evc23   12m34s     python train.py --config ...
   6  (unnamed) PENDING  —       wait:3m    python eval.py --model ...
   3  (unnamed) DONE     evc39   1h30m12s   python train.py --config ...
   2  (unnamed) FAILED   evc36   3m02s      python bad_script.py
```

ELAPSED for `running` = wall time since started. For `done`/`failed` = total execution time. For `pending` = `wait:Xm` since submitted.

### Logs

```bash
hive queue logs 5         # print full log
hive queue logs 5 -f      # tail -f (live stream, Ctrl-C to stop)
```

Log location: `~/.hive/logs/task-<id>.log`

Log format:

```
=== hive task #5 started at 2026-04-15T10:01:05 ===
=== node: evc23  slurm_jobid: 584954 ===
=== workdir: /lustre/home/user/project ===
=== cmd: python train.py --config exp/v1.yaml ===

[... stdout + stderr from your command ...]

=== hive task #5 finished at 2026-04-15T11:31:22  exit_code=0 ===
```

If the node is gone or the SLURM job expired, srun's error message appears at the top (before the header).

### Wait (agent pattern)

Block until a task reaches a terminal state, then print its log and exit with the task's exit code:

```bash
hive queue wait <id>
hive queue wait <id> --no-log        # don't print log, just block and return exit code
hive queue wait <id> --interval 10   # poll every 10s (default: 5s)
```

Intended for chaining steps in agent workflows:

```bash
ID=$(hive queue submit "python train.py" | grep -oP '#\K\d+')
hive queue wait $ID || { echo "Train failed"; exit 1; }
hive queue submit "python eval.py --checkpoint results/best.pt"
```

### Cancel and Remove

```bash
hive queue cancel <id>    # PENDING → cancelled; RUNNING → kill srun_pid + cancelled
hive queue rm <id>        # delete record for DONE/FAILED/CANCELLED tasks
```

### Queue Daemon

```bash
hive queue daemon start    # start hive-sched (auto-started by submit if not running)
hive queue daemon stop
hive queue daemon status   # PID, host, last heartbeat time
hive queue daemon logs     # last 40 lines of sched.log
```

- PID file: `~/.hive/sched.pid`
- Log file: `~/.hive/sched.log` (rolling 500 lines)
- Heartbeat: `~/.hive/sched.heartbeat` (updated every 30s; cross-node liveness check)

`hive-sched` can run on any node — it reads/writes `~/.hive/` which is on shared storage. Multiple agents on different nodes can all submit to the same queue safely.

### `queue.json` schema

```json
{
  "version": 1,
  "next_id": 6,
  "tasks": {
    "5": {
      "id": 5,
      "name": "train-v1",
      "cmd": "python train.py --config exp/v1.yaml",
      "workdir": "/lustre/home/user/project",
      "state": "running",
      "priority": 5,
      "submitted_at": "2026-04-15T10:00:00",
      "started_at":   "2026-04-15T10:01:05",
      "finished_at":  null,
      "slurm_jobid":  "584954",
      "node":         "evc23",
      "srun_pid":     28741,
      "exit_code":    null,
      "log":          "/home/user/.hive/logs/task-5.log"
    }
  }
}
```

---

## `hive jobs` — SLURM Queue Dashboard

Enhanced `squeue` view for your own jobs (or all users).

```bash
hive jobs              # your jobs only
hive jobs -a           # all users
hive jobs -r           # running only
hive jobs -w           # pending only
hive jobs -p highgpu   # filter by partition
```

Output includes: job ID, partition, user, name, state, CPUs, memory, elapsed, time limit, node/reason.

Footer shows: idle nodes in each partition, QOS blocks, reasons your pending jobs are waiting.

---

## File Reference

```
~/.hive/
├── pool_config.json        # preset sbatch scripts (local only, not in git)
├── node_monitor.json       # live node state (written by hive-daemon every 120s)
├── node_monitor.pid        # hive-daemon PID
├── node_monitor.log        # daemon log (rolling 500 lines)
├── queue.json              # task queue DB (written by hive-sched + hive-queue)
├── queue.lock              # flock file — protects all queue.json writes
├── sched.pid               # hive-sched PID + host (two lines)
├── sched.log               # scheduler log (rolling 500 lines)
├── sched.heartbeat         # updated every 30s by hive-sched; cross-node liveness
├── heartbeat/
│   ├── <task_id>           # timestamp written every 30s by running task wrapper
│   └── <task_id>.exit      # exit code written when task finishes
└── logs/
    └── task-<id>.log       # stdout + stderr + header/footer for each task
```

---

## Troubleshooting

| Symptom | Diagnosis | Fix |
|---------|-----------|-----|
| `hive queue list` shows PENDING forever | `hive nodes` — any IDLE nodes? | `hive pool add` to expand pool |
| Pending stays >2 min with IDLE nodes | `hive queue daemon status` | `hive queue daemon start` |
| Log is empty | `hive queue logs <id>` — check first lines | srun error at top of log |
| Task FAILED immediately | Log says "cannot cd to workdir" | Check `--workdir` path exists on node |
| Task FAILED after running | `exit_code != 0` or heartbeat gap >5min | Check log for OOM / crash |
| Node shows `?????` | Daemon can't probe (node draining/down) | `hive poll` to retry; if persistent, the hold job may have expired |
| `hive nodes` shows stale data | Daemon stopped | `hive daemon start` |
| Multiple agents race-submitted, duplicate IDs | Shouldn't happen — flock prevents this | Check `queue.lock` permissions |
| `hive pool config` shows `✗ not found` | Script path wrong in `pool_config.json` | Edit `~/.hive/pool_config.json`, fix path |

---

## Requirements

- SLURM with `srun --overlap` support
- NVIDIA GPUs + `nvidia-smi`
- Python 3.6+
- Shared filesystem (`~/.hive/` accessible from all nodes)
