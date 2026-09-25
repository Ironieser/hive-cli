---
id: 18
title: hive list shows tasks 7723-7726 as RUNNING 23h+ although their logs record 'fini
severity: medium
status: triaged
tags: []
submitter: si384883
hive_version: 0.4.0
task_ids: []
created: 2026-08-01T10:48:28
updated: 2026-09-25T06:53:38
source: cli
triage_note: Scheduler process died -> running tasks never reaped. Proposed: hive wait/list auto-start the scheduler when stopped with running tasks (like submit). Not fixed yet.
---

hive list shows tasks 7723-7726 as RUNNING 23h+ although their logs record 'finished ... exit_code=0' at 2026-07-31T11:36-12:02. Terminal state not reaped; queue.json out of sync with the task logs.

## Auto-captured context

- hive_version: 0.4.0
- queue: cancelled:2049  done:4787  failed:785  running:4
- nodes: updated=2026-07-31T15:28:57  idle:8  probe_failed:2
