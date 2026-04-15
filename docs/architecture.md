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
                   │  loop every 120s:   │
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
node_monitor.json    # DB written by daemon/poll, read by nodes/top
{
  "updated": "ISO8601",
  "jobs": {
    "<jobid>": {
      "node": "evcXX",
      "partition": "highgpu|normal",
      "job_elapsed": "3d13h",
      "gpu": [{"index":0, "util":87, "mem_used":42301, "mem_total":81920}],
      "processes": [{"pid":1234, "cpu":242, "mem":5.4, "elapsed":"1d2h", "cmd":"python ..."}],
      "status": "busy|idle|unknown",
      "polled_at": "ISO8601"
    }
  }
}

node_monitor.pid     # daemon PID (validated with kill -0 before use)
node_monitor.log     # daemon log, rolling 500 lines
```

## Node Probing Mechanism

```
hive-daemon (every 120s)
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
        ├── status = "busy" if any real process found, else "idle"
        └── write /tmp/hive_daemon_PID/jobid.json
      
  atomic: cat all frags → .json.tmp → mv → node_monitor.json
```

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
