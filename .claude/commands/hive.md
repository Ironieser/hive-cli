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
hive submit --name train --est-runtime 2h "python train.py ..."  # runtime estimate (walltime-aware)
hive submit --name train --est-runtime auto "python train.py ..."# estimate from this name's history (P90)
hive submit experiment.hive                                       # from .hive script file

# Wait for a task (agent pattern — blocks until done)
hive wait <ID>            # block, print log when done, exit with task's exit code
hive wait <ID> --pending-timeout 600   # give up (exit 75) if it never dispatches in 600s

# Inspect
hive logs <ID>            # print full log
hive logs <ID> -f         # tail -f (live stream)
hive list --state running # filter by state
hive stats [NAME]         # completed-run durations by name (min/median/P90/max)

# Cancel / clean up
hive cancel <ID>          # cancel pending or running task
hive queue rm <ID>        # delete one done/failed/cancelled record
hive prune --dry-run      # preview which old terminal tasks would be dropped
hive prune --older-than 7d# trim old terminal tasks (history kept in events.jsonl)

# Pool management
hive pool add                    # sbatch a new hold job (default preset)
hive pool add highgpu            # named preset
hive pool add ~/hold.slurm       # direct script path
hive pool add --count 2 --time 12:00:00
hive pool add --no-validate ...  # skip the sbatch --test-only pre-flight check
hive pool release --idle         # scancel all IDLE hold jobs (⚠️ see rules below)
hive pool release <JOBID>        # scancel one specific hold job
hive pool config                 # show presets + flag any SLURM-rejected ones (✓/✗)
```

`hive pool add` validates the preset with `sbatch --test-only` before submitting, so
a malformed script fails once with a clear message (not N times), and hold-job
stdout is redirected to `~/.hive/pool-logs/` instead of dumping `slurm-<id>.out`
into your cwd.

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
| `name` | — | Label shown in `hive list`; also the key for runtime history (`hive stats`, `--est-runtime auto`) |
| `need_mb` | 0 | Min **free** GPU memory (MiB) required before dispatch. The scheduler keeps the task PENDING (reason `waiting_for_mem`) until a pool GPU has at least this much free — use it for large models to avoid OOM-on-startup. |
| `est_runtime` | — | Estimated runtime: `2h`, `90m`, `1-12:00:00`, raw seconds, or `auto` (P90 of this name's history). The scheduler won't place the task on a node whose remaining walltime < estimate + 10 min (reason `insufficient_walltime`). |

CLI flags `--workdir`, `--priority`, `--name`, `--need-mb`, `--est-runtime` override the file's directives.

The scheduler also refuses to dispatch onto a GPU with **>5 GB already in use**
(a zombie process or an out-of-band co-tenant), marking such a task PENDING with
reason `gpu_dirty` instead of dispatching into a near-full card.

---

## Agent Workflow: Submit → Wait → Read Results

```bash
# Step 1: submit
hive submit --workdir /path/to/project "python eval.py --model results/best.pt"
# Output: Submitted task #7

# Step 2: block until done (exit code = 0 success, non-zero failure)
hive wait 7
# prints state transitions:  [0s] PENDING  →  [4s] RUNNING on evc23  →  [12m34s] DONE
# then a measured timing line:  ✓ task #7 done · queued 4s · ran 12m34s · on evc23
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
  queue · 2 running · 1 pending · since 06-01 (last 7d) · 18 older hidden (--all)
  ID  NAME      STATE    NODE             ELAPSED    CMD
  ─────────────────────────────────────────────────────────────────────
   5  train-v1  RUNNING  evc23            12m34s     python train.py --config ...
   6  —         PENDING  waiting_for_mem  wait:3m02s python eval.py --model ...
  ── Today  06-08 ──────────────────────────────────────────────────────
   4  —         DONE     evc39            1h30m      python train.py --config ...
   3  —         FAILED   evc36            3m02s      python bad_script.py
  ── 06-07 ─────────────────────────────────────────────────────────────
   2  prep      DONE     evc12            45m        python prep.py
```

- **Scope line** (top) states what you're seeing: counts + the time window + how many
  older tasks are hidden.
- **Active tasks** (running/pending) are listed first; **terminal tasks are grouped by
  calendar date** (`Today` / `Yesterday` / `MM-DD`), newest first.
- Default shows the **last 7 days** of terminal tasks (plus all active). `hive list
  --days N` widens/narrows the window; `hive list --all` shows everything; `hive list
  --state done|failed|...` filters. Use `hive prune` to actually drop old ones.

States: `RUNNING` (green) → `PENDING` (yellow) → `DONE` (dim) → `FAILED` (red) → `CANCELLED` (dim)

ELAPSED format: `<N>s` / `<N>m<SS>s` / `<N>h<MM>m`. Pending shows `wait:<time>` (since submitted).

For **PENDING** tasks the NODE column shows the scheduler's `pending_reason` — *why*
it hasn't dispatched yet (no more guessing from `squeue`):

| Reason | Meaning |
|---|---|
| `no_dispatchable_node` | No idle pool node available — `hive pool add` or wait |
| `waiting_for_mem` | No node has the task's `need_mb` free yet |
| `gpu_dirty` | Idle nodes have >5 GB resident (zombie / co-tenant) — not safe to dispatch |
| `node_busy_on_verify` | A node looked idle but a live probe found it occupied |
| `probe_unverifiable` | Could not confirm a node is free (transient `srun` failure) |
| `insufficient_walltime` | No node has enough remaining walltime for this task's `est_runtime` + 10 min — `hive pool add --time …` or lower the estimate |
| `redispatched_after_crash` | Was requeued after a scheduler restart found no running step (never ran — no progress lost) |
| `infra_failure_redispatch` | The node was reclaimed **mid-run**; requeued to another node. ⚠ The new run starts fresh — progress is lost unless your command checkpoints. Shown as `(re-disp xN)` in the NODE column. |

The bottom of `hive list` also shows scheduler status: `scheduler: running  2 running  1 pending`

> **Checkpoint-loss warning.** If a node expires while your task is running, hive
> auto-requeues it onto another node — but SLURM cannot resume your process, so it
> restarts from scratch. hive tells you three ways: a `⚠` line in `hive wait`, a
> `(re-disp xN)` tag in `hive list`, and a banner in the task log. **If your job is
> long, make it checkpoint-and-resume, and submit with `--est-runtime` so the
> scheduler avoids placing it on a soon-expiring node in the first place.**

`hive nodes` has a **LEFT** column showing each node's remaining walltime (red when
under an hour) so you can see how long a node can run before SLURM reclaims it.

---

## Task Log Format

Each task writes to `~/.hive/logs/task-<ID>.log`:

```
=== hive task #5 started at 2026-04-15T10:01:05 ===
=== node: evc23  slurm_jobid: 584954 ===
=== node walltime remaining at dispatch: 12h30m (then this node is reclaimed) ===
=== estimated runtime: 2h00m (source: user) ===
=== workdir: /lustre/home/user/project ===
=== cmd: python train.py --config exp/v1.yaml ===

[... stdout + stderr from your command ...]

=== hive task #5 finished at 2026-04-15T11:31:22  exit_code=0 ===
```

The header records the node's **remaining walltime at dispatch** and the runtime
estimate. If the task was re-dispatched after a node was reclaimed mid-run, a `WARNING:
re-dispatch #N … prior progress is lost unless your command resumes from a checkpoint`
banner is written too.

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

**Multi-node / multi-agent note.** State lives in shared-FS `~/.hive`, so all agents on
all nodes share **one** queue, **one** scheduler, and **one** node poller. Both daemons
are cluster-wide singletons (hostname-tagged PID + shared-FS heartbeat), so invoking
hive from another node will **not** spawn a duplicate poller. `hive daemon status` shows
which host the poller runs on; `hive daemon stop` from a different node writes a
stop-request the remote daemon honors within one heartbeat (or ssh to that host). To
force an immediate re-poll from any node, `hive poll` (runs a one-shot probe) — you do
not need to signal the daemon.

---

## Critical Rules

### Pool / node management

1. **NEVER `scancel` hold jobs directly** — always use `hive pool release`. Directly scancelling leaves orphaned tasks in the queue.

2. **NEVER run `hive pool release --idle` if there are PENDING tasks** — the scheduler will try to dispatch them to nodes that no longer exist → immediate FAILED.

3. **Only release nodes you intend to give back** — `hive list` shows `IDLE`, you have no pending tasks, session is over → `hive pool release --idle` is safe.

### Writing the cmd correctly

4. **NEVER put `srun` in the cmd.** hive handles dispatch via `srun --overlap` internally. Adding `srun --jobid=...` inside the cmd creates a nested srun that is fragile and redundant.

   ```bash
   # ✗ WRONG — srun prefix is redundant, hive already dispatches to the node
   hive submit "srun --jobid=584951 python train.py ..."

   # ✓ CORRECT — just the command; hive puts it on the right node
   hive submit "python train.py ..."
   ```

5. **NEVER use a HuggingFace model name string as `--model` when the model needs to load from NFS.** Loading large models from NFS (Lustre) is slow and can cause shard-level deadlocks on some nodes. Always rsync the model to node-local `/tmp/` first.

   ```bash
   # ✗ WRONG — loads from NFS every time, slow and deadlock-prone on highgpu nodes
   hive submit "python eval.py --model Qwen/Qwen3-VL-4B-Instruct ..."

   # ✓ CORRECT — rsync to /tmp/ if not already there, then load locally
   hive submit "
   MODEL=/tmp/Qwen3-VL-4B-Instruct
   [ ! -d \$MODEL ] && rsync -aL \
     \$(ls -d ~/.cache/huggingface/hub/models--Qwen--Qwen3-VL-4B-Instruct/snapshots/*/ | tail -1) \
     \$MODEL/
   python eval.py --model \$MODEL ...
   "
   ```

   Or write it as a `.hive` file (recommended for multi-line commands):

   ```bash
   #!/bin/bash
   #HIVE workdir=/lustre/fs1/home/user/project
   #HIVE name=eval-v1

   MODEL=/tmp/Qwen3-VL-4B-Instruct
   [ ! -d $MODEL ] && rsync -aL \
     $(ls -d ~/.cache/huggingface/hub/models--Qwen--Qwen3-VL-4B-Instruct/snapshots/*/ | tail -1) \
     $MODEL/

   python eval.py --model $MODEL ...
   ```

6. **Queue is flock-protected** — multiple agents submitting concurrently is safe.

7. **Workdir must exist on the compute node** — `~` and lustre/NFS paths are fine. Local `/tmp/` paths on the *submit* node won't exist on the compute node (but `/tmp/` paths created *inside the cmd* are fine — they run on the compute node).

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

# status ∈ {idle, busy, cpu, warning, probe_failed}. A probe_failed node may carry
# v["carried_forward"]=True (last-good state reused after a transient probe miss).
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

## Filing Feedback (for agents)

Hit a bug, rough edge, or missing feature in hive itself? File it so the agent
that maintains the hive-cli repo can triage it — don't just work around it silently.

```bash
hive feedback "list 的 CMD 列被截断，看不出 shard 后缀"          # quick one-liner
hive feedback submit --title "OOM-blind dispatch" \
    --severity high --tags scheduler,oom --task 3536,3541 \
    --file /tmp/details.md                                    # structured
hive feedback list                                           # see what's filed
```

Each submit auto-captures the hive version, a `queue.json` + `node_monitor.json`
state snapshot, and the tail of any `--task` log, so the maintainer can reproduce.
Reports live in `<repo>/feedback/inbox/`; see `feedback/TRIAGE.md`.

---

## Troubleshooting

| Symptom | Check | Fix |
|---------|-------|-----|
| Task stuck PENDING | `hive list` — read the **reason** in the NODE column | See `pending_reason` table above |
| Pending = `no_dispatchable_node` w/ free GPUs | `hive queue daemon status` | `hive queue daemon start` |
| Pending = `gpu_dirty` / `node_busy_on_verify` | `hive nodes` — GPU mem in use | Zombie/co-tenant on the card; free it or wait |
| Pending = `waiting_for_mem` | task's `need_mb` vs node free mem | Lower `need_mb`, or `hive pool add` a bigger card |
| Task FAILED immediately | `hive logs <id>` — check top lines | srun error or bad workdir |
| `hive submit` returns error | run `hive queue daemon status` | Scheduler may have crashed |
| Node shows `PFAIL` | `hive poll` | Probe failed (srun contention) or hold job expired; scheduler still verifies & may carry forward the last-good state |
