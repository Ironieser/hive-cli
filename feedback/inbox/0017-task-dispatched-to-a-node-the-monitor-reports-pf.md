---
id: 17
title: Task dispatched to a node the monitor reports PFAIL, then hangs. Task 7706 (v61e
severity: medium
status: triaged
tags: []
submitter: si384883
hive_version: 0.4.0
task_ids: []
created: 2026-07-31T11:17:21
updated: 2026-09-25T06:53:37
source: cli
triage_note: (b) fixed: TASK column + CLAIM shown regardless of GPU status. (a) still open: probe succeeded on evc34 but the node hung; needs node-health quarantine (ROADMAP C4/S6).
---

Task dispatched to a node the monitor reports PFAIL, then hangs. Task 7706 (v61el_gemma2_9b_s0) was placed on evc34 while BOTH evc34 holds (715320, 715322) read PFAIL 0% 0G/0G in 'hive nodes'. The task loaded model weights (11:11:56 'Model loading took 141s') and then produced no further output for >5 min, where the same eval on healthy nodes finishes end-to-end in 1-3 min. The node table also shows an empty TASK column for every node while 7706 and 7717 were RUNNING, so the poller is not attributing running tasks to nodes. Two asks: (a) do not dispatch to a hold whose last probe was PFAIL (or re-probe before placement), and (b) surface the task->node attribution even when the probe fails, since 'RUNNING on evc34' plus 'evc34 PFAIL' is currently the only way to notice. Falling back to srun --overlap on a healthy hold to finish the run. --title PFAIL node still receives dispatch; task hangs after model load --severity high --tags scheduler,dispatch,pfail --task 7706

## Auto-captured context

- hive_version: 0.4.0
- queue: cancelled:2049  done:4778  failed:785  running:4
- nodes: updated=2026-07-31T15:11:41  idle:8  probe_failed:2
