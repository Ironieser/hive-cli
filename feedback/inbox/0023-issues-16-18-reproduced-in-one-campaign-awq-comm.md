---
id: 23
title: issues #16/#18 reproduced in one campaign: AWQ Command-R hit the set_device 'CUD
severity: medium
status: done
tags: []
submitter: si384883
hive_version: 0.4.0
task_ids: []
created: 2026-08-01T13:01:03
updated: 2026-09-25T07:29:18
source: cli
triage_note: Fixed: hive health — verify-before-dispatch creates a real CUDA context (ctypes/libcuda) and quarantines the node on failure (evc43 verified: cuCtxCreate=999); fast task failures with a CUDA-init log signature strike the node (2 → quarantine + requeue); periodic probes release it. hive health report/check/clear.
---

issues #16/#18 reproduced in one campaign: AWQ Command-R hit the set_device 'CUDA-capable device(s) is/are busy or unavailable' crash on evc43 three times (tasks 7866, 7885, 7891 — 7885 and 7891 both landed on evc43), and 'hive list' showed 5 tasks as RUNNING whose logs already carried 'finished ... exit_code=0'. Agents that trust list-state instead of the log exit record will wait forever or double-submit. Suggest: (a) blacklist a node after N consecutive set_device failures, (b) reap terminal state from the log's exit record.

## Auto-captured context

- hive_version: 0.4.0
- queue: cancelled:2084  done:4886  failed:818  pending:1  running:2
- nodes: updated=2026-08-01T16:43:25  busy:2  cpu:1  idle:12  probe_failed:2
