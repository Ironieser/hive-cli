---
id: 29
title: evc50 (holds 759322/759323, not mine) accepts dispatch but torch CUDA init fails
severity: medium
status: triaged
tags: []
submitter: si384883
hive_version: 0.4.0
task_ids: []
created: 2026-08-14T15:18:02
updated: 2026-09-25T06:53:37
source: cli
triage_note: Node-health theme: co-tenant / broken node (evc43, evc50) accepts dispatch, CUDA init fails. Needs squeue -w foreign-job detection + auto-quarantine after N fast failures (ROADMAP C4). Not fixed yet.
---

evc50 (holds 759322/759323, not mine) accepts dispatch but torch CUDA init fails there every time: 'RuntimeError: CUDA unknown error ... Setting the available devices to be zero'. 3/3 W15-5 tasks placed on it died in <30s (#8374, #8378, #8387); the same commands run fine on evc24/evc36/evc42. The node probes IDLE at 2% so the scheduler keeps selecting it. A dispatch-time CUDA-availability probe, or quarantining a node after N consecutive sub-minute failures, would stop the queue draining into it.

## Auto-captured context

- hive_version: 0.4.0
- queue: cancelled:2178  done:5209  failed:886  pending:7  running:6
- nodes: updated=2026-08-14T19:16:12  cpu:1  idle:6
