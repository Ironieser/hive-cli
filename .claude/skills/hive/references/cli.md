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
hive list [--state S] [--limit N] [--days N] [--all]
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
hive submit [--name NAME] [--est-runtime DUR|auto] [--need-mb MiB] [--gpus N] \
            [--workdir DIR] [--priority N] "command string" | job.hive
```

| Flag | Default | Meaning |
|---|---|---|
| `--name` / `-n` | — | label; key for runtime history (`hive stats`, `--est-runtime auto`) |
| `--est-runtime` | — | `2h`, `90m`, `1-12:00:00`, seconds, or `auto` (P90 of NAME's history). With an estimate the scheduler never places the task on a node whose remaining walltime < estimate + 10 min (`insufficient_walltime`). Without one the task is walltime-blind. |
| `--need-mb` | 0 | minimum **free** GPU memory; task waits (`waiting_for_mem`) until a card has it |
| `--gpus` | 1 | GPUs the task may see. A hold job may own more; hive narrows `CUDA_VISIBLE_DEVICES` to the first N so frameworks don't auto-`DataParallel` over cards you didn't ask for. `--gpus 2` only places on hold jobs with ≥ 2 GPUs (`insufficient_gpus`). |
| `--workdir` / `-w` | cwd | working directory on the node |
| `--priority` / `-p` | 0 | higher dispatches first (`-p=-5` for negatives) |

CLI flags override the same directives in a `.hive` file. The command string runs
verbatim under bash on the node; quotes, `$VARS`, `&&`, multi-line — all fine.

## `.hive` script files

```bash
#!/bin/bash
#HIVE name=eval-v1
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

Directives: `workdir`, `priority`, `name`, `need_mb`, `gpus`, `est_runtime`. Other `#` lines
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
- For PENDING tasks the NODE column holds the scheduler's `pending_reason`.
- `(re-disp xN)` marks a task re-dispatched after its node was reclaimed.
- ELAPSED: `wait:…` since submit for pending; run time for running/finished.

## `pending_reason` values

| Reason | Meaning |
|---|---|
| `no_dispatchable_node` | No idle pool node → wait or `hive pool add` |
| `waiting_for_mem` | No card has the task's `--need-mb` free |
| `insufficient_gpus` | No hold job owns `--gpus N` cards → add a `--gres=gpu:N` hold job or lower N |
| `insufficient_walltime` | No node has est + 10 min left → `hive pool add --time …` or lower `--est-runtime` |
| `gpu_dirty` | Idle-looking card has > 5 GB resident (zombie / co-tenant) |
| `node_busy_on_verify` | Pre-dispatch probe found the card in use |
| `probe_unverifiable` | Couldn't run the probe (transient srun failure) |
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
hive pool config                 # presets + sbatch --test-only validation (✓/✗)
hive pool release --idle | JOBID # ⚠ HUMAN-ONLY: refuses without a TTY; no --yes/--force
```

`pool add` validates with `sbatch --test-only` first and redirects hold-job stdout to
`~/.hive/pool-logs/`. Never `scancel` hold jobs directly: running tasks on them would
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
