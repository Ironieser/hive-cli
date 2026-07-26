---
id: 6
title: Scheduler stuck: 11 highgpu nodes GPU-idle for hours (some 4.5h, 0% util, no pro
severity: medium
status: duplicate
tags: []
submitter: si384883
hive_version: 0.3.2
task_ids: []
created: 2026-07-14T20:55:37
updated: 2026-07-26T13:50:45
source: cli
triage_note: Same root cause as #10 (warning was a terminal state: hive-dbpost never cleared gpu_idle_since, hive-sched excluded warning). Fixed with #10.
---

Scheduler stuck: 11 highgpu nodes GPU-idle for hours (some 4.5h, 0% util, no processes) permanently shown 'warning'/'busy' in hive nodes, never transition to IDLE, so scheduler only dispatches to the evc26 CPU-trap. mynode-poll writes status=idle but hive nodes shows warning -> starvation (16 pending, 0 dispatched). Node monitor daemon restart did not clear it.

## Auto-captured context

- hive_version: 0.3.2
- queue: cancelled:1757  done:4273  failed:710  pending:11  running:6
- nodes: updated=2026-07-15T00:54:23  busy:3  cpu:1  idle:1  warning:11
