---
id: 11
title: 调度器应按显存过滤槽位：CPU-only 的 hold job 会持续吃掉 GPU 任务
severity: medium
status: triaged
tags: []
submitter: si384883
hive_version: 0.4.0
task_ids: []
created: 2026-07-28T04:01:19
updated: 2026-09-25T06:53:37
source: cli
triage_note: cpu status is already excluded from get_candidates; live_probe on a no-GPU job fails (probe_unverifiable). Root cause of the observed dispatch unclear (stale idle reading?). Fold into node-health quarantine work.
---

调度器应按显存过滤槽位：CPU-only 的 hold job 会持续吃掉 GPU 任务

现象：evc43 上有两个 hold job
  705229  evc43  normal  CPU   0%  0G/0G   <- CPU-only,无 GPU
  711245  evc43  normal  IDLE  2%  0G/80G  <- 正常 GPU 槽

调度器把 GPU 任务派给了 705229 那个槽,任务在模型加载阶段直接崩:
  torch.AcceleratorError: CUDA error: CUDA-capable device(s) is/are busy or unavailable

连续命中两次(task #7557 evc43 13m32s FAILED, #7562 evc43 5m01s FAILED),
每次浪费 5-14 分钟,而且因为是 exit 1,任务不会自动重试到别的节点。

hive nodes 已经把这类槽位显示为 'CPU' 状态且 0G/0G,说明探测逻辑已经知道它没有 GPU,
但 dispatch 时没有用这个信息过滤。

建议:hive-sched 在选节点时排除 gpu_total==0 或 state=='CPU' 的槽位;
或者至少在这类槽位上失败后自动 requeue 到别的节点而不是标 FAILED。

## Auto-captured context

- hive_version: 0.4.0
- queue: cancelled:2035  done:4672  failed:754  running:1
- nodes: updated=2026-07-28T07:54:46  cpu:1  idle:11  probe_failed:3
