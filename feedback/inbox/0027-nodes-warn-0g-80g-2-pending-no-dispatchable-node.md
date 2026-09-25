---
id: 27
title: nodes 显示 WARN + 0G/80G + 2% 且 pending 报 no_dispatchable_node,但 srun --overlap 直接
severity: medium
status: triaged
tags: []
submitter: si384883
hive_version: 0.4.0
task_ids: []
created: 2026-08-02T19:27:19
updated: 2026-09-25T06:53:38
source: cli
triage_note: Display: per-row age for WARN rows. Related CLAIM fix landed; stale marker (!) exists at >10min. Open enhancement.
---

nodes 显示 WARN + 0G/80G + 2% 且 pending 报 no_dispatchable_node,但 srun --overlap 直接探测同一 hold job 得到 100% util / 23-44GB used——即节点其实在跑我自己的任务。poller 的 last-polled 状态过期(~10min)时 WARN 行与 BUSY 行难以区分,容易被误判成 feedback #25/#26 的 idle-node bug 而去做不必要的绕过。建议 WARN 行显式标注 stale-since 时间戳

## Auto-captured context

- hive_version: 0.4.0
- queue: cancelled:2146  done:5106  failed:843  pending:29  running:15
- nodes: updated=2026-08-02T23:15:29  busy:6  idle:2  warning:7
