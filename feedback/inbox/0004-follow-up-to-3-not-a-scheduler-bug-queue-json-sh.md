---
id: 4
title: follow-up to #3: NOT a scheduler bug — queue.json showed 8 running tasks holding
severity: medium
status: done
tags: []
submitter: si384883
hive_version: 0.3.2
task_ids: []
created: 2026-07-14T14:03:21
updated: 2026-09-25T06:53:37
source: cli
triage_note: Fixed: hive nodes/top show CLAIM (+task name, claimed: count) when queue.json has a running task on a GPU-idle hold job.
---

follow-up to #3: NOT a scheduler bug — queue.json showed 8 running tasks holding all 8 hold-jobs (slot-based placement), while 'hive nodes' displayed all 8 as IDLE 0GB (GPU-utilization-based status). The IDLE display + 'no_dispatchable_node' reason combo is misleading when slots are claimed by running tasks that aren't using the GPU yet; suggest a distinct reason like 'all_slots_claimed' and/or showing claimed-but-gpu-idle in hive nodes.

## Auto-captured context

- hive_version: 0.3.2
- queue: cancelled:1660  done:4217  failed:698  pending:14  running:8
- nodes: updated=2026-07-14T18:02:33  busy:1  idle:7
