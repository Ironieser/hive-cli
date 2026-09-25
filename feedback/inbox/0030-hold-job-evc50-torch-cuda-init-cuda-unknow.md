---
id: 30
title: 两个 hold job 落在同一节点 evc50 时,调度器把两个任务同时派到该节点,第二个任务 torch CUDA init 失败 'CUDA unknow
severity: medium
status: done
tags: []
submitter: si384883
hive_version: 0.4.0
task_ids: []
created: 2026-08-14T15:36:59
updated: 2026-09-25T07:29:18
source: cli
triage_note: Fixed: hive health — verify-before-dispatch creates a real CUDA context (ctypes/libcuda) and quarantines the node on failure (evc43 verified: cuCtxCreate=999); fast task failures with a CUDA-init log signature strike the node (2 → quarantine + requeue); periodic probes release it. hive health report/check/clear.
---

两个 hold job 落在同一节点 evc50 时,调度器把两个任务同时派到该节点,第二个任务 torch CUDA init 失败 'CUDA unknown error ... Setting the available devices to be zero'(task 8405/8406 同时在 evc50,均 FAILED)。建议 per-node 而非 per-holdjob 的并发闸门。

## Auto-captured context

- hive_version: 0.4.0
- queue: cancelled:2178  done:5234  failed:898  pending:9  running:8
- nodes: updated=2026-08-14T19:34:36  busy:5  cpu:1  idle:3
