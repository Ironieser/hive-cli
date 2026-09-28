---
name: hive
description: Run GPU experiments on the SLURM cluster through hive — the pre-allocated node pool + task queue (hive submit / wait / list / logs / nodes / health). Use this whenever the user wants to train, fine-tune, evaluate or batch-infer on GPUs, mentions hive, hold jobs, the node pool, task IDs, or asks why a job is pending, stuck, slow, or died at CUDA init — even when they don't say "hive" and even if they phrase it as srun/sbatch. Also use it to check node status or cancel/inspect experiment tasks. Plain SLURM queue questions unrelated to running experiments belong to the slurm skill.
---

# hive — GPU node pool & experiment queue

hive keeps a pool of GPU nodes already allocated as long-lived SLURM "hold jobs" and
dispatches your commands onto them with `srun --overlap`, so an experiment starts in
seconds instead of waiting in the SLURM queue. You submit a command, hive picks a free
GPU, runs it, and records the exit code and log. Everything below is designed so a
single iteration costs you one short command and a few lines of output.

## The loop: submit → wait → branch

```bash
export HIVE_OWNER=my-agent-or-project      # once per session: tags every submit, scopes hive list
ID=$(hive submit --name train_v1 --est-runtime 2h \
      "python train.py --config v1.yaml --output_dir runs/v1 --resume" | grep -oP '#\K\d+')
hive wait "$ID" --pending-timeout 1800      # blocks; prints state changes, then the log TAIL
# exit codes:  0 done · 1 failed · 75 never dispatched · 130 cancelled
```

`hive wait` prints the last 40 log lines plus the header (node, cmd, GPU visibility). That
is usually enough to judge the run. Read more only when you need it:
`hive logs ID -n 200`, or `hive logs ID --full` for everything.

### Make every command resumable — before you submit

A hold job can be reclaimed by SLURM while your task runs. hive then requeues the task
onto another node and **re-runs the command from scratch** (up to 3 attempts); it cannot
resume a live process. So:

- training → checkpoint periodically and **load the latest checkpoint on start**;
- inference/batch → write outputs incrementally and **skip inputs already done**;
- pass `--est-runtime` so hive avoids nodes whose remaining walltime is too short.

A re-dispatch is visible as a `⚠` line in `hive wait`, `(re-disp xN)` in `hive list`, and
a banner in the log. A task whose *own command* crashes is `failed` and is not retried.

### Submit flags (all optional)

| Flag | Why you'd set it |
|---|---|
| `--owner NAME` | who this task belongs to (agent / project); defaults to `$HIVE_OWNER`. Several agents share one queue — this is how you find yours again |
| `--name NAME` | groups runtime history → `hive stats NAME`, `--est-runtime auto` |
| `--est-runtime 2h\|90m\|auto` | walltime-aware placement (`auto` = P90 of NAME's history) |
| `--need-mb 60000\|auto` | hold until a GPU has that much free memory (`auto` = measured peak of NAME's past runs + 10 %) |
| `--gpus N` | GPUs the task may see (default **1**; extras are hidden so frameworks don't auto-DataParallel) |
| `--timeout 2h` | kill the task after that much run time (failed, exit 124) — use it for anything that can hang |
| `--notify CMD` | run CMD when the task finishes or is requeued (`HIVE_TASK_*` env; runs on the scheduler host; default `$HIVE_NOTIFY`) |
| `--after ID,ID` | pipeline: run only after those tasks ended done (fails with them, exit 125). `--after-any` = whatever their outcome |
| `--array 0-9%4` | sweep: one task per index (`$HIVE_ARRAY_INDEX`, single-quote the command), at most 4 at once. `hive wait --array ID`, `hive cancel --array ID` |
| `--max-running N` | don't take more than N nodes at once for this owner |
| `--exclude NODES` | nodes this task must not run on (`evc22,evc[40-43]`) |
| `--workdir DIR` | cwd on the node (default: cwd at submit; must exist on the node) |
| `--priority N` | higher dispatches first |

Multi-line jobs go in a `.hive` file (`#HIVE name=… / est_runtime=… / gpus=…` directives,
then plain bash) and are submitted with `hive submit job.hive`. Full syntax:
[references/cli.md](references/cli.md).

## Reading state without flooding your context

These commands are sized for agents; prefer them over `cat ~/.hive/…` or `squeue`.

| Need | Command | Size |
|---|---|---|
| my tasks | `hive list` | active + last 10 finished, **only `$HIVE_OWNER`'s when set** (`--owner NAME`, `--owner all`, `--limit N`, `--state failed`) |
| one task's verdict | `hive wait ID` / `hive logs ID -n 50` | header + tail |
| several tasks / a sweep | `hive wait ID ID …` / `hive wait --array ID` | one line per task, no logs; exit 1 if any did not end done |
| is the pool healthy | `hive nodes` | one line per hold job + summary |
| why is it pending | `hive list` → NODE column shows the `pending_reason` | — |
| bad nodes | `hive health` | one line per node |
| how long do runs take | `hive stats NAME` | one line per name |

`hive logs ID` with no flags prints a log whole only if it is ≤ 200 lines; otherwise the
header and the last 100 lines with an omission notice.

## When a task is PENDING

The NODE column of `hive list` says why. The common ones:

| reason | meaning → what to do |
|---|---|
| `pool_empty` | the pool has no hold job → `hive pool add` (waiting will not help) |
| `no_dispatchable_node` | every hold job is busy → `hive nodes`; wait or `hive pool add` |
| `waiting_for_mem` / `insufficient_gpus` / `insufficient_walltime` | your task's requirement isn't met by any node → lower it or `hive pool add …` |
| `gpu_dirty` / `node_busy_on_verify` | a card looked free but isn't (co-tenant / zombie) → wait |
| `gpu_unresponsive` / `cuda_unavailable_on_verify` / `node_quarantined` | the node's GPU is broken; hive quarantines it → `hive health`, then `hive pool add` for a replacement |
| `infra_failure_redispatch` | node reclaimed mid-run; re-running elsewhere (progress lost unless checkpointed) |

Full table and node STATUS legend (`BUSY/CLAIM/IDLE/WARN/PFAIL/QUAR/CPU`):
[references/cli.md](references/cli.md). Stuck with free nodes? See
[references/troubleshooting.md](references/troubleshooting.md).

## Bad nodes are handled for you — but report what you see

Some nodes read IDLE yet kill every task at CUDA init (`CUDA-capable device(s) is/are busy
or unavailable`, `CUDA unknown error`). hive creates a real CUDA context before every
dispatch and quarantines a node that fails; two fast task deaths with that signature also
quarantine it, and the tripping task is re-run elsewhere. It re-probes quarantined nodes
every 10 min and releases them when healthy. If you still see a node killing tasks:

```bash
hive health report evc43 --reason "tasks die at CUDA init"   # quarantine now
hive health check evc43                                      # probe it right now
```

Don't `scancel` the hold job for this — the node, not the job, is at fault.

## Rules that keep the pool usable

1. **Never `scancel` hold jobs.** Use `hive pool release` — and only a human at a TTY can
   run it (it refuses non-interactive use; there is no `--yes`). Ask the user.
2. **Never put `srun` in the command.** hive already dispatches with `srun --overlap`.
3. **Stage big models to node-local `/tmp` inside the command** before loading; loading
   from Lustre is slow and can stall. Example in [references/cli.md](references/cli.md).
4. **`--workdir` must exist on the compute node** (home / Lustre paths are fine; a `/tmp`
   path from the submit node is not).
5. Concurrent `hive submit` from many agents/nodes is safe (one shared queue, one
   scheduler, one poller, all cluster-wide singletons). Don't start extra daemons by hand.
6. **Hit a hive bug or rough edge? File it**, don't work around it silently:
   `hive feedback "one line"` or `hive feedback submit --title … --severity … --task ID`.

## References (read when you need the detail)

- [references/cli.md](references/cli.md) — every command and flag, `.hive` directives,
  log format, all `pending_reason` values, node STATUS legend, daemons & multi-node.
- [references/troubleshooting.md](references/troubleshooting.md) — symptom → check → fix,
  scheduler health, quarantine mechanics, checkpoint-loss on reclaim.
- [references/state.md](references/state.md) — reading `~/.hive/*.json` programmatically
  (fields, which timestamps to use).
