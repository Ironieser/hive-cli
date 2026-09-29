# hive CLI reference

Contents: [Commands](#commands) · [`hive submit`](#hive-submit) · [`.hive` files](#hive-script-files)
· [`hive list`](#hive-list) · [`pending_reason` values](#pending_reason-values)
· [Node STATUS legend](#node-status-legend) · [Task log format](#task-log-format)
· [`hive health`](#hive-health) · [Pool management](#pool-management)
· [Daemons & multi-node](#daemons--multi-node) · [Staging models to /tmp](#staging-models-to-tmp)

## Commands

```bash
hive submit "CMD" [flags] | hive submit job.hive     # queue a task → "Submitted task #N"
hive submit -q …                                      # prints only the id (array id for --array)
hive wait ID [--pending-timeout SEC] [--log-lines N | --full-log | --no-log]
hive wait ID ID … | --array A [--pending-timeout SEC] # several: one line each, no logs
hive list [--owner NAME|all] [--state S] [--limit N] [--days N] [--all]
hive logs ID [-n N | --full] [-f]
hive cancel ID | --array A [--force]                  # pending or running
hive hold ID… | --array A      ·  hive unhold …       # keep pending tasks out of dispatch
hive priority N ID… | --array A                       # reprioritise pending tasks
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

Exit codes of `hive wait ID` (one task): **the command's own exit code** — `0` done, anything
else failed — with these reserved values:

| Code | Meaning |
|---|---|
| `124` | killed by `--timeout` |
| `125` | never ran: a task it was submitted `--after` did not end done |
| `1` | also: declared dead (heartbeat lost, recorded as -1) |
| `2` | no such task |
| `75` | still pending when `--pending-timeout` expired |
| `130` | cancelled |

`hive wait ID ID …` / `--array` (several tasks): `0` all done · `1` at least one failed or was
cancelled · `2` an id that never existed · `75` pending timeout. Test for `!= 0`, not `== 1`.

## `hive submit`

```bash
hive submit [--name NAME] [--est-runtime DUR|auto] [--need-mb MiB] [--gpus N] [--exclude NODES] \
            [--nodelist NODES] [--partition NAME] \
            [--timeout DUR] [--notify CMD] [--after ID,ID | --after-any ID,ID] \
            [--array SPEC] [--max-running N] [--nodes N [--same-node]] \
            [--preempt | --preemptible] \
            [--begin WHEN] [--cpus N] [--mem MiB] [--warn-before DUR] \
            [--workdir DIR] [--priority N] "command string" | job.hive
```

| Flag | Default | Meaning |
|---|---|---|
| `--name` / `-n` | — | label; key for runtime history (`hive stats`, `--est-runtime auto`) |
| `--owner` / `-o` | `$HIVE_OWNER` | owner tag (agent or project name). Precedence: `--owner` > `#HIVE owner=` > `$HIVE_OWNER`. Shown as an OWNER column in `hive list --owner all` and as `[owner]` in the `hive nodes` TASK column |
| `--est-runtime` | — | `2h`, `90m`, `1-12:00:00`, seconds, or `auto` (P90 of NAME's history). With an estimate the scheduler never places the task on a node whose remaining walltime < estimate + 10 min (`insufficient_walltime`). Without one the task is walltime-blind. |
| `--need-mb` | 0 | minimum **free** GPU memory (MiB), or `auto` = P90 of the GPU peak this NAME reached in past runs + 10 %. Task waits (`waiting_for_mem`) until a card has it |
| `--gpus` | 1 | GPUs the task gets. A hold job with N cards is N slots: it runs several tasks at once, each seeing only its own cards in `CUDA_VISIBLE_DEVICES` (so frameworks don't auto-`DataParallel` over cards you didn't ask for). `--gpus 2` only places on hold jobs with ≥ 2 GPUs (`insufficient_gpus`) of which 2 are free (`waiting_for_gpu`). `--gpus 0` is a CPU task: every GPU is hidden and none is taken. See [CPU tasks](#cpu-tasks). |
| `--timeout` | — | hard limit on **run** time (`2h`, `90m`). Over it the task is killed and ends `failed`, exit code 124, `fail_reason: timeout`; not retried. Unlike `--est-runtime`, which only steers placement |
| `--notify` | `$HIVE_NOTIFY` | shell command run when the task finishes (done / failed / cancelled / timeout) or is requeued after a node loss. See [Notification hook](#notification-hook) |
| `--quiet` / `-q` | — | print only the new id on stdout (notes go to stderr): `ID=$(hive submit -q …)` |
| `--after` | — | `ID,ID` or `aN` (= every member of array N). Run only after these tasks ended `done`. If one failed or was cancelled this task fails without running: exit code 125, `fail_reason: dependency_failed`, and so do the tasks `--after` it. Until then `waiting_for_dependency` |
| `--after-any` | — | run after these tasks ended, whatever the outcome (cleanup, reports) |
| `--array` | — | one task per index: `0-9`, `1,3,5`, `0-20:5`; append `%N` to run at most N at once (`0-9%4`). Same command for all, index in `$HIVE_ARRAY_INDEX`. See [Arrays](#arrays) |
| `--max-running` | `$HIVE_MAX_RUNNING` | cap on the **owner**: while any pending or running task of the owner carries a cap, at most that many (the lowest, if they differ) of the owner's tasks run at once — also the ones submitted without the flag (`owner_limit`). Needs an owner |
| `--allow-slow` / `--no-slow` | by estimate | whether the task may run on a **SLOW** node (works, but CUDA needs minutes to initialise — measured 146 s vs 4 s — then runs at its normal rate). Default: allowed when `--est-runtime` ≥ 1 h. Slow nodes are only used after every faster node |
| `--nodes` | — | multi-node task, 2–16 members. See [Multi-node tasks](#multi-node-tasks). Not with `--array`, `--preempt`, `--preemptible`, `--gpus 0` |
| `--same-node` | no | with `--nodes`: members may share a node, each with GPUs of its own |
| `--preemptible` | no | the task may be stopped and **requeued** (it starts afresh; at most 5 times) when a `--preempt` task of strictly higher priority finds no node. Pending reason afterwards: `preempted` |
| `--preempt` | no | when no node is free, stop ONE running `--preemptible` task of **strictly lower priority** whose node this task can use — so give it a `--priority` above 0. The freed node is kept for it. Shows `preempting` until it has the node. `--no-preempt` / `--no-preemptible` override a `.hive` file |
| `--begin` | — | earliest start: a delay (`2h`), a date and time (`2026-10-01T08:00`) or a time of day (`08:00`, tomorrow if past). A bare number or a time in the past is an error. `waiting_for_begin` until then |
| `--cpus` / `--mem` | 1 / none | CPUs and memory (MiB) the task takes from its hold job, and the limits of its step. The tasks on a hold job never take more than it has (`hive nodes` shows a CPU hold job's `taken/total CPU`): a task waits (`waiting_for_cpu`, `waiting_for_ram`) while they are taken and is told at submit when no hold job is that large (`insufficient_cpus`, `insufficient_ram`). A task is bound to cores of its own (`taskset`; its log says which, `nproc` and `$HIVE_CPUS` give the number) — SLURM itself puts every task of a hold job on the same cores. Without `--cpus` a task with a GPU takes no CPUs of its own (it shares all of its hold job's, as before) and a `--gpus 0` task takes 1. A task over its `--mem` is killed by SLURM |
| `--warn-before` | — | send `SIGUSR1` this long before the node's walltime ends — once per run, to **every process of the command** (it may arrive twice). The program that should checkpoint must handle it: a process without a handler is ended by SIGUSR1. Shell scripts around it are taken care of. Not sent in a command's first 60 s; the task is not placed on a node that expires sooner than that. The notify hook gets `HIVE_TASK_EVENT=expiring` |
| `--nodelist` | — | run ONLY on these nodes (`evc104,evc[102-103]`). The task waits (`waiting_for_node`) while none of them has a free hold job — also if the pool has no hold job there at all, which submit tells you |
| `--partition` | — | run only on hold jobs of this SLURM partition (`hive nodes`, column PART) |
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
`timeout`, `notify`, `after`, `after_any`, `array`, `max_running`, `allow_slow`, `nodes`,
`same_node`, `preempt`, `preemptible`, `begin`, `cpus`, `mem`, `warn_before`, `nodelist`,
`partition`. They may be
spelled like the flags (`#HIVE warn-before=10m`); yes/no ones may stand alone
(`#HIVE preemptible`). An unknown directive, or one without its value, is an error. Other `#` lines
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
hive submit --name report --after a120 "python report.py"     # after the WHOLE array
```

An array is N ordinary tasks that share `array_id` (the id of the first). They show as
`sweep[3]` in `hive list`, keep one name for `hive stats` / `auto`, and each has its own
log. Inside the command: `HIVE_TASK_ID`, `HIVE_TASK_NAME`, `HIVE_TASK_OWNER`,
`HIVE_ARRAY_ID`, `HIVE_ARRAY_INDEX`. `--after 120` waits for task 120 only — member `[0]`; the
whole array is `--after a120`.

### Multi-node tasks

```bash
hive submit --name ddp --nodes 4 --est-runtime 6h \
  'python train.py --rank $HIVE_GANG_RANK --world $HIVE_GANG_SIZE --master $(echo $HIVE_GANG_HOSTS | cut -d, -f1)'
#   Submitted multi-node task #300: 4 members (ids 300-303)
hive wait --array 300
```

The command runs once per member, on N **different nodes**, all started in the same
scheduler cycle or not at all (`waiting_for_gang` until N nodes are free at once).
`HIVE_GANG_HOSTS` lists the nodes in rank order. hive starts the processes; connecting
them (rank 0 listening, the others joining) is the command's job. If one member fails, is
cancelled or loses its node, the others are stopped — they end `cancelled` with
`fail_reason: gang_member_failed` — and members that had not started fail with exit
code 126. A multi-node task is never restarted in part. `hive wait ID`, `hive hold`,
`hive priority` on one member act on the whole task.

**Cards of different hold jobs on one node.** SLURM shows a job only its own GPUs, so no
single process can use the cards of two hold jobs, even on the same node. Two processes
can: `--nodes 2 --same-node` starts one member in each hold job, and they work together
like on two nodes. For one process with several cards, the hold job itself must own them
(`--gres=gpu:N` in the pool preset), then `--gpus N`.

### CPU tasks

`hive submit --gpus 0 [--cpus N] [--mem MiB] "…"` is a task that needs no GPU
(preprocessing, scoring, packing results).

- **Where it runs.** On a hold job without a GPU if the pool has one — `hive nodes` shows
  it as `CPU` with `taken/total CPU` — else beside the tasks of a GPU hold job (one that still
  has a free card), sharing its cores with GPU tasks that did not say `--cpus`.
- **How many at once.** A CPU hold job runs as many tasks as it has CPUs for: each takes
  its `--cpus` (1 unless it says so) and is bound to that many cores of its own. A 32-CPU hold job runs 32
  one-CPU tasks, or 4 with `--cpus 8`; the rest wait (`waiting_for_cpu`).
- **Getting one.** A hold job without a GPU is an sbatch script without `--gres`, as a
  preset in `pool_config.json`: `hive pool add cpu`. Its job name must not be
  `cursor_ssh_proxy` (such jobs are not part of the pool). Autoscale neither counts
  nor submits them.
- Before a task is sent to a CPU hold job hive checks that a step can be started there
  (the hold job may have expired since the last poll).
- On a GPU hold job a `--gpus 0` task steps aside (`waiting_for_cpu`) while a GPU task
  ahead of it in the queue waits for CPUs there.
- `--cpus` on a GPU hold job gives the task cores of its own among the tasks that said
  `--cpus`; tasks that did not are not bound and may use those cores too.
- A task that wants a GPU never goes to a CPU hold job; a CPU task neither clears nor
  adds to a node's GPU strikes. A CPU hold job is used even on a node quarantined for
  its GPU.

### Notification hook

The hook runs **on the scheduler's host** (see `hive queue daemon status`), not where you
submitted and not on the compute node; cwd is `$HOME`, limit 60 s, output in
`~/.hive/logs/notify.log`. A failing hook never changes the task's outcome. Its environment
is minimal — `PATH`, `HOME`, `USER`, `HIVE_DIR`, `HIVE_OWNER` (the task's) and the
`HIVE_TASK_*` below — not your shell's: pass what it needs inside the command. At most 8
hooks run at once; `--notify ''` switches `$HIVE_NOTIFY` off for one task.

| Variable | Value |
|---|---|
| `HIVE_TASK_EVENT` | `finish`, `requeue` (node lost or preempted; the task will run again from scratch) or `expiring` (`--warn-before`) |
| `HIVE_TASK_ID` / `NAME` / `OWNER` | as submitted |
| `HIVE_TASK_STATE` | `done`, `failed`, `cancelled`; `pending` on `requeue` |
| `HIVE_TASK_EXIT_CODE` | exit code; 124 = timeout, 125 = dependency failed (never ran), -1 = declared dead |
| `HIVE_TASK_FAIL_REASON` | `timeout`, `dependency_failed`, `gang_member_failed` or empty |
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
| `node_slow` | Only SLOW nodes are free and the task does not accept them → `--allow-slow`, or give an `--est-runtime` ≥ 1 h, or wait for a fast node |
| `verifying_node` | The node it would take is being probed; decided within a cycle (~30 s) |
| `waiting_for_begin` | `--begin` lies in the future |
| `waiting_for_gang` | A multi-node task needs N nodes free at the same time |
| `preempted` / `preempting` | It was stopped for a task of higher priority and waits again / it has asked a `--preemptible` task to stop |
| `waiting_for_cpu` / `waiting_for_ram` | The hold jobs it could use have their CPUs / memory taken by other tasks (`--cpus`, `--mem`); it starts when one ends |
| `insufficient_cpus` / `insufficient_ram` | No hold job HAS that many CPUs / that much memory → lower `--cpus` / `--mem` or add a larger hold job |
| `waiting_for_gpu` | The hold jobs with enough cards have them taken by other hive tasks; it starts when one ends |
| `held` | `hive hold` was used on it; `hive unhold ID` lets it go |
| `waiting_for_node` | The nodes or partition it was restricted to (`--nodelist`, `--partition`) have no free hold job |
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
| `SLOW` | works, but CUDA needs minutes to initialise (`hive health`) | long tasks / `--allow-slow` only, after faster nodes |
| `CPU` | hold job has no GPU | no |

GPU% is the busiest card of the hold job, MEM the sum over its cards (`x2` = two cards).
`LEFT` = remaining walltime (red under 1 h). `node!` = that row's poll is > 10 min old;
its TASK column then says how old (`[read 25m ago]`).
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
probes (minimum 1 h hold). A node without any hold job is checked by the health monitor
instead (reboot detection, canary job — see troubleshooting.md).
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
level or per preset). `--no-auto-exclude` turns the first off.

**Autoscale.** With an `"autoscale"` block in `~/.hive/pool_config.json` the scheduler keeps
`min_nodes` usable hold jobs by itself and replaces the ones about to expire; agents do not
need to `pool add`. `hive pool autoscale` shows the setting and what it would do now. It
is bounded by `max_nodes` (default `min_nodes` + 2; every hold job counts, usable or
not), 2 submissions per decision, one decision per 10 min, 12 submissions per day and
`until`, which is required. It counts hold jobs by asking SLURM and submits nothing when
it cannot, or when its state file cannot be read or written. `"enabled"` must be `true`.
With `"active_within": "48h"` it only acts while a task was submitted in that time (or is
still pending or running): an unused pool runs out by itself, and the first `hive submit`
afterwards brings it back — the hold jobs are submitted at the next decision, within
10 minutes, and then wait in the SLURM queue like any job.

Other keys of `pool_config.json`: `"prefer_partitions": ["highgpu"]` (tried first),
`"fair_share": true` (equal priority: the owner who used fewer GPU-hours in the last 24 h
goes first), `"exclude"`, `"auto_prune_days"` (finished tasks leave the queue after this
many days, default 14, 0 = never; their history and `auto` values stay),
`"log_keep_days"` (task logs are deleted after this many days; default: never).

 Hold jobs still waiting in the SLURM queue get
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
