---
id: 7
title: hive nodes shows STATUS=IDLE (busy:0 idle:6) for nodes that have a RUNNING hive
severity: medium
status: triaged
tags: []
submitter: si384883
hive_version: 0.3.2
task_ids: []
created: 2026-07-17T08:25:10
updated: 2026-07-26T13:51:03
source: cli
triage_note: Display-only, canonical entry for the IDLE-vs-claimed confusion (with #4). Dispatch is correct (get_candidates excludes used_jobids); hive nodes should show CLAIMED/STARTING when queue.json has a running task on that jobid.
---

hive nodes shows STATUS=IDLE (busy:0 idle:6) for nodes that have a RUNNING hive task, when the task is still in cold import / pre-GPU phase (0% GPU, 0G mem). This contradicts 'no_dispatchable_node' on pending tasks and reads as a scheduler bug. get_candidates() correctly excludes jobids in used_jobids (running tasks), so dispatch is right — only the display is wrong. Suggest: show BUSY (or a distinct 'STARTING'/'CLAIMED' state) when a node's hold-jobid has a running task in queue.json, regardless of GPU utilization. Cost us a false-alarm debug session chasing the hive-dbpost stuck-warning bug.

## Auto-captured context

- hive_version: 0.3.2
- queue: cancelled:1776  done:4287  failed:746  pending:26  running:9
- nodes: updated=2026-07-17T12:24:29  idle:9
