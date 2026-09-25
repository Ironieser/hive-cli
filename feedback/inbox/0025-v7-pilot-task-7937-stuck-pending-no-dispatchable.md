---
id: 25
title: v7 pilot: task #7937 stuck PENDING no_dispatchable_node for 20min while node_mon
severity: medium
status: done
tags: []
submitter: si384883
hive_version: 0.4.0
task_ids: []
created: 2026-08-01T15:32:16
updated: 2026-09-25T06:53:36
source: cli
triage_note: Same root cause as #24/#26: #7935/#7936 (gpus=2, need_mb 78000, prio 160) sat at the head and consumed all candidates. Fixed with #24.
---

v7 pilot: task #7937 stuck PENDING no_dispatchable_node for 20min while node_monitor.json reports 13 idle nodes and both daemons have fresh heartbeats (poll 15:31:32). Idle entries carry mem_free_mb=None, so a --need-mb 60000 task appears to be unschedulable even though identical --need-mb 60000 tasks (#7915/#7929/#7931/#7932/#7934) dispatched normally on the same pool minutes earlier. Suspect the memory probe returned null and the scheduler treats null as 'not enough' rather than 'unknown'.

## Auto-captured context

- hive_version: 0.4.0
- queue: cancelled:2090  done:4916  failed:828  pending:2
- nodes: updated=2026-08-01T19:31:32  busy:1  cpu:1  idle:13
