---
id: 28
title: W25: two hold jobs (741310, 741311) landed on the same node evc43, both AllocTRE
severity: medium
status: triaged
tags: []
submitter: si384883
hive_version: 0.4.0
task_ids: []
created: 2026-08-05T14:28:20
updated: 2026-09-25T06:53:37
source: cli
triage_note: Node-health theme: co-tenant / broken node (evc43, evc50) accepts dispatch, CUDA init fails. Needs squeue -w foreign-job detection + auto-quarantine after N fast failures (ROADMAP C4). Not fixed yet.
---

W25: two hold jobs (741310, 741311) landed on the same node evc43, both AllocTRES gres/gpu=1. hive nodes reports one as CPU (0G/0G) and the other IDLE (0G/80G), so the gpu_dirty guard passes, but every dispatched task dies at vLLM init with 'CUDA error: CUDA-capable device(s) is/are busy or unavailable' (tasks 8249, 8253, 8288). Suggest the poller treat two hold jobs co-located on one node with gpu=1 each as a contention condition, or have the pre-dispatch probe actually allocate a CUDA context rather than only reading free memory.

## Auto-captured context

- hive_version: 0.4.0
- queue: cancelled:2178  done:5158  failed:851  running:1
- nodes: updated=2026-08-05T18:24:57  cpu:1  idle:1
