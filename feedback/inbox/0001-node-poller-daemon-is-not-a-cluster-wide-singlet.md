---
id: 1
title: Node poller daemon is not a cluster-wide singleton → cross-node duplicate daemons cause squeue/step chaos
severity: high
status: done
tags: [scheduler, daemon, cross-node, multi-node]
submitter: si384883
hive_version: 0.3.2
task_ids: []
created: 2026-06-03T03:15:41
updated: 2026-06-03T03:25:33
source: cli
triage_note: Implemented (heartbeat+host singleton on poller + poll-request/stop-request files). hive-daemon/hive-nodes/hive-top/hive-sched updated; logic tested. Deploy via install.sh + restart both daemons.
---

## Symptom
hive runs on a SLURM cluster with state in a shared-FS `~/.hive`. Multiple agents on
DIFFERENT nodes each invoke hive; each independently spawns its OWN node-poller daemon
(`hive-daemon`). With N pollers, every cycle runs N× `squeue -u $USER -t R` + N×
`srun --overlap` probes into all hold jobs → MaxStepCount pressure, probe contention,
probe failures, and oscillating `node_monitor.json` writes. Observed as "squeue /
scheduling chaos" when several projects use hive concurrently.

## Root cause (file:line)
- The node poller is NOT a cluster-wide singleton. `daemon_pid()` (hive-nodes:36) uses
  host-local `kill -0` and `node_monitor.pid` carries NO hostname (unlike `sched.pid`).
  From another node, `kill -0 <other-host-pid>` fails (or false-positives on an
  unrelated PID) → `daemon_start` (hive-nodes:454/464/503) launches a second daemon.
- The stale-daemon sweep `pgrep -f hive-daemon` (hive-nodes:90) is host-local → cannot
  reap cross-node duplicates.
- Cross-node force-repoll is broken too: scheduler `request_repoll()` and `hive poll`
  use `os.kill(pid, SIGUSR1)` (hive-sched:148) which only reaches a SAME-NODE poller.

## Contrast: the SCHEDULER is already correct
`hive-sched` IS a cluster singleton: hostname-tagged `sched.pid` + shared-FS
`sched.heartbeat` mtime check (`is_sched_running`, hive-sched:492/532). Task dispatch
is therefore NOT doubled — only the poller side lacks this mechanism.

## Proposed fix (mirror the scheduler's proven pattern — additive, low risk)
1. Singleton via shared-FS heartbeat + host record: write `node_monitor.pid` as
   `<pid>\n<host>`; daemon writes `node_monitor.heartbeat` each cycle; `daemon_running`
   = heartbeat age < 2×interval (cross-node) OR same-host `kill -0`.
2. Cross-node force-repoll via a `poll.request` file polled in the daemon's 1s tick
   (replaces SIGUSR1 for the cross-node case; keep SIGUSR1 as same-host fast path).
3. `hive daemon status/stop` cross-node aware (show host; stop via ssh or a stop-request
   file instead of silently spawning another).
4. (Optional) Lustre flock for hard mutual exclusion — but the author deliberately
   avoided flock on the networked FS for the scheduler, so prefer the heartbeat mirror.

## Not previously reported
Other reports flagged same-host concurrent-poll DB corruption (OPT-C, now fixed with a
flock) and praised the scheduler's cross-node heartbeat, but none noticed the poller
lacks the same cross-node singleton mechanism.

## Auto-captured context

- hive_version: 0.3.2
- queue: cancelled:1351  done:1984  failed:296  pending:92  running:17
- nodes: updated=2026-06-03T07:15:33  busy:15  cpu:1  idle:12
