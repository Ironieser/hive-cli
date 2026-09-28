# hive CLI reference

Contents: [Commands](#commands) · [`hive submit`](#hive-submit) · [`.hive` files](#hive-script-files)
· [`hive list`](#hive-list) · [`pending_reason` values](#pending_reason-values)
· [Node STATUS legend](#node-status-legend) · [Task log format](#task-log-format)
· [`hive health`](#hive-health) · [Pool management](#pool-management)
· [Daemons & multi-node](#daemons--multi-node) · [Staging models to /tmp](#staging-models-to-tmp)

## Commands

```bash
hive submit "CMD" [flags] | hive submit job.hive     # queue a task → "Submitted task #N"
hive wait ID [--pending-timeout SEC] [--log-lines N | --full-log | --no-log]
hive list [--owner NAME|all] [--state S] [--limit N] [--days N] [--all]
hive logs ID [-n N | --full] [-f]
hive cancel ID                                        # pending or running
hive stats [NAME]                                     # min/median/P90/max run time by name
hive prune [--older-than 7d] [--keep N] [--dry-run]  # trim finished tasks from the queue
hive nodes                                            # pool table (auto-starts the poller)
hive poll                                             # force an immediate node poll
hive health [report NODE [-r TEXT] | check NODE | clear NODE]
hive pool add [PRESET|script] [--count N] [--time T] | hive pool config | hive pool release …
hive feedback "text" | submit --title T [--severity S] [--tags a,b] [--task 1,2] | list | show ID
hive queue daemon start|stop|restart|status|logs      # scheduler
hive daemon start|stop|restart|status|logs            # node poller
```

Exit codes of `hive wait`: `0` done · `1` failed · `75` never dispatched (`--pending-timeout`)
· `130` cancelled.

## `hive submit`

```bash
hive submit [--name NAME] [--est-runtime DUR|auto] [--need-mb MiB] [--gpus N] [--exclude NODES] \
            [--timeout DUR] [--notify CMD] [--after ID,ID | --after-any ID,ID] \
            [--array SPEC] [--max-running N] \
            [--workdir DIR] [--priority N] "command string" | job.hive
```

| Flag | Default | Meaning |
|---|---|---|
| `--name` / `-n` | — | label; key for runtime history (`hive stats`, `--est-runtime auto`) |
| `--owner` / `-o` | `$HIVE_OWNER` | owner tag (agent or project name). Precedence: `--owner` > `#HIVE owner=` > `$HIVE_OWNER`. Shown as an OWNER column in `hive list --owner all` and as `[owner]` in the `hive nodes` TASK column |
| `--est-runtime` | — | `2h`, `90m`, `1-12:00:00`, seconds, or `auto` (P90 of NAME's history). With an estimate the scheduler never places the task on a node whose remaining walltime < estimate + 10 min (`insufficient_walltime`). Without one the task is walltime-blind. |
| `--need-mb` | 0 | minimum **free** GPU memory (MiB), or `auto` = P90 of the GPU peak this NAME reached in past runs + 10 %. Task waits (`waiting_for_mem`) until a card has it |
| `--gpus` | 1 | GPUs the task may see. A hold job may own more; hive narrows `CUDA_VISIBLE_DEVICES` to the first N so frameworks don't auto-`DataParallel` over cards you didn't ask for. `--gpus 2` only places on hold jobs with ≥ 2 GPUs (`insufficient_gpus`). |
| `--timeout` | — | hard limit on **run** time (`2h`, `90m`). Over it the task is killed and ends `failed`, exit code 124, `fail_reason: timeout`; not retried. Unlike `--est-runtime`, which only steers placement |
| `--notify` | `$HIVE_NOTIFY` | shell command run when the task finishes (done / failed / cancelled / timeout) or is requeued after a node loss. See [Notification hook](#notification-hook) |
| `--after` | — | run only after these tasks ended `done`. If one failed or was cancelled this task fails without running: exit code 125, `fail_reason: dependency_failed`, and so do the tasks `--after` it. Until then `waiting_for_dependency` |
| `--after-any` | — | run after these tasks ended, whatever the outcome (cleanup, reports) |
| `--array` | — | one task per index: `0-9`, `1,3,5`, `0-20:5`; append `%N` to run at most N at once (`0-9%4`). Same command for all, index in `$HIVE_ARRAY_INDEX`. See [Arrays](#arrays) |
| `--max-running` | `$HIVE_MAX_RUNNING` | hold this task while its owner already has N running (`owner_limit`). Needs an owner |
| `--exclude` / `-x` | — | nodes the task must not run on (`evc22,evc[40-43]`); it waits (`node_excluded`) rather than use them. Broken nodes don't need this — hive quarantines them itself |
| `--workdir` / `-w` | cwd | working directory on the node |
| `--priority` / `-p` | 0 | higher dispatches first (`-p=-5` for negatives) |

CLI flags override the same directives in a `.hive` file. The command string runs
verbatim under bash on the node; quotes, `$VARS`, `&&`, multi-line — all fine.

## `.hive` script files

```bash
#!/bin/bash
#HIVE name=eval-v1
#HIVE owner=projA
#HIVE workdir=/lustre/fs1/home/user/project
#HIVE est_runtime=45m
#HIVE need_mb=30000
#HIVE gpus=1
#HIVE priority=5

MODEL=/tmp/Qwen3-VL-4B-Instruct
[ ! -d $MODEL ] && rsync -aL \
  $(ls -d ~/.cache/huggingface/hub/models--Qwen--Qwen3-VL-4B-Instruct/snapshots/*/ | tail -1) $MODEL/
python eval.py --model $MODEL --skip-existing
```

Directives: `workdir`, `priority`, `name`, `owner`, `need_mb`, `gpus`, `est_runtime`, `exclude`,
`timeout`, `notify`, `after`, `after_any`, `array`, `max_running`. Other `#` lines
are comments; the rest is the command.

## `hive list`

```
queue · 1 running · 2 pending · 10 of 243 finished in last 7d shown · +233 more (--limit N)
    ID  NAME      STATE       NODE                ELAPSED     CMD
   ─────────────────────────────────────────────────────────────────
  9201  train-v1  RUNNING     evc104              12m34s      python train.py --config …
  9202  —         PENDING     waiting_for_mem     wait:3m02s  python eval.py …
  9203  —         CANCELLING  evc38               4m10s       …        ← cancel in flight
  ── Today  09-25 ─────────────────────────────────────────────────
  9200  prep      DONE        evc12               45m         python prep.py
  scheduler: running  ·  +233 more: hive list --limit N | --all | --state S
```

- Active tasks are always shown in full; finished ones are capped at 10 (`--limit N`,
  `--limit 0` or `--all` for everything; `--days N` widens the window; `--state
  done|failed|cancelled|pending|running` filters).
- **Owner scoping**: with `$HIVE_OWNER` set the list shows only that owner's tasks and
  says so in the scope line; `--owner NAME` picks another, `--owner all` shows everyone
  (adds an OWNER column).
- For PENDING tasks the NODE column holds the scheduler's `pending_reason`.
- `(re-disp xN)` marks a task re-dispatched after its node was reclaimed.
- ELAPSED: `wait:…` since submit for pending; run time for running/finished.

### Arrays

```bash
hive submit --name sweep --array 0-9%4 'python train.py --seed $HIVE_ARRAY_INDEX'   # single quotes!
#   Submitted array #120: 10 tasks #120–#129
hive wait --array 120        # one line per task as it ends; exit 1 if any did not end done
hive cancel --array 120      # every pending/running member
hive submit --name report --after 120,121,122 "python report.py"
```

An array is N ordinary tasks that share `array_id` (the id of the first). They show as
`sweep[3]` in `hive list`, keep one name for `hive stats` / `auto`, and each has its own
log. Inside the command: `HIVE_TASK_ID`, `HIVE_TASK_NAME`, `HIVE_TASK_OWNER`,
`HIVE_ARRAY_ID`, `HIVE_ARRAY_INDEX`. `--after` takes task ids, not an array id.

### Notification hook

The hook runs **on the scheduler's host** (see `hive queue daemon status`), not where you
submitted and not on the compute node; cwd is `$HOME`, limit 60 s, output in
`~/.hive/logs/notify.log`. A failing hook never changes the task's outcome.

| Variable | Value |
|---|---|
| `HIVE_TASK_EVENT` | `finish` or `requeue` (node lost; the task will run again from scratch) |
| `HIVE_TASK_ID` / `NAME` / `OWNER` | as submitted |
| `HIVE_TASK_STATE` | `done`, `failed`, `cancelled`; `pending` on `requeue` |
| `HIVE_TASK_EXIT_CODE` | exit code; 124 = timeout, 125 = dependency failed (never ran), -1 = declared dead |
| `HIVE_TASK_FAIL_REASON` | `timeout` or empty |
| `HIVE_TASK_NODE` / `DURATION_SECS` / `LOG` / `WORKDIR` / `REQUEUE_COUNT` | — |
| `HIVE_TASK_GPU_PEAK_MB` / `GPU_AVG_UTIL` | measured GPU usage (empty if nothing was sampled) |

```bash
export HIVE_NOTIFY='echo "$HIVE_TASK_ID $HIVE_TASK_NAME $HIVE_TASK_STATE" >> ~/hive-done.txt'
hive submit --notify 'curl -s -d "task $HIVE_TASK_NAME: $HIVE_TASK_STATE" https://ntfy.sh/mytopic' "python train.py"
```

## `pending_reason` values

| Reason | Meaning |
|---|---|
| `pool_empty` | The pool has no hold job at all (expired / never added) → `hive pool add`; waiting will not help |
| `no_dispatchable_node` | Hold jobs exist but all are busy → wait or `hive pool add` |
| `waiting_for_mem` | No card has the task's `--need-mb` free |
| `waiting_for_dependency` | A task it was submitted `--after` has not ended yet |
| `array_limit` / `owner_limit` | The array's `%N` / the owner's `--max-running` is reached; it starts when one of them ends |
| `node_excluded` | The only free nodes are in the task's `--exclude` list |
| `insufficient_gpus` | No hold job owns `--gpus N` cards → add a `--gres=gpu:N` hold job or lower N |
| `insufficient_walltime` | No node has est + 10 min left → `hive pool add --time …` or lower `--est-runtime` |
| `gpu_dirty` | Idle-looking card has > 5 GB resident (zombie / co-tenant) |
| `node_busy_on_verify` | Pre-dispatch probe found the card in use |
| `probe_unverifiable` | Couldn't run the probe (srun failed / no answer); that hold job is retried with backoff (1 → 10 min) |
| `gpu_unresponsive` | The probe ran but `nvidia-smi` never answered — the node's GPU driver is wedged. Two in a row quarantine the node |
| `no_gpu_devices` | SLURM granted a GPU but `nvidia-smi` lists none — broken node; same strikes as above |
| `cuda_unavailable_on_verify` | Pre-dispatch CUDA context creation failed → node quarantined |
| `node_quarantined` | Every remaining node is quarantined → `hive health` |
| `node_quarantined_redispatch` | Died at CUDA init on a node that just got quarantined; re-running elsewhere |
| `redispatched_after_crash` | Scheduler restart found no running step; never ran, nothing lost |
| `infra_failure_redispatch` | Node reclaimed mid-run; re-running elsewhere — progress lost unless checkpointed |
| `dispatch_error` | `srun` launch failed; retried next cycle |

Only task-specific reasons (`waiting_for_mem`, `insufficient_*`) are about *your* task;
the rest describe nodes and apply to everyone.

## Node STATUS legend (`hive nodes`)

```
  JOBID     NODE    PART     ELAPSED   STATUS  GPU%  MEM       LEFT    TASK
  847876    evc101  highgpu  7h42m     IDLE    0%    0G/80G    16h17m  —
  847885    evc104  highgpu  5h54m     BUSY    100%  75G/80G   6h05m   #9195 mj_Gours: JOB=…
  847900    evc38   normal   8h15m     CLAIM   0%    0G/80G    3h44m   #9198 …: …
  847888    evc43   normal   11h36m    QUAR    0%    0G/80G    6m      —
  busy: 1   cpu: 0   idle: 1   claimed: 1   quarantined: 1   total: 4
```

| STATUS | Meaning | Dispatchable? |
|---|---|---|
| `IDLE` | GPU allocated, quiet | yes |
| `BUSY` | GPU util ≥ 5 % or ≥ 500 MB used | no |
| `CLAIM` | a hive task is RUNNING here but the GPU isn't hot yet (import / model load) | no (slot taken) |
| `WARN` | was busy, went quiet < 3 min ago (grace) | after a live probe |
| `PFAIL` | probe couldn't run | after a live probe |
| `QUAR` | node quarantined by `hive health` (CUDA init fails there) | no |
| `CPU` | hold job has no GPU | no |

`LEFT` = remaining walltime (red under 1 h). `node!` = that row's poll is > 10 min old.
The header shows when the pool was last polled; the poller runs every 15 min, and every
dispatch is live-verified anyway, so a slightly stale table is normal.

## Task log format

`~/.hive/logs/task-<ID>.log`:

```
=== hive task #5 started at 2026-04-15T10:01:05 ===
=== node: evc23  slurm_jobid: 584954 ===
=== node walltime remaining at dispatch: 12h30m (then this node is reclaimed) ===
=== estimated runtime: 2h00m (source: user) ===
=== workdir: /lustre/home/user/project ===
=== cmd: python train.py --config exp/v1.yaml ===
=== gpus: requested 1, visible=[0] (hold job provided [0,1]) ===

[... stdout + stderr ...]

=== hive task #5 finished at 2026-04-15T11:31:22  exit_code=0 ===
```

If `srun` itself failed, its error is at the very top. A re-dispatch adds a
`=== WARNING: re-dispatch #N …` banner; a quarantine-triggered re-run adds a
`=== hive: <node> quarantined …` note.

## `hive health`

```bash
hive health                              # NODE  STATE  STRIKES  LAST CHECK  RESULT  REASON
hive health report evc43 -r "why"        # quarantine now (source=agent)
hive health check evc43                  # create a CUDA context there now; exit 0 = ok
hive health clear evc43                  # release manually
```

Mechanics: before every dispatch hive runs `nvidia-smi` **and** creates a CUDA context on
the target hold job's GPU; `fail` → node quarantined. A task that fails within 3 min with
a CUDA-init signature in its log is a strike; two strikes → quarantine + the task is
re-run elsewhere; a successful task clears strikes. Quarantined nodes are re-probed every
10 min through a hold job with no running task and released after 2 consecutive healthy
probes (minimum 1 h hold). A node without any hold job can't be probed and stays listed.
Ordinary crashes, OOMs and slow failures never count.

## Pool management

```bash
hive pool add                    # sbatch a new hold job (default preset)
hive pool add highgpu            # named preset from ~/.hive/pool_config.json
hive pool add ~/hold.slurm       # explicit script
hive pool add --count 2 --time 12:00:00
hive pool add --exclude evc22,evc[40-43]   # keep the hold job off these nodes
hive pool config                 # presets + sbatch --test-only validation (✓/✗)
hive pool release --idle | JOBID # ⚠ HUMAN-ONLY: refuses without a TTY; no --yes/--force
```

`pool add` validates with `sbatch --test-only` first and redirects hold-job stdout to
`~/.hive/pool-logs/`. It excludes every quarantined node (`hive health`) by itself —
SLURM favours broken nodes because their GPUs are always free — plus `--exclude`, the
script's own `#SBATCH --exclude`, and an `"exclude"` key in `pool_config.json` (top
level or per preset). `--no-auto-exclude` turns the first off. Hold jobs still waiting in the SLURM queue get
nodes quarantined later added to their exclude list automatically. Excluded nodes come back by themselves: see
the health monitor in [troubleshooting.md](troubleshooting.md). Never `scancel` hold jobs directly: running tasks on them would
be orphaned and requeued.

## Daemons & multi-node

Two background daemons, both cluster-wide singletons over the shared `~/.hive`:
the **scheduler** (`hive queue daemon …`, 30 s loop: dispatch, liveness, health checks,
cancel requests) and the **node poller** (`hive daemon …`, 15 min loop: `nvidia-smi`
per hold job → `node_monitor.json`). Both auto-start when needed (`hive submit`,
`hive nodes`); invoking hive from another node never spawns a duplicate. `hive queue
daemon status` prints host + heartbeat age. `hive cancel` on a running task from a node
other than the scheduler's shows `CANCELLING` for up to one cycle while the scheduler
stops the step.

## Staging models to /tmp

Loading a large model from Lustre/NFS is slow and can stall under concurrent loaders.
Copy it to node-local `/tmp` **inside the command** (it runs on the compute node):

```bash
hive submit --name eval-v1 --est-runtime 1h "
MODEL=/tmp/Qwen3-VL-4B-Instruct
[ ! -d \$MODEL ] && rsync -aL \
  \$(ls -d ~/.cache/huggingface/hub/models--Qwen--Qwen3-VL-4B-Instruct/snapshots/*/ | tail -1) \$MODEL/
python eval.py --model \$MODEL --skip-existing
"
```
