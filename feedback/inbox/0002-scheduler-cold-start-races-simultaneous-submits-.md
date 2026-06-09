---
id: 2
title: Scheduler cold-start races: simultaneous submits each spawn a hive-sched
severity: high
status: done
tags: [scheduler, concurrency, singleton]
submitter: si384883
hive_version: 0.3.2
task_ids: []
created: 2026-06-03T04:57:35
updated: 2026-06-03T04:57:35
source: cli
triage_note: Fixed: flock start-mutex in hive-queue cmd_daemon + hive-nodes daemon_start; immediate liveness publish. Verified 8-way concurrent -> 1+1.
---

## Symptom
When multiple agents `hive submit` SIMULTANEOUSLY with no scheduler yet running, each
sees `is_sched_running()==False` and starts its OWN hive-sched. Tested: 8 concurrent
agents against an isolated HIVE_DIR → 4 schedulers spawned. (The node poller stayed a
singleton — only the scheduler cold-start raced.)

## Root cause
`cmd_daemon("start")` in hive-queue was check-then-start (TOCTOU) with no mutex; the
scheduler writes its sched.pid/heartbeat only after it boots (~0.6s+), so racers in that
window all start. Pre-existing (not introduced by the C5 poller work).

## Impact
Multiple schedulers all dispatch from the one queue. queue.json flock prevents
data corruption / double-dispatch (each cycle re-reads under lock), but it multiplies
live-probe srun steps and is clearly unintended. Staggered submits were always fine
(2nd agent sees the heartbeat); only simultaneous cold-start raced.

## Fix (done)
Atomic start mutex: flock on sched.start.lock around the check+start, and publish
sched.pid+heartbeat immediately after Popen so racers bail fast. Same flock guard added
to the poller's daemon_start for robustness. Re-tested: 8 concurrent agents -> exactly
1 poller + 1 scheduler. flock is same-node-atomic always; cross-node-atomic where the
shared FS honors it (heartbeat publish covers the residual Lustre-visibility window).

## Auto-captured context

- hive_version: 0.3.2
- queue: cancelled:1351  done:1990  failed:336  pending:48  running:18
- nodes: updated=2026-06-03T08:57:08  busy:24  cpu:1  idle:4
