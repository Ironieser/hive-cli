# hive-cli

> 面向 AI 编码 Agent 的个人 GPU 节点管理器，专为 SLURM 集群设计。预占节点池，零队列延迟提交实验。

[English](README.md)

## ⚠️ 共享集群使用规范

**hive-cli 适用于短期活跃调试，不应用于长期占用资源。**

在共享 HPC 集群上预占节点会影响所有排队等待的用户，请遵守以下准则：

- **及时释放空闲节点。** 如果 `hive nodes` 显示节点 `IDLE` 超过 30–60 分钟且你没有在积极迭代，请运行 `hive pool release --idle` 归还节点。
- **保持短期使用。** 占卡作业应以小时计，而不是天。
- **高峰期不要囤积节点。** 队列积压时，缩减节点池大小，一两个节点足够大多数调试工作。
- **保持透明。** 你的占卡作业在 `squeue` 中对所有用户可见。

> 占卡是为了消除排队等待的摩擦，不是为了独占资源。不在积极迭代时，请放手。

---

## 为什么需要它

AI 编码 Agent（Claude Code、Cursor 等）需要高频的「改代码→跑实验→看结果」循环。SLURM 的排队延迟（几秒到几小时）完全打断这个节奏。hive-cli 预先申请 GPU 节点作为持久 session，提供轻量接口调度、监控和管理实验——每次重跑无需重新排队。

## 安装

```bash
git clone git@github.com:Ironieser/hive-cli.git
cd hive-cli && bash install.sh
```

安装到 `~/.local/share/hive-cli/`，在 `~/bin/` 创建 `hive` 软链接。

## 命令

### 节点池管理

```bash
hive pool init                          # 首次使用：创建 ~/.hive/pool_config.json
hive pool add                           # sbatch 一个新占卡作业（默认 preset）
hive pool add highgpu                   # 使用指定 preset
hive pool add ~/my.slurm                # 直接传 slurm 脚本路径
hive pool add --count 3 --time 12:00:00 # 同时提交 3 个，覆盖时长
hive pool release 584954                # scancel 指定占卡作业
hive pool release --idle                # 自动 scancel 所有空闲节点
hive pool config                        # 查看 preset 配置，验证脚本路径
```

**首次配置**：运行 `hive pool init` 后编辑 `~/.hive/pool_config.json`：

```json
{
  "default": "normal",
  "presets": {
    "normal":  { "script": "~/1_normal_gpu.slurm", "description": "普通分区" },
    "highgpu": { "script": "~/1_gpu.slurm",        "description": "高端 GPU 分区" }
  }
}
```

> `pool_config.json` 仅本地保存，不会被提交到 git。

### 节点监控

```bash
hive nodes                  # 一次性节点状态表（自动启动后台 daemon）
hive top                    # 交互式实时监控（类 htop，q 退出）
hive poll                   # 立即强制刷新
hive daemon start|stop|status|logs
```

```
  JOBID   NODE   PART     ELAPSED  STATUS  GPU%  MEM       LEFT    TASK
  ──────────────────────────────────────────────────────────────────────
  582228  n1     highgpu  3d13h    BUSY    87%   42G/80G   8h12m   python train.py ...
  584954  n2     normal   2h20m    IDLE     0%    0G/80G   21h40m  —
```

`LEFT` 是该节点被 SLURM 回收前的剩余墙钟时间（不足 1 小时标红）。

### 任务队列

直接提交实验，调度器自动找空闲节点运行，无需手动分配。

```bash
hive submit "python train.py --config exp/v1.yaml"      # 提交命令（= queue submit 的快捷方式）
hive submit job.hive                                     # 提交 .hive 脚本
hive submit --priority 10 --name train "python train.py"  # 优先级越高越先派发（默认 0）
hive submit --need-mb 40000 "python train_big.py"       # 等到有 ≥40 GB 空闲显存再派发
hive submit --est-runtime 2h "python train.py"          # 运行时长估计 → 墙钟感知调度（或 'auto'）
hive list                                               # 队列：活跃任务 + 最近 10 条已结束
hive list --limit 50  |  --all  |  --state failed       # 更多历史 / 全部 / 按状态过滤
hive logs 3 -f                                          # 实时跟踪日志
hive wait 3                                             # 阻塞到完成 → 打印日志，以任务退出码退出
hive wait 3 --pending-timeout 600                       # 600s 还没派发就放弃（退出码 75）
hive stats [NAME]                                       # 已完成任务的运行时长（min/median/P90/max）
hive cancel 3                                           # 取消 pending/running 任务
hive prune --older-than 7d                              # 清理旧的终态任务（历史保留在 events.jsonl）
hive queue daemon start|stop|status|logs               # 管理调度器（submit 会自动拉起）
```

`hive wait` 退出码（供 agent 判断）：**0** 完成 · **1** 失败 · **75** 一直没派发
（`--pending-timeout`）· **130** 取消。

**`.hive` 脚本格式**（类比 SLURM 的 `#SBATCH` 指令）：

```bash
#!/bin/bash
#HIVE workdir=/path/to/project
#HIVE priority=5           # 越大越先派发（默认 0）
#HIVE name=train-v1        # 也是运行时长历史的键（hive stats / --est-runtime auto）
#HIVE need_mb=25000        # 可选：派发前要求的最小空闲显存 (MiB)
#HIVE est_runtime=2h       # 可选：运行时长估计 → 不会派发到快过期的节点
#HIVE gpus=1               # 可选：任务可见的 GPU 数（默认 1，多余的卡会被隐藏）

python train.py --config exp/v1.yaml
```

**任务日志**位于 `~/.hive/logs/task-<id>.log`，包含完整的 stdout/stderr、
srun 错误信息以及执行 header/footer（节点、工作目录、退出码）。

**调度原理**：`hive-sched` 后台进程每 30 秒读取节点状态，通过 `srun --overlap`
将 pending 任务分配到 IDLE 节点；心跳文件（每 30s 更新）用于检测崩溃任务（5
分钟超时 → 标记 FAILED）。调度器**不会**派发到已被占用 >5GB 的卡（僵尸进程或
其它用户的进程），并在派发前对不确定的节点做一次实时探测；无法派发的任务会在
`hive list` 的 NODE 列显示原因（如 `waiting_for_mem` / `gpu_dirty`）。详见
[`docs/status_model.md`](docs/status_model.md)。

### 墙钟感知调度 & 运行时长历史

占卡作业会过期。如果任务带了运行时长估计，调度器**不会**把它派发到剩余墙钟低于
`估计 + 10 分钟` 的节点——而是以 `insufficient_walltime` 挂起,避免跑到一半被回收：

```bash
hive submit --name train --est-runtime 4h "python train.py"   # 2h / 90m / 1-12:00:00 / 14400
hive submit --name train --est-runtime auto "python train.py" # 取该 name 历史的 P90
hive stats train                                              # 查看历史（count/min/median/P90/max）
```

估计是可选的——不填则保持墙钟无感知调度（行为不变）。每个完成的任务会把真实的
**排队**和**运行**时长写入持久化追加日志(`~/.hive/events.jsonl`),`hive stats` 与
`--est-runtime auto` 都从它读取——所以历史在 `hive prune` 后依然保留。

### 容错：节点回收 & 断点丢失

如果节点在任务**运行中**被回收,hive 会自动把任务重投到另一个活节点。但 SLURM 无法
恢复进程——它从头开始跑——所以 hive 会明确告知：

- `hive wait` 打印 `⚠`（挂起时还会打印拦截原因）；
- `hive list` 标记 `(re-disp xN)`；
- 任务日志写入 banner。

**把长作业都设计成可续跑**,并带上 `--est-runtime`,让调度器一开始就避开快过期的节点:

- **训练** —— 定期存 checkpoint,启动时加载最新的(`--resume`),重投后从断点续跑。
- **推理 / 批处理** —— 增量写输出,并做到**幂等**:启动时跳过已经有输出的输入,重跑只补
  缺失的部分,绝不重复或损坏已完成的结果。

任务**自身命令**崩溃(节点还活着)则标记 `failed`,**不**重试。

### 保持队列整洁

```bash
hive prune --dry-run            # 预览会清掉哪些
hive prune --older-than 7d      # 清理结束超过 7 天的终态任务（默认）
hive prune --keep 50            # 或只保留最近 50 个终态任务
```

`prune` 绝不动 pending/running 任务；默认保留任务日志(加 `--logs` 才删)。运行时长历史
始终保留在 `events.jsonl`。

### SLURM 队列面板

```bash
hive jobs           # 查看自己的作业
hive jobs -a        # 查看所有用户
hive jobs -r        # 仅显示运行中
hive jobs -p gpu    # 按分区过滤
```

### 反馈 (feedback)

在使用 hive 时遇到 bug 或不顺手的地方？提交反馈给维护本仓库的 agent 处理：

```bash
hive feedback "list 的 CMD 列被截断"                      # 一句话快速提交
hive feedback submit --title "OOM-blind dispatch" \
    --severity high --tags scheduler,oom --task 42       # 结构化提交
hive feedback list                                       # 查看已提交的反馈
```

每次提交会自动附带 hive 版本与队列/节点状态快照。反馈存入 `feedback/inbox/`，
参见 [`feedback/TRIAGE.md`](feedback/TRIAGE.md)。

## 文件说明

| 文件 | 位置 | 说明 |
|---|---|---|
| `pool_config.json` | `~/.hive/` | 占卡脚本 preset（本地，不进 git）|
| `node_monitor.json` | `~/.hive/` | 节点状态数据库（daemon + hive-dbpost 写入）|
| `queue.json` | `~/.hive/` | 实时任务队列（hive-sched 写入；由 `hive prune` 清理）|
| `events.jsonl` | `~/.hive/` | 持久化追加的任务/时长历史（运行时长统计来源）|
| `pool-logs/` | `~/.hive/` | 占卡作业 stdout（由 `hive pool add` 重定向）|
| `logs/task-<id>.log` | `~/.hive/` | 各任务的完整日志 |
| `feedback/` | `<repo>/` | 反馈收件箱与分诊（纳入 git）|

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `HIVE_DIR` | `~/.hive/` | 配置/数据目录 |
| `HIVE_PYTHON` | 自动检测 | Python 解释器路径 |

## AI Agent 使用指南

请参阅 **[docs/agent_guide.md](docs/agent_guide.md)**，了解如何在 AI agent session 中安装、配置和使用 hive-cli。

安装时会自动安装 **Claude Code skill**。安装完成后，在 Claude Code 中直接使用：`/hive status`、`/hive submit "cmd"`、`/hive wait <id>` 等命令。

## 测试

一套确定性的**离线**测试（mock SLURM + 影子 `HIVE_DIR`，绝不碰 `~/.hive` 或真实集群），
覆盖 poller、调度器、队列 CLI、walltime 门控、节点回收重投、事件日志与 prune：

```bash
bash tests/run.sh        # 退出码 0 = 全部通过
```

仓库无 CI；改动后安装前请先跑一遍。

## 系统要求

- SLURM（支持 `srun --overlap`）
- NVIDIA GPU + `nvidia-smi`
- Python 3.6+

## License

MIT
