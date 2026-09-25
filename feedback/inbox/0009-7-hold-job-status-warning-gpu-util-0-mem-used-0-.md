---
id: 9
title: 节点池里 7 个 hold job 长期停留在 status=warning（GPU util 0%、mem_used 0、processes 空），调度器不往
severity: medium
status: duplicate
tags: []
submitter: si384883
hive_version: 0.3.2
task_ids: []
created: 2026-07-26T12:26:15
updated: 2026-07-26T13:50:45
source: cli
triage_note: Same root cause as #10 (warning was a terminal state: hive-dbpost never cleared gpu_idle_since, hive-sched excluded warning). Fixed with #10.
---

节点池里 7 个 hold job 长期停留在 status=warning（GPU util 0%、mem_used 0、processes 空），调度器不往这些节点派发，导致 25 个任务卡在 no_dispatchable_node 而 9 张 GPU 空转。这些 warning 记录的 job_elapsed 跨多次 poll 不更新（冻结在 11h43m），而 busy 节点的 elapsed 正常刷新，怀疑是 poll 探测超时后沿用了旧状态（日志里有 'WARNING: timeout waiting for probes after 35s'）。同一物理节点上占卡数越多越明显：evc103 占 6 只跑 3，evc101 占 4 只跑 2。重启 hive daemon 和 hive queue daemon 均无效。期望：warning 状态若 GPU 确实空闲应可派发，或至少在 hive nodes 输出里区分'探测超时沿用旧值'与'确认忙'。

## Auto-captured context

- hive_version: 0.3.2
- queue: cancelled:2019  done:4601  failed:751  pending:25  running:11
- nodes: updated=2026-07-26T16:25:58  busy:11  probe_failed:2  warning:7
