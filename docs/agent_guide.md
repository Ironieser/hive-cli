# hive-cli Agent Guide

This guide is written for **AI coding agents** (Claude Code, Cursor, etc.) running inside a SLURM cluster session. It covers installation, first-time setup, and the full workflow for submitting and monitoring experiments without queue latency.

---

## Prerequisites

- Access to a SLURM cluster with `srun --overlap` support
- NVIDIA GPUs + `nvidia-smi`
- Python 3.6+ (a conda env with PyTorch works)
- Your `~/bin` is on `$PATH` (standard on most HPC setups)

---

## Claude Code Skill

hive-cli ships a Claude Code skill at `.claude/commands/hive.md`. It is installed automatically by `install.sh` to `~/.claude/commands/hive.md`.

Once installed, invoke it in any Claude Code session with:

```
/hive status          # check node pool + queue
/hive submit "cmd"    # submit an experiment
/hive wait <id>       # block until task done, show log
/hive logs <id>       # read task log
/hive cancel <id>     # cancel a task
/hive add             # add a node to the pool
/hive release --idle  # return idle nodes to cluster
```

The skill contains actionable instructions for Claude: exact commands to run, critical rules (don't scancel hold jobs), chaining patterns, and troubleshooting quick-reference.

To install the skill manually without running `install.sh`:

```bash
mkdir -p ~/.claude/commands
cp ~/.local/share/hive-cli/.claude/commands/hive.md ~/.claude/commands/hive.md
```

---

## Installation

### One-line install

```bash
git clone git@github.com:Ironieser/hive-cli.git
cd hive-cli && bash install.sh
```

This:
- Copies files to `~/.local/share/hive-cli/`
- Creates `~/bin/hive` symlink
- Creates `~/.hive/` state directory

Verify the install:

```bash
hive --help
# or if ~/bin isn't on PATH yet:
~/.local/share/hive-cli/hive --help
```

### Update to latest

```bash
bash ~/.local/share/hive-cli/install.sh
# install.sh detects an existing install and does git fetch + hard reset
```

---

## First-Time Setup

### 1. Configure node pool presets

```bash
hive pool init
# → creates ~/.hive/pool_config.json from the bundled example
```

Edit `~/.hive/pool_config.json` to point to your cluster's sbatch scripts:

```json
{
  "default": "normal",
  "presets": {
    "normal":  { "script": "~/hold_normal.slurm",  "description": "H100 PCIe, normal partition, 4 CPU 16G, 3d" },
    "highgpu": { "script": "~/hold_highgpu.slurm", "description": "H100 SXM, highgpu partition, 4 CPU 64G, 24h" }
  }
}
```

Each preset script is a regular sbatch script that sleeps indefinitely to hold the node:

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

> `~/.hive/pool_config.json` is **never committed to git** — it's cluster-specific and local only.

### 2. Add a node to your pool

```bash
hive pool add                         # submit one hold job (default preset)
hive pool add highgpu                 # use a named preset
hive pool add ~/hold_normal.slurm     # pass a script path directly
hive pool add --count 2               # submit 2 hold jobs at once
hive pool add --time 12:00:00         # override wall time
```

Check that it landed:

```bash
hive pool config    # verify script paths are valid
squeue -u $USER     # confirm the hold job appears (state: R or PD)
```

### 3. Start the node monitor daemon

```bash
hive daemon start
# daemon probes all hold jobs every 120s via srun --overlap
# results written to ~/.hive/node_monitor.json (shared across all nodes via Lustre/NFS)
```

### 4. Start the task scheduler

```bash
hive queue daemon start
# hive-sched polls ~/.hive/node_monitor.json every 30s
# dispatches pending tasks to IDLE nodes via srun --overlap
# heartbeat file updated every 30s; crash detected after 5 min silence → FAILED
```

Both daemons **auto-start** when needed (`hive nodes` starts the monitor, `hive queue submit` starts the scheduler).

---

## Daily Workflow

### Check node status

```bash
hive nodes
```

```
  JOBID   NODE    PART     ELAPSED  STATUS  GPU%  MEM       TASK
  ─────────────────────────────────────────────────────────────
  582228  evc104  highgpu  3d13h    BUSY    87%   42G/80G   python train.py ...
  584954  evc23   normal   2h20m    IDLE     0%    0G/80G   —
```

```bash
hive top    # interactive live view, q to quit
```

### Submit an experiment

**Option A — inline command:**

```bash
hive queue submit "python train.py --config exp/v1.yaml"
# → Submitted task #4
```

**Option B — `.hive` script** (recommended for multi-line or parameterized runs):

```bash
# train_v1.hive
#!/bin/bash
#HIVE workdir=/lustre/home/user/project
#HIVE priority=5
#HIVE name=train-v1

python train.py \
  --config exp/v1.yaml \
  --output results/v1
```

```bash
hive queue submit train_v1.hive
# → Submitted task #5
```

**Options:**

```bash
hive queue submit --workdir /path/to/project "python eval.py"
hive queue submit --priority 10 "urgent_experiment.sh"   # higher priority runs first
hive queue submit --name my-run "python train.py"
```

The task enters `PENDING` state and starts within ≤30s once an IDLE node is available.

### Monitor the queue

```bash
hive queue list
```

```
  ID  NAME      STATE    NODE    ELAPSED   CMD
  ──────────────────────────────────────────────────────────────
   5  train-v1  RUNNING  evc23   12m       python train.py --config ...
   6  (unnamed) PENDING  —       —         python eval.py --model ...
   3  (unnamed) DONE     evc39   1h30m     python train.py --config ...
   2  (unnamed) FAILED   evc36   3m        python bad_script.py
```

```bash
hive queue list --state pending    # filter by state
```

### Stream logs

```bash
hive queue logs 5         # print full log so far
hive queue logs 5 -f      # tail -f (live stream)
```

Each task log is at `~/.hive/logs/task-<id>.log`. It contains:

```
=== hive task #5 started at 2026-04-15T10:01:05 ===
=== node: evc23  slurm_jobid: 584954 ===
=== workdir: /lustre/home/user/project ===
=== cmd: python train.py --config exp/v1.yaml ===

... stdout + stderr from your command ...

=== hive task #5 finished at 2026-04-15T11:31:22  exit_code=0 ===
```

If `srun` itself fails (node gone, job expired), the error appears at the top of the log.

### Wait for completion (agent pattern)

For automated pipelines where you need to block until a task finishes:

```bash
TASK_ID=$(hive queue submit "python eval.py" | grep -oP '#\K\d+')
hive queue wait $TASK_ID
# blocks until DONE/FAILED/CANCELLED
# prints the full log when done
# exits with the task's exit code ($? = 0 for success, non-zero for failure)
```

This is the recommended pattern for agent workflows that need to chain steps:

```bash
# Step 1: train
hive queue submit --name train "python train.py --config v1.yaml"
TRAIN_ID=$(hive queue list | grep train | awk '{print $1}')
hive queue wait $TRAIN_ID || { echo "Training failed"; exit 1; }

# Step 2: eval (only runs if training succeeded)
hive queue submit "python eval.py --checkpoint results/v1/best.pt"
```

### Clean up completed tasks

```bash
hive queue rm 3          # remove a DONE/FAILED/CANCELLED task record
hive queue cancel 6      # cancel PENDING or RUNNING task
```

---

## Node Pool Management

### Expand pool

```bash
hive pool add                              # one more node (default preset)
hive pool add highgpu --count 2            # two high-GPU nodes
hive pool add ~/custom.slurm --time 6:00:00   # custom script, 6h wall time
```

### Release nodes

```bash
# ⚠️  Only release nodes YOU intentionally want to give back.
# NEVER scancel hold jobs that have running experiments.

hive pool release --idle       # release all idle (safe: no experiments running)
hive pool release 584954       # release one specific hold job by SLURM job ID
```

> **Rule:** if `hive nodes` shows `BUSY`, do NOT release that node. Only release `IDLE` nodes you don't plan to use soon.

---

## Scheduler and Daemon Status

```bash
hive daemon status              # node monitor daemon
hive queue daemon status        # task scheduler (hive-sched)
hive queue daemon logs          # last 40 lines of sched.log
```

Both daemons write heartbeat files to `~/.hive/` on the shared filesystem, so you can check their status from any node:

```bash
ls -la ~/.hive/sched.heartbeat  # updated every 30s if hive-sched is alive
ls -la ~/.hive/node_monitor.pid
```

---

## Programmatic Access (Python)

Agents can read hive state files directly without running CLI commands:

```python
import json, os, time

HIVE_DIR = os.path.expanduser("~/.hive")

# Read node status
db = json.load(open(f"{HIVE_DIR}/node_monitor.json"))
idle_nodes = [(jid, v["node"]) for jid, v in db["jobs"].items() if v["status"] == "idle"]
print(f"{len(idle_nodes)} idle node(s): {idle_nodes}")

# Read queue
q = json.load(open(f"{HIVE_DIR}/queue.json"))
pending = [t for t in q["tasks"].values() if t["state"] == "pending"]
running = [t for t in q["tasks"].values() if t["state"] == "running"]
print(f"Queue: {len(pending)} pending, {len(running)} running")

# Read task log
task_id = 5
log_path = f"{HIVE_DIR}/logs/task-{task_id}.log"
if os.path.exists(log_path):
    print(open(log_path).read())
```

---

## Troubleshooting

| Symptom | Check | Fix |
|---------|-------|-----|
| `hive queue list` shows PENDING forever | `hive nodes` — any IDLE nodes? | Add more nodes: `hive pool add` |
| Task stays PENDING > 2 min with IDLE node | `hive queue daemon status` | Restart: `hive queue daemon start` |
| Log is empty or missing | `hive queue logs <id>` | Check if srun error in first lines of log |
| Node shows `?????` | Daemon can't probe it | `hive poll` to force retry |
| Task marked FAILED immediately | Log says "cannot cd to workdir" | Check `--workdir` path exists |
| Task marked FAILED after running | Heartbeat timed out (>5 min gap) | Probably OOM or node crashed; check log |

---

## File Reference

```
~/.hive/
├── pool_config.json       # your preset sbatch scripts (local only, not in git)
├── node_monitor.json      # live node state (updated every 120s by daemon)
├── node_monitor.pid       # daemon PID
├── node_monitor.log       # daemon log (rolling 500 lines)
├── queue.json             # task queue DB
├── queue.lock             # flock file (protects queue.json writes)
├── sched.pid              # hive-sched PID
├── sched.log              # scheduler log (rolling 500 lines)
├── sched.heartbeat        # updated every 30s; used for cross-node liveness check
├── heartbeat/
│   └── <task_id>          # per-task heartbeat (updated every 30s while running)
└── logs/
    └── task-<id>.log      # stdout + stderr for each task
```

---

## Responsible Use

**hive-cli is for active, short-term debug sessions — not permanent resource reservation.**

- Release idle nodes when you're done: `hive pool release --idle`
- Keep hold jobs short (hours, not days)
- Reduce pool size during peak hours
- Your hold jobs are visible to all users in `squeue`

> Clusters work best when resources are borrowed, not owned.
