---
id: 23
title: issues #16/#18 reproduced in one campaign: AWQ Command-R hit the set_device 'CUD
severity: medium
status: triaged
tags: []
submitter: si384883
hive_version: 0.4.0
task_ids: []
created: 2026-08-01T13:01:03
updated: 2026-09-25T06:53:37
source: cli
triage_note: Node-health theme: co-tenant / broken node (evc43, evc50) accepts dispatch, CUDA init fails. Needs squeue -w foreign-job detection + auto-quarantine after N fast failures (ROADMAP C4). Not fixed yet.
---

issues #16/#18 reproduced in one campaign: AWQ Command-R hit the set_device 'CUDA-capable device(s) is/are busy or unavailable' crash on evc43 three times (tasks 7866, 7885, 7891 — 7885 and 7891 both landed on evc43), and 'hive list' showed 5 tasks as RUNNING whose logs already carried 'finished ... exit_code=0'. Agents that trust list-state instead of the log exit record will wait forever or double-submit. Suggest: (a) blacklist a node after N consecutive set_device failures, (b) reap terminal state from the log's exit record.

## Auto-captured context

- hive_version: 0.4.0
- queue: cancelled:2084  done:4886  failed:818  pending:1  running:2
- nodes: updated=2026-08-01T16:43:25  busy:2  cpu:1  idle:12  probe_failed:2
