# hive — GPU Node Pool & Experiment Queue

Manage pre-allocated GPU nodes on a SLURM cluster. Submit experiments to the task queue; the scheduler auto-assigns them to idle nodes.

---

## Quick Command Reference

```bash
# Check status
hive nodes                                    # node pool: BUSY/IDLE, GPU%, running process
hive list                                     # task queue: pending/running/done/failed

# Submit an experiment
hive submit "python train.py --config v1.yaml"                   # inline command
hive submit --workdir /path/to/project "python train.py ..."     # explicit workdir
hive submit --priority 10 --name train-v1 "python train.py ..."  # with priority + name
hive submit experiment.hive                                       # from .hive script file

# Wait for a task (agent pattern — blocks until done)
hive wait <ID>            # block, print log when done, exit with task's exit code

# Inspect
hive logs <ID>            # print full log
hive logs <ID> -f         # tail -f (live stream)
hive list --state running # filter by state

# Cancel / clean up
hive cancel <ID>          # cancel pending or running task
hive queue rm <ID>        # delete done/failed/cancelled record

# Pool management
hive pool add                    # sbatch a new hold job (default preset)
hive pool add highgpu            # named preset
hive pool add ~/hold.slurm       # direct script path
hive pool add --count 2 --time 12:00:00
hive pool release --idle         # scancel all IDLE hold jobs (⚠️ see rules below)
hive pool release <JOBID>        # scancel one specific hold job
```

---

## The `.hive` Script Format

For multi-line or parameterized runs, write a `.hive` file (like `#SBATCH` for SLURM):

```bash
#!/bin/bash
#HIVE workdir=/lustre/home/user/project
#HIVE priority=5
#HIVE name=train-v1

python train.py \
  --config exp/v1.yaml \
  --output results/v1
```

Submit with: `hive submit experiment.hive`

Supported `#HIVE` directives:

| Directive | Default | Description |
|---|---|---|
| `workdir` | `$PWD` at submit time | Working directory on the node |
| `priority` | 0 | Higher = dispatched first |
| `name` | — | Label shown in `hive list` |

CLI flags `--workdir`, `--priority`, `--name` override the file's directives.

---

## Agent Workflow: Submit → Wait → Read Results

```bash
# Step 1: submit
hive submit --workdir /path/to/project "python eval.py --model results/best.pt"
# Output: Submitted task #7

# Step 2: block until done (exit code = 0 success, non-zero failure)
hive wait 7
# prints state transitions:  [0s] PENDING  →  [4s] RUNNING on evc23  →  [12m34s] DONE
# then prints full log
# exits with the task's exit code

# Step 3: check exit code
echo "exit: $?"

# Or chain directly:
hive wait 7 && echo "Done" || { echo "Failed"; hive logs 7; exit 1; }
```

**Capture task ID from submit:**

```bash
ID=$(hive submit "python train.py" | grep -oP '#\K\d+')
hive wait $ID
```

**Multi-step pipeline:**

```bash
ID=$(hive submit --name train "python train.py --config v1.yaml" | grep -oP '#\K\d+')
hive wait $ID || { echo "Training failed"; hive logs $ID; exit 1; }
hive submit "python eval.py --checkpoint results/v1/best.pt"
```

---

## `hive list` Output

```
  ID  NAME      STATE    NODE    ELAPSED    CMD
  ─────────────────────────────────────────────────────────────────────
   5  train-v1  RUNNING  evc23   12m34s     python train.py --config ...
   6  —         PENDING  —       wait:3m02s python eval.py --model ...
   3  —         DONE     evc39   1h30m      python train.py --config ...
   2  —         FAILED   evc36   3m02s      python bad_script.py
```

States: `RUNNING` (green) → `PENDING` (yellow) → `DONE` (dim) → `FAILED` (red) → `CANCELLED` (dim)

ELAPSED format: `<N>s` / `<N>m<SS>s` / `<N>h<MM>m`. Pending shows `wait:<time>` (since submitted).

The bottom of `hive list` also shows scheduler status: `scheduler: running  2 running  1 pending`

---

## Task Log Format

Each task writes to `~/.hive/logs/task-<ID>.log`:

```
=== hive task #5 started at 2026-04-15T10:01:05 ===
=== node: evc23  slurm_jobid: 584954 ===
=== workdir: /lustre/home/user/project ===
=== cmd: python train.py --config exp/v1.yaml ===

[... stdout + stderr from your command ...]

=== hive task #5 finished at 2026-04-15T11:31:22  exit_code=0 ===
```

If `srun` itself fails (node expired, job gone), the srun error appears at the **top** of the log, before the header.

---

## Daemon Management

```bash
hive queue daemon start           # start hive-sched (auto-started by hive submit)
hive queue daemon stop
hive queue daemon status          # → hive-sched: running  PID 2201107  host evc1  last heartbeat 20s ago
hive queue daemon logs            # last 40 lines of scheduler log

hive daemon start                 # start node monitor daemon (auto-started by hive nodes)
hive daemon stop
hive daemon status
hive daemon logs
```

Both daemons auto-start when needed — you only need to manage them manually when troubleshooting.

---

## Critical Rules

1. **NEVER `scancel` hold jobs directly** — always use `hive pool release`. Directly scancelling leaves orphaned tasks in the queue.

2. **NEVER run `hive pool release --idle` if there are PENDING tasks** — the scheduler will try to dispatch them to nodes that no longer exist → immediate FAILED.

3. **Only release nodes you intend to give back** — `hive list` shows `IDLE`, you have no pending tasks, session is over → `hive pool release --idle` is safe.

4. **Queue is flock-protected** — multiple agents submitting concurrently is safe. No need to coordinate submissions between sessions.

5. **Workdir must exist on the compute node** — `~` and lustre/NFS paths are fine. Local `/tmp/` paths on the submit node won't exist on the compute node.

---

## Checking Scheduler Health

```bash
hive queue daemon status
# → hive-sched: running  PID 2201107  host evc1  last heartbeat 20s ago
# → hive-sched: stopped        ← means no scheduler; start with: hive queue daemon start
```

If tasks are stuck in PENDING and you have IDLE nodes, the scheduler is likely stopped:

```bash
hive nodes            # confirm there are IDLE nodes
hive queue daemon start
```

---

## Programmatic State Reading (Python)

```python
import json, os

HIVE_DIR = os.path.expanduser("~/.hive")

db = json.load(open(f"{HIVE_DIR}/node_monitor.json"))
q  = json.load(open(f"{HIVE_DIR}/queue.json"))

idle  = [(jid, v["node"]) for jid, v in db["jobs"].items() if v["status"] == "idle"]
run   = [t for t in q["tasks"].values() if t["state"] == "running"]
pend  = [t for t in q["tasks"].values() if t["state"] == "pending"]
print(f"IDLE nodes: {idle}")
print(f"Queue: {len(run)} running, {len(pend)} pending")

# Read a specific task's log
task_id = 5
print(open(f"{HIVE_DIR}/logs/task-{task_id}.log").read())
```

---

## Troubleshooting

| Symptom | Check | Fix |
|---------|-------|-----|
| Task stuck PENDING | `hive nodes` — IDLE nodes? | No idle → `hive pool add` |
| Pending stays >2 min with IDLE nodes | `hive queue daemon status` | `hive queue daemon start` |
| Task FAILED immediately | `hive logs <id>` — check top lines | srun error or bad workdir |
| Log empty / not found | `hive list` — is it still PENDING? | Wait for scheduler to dispatch (≤30s) |
| `hive submit` returns error | run `hive queue daemon status` | Scheduler may have crashed |
| Node shows `?????` | `hive poll` | Force re-probe; hold job may have expired |
