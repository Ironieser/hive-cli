---
id: 18
title: hive list shows tasks 7723-7726 as RUNNING 23h+ although their logs record 'fini
severity: medium
status: done
tags: []
submitter: si384883
hive_version: 0.4.0
task_ids: []
created: 2026-08-01T10:48:28
updated: 2026-09-28T15:36:33
source: cli
triage_note: hive list / hive wait restart a dead scheduler when tasks are active (ensure_sched); the scheduler heartbeats from its own thread, exits when its main loop is stuck, and refuses to start twice. Commits 44d90c1, 5b0acd2, e791466.
---

hive list shows tasks 7723-7726 as RUNNING 23h+ although their logs record 'finished ... exit_code=0' at 2026-07-31T11:36-12:02. Terminal state not reaped; queue.json out of sync with the task logs.

## Auto-captured context

- hive_version: 0.4.0
- queue: cancelled:2049  done:4787  failed:785  running:4
- nodes: updated=2026-07-31T15:28:57  idle:8  probe_failed:2
