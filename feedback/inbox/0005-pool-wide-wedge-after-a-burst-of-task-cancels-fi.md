---
id: 5
title: pool-wide wedge: after a burst of task cancels/finishes, ALL 15 hold jobs stuck
severity: medium
status: duplicate
tags: []
submitter: si384883
hive_version: 0.3.2
task_ids: []
created: 2026-07-14T16:48:07
updated: 2026-07-26T13:50:45
source: cli
triage_note: Same root cause as #10 (warning was a terminal state: hive-dbpost never cleared gpu_idle_since, hive-sched excluded warning). Fixed with #10.
---

pool-wide wedge: after a burst of task cancels/finishes, ALL 15 hold jobs stuck in 'warning' (0% GPU, 0GB, no processes) — hive-dbpost busy->idle grace keeps gpu_idle_since forever and re-marks 'warning' every poll (now-old_since>=WARN_SECS branch has no reset path when no task is attached). Scheduler excludes 'warning' nodes -> 0 running tasks + top-priority pending task can never dispatch = deadlock until a fresh hold job joins. Suggest: clear gpu_idle_since when queue has no running task on that jobid, or treat clean 'warning' (no procs, <500MB) as dispatchable-with-verify.

## Auto-captured context

- hive_version: 0.3.2
- queue: cancelled:1746  done:4270  failed:701  pending:1
- nodes: updated=2026-07-14T20:47:38  warning:15
