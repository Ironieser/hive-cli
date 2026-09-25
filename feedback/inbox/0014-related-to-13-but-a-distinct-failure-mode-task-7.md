---
id: 14
title: Related to #13 but a DISTINCT failure mode: task #7616 was dispatched to evc104
severity: medium
status: triaged
tags: []
submitter: si384883
hive_version: 0.4.0
task_ids: []
created: 2026-07-30T22:49:47
updated: 2026-09-25T06:53:37
source: cli
triage_note: Node-health theme: co-tenant / broken node (evc43, evc50) accepts dispatch, CUDA init fails. Needs squeue -w foreign-job detection + auto-quarantine after N fast failures (ROADMAP C4). Not fixed yet.
---

Related to #13 but a DISTINCT failure mode: task #7616 was dispatched to evc104 where a foreign user's job (squeue: yo036310, job 723766) held ~66 GB. Unlike the evc43 case in #13, the co-tenant's memory WAS visible from our cgroup — vLLM reported 'Free memory on device cuda:0 (13.43/79.18 GiB) on startup is less than desired'. So the existing gpu_dirty guard (>5 GB resident = do not dispatch) had the information it needed and still dispatched, which suggests the check is evaluated from a stale poll rather than at dispatch time. A pre-dispatch re-probe of free memory (not last-poll memory) would prevent this class; the #13 case additionally needs a CUDA-context probe because there the usage is invisible. Both cost a full model rsync + load per failed attempt.

## Auto-captured context

- hive_version: 0.4.0
- queue: cancelled:2046  done:4704  failed:765
- nodes: updated=2026-07-31T02:42:32  busy:2  idle:2  probe_failed:2
