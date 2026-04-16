# hive — GPU Node Pool & Experiment Queue

Manage pre-allocated GPU nodes and submit experiments via the hive task queue. Usage: `/hive <action> [args]`

## Actions

- `/hive status` — show node pool status + queue state
- `/hive submit <"cmd" | script.hive>` — submit an experiment to the task queue
- `/hive wait <id>` — block until task finishes, show log, return exit code
- `/hive logs <id>` — show task log (add `-f` to follow)
- `/hive cancel <id>` — cancel a pending or running task
- `/hive add [preset] [--count N] [--time T]` — add a node to the pool
- `/hive release --idle` — release idle hold nodes back to the cluster

---

## Instructions

When this command is invoked, perform the following based on the action:

### status

Run both:
```bash
hive nodes          # node pool: BUSY/IDLE status, GPU%, running task
hive queue list     # task queue: pending/running/done/failed tasks
```

Show the combined picture: how many nodes are available, what's running, what's queued.

If `hive-sched` is not running, note it and offer to start:
```bash
hive queue daemon status
hive queue daemon start   # if not running
```

### submit

1. Determine the command and workdir:
   - If given an inline string: `hive queue submit --workdir <project_dir> "<cmd>"`
   - If given a `.hive` file: `hive queue submit <file.hive>` (workdir/priority/name from `#HIVE` directives)
   - Default workdir = current project directory

2. Ensure the scheduler is running before submitting:
   ```bash
   hive queue daemon status   # check
   # → auto-started by submit if not running
   ```

3. Submit and capture the task ID:
   ```bash
   hive queue submit --workdir /path/to/project "python train.py --config ..."
   # → Submitted task #N
   ```

4. After submit, show `hive queue list` so the user can see the task entered the queue.

5. **Do not `hive queue wait` automatically** unless the user explicitly asks to block. For background experiments, just confirm submission and move on.

### wait

Block until the task reaches a terminal state (done/failed/cancelled):

```bash
hive queue wait <id>
# → prints state transitions as they happen
# → prints full log when done
# → exits with the task's exit code
```

Use exit code to decide next step:
```bash
hive queue wait 5 && echo "Success" || echo "Failed — check hive queue logs 5"
```

For multi-step pipelines:
```bash
ID=$(hive queue submit "python train.py" | grep -oP '#\K\d+')
hive queue wait $ID || { echo "Training failed"; exit 1; }
hive queue submit "python eval.py --checkpoint results/best.pt"
```

### logs

```bash
hive queue logs <id>        # full log
hive queue logs <id> -f     # live tail (Ctrl-C to stop)
```

Log is at `~/.hive/logs/task-<id>.log`. Includes:
- Header: node, workdir, cmd, start time
- Full stdout + stderr from the command
- srun errors (if node was unavailable)
- Footer: finish time, exit code

### cancel

```bash
hive queue cancel <id>
```

- PENDING tasks: immediately cancelled
- RUNNING tasks: kills the srun process, marks cancelled

After cancelling, run `hive queue list` to confirm.

### add

Add a new hold job to expand the pool:

```bash
hive pool add                           # default preset
hive pool add highgpu                   # named preset
hive pool add ~/my_hold.slurm           # direct script path
hive pool add --count 2 --time 12:00:00 # 2 nodes, 12h wall time
```

After adding, run `squeue -u $USER` to confirm the job was submitted. The new node appears in `hive nodes` once it starts running and the daemon polls it (up to 120s).

### release

**⚠️ Use with caution — this cancels SLURM jobs.**

```bash
hive pool release --idle     # scancel all IDLE nodes (safe to return to cluster)
hive pool release <jobid>    # scancel one specific hold job
```

Only release nodes that are confirmed IDLE (`hive nodes` shows `IDLE`). Never release a node with `BUSY` status or one that has a task in `running` state in `hive queue list`.

---

## Critical Rules

1. **NEVER `scancel` hold jobs directly** — always use `hive pool release`. Directly scancelling bypasses the queue state machine and leaves tasks orphaned.

2. **NEVER run `hive pool release --idle` if there are PENDING tasks in the queue** — the scheduler will try to dispatch them to nodes that no longer exist, causing immediate FAILED status.

3. **Always check `hive nodes` before manually entering a node** — if a node shows BUSY, another experiment is running there. Don't `srun` in manually unless you intend to share the GPU.

4. **`hive pool release --idle` is for returning unused resources** — only use it when you're done with a session and have nothing pending.

5. **The queue is flock-protected** — multiple agents submitting simultaneously is safe and tested. Don't worry about races when submitting from concurrent sessions.

---

## Workflow: Agent Running an Experiment

```bash
# 1. Check what's available
hive nodes
hive queue list

# 2. Submit experiment (background, non-blocking)
hive queue submit --workdir ~/project "python train.py --config v1.yaml"
# → Submitted task #7

# 3. Check status after 30s (node assignment takes ≤30s)
hive queue list

# 4. Stream logs if needed
hive queue logs 7 -f

# 5. Wait for completion (for chained steps)
hive queue wait 7
echo "exit: $?"
```

## Workflow: Pool Maintenance

```bash
# Add a node when pool is empty
hive pool add

# Check it appeared
hive nodes   # (after ~120s for daemon to poll)

# Release idle nodes at end of session
hive nodes   # verify IDLE
hive queue list   # verify no PENDING tasks
hive pool release --idle
```

---

## State Files (readable from any node)

```
~/.hive/node_monitor.json   # live node status (updated every 120s)
~/.hive/queue.json          # task queue DB
~/.hive/logs/task-<id>.log  # per-task stdout+stderr
~/.hive/sched.heartbeat     # hive-sched liveness (mtime ≤ 30s if alive)
```

Quick Python read:

```python
import json, os
db   = json.load(open(os.path.expanduser("~/.hive/node_monitor.json")))
q    = json.load(open(os.path.expanduser("~/.hive/queue.json")))
idle = [jid for jid, v in db["jobs"].items() if v["status"] == "idle"]
pend = [t for t in q["tasks"].values() if t["state"] == "pending"]
run  = [t for t in q["tasks"].values() if t["state"] == "running"]
print(f"Nodes IDLE: {len(idle)} | Queue: {len(pend)} pending, {len(run)} running")
```

---

## Troubleshooting Quick Reference

| Symptom | Command | Action |
|---------|---------|--------|
| Task stuck PENDING | `hive nodes` | No IDLE nodes → `hive pool add` |
| Scheduler stopped | `hive queue daemon status` | `hive queue daemon start` |
| Task FAILED fast | `hive queue logs <id>` | Read first lines for srun/workdir error |
| Node stuck `?????` | `hive poll` | Force re-probe; may need new hold job |
| Log missing | `hive queue logs <id>` | Task not started yet (still PENDING) |
