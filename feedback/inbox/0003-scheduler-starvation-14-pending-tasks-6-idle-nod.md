---
id: 3
title: scheduler starvation: 14 pending tasks + 6 IDLE nodes for >20min, 'no_dispatchab
severity: medium
status: duplicate
tags: []
submitter: si384883
hive_version: 0.3.2
task_ids: []
created: 2026-07-14T13:48:49
updated: 2026-07-26T13:51:03
source: cli
triage_note: Superseded by the reporter's own follow-up #4: not a scheduler bug, a display issue (slot-claimed nodes shown IDLE). Tracked under #7.
---

scheduler starvation: 14 pending tasks + 6 IDLE nodes for >20min, 'no_dispatchable_node'; dispatches only ever fire immediately after a 'Task finished' event (see sched log 13:00-13:48 on 07-14), the periodic idle scan never places tasks. Forced 'hive poll' does not help. Tasks affected: 6648 (waited ~25min with idle cards), 6651.

## Auto-captured context

- hive_version: 0.3.2
- queue: cancelled:1659  done:4216  failed:698  pending:14  running:8
- nodes: updated=2026-07-14T17:48:36  busy:1  idle:6  warning:1
