---
id: 10
title: warning 是空闲节点的终态: hold job 跑完一个任务后被永久踢出调度
severity: high
status: done
tags: [scheduler, dbpost, node-status, dispatch]
submitter: si384883
hive_version: 0.3.2
task_ids: []
created: 2026-07-26T13:44:06
updated: 2026-07-26T13:50:40
source: cli
triage_note: Fixed: hive-dbpost clears gpu_idle_since + returns to idle when the grace expires; hive-sched treats warning as uncertain (verify-before-dispatch) so stale DBs self-heal; hive nodes counts warn separately. Regression tests in tests/test_hive.py.
---

# `warning` 是空闲节点的终态，导致 hold job 跑完一个任务后被永久踢出调度

## 摘要

`hive-dbpost` 的 busy→idle grace timer 一旦触发就再也不会复位：`gpu_idle_since`
永远不被清除，节点在 `WARN_SECS`(180s) 之后固定为 `status="warning"`。而
`hive-sched.get_candidates()` 把 `warning` 排除出派发候选。

**净效果：任何 hold job 只要跑完一个任务、闲置超过 180 秒，就被永久移出调度池。**
`hive daemon` / `hive queue daemon` 重启均无效，因为状态持久化在
`~/.hive/node_monitor.json` 里。

这与 `hive-dbpost` 自己的模块 docstring 直接矛盾，判断为 regression 而非设计意图。

## 现场

20 个 hold job，其中 7 个卡在 `warning`，25 个任务卡在 `no_dispatchable_node`，
9 张 H100 空转，实际只有 11 张在干活。

```
busy: 18   cpu: 0   idle: 0   probe-fail: 2   total: 20
    ^^ 这里把 warning 计入 busy，掩盖了问题
```

`node_monitor.json` 里 warning 节点的记录（GPU 完全空闲）：

```json
{
  "node": "evc101", "partition": "highgpu", "job_elapsed": "11h43m",
  "gpu": [{"index": 0, "util": 0, "mem_used": 0, "mem_total": 81559}],
  "processes": [],
  "status": "warning",
  "gpu_idle_since": 1785068578.133884,
  "time_left_secs": 44176
}
```

`processes: []`、`mem_used: 0`、`util: 0` —— 节点确实是空的。

同一物理节点上 hold job 越多越明显（探测更容易超时，更多节点走到 idle 分支）：

| 节点 | 占卡 | 在跑 |
|---|---:|---:|
| evc103 | 6 | 3 |
| evc101 | 4 | 2 |
| evc102 | 2 | 1 |
| evc104 | 2 | 1 |

## 根因

### `libexec/hive-dbpost:96-111`

```python
if status == "idle":
    old_since   = old_job.get("gpu_idle_since")
    old_status  = old_job.get("status")
    old_max_mem = max((g.get("mem_used", 0) for g in old_job.get("gpu", [])), default=0)
    if old_since is not None:
        job["gpu_idle_since"] = old_since          # <-- 永远原样带过去
        job["status"] = "warning" if (now - old_since >= WARN_SECS) else "busy"
        changed = True
    elif old_status in ("busy", "warning") and old_max_mem >= 500:
        job["gpu_idle_since"] = now
        job["status"] = "busy"
        changed = True
    else:
        if job.get("gpu_idle_since") is not None:
            job["gpu_idle_since"] = None
            changed = True
```

一旦 `gpu_idle_since` 被设上，第一个分支就永远命中，`gpu_idle_since` 每轮原样
传递、`status` 恒为 `warning`。第三个分支（唯一会清除该字段的地方）再也走不到。

### `libexec/hive-sched:384-405`

```python
def get_candidates(jobs_db, used_jobids, freed):
    """...  `busy`/`warning`/`cpu` are excluded. ..."""
    ...
    is_idle = (st == "idle")
    is_uncertain = st in ("probe_failed", "unknown")
    if not (is_idle or is_uncertain or forced):
        continue
```

`warning` 既不是 `idle` 也不是 `uncertain`，直接 `continue`。

### 与 docstring 矛盾

`hive-dbpost:12-14`：

> 1. busy→idle WARNING grace timer (`gpu_idle_since`): a job that had a real model
>    loaded (GPU mem ≥500MB) and then went quiet is held as `busy` for WARN_SECS
>    **before flipping to a true `idle`**, so a brief GPU dip doesn't churn the table.

文档说 grace 期满后应该翻到 **true idle**，代码写的是 `warning`。

## 复现

1. 让一个 hold job 跑一个会占 ≥500MB 显存的任务
2. 任务结束，节点空闲
3. 等 > 180 秒
4. `hive nodes` 显示该 job 为 `WARN`，`hive queue ls` 里新任务 pending 原因为
   `no_dispatchable_node`，即使该节点 GPU 完全空闲
5. 重启两个 daemon 无效（状态在 `~/.hive/node_monitor.json`）

## 建议的修复

### 1. `hive-dbpost` —— grace 期满后回到 `idle`

```python
            if old_since is not None:
                if now - old_since >= WARN_SECS:
                    # Grace expired and the GPU is still quiet -> a *true* idle
                    # again (what this module's docstring promises). Previously
                    # this flipped to "warning" and never cleared
                    # gpu_idle_since, so the node stayed "warning" forever and
                    # get_candidates() excluded it permanently.
                    job["gpu_idle_since"] = None
                    job["status"] = "idle"
                else:
                    job["gpu_idle_since"] = old_since
                    job["status"] = "busy"
                changed = True
```

### 2. `hive-sched` —— 让残留的 `warning` 也可派发（强制 live-probe）

这样**旧的 `node_monitor.json` 不用手工修**就能自愈：

```python
        # "warning" is included so a DB written by an older hive (where the
        # grace timer never cleared) doesn't strand a node forever. It always
        # carries needs_verify, so we live-probe the GPU before dispatching.
        is_uncertain = st in ("probe_failed", "unknown", "warning")
```

docstring 同步改成：

```
    `probe_failed`/`unknown`/`warning` and just-freed nodes — but those carry
    needs_verify so we live-probe before trusting them. `busy`/`cpu` are excluded.
```

因为 `needs_verify=True` 会走 `live_probe()` 实测 GPU，误判风险为零：真忙的节点
探测出来会被拒。

### 3. 建议补一个回归测试

`tests/run.sh` 目前没覆盖这条路径。建议加：模拟 `busy(mem≥500) → idle`，
推进时钟越过 `WARN_SECS`，断言最终 `status == "idle"` 且该 job 出现在
`get_candidates()` 的返回里。

## 附带的两个小建议

1. **`hive nodes` 的汇总把 `warning` 计入 `busy`**（输出 `busy: 18` 而实际只有
   11 个在跑），掩盖了这个问题。建议单列 `warning: N`。
2. **poll 探测超时**：日志里频繁出现
   `WARNING: timeout waiting for probes after 35s`。同一物理节点上 hold job 多时
   （evc103 上有 6 个）并发 `srun --overlap` 探测容易超时，进而更多节点走进 idle
   分支、被这个 bug 捕获。或许可以按物理节点串行化探测，或提高超时预算。

## 环境

- hive-cli @ `0c7998f`
- 20 hold jobs（highgpu + normal 混合），跨 9 个物理节点
- 症状持续 >11 小时，跨多次 daemon 重启和 `hive poll`

## Auto-captured context

- hive_version: 0.3.2
- queue: cancelled:2019  done:4605  failed:751  pending:21  running:11
- nodes: updated=2026-07-26T17:41:38  busy:11  probe_failed:2  warning:7
