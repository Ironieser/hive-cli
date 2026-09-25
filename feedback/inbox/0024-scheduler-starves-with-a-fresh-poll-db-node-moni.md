---
id: 24
title: scheduler starves with a FRESH poll DB: node_monitor.json written 18:05:55Z show
severity: medium
status: done
tags: []
submitter: si384883
hive_version: 0.4.0
task_ids: []
created: 2026-08-01T14:07:37
updated: 2026-09-25T06:53:36
source: cli
triage_note: Root cause: head-of-queue task-level rejection (insufficient_gpus/walltime/mem) popped every candidate; tasks behind it saw an empty list. Fixed: task-level rejections no longer consume candidates (hive-sched dispatch loop). Regression test added.
---

scheduler starves with a FRESH poll DB: node_monitor.json written 18:05:55Z shows >=10 holds status=idle with mem_used=0/81559, yet 7 tasks sit PENDING reason=no_dispatchable_node for 7+ minutes and hive-sched logs no dispatch attempt at all after 14:04:20 (heartbeat healthy, PID 1542155 on evc21). Same family as #16/#18/#23. Also: evc43 again accepted dispatch and died at vllm/platforms/cuda.py set_device -> torch.AcceleratorError CUDA-capable device busy or unavailable (task 7918), so it is still being handed work. Falling back to srun --overlap.

## Auto-captured context

- hive_version: 0.4.0
- queue: cancelled:2086  done:4906  failed:824  pending:7  running:4
- nodes: updated=2026-08-01T18:05:55  busy:4  cpu:1  idle:10  probe_failed:2
