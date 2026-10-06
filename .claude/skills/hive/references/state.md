# Reading hive state programmatically

Prefer the CLI (`hive list`, `hive nodes`, `hive health`) — it is sized for context.
When you need structured data, the files under `~/.hive/` are plain JSON:

| File | Written by | Holds |
|---|---|---|
| `queue.json` | `hive submit` / scheduler | every task (`tasks: {id: {...}}`) |
| `node_monitor.json` | node poller | one record per hold job (`jobs: {jobid: {...}}`) + `updated` |
| `node_health.json` | scheduler / `hive health` | per-node quarantine state |
| `events.jsonl` | submit / scheduler | append-only lifecycle log (submit, dispatch, finish, requeue, cancel, quarantine, release, pool_release) |
| `logs/task-<id>.log` | the task | stdout + stderr with hive header/footer |

```python
import json, os, time
H = os.path.expanduser("~/.hive")
q  = json.load(open(f"{H}/queue.json"))["tasks"]
db = json.load(open(f"{H}/node_monitor.json"))
hl = json.load(open(f"{H}/node_health.json")).get("nodes", {}) if os.path.exists(f"{H}/node_health.json") else {}

running = [t for t in q.values() if t["state"] == "running"]
pending = [t for t in q.values() if t["state"] == "pending"]
idle    = [(j, v["node"]) for j, v in db["jobs"].items() if v["status"] == "idle"]
quarantined = {n for n, r in hl.items() if r.get("state") == "quarantined"}
```

## Task fields worth knowing

- `owner`: agent / project tag from `--owner` / `#HIVE owner=` / `$HIVE_OWNER` (may be empty).
- `state`: `pending → running → done | failed | cancelled`; `pending_reason` while pending;
  `cancel_requested` on a running task the scheduler is about to stop.
- **Timestamps: use the epoch fields** `submitted_ts`, `started_ts`, `dispatched_ts`,
  `finished_ts` for arithmetic. The matching `*_at` strings are naive local time of
  whichever process wrote them (scheduler, poller and agents can run under different
  time zones), so subtracting two of them can be off by hours. Tasks written before
  v0.4.1 only have `*_at`.
- `duration_secs`, `queued_secs`: measured at finish.
- `exit_code`: the command's exit status (`-1` = declared dead: heartbeat lost).
- `requeue_count`, `checkpoint_warning`, `attempts`: re-dispatch history.
- `gpus`, `need_mb`, `est_runtime_secs`, `est_source` (`user` | `history`): placement inputs.
- `node`, `slurm_jobid`, `log`: where it ran and where its output is.
- `timeout_secs`, `notify`, `exclude_nodes`, `only_nodes`, `partition`, `allow_slow`,
  `held`: as submitted.
- `depends_on` (ids), `depends_mode` (`ok` | `any`), `failed_dependency`.
- `array_id`, `array_index`, `array_max_running`; `max_running` (owner cap).
- `fail_reason` (`timeout` | `dependency_failed`), `exit_code` (124 / 125 for those).
- `gang_size`, `gang_hosts` (multi-node members; `array_id` / `array_index` = rank),
  `preempt`, `preemptible`, `preempt_count`, `begin_ts`, `cpus`, `mem_mb`, `cpu_slots` (positions in the hold job's core list the task is bound to),
  `warn_before_secs`, `warned_ts`.
- `gpu_slots`: positions of the cards it holds within the hold job (absent on tasks
  dispatched before GPU slots: those hold the whole hold job).
- `gpu_peak_mb`, `gpu_avg_util`, `gpu_max_util`, `gpu_samples`: measured on the node.

## Node record fields

- `status`: `idle | busy | warning | probe_failed | cpu` (`CLAIM` and `QUAR` in
  `hive nodes` are display-only overlays from `queue.json` / `node_health.json`).
- `gpu`: `[{index, util, mem_used, mem_total}]` for every GPU the hold job owns.
- `cpus`, `mem_mb`: what SLURM allocated to the hold job (`squeue -O tres-alloc`); `null` =
  not known, then nothing is counted against it. `cpu_only`: allocated no GPU.
- `time_left_secs`: remaining walltime measured at the DB's `updated` (`-1` unlimited,
  `null` unknown); subtract the DB age for a live value.
- `carried_forward: true`: last-good reading reused after a transient probe miss.
- `probe_detail`: why a `probe_failed` probe failed — `srun_failed`, `gpu_unresponsive`,
  `no_gpu_devices`; empty otherwise. The last two are never carried forward.
- `polled_at`: when this record was last probed successfully (UTC).

## Health record fields

`state` (`quarantined | slow | ok`), `slow_init_secs`, `reason`, `source` (`verify | auto | agent | manual`),
`strikes`, `ok_streak`, `since`, `until` (minimum hold), `last_check`, `last_result`,
`history` (last 20 events), `hold_fails` (`{jobid: {n, first, last}}`: failed periodic checks
through each hold job, in a row). Top level, next to `nodes`: `released_jobs` — hold jobs
the scheduler cancelled for a broken GPU (`jid`, `node`, `t`, `reason`; `failed` if SLURM refused).
