# hive troubleshooting

| Symptom | Check | Fix |
|---|---|---|
| Task stuck PENDING | `hive list` → NODE column = `pending_reason` | see the table in [cli.md](cli.md#pending_reason-values) |
| `no_dispatchable_node` but `hive nodes` shows IDLE | `hive queue daemon status` | `stopped` → `hive queue daemon start`; `running` → nodes are being live-verified and rejected (look at the reason on the next cycle) |
| `pool_empty` | `hive nodes` shows no rows | every hold job expired; `hive pool add` — nothing dispatches until then |
| `gpu_unresponsive` / `no_gpu_devices` | `hive health` | the node's GPU driver is wedged (SLURM keeps handing such GPUs out because nobody keeps them). hive quarantines the node after 2 strikes; its hold job still uses allocation → `hive pool release JOBID`, then `hive pool add` |
| Queue stuck and the reason is unclear | `grep "not dispatchable" ~/.hive/sched.log \| tail` | the scheduler logs why it passed over each hold job |
| `insufficient_walltime` never clears | `hive nodes` LEFT column | `hive pool add --time …` or lower `--est-runtime`; this reason does not resolve by itself |
| `waiting_for_mem` | `hive nodes` MEM column | lower `--need-mb` or add a bigger card |
| `gpu_dirty` / `node_busy_on_verify` | `hive nodes` MEM | a zombie or co-tenant holds the card; wait or add nodes |
| Task FAILED in < 1 min | `hive logs ID -n 40` | CUDA-init errors → node problem: `hive health` (probably already quarantined); `hive health report NODE` if not. Otherwise it is your command. |
| Task FAILED, log ends abruptly, no footer | top of log / `hive list` | `srun` error at the top = hold job died; hive requeues those (`infra_failure_redispatch`) |
| `hive wait` prints ⚠ re-dispatched | — | node reclaimed mid-run; the new run started from scratch — make the command resume from its checkpoint |
| `hive list` shows RUNNING for hours but the log has a `finished` footer | `hive queue daemon status` | scheduler died before reaping. `hive list` / `hive wait` restart it by themselves when tasks are active; otherwise `hive queue daemon start` |
| Node shows `PFAIL` | `hive poll` | probe failed; `probe_detail` in `node_monitor.json` says why (`srun_failed` = couldn't run, `gpu_unresponsive` / `no_gpu_devices` = broken node). hive still verifies before dispatch |
| Task stays `CANCELLING` | `hive queue daemon status` | the scheduler stops running tasks (≤ 30 s). If it is alive but not acting: `hive cancel --force ID` marks the task cancelled now; from another host the step itself is only stopped once that scheduler acts |
| `hive queue daemon stop` says the scheduler is on another host | — | it leaves a stop request the scheduler honours within seconds; if it does not, log in to that host and stop it there |
| `pool_empty` although autoscale is on | `hive pool autoscale` | it says why it is not submitting (max_nodes reached by unusable hold jobs, daily limit, `until` passed) |
| `waiting_for_gang` never clears | `hive nodes` | the pool has fewer free NODES than `--nodes` asks for; two hold jobs on one node are one node |
| Task `failed` with exit 126, or `cancelled` with `gang_member_failed` | `hive list --state failed` | another member of its multi-node task failed; that member's log says why |
| `hive nodes` row says `[unreadable record: …]` | `hive poll` | the node DB holds something the table cannot read for that hold job; the other rows are right |
| Node shows `SLOW` | `hive health` | works, but CUDA needs minutes to initialise. Takes long tasks (`--est-runtime` ≥ 1h) or `--allow-slow`, after every faster node. A task there is silent for minutes at start-up — that is not a hang (the log header says so). Becomes normal again after 2 checks at normal speed |
| Node shows `QUAR` | `hive health` | quarantined; auto-released after 2 healthy probes, or `hive health clear NODE` |
| Quarantined node with no hold job on it | `hive health` RESULT column | the health monitor takes over: released when SLURM reports the node **rebooted** since the quarantine, or when a `hive_canary` job (10 min, 1 GPU, every 6 h, pinned to the node) probes healthy twice. `canary_pending` = waiting in the SLURM queue. Don't cancel `hive_canary` jobs |
| Node shows `CLAIM` with 0 % GPU | — | normal: a task is in cold import / model load; the slot is taken |
| `hive nodes` looks stale (`node!`, old "last polled") | `hive poll` | forces a poll; dispatch never trusts the table alone |
| `hive submit` errors "cmd must not contain srun" | — | drop the `srun` prefix; hive dispatches itself |
| Same command works on one node, dies at `torch.cuda.set_device` on another | `hive health check NODE` | node fault; report it |

## Scheduler health

```bash
hive queue daemon status   # hive-sched: running  PID …  host evc21  last heartbeat 20s ago
hive queue daemon logs     # last 40 lines
hive queue daemon start    # safe to call any time; a running scheduler is detected cluster-wide
```

The scheduler runs a cycle every 30 s: reap finished tasks, handle cancel requests,
re-probe quarantined nodes, dispatch pending tasks (live-verifying each target), then a
starvation watchdog that forces a node poll after 3 cycles with pending tasks and no
dispatch. A stopped scheduler means nothing dispatches and nothing is reaped.

## Quarantine mechanics (why a node is QUAR)

A node enters quarantine when creating a CUDA context on it fails — the pre-dispatch
verify probe does this every time, and `hive health check` does it on demand — or after
two tasks died there within 3 min with a CUDA-init signature (`CUDA-capable device(s)
is/are busy or unavailable`, `cudaErrorDevicesUnavailable`, `CUDA unknown error`,
`CUDA unknown error`, …). Reasons this happens: another user's job holds
the card outside your cgroup, a wedged driver, a hold job that got no visible device.

While quarantined the node receives no tasks. Every 10 min the scheduler creates a CUDA
context through one of the node's idle hold jobs; two consecutive successes (after a
1 h minimum) release it, a failure re-arms it, and "can't tell" (no python / libcuda)
is ignored. `hive health` shows strikes, last check time and result.

## Checkpoint loss on reclaim

Hold jobs have a walltime. When one expires while a task runs, the task is requeued to a
live node with `requeue_count` incremented, `checkpoint_warning` set, a banner in the log,
a `⚠` line in `hive wait`, and `(re-disp xN)` in `hive list`. The new run **starts from
scratch**. Two defences: make the command resume from its own checkpoints, and submit
with `--est-runtime` so the scheduler never places it on a node with less than
estimate + 10 min left.

## Filing feedback

Anything that looks like a hive bug or a missing feature: `hive feedback "…"` (one line)
or `hive feedback submit --title … --severity high --tags scheduler --task 123 --file
notes.md`. It auto-attaches the hive version, queue/node snapshots and the tail of the
named task logs, and lands in the maintainer's checkout for triage.

vLLM's `Engine core initialization failed` alone is NOT a node fault: it is printed for
every failure while the engine starts (an assertion in its CUDA-graph capture, a model
that does not fit the card, a wrong argument). Read the traceback above it in the log.
