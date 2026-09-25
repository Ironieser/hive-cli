# hive-cli Architecture

## Workflow Overview

```
┌─────────────────────────────────────────────────────────────────────┐
│                         User / Agent                                 │
└───────────────────────────────┬─────────────────────────────────────┘
                                │  hive <subcommand>
                                ▼
┌─────────────────────────────────────────────────────────────────────┐
│                        hive  (dispatcher)                            │
│                                                                      │
│  argv[0] compat routing:                                             │
│    myjob → jobs │ mynode → nodes │ jobtop → top                     │
│                                                                      │
│  hive jobs ──────────────────────────────► libexec/hive-jobs        │
│  hive nodes ─────────────────────────────► libexec/hive-nodes       │
│  hive top  ──────── python3 ─────────────► libexec/hive-top         │
│  hive daemon start/stop/… ───────────────► libexec/hive-nodes       │
│  hive poll ──────────────────────────────► libexec/hive-poll        │
└─────────────────────────────────────────────────────────────────────┘
         │                    │                         │
         ▼                    ▼                         ▼
  ┌─────────────┐    ┌─────────────────┐      ┌──────────────────┐
  │ hive-jobs   │    │  hive-nodes     │      │  hive-poll       │
  │             │    │                 │      │  (one-shot)      │
  │ squeue      │    │ start/stop      │      │                  │
  │ sinfo       │    │ daemon          │      │  srun --overlap  │
  │             │    │                 │      │  nvidia-smi + ps │
  │ Shows:      │    │ show_table()    │      │  → DB write      │
  │ • job list  │    │ reads DB        │      └──────────────────┘
  │ • idle H100 │    │                 │               ▲
  │ • QOS block │    └────────┬────────┘               │
  └─────────────┘             │ auto-start             │ trigger
                              ▼                        │
                   ┌─────────────────────┐             │
                   │   hive-daemon       │─────────────┘
                   │   (background)      │  SIGUSR1 = poll now
                   │                     │
                   │  loop every 900s:   │
                   │  ┌───────────────┐  │
                   │  │ squeue -t R   │  │
                   │  │ for each job: │  │
                   │  │  srun overlap │  │
                   │  │  nvidia-smi   │  │
                   │  │  ps -u $USER  │  │
                   │  │  → JSON frag  │  │
                   │  └──────┬────────┘  │
                   │         │           │
                   │  atomic mv → DB     │
                   └─────────┬───────────┘
                             │ writes
                             ▼
                   ┌─────────────────────┐
                   │  ~/.hive/           │
                   │  node_monitor.json  │◄──── hive-nodes (reads)
                   │  node_monitor.pid   │◄──── hive-top   (reads)
                   │  node_monitor.log   │
                   └─────────────────────┘
                             ▲
                   ┌─────────┴───────────┐
                   │   hive-top (TUI)    │
                   │                     │
                   │  curses loop:       │
                   │  • read DB / 30s    │
                   │  • ↑↓ navigate      │
                   │  • Enter: expand    │
                   │    full cmd + procs │
                   │  • r: SIGUSR1 →     │
                   │    daemon poll now  │
                   │  • q: quit          │
                   └─────────────────────┘
```

## Component Responsibilities

| Component | Role | Reads | Writes |
|---|---|---|---|
| `hive` | Dispatcher, path resolution, argv[0] compat | — | exports env vars |
| `hive-jobs` | SLURM queue dashboard | squeue/sinfo | stdout |
| `hive-nodes` | Node table (one-shot) + daemon lifecycle | `node_monitor.json` | stdout |
| `hive-daemon` | Background poller (persistent process) | squeue, srun, nvidia-smi, ps | `node_monitor.json` |
| `hive-poll` | One-shot poller (same logic as daemon cycle) | squeue, srun, nvidia-smi, ps | `node_monitor.json` |
| `hive-top` | Interactive curses TUI | `node_monitor.json` | SIGUSR1 to daemon |

## Data Flow

```
SLURM cluster
  squeue ──► job list ──► daemon ──► node_monitor.json ──► hive-nodes
                                                        └──► hive-top

  srun --jobid=<id> --overlap
    └── nvidia-smi ──► GPU util/mem
    └── ps -u $USER ──► processes    } assembled into per-job JSON
```

## State Files (`~/.hive/`)

```
node_monitor.json    # DB written by daemon/poll (+ hive-dbpost), read by nodes/top/sched
{
  "updated": "ISO8601",
  "jobs": {
    "<jobid>": {
      "node": "evcXX",
      "partition": "highgpu|normal",
      "job_elapsed": "3d13h",
      "gpu": [{"index":0, "util":87, "mem_used":42301, "mem_total":81920}],
      "processes": [{"pid":1234, "cpu":242, "mem":5.4, "elapsed":"1d2h", "cmd":"python ..."}],
      "status": "idle|busy|warning|cpu|probe_failed",   # see docs/status_model.md
      "gpu_idle_since": null,                            # busy→idle grace timer
      "carried_forward": true,                           # (optional) last-good reused after a probe miss
      "time_left_secs": 79200,                           # remaining hold-job walltime (squeue %L); -1=unlimited, null=unknown
      "polled_at": "ISO8601"
    }
  }
}

node_monitor.json.lock   # flock serializing concurrent pollers (poll + daemon)
node_monitor.pid         # daemon PID (validated with kill -0 before use)
node_monitor.log         # daemon log, rolling 500 lines
pool-logs/slurm-<id>.out # hold-job stdout (redirected by `hive pool add`)

queue.json               # task queue (hive-queue / hive-sched, flock on queue.lock)
# per task: id, cmd, workdir, name, state, priority, need_mb, gpus, est_runtime_secs,
#   slurm_jobid, node, srun_pid, exit_code, pending_reason, requeue_count, log, …
#   submitted_at/started_at/dispatched_at/finished_at   naive-local strings (display)
#   submitted_ts/started_ts/dispatched_ts/finished_ts   epoch seconds — USE THESE for
#                                                        arithmetic (processes run under
#                                                        different TZs; see status_model.md)
#   cancel_requested   set by `hive cancel` from a node other than the scheduler's; the
#                      scheduler kills the srun step and marks the task cancelled.

events.jsonl             # durable append-only task lifecycle log (hive_events.py)
{"ts":"…","t":1780000000.0,"event":"submit",  "task":5,"name":"train","est_runtime_secs":7200}
{"ts":"…","event":"dispatch","task":5,"node":"evc23","slurm_jobid":"584954","node_time_left_secs":45000}
{"ts":"…","event":"finish",  "task":5,"state":"done","run_secs":4332,"queued_secs":192}
{"ts":"…","event":"requeue", "task":6,"reason":"infra_failure","checkpoint_lost":true}
# append-only timing history (run/queue durations); survives queue.json pruning.
# read by `hive stats` + `--est-runtime auto`; appends serialized by queue.lock.
# bounded: auto-trimmed to the recent tail past ~4 MB (hive_events.py), so it can't
# grow without limit and reads stay cheap (stats/estimates reflect recent history).
```

> **Status semantics & dispatch gating are specified in
> [status_model.md](status_model.md).** Probe failures become `probe_failed`
> (never `cpu`/`busy`); the scheduler verifies uncertain nodes with a live probe and
> gates dispatch on a GPU-clean / free-mem check rather than trusting `status`
> blindly.

## Cluster-singleton daemons (multi-node safety)

`~/.hive` is on the shared filesystem and agents may invoke hive from **different**
nodes. Both daemons are therefore **cluster-wide singletons**, coordinated over the
shared FS rather than with host-local PID checks (which would let every node spawn its
own copy — feedback C5):

| | scheduler `hive-sched` | poller `hive-daemon` |
|---|---|---|
| PID file | `sched.pid` = `<pid>\n<host>` | `node_monitor.pid` = `<pid>\n<host>` |
| heartbeat | `sched.heartbeat` (30s) | `node_monitor.heartbeat` (~30s) |
| "running?" | heartbeat fresh (any node) **or** same-host PID alive | same |
| force action | — | `node_monitor.poll-request` file → immediate poll |
| remote stop | SIGTERM same-host | `node_monitor.stop-request` file (honored each tick) |
| cancel a running task | `cancel_requested` on the task → scheduler SIGTERMs its srun child (same-host CLI fast path: direct kill) | — |

SIGUSR1 is kept only as a same-host fast path for forcing a re-poll; the request files
are what make it work across nodes. Result: one queue, one scheduler, one poller for the
whole cluster regardless of how many agents/nodes call hive.

## Node Probing Mechanism

```
hive-daemon (every 900s)
  │
  ├── squeue -u $USER -t R  →  job list (jobid|node|partition|elapsed)
  │
  └── for each job (parallel, </dev/null to prevent stdin consumption):
        srun --jobid=<id> --overlap -n1 --mem=0 bash -c "
          nvidia-smi --query-gpu=index,util,mem_used,mem_total --format=csv
          echo '---PS---'
          ps -u $USER -o pid,%cpu,%mem,etime,args
        "
        │
        ├── parse nvidia-smi → gpu[] array
        ├── parse ps → processes[] array (filter: sleep/bash/srun noise)
        ├── status from GPU only: busy if util≥5 or mem≥500MiB; cpu if no GPU;
        │      probe_failed if the probe returned nothing (after one retry); else idle
        └── write /tmp/hive_daemon_PID/jobid.json
      
  flock(node_monitor.json.lock); cat all frags → .json.$$.tmp → mv → node_monitor.json
  then hive-dbpost: apply busy→idle warning timer + carry-forward of probe_failed jobs
```

The scheduler (`hive-sched`) does **not** trust `status` blindly: see
[status_model.md](status_model.md) for verify-before-dispatch, the GPU-clean gate,
`pending_reason`, the starvation watchdog, and crash-safe dispatch.

## Environment Variables

| Variable | Default | Purpose |
|---|---|---|
| `HIVE_DIR` | `~/.hive` | Config/DB directory |
| `HIVE_PYTHON` | auto-detected | Python3 interpreter for `hive top` |
| `DB_FILE` | `$HIVE_DIR/node_monitor.json` | Overridable DB path |
| `PID_FILE` | `$HIVE_DIR/node_monitor.pid` | Daemon PID file |
| `LOG_FILE` | `$HIVE_DIR/node_monitor.log` | Daemon log file |
| `DAEMON_BIN` | `libexec/hive-daemon` | Exported by dispatcher |
| `POLL_BIN` | `libexec/hive-poll` | Exported by dispatcher |
