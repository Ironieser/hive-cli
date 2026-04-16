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
  JOBID   NODE   PART     ELAPSED  STATUS  GPU%  MEM       TASK
  ─────────────────────────────────────────────────────────────────
  582228  n1     highgpu  3d13h    BUSY    87%   42G/80G   python train.py ...
  584954  n2     normal   2h20m    IDLE     0%    0G/80G   —
```

### 任务队列

直接提交实验，调度器自动找空闲节点运行，无需手动分配。

```bash
hive queue submit "python train.py --config exp/v1.yaml"   # 提交命令字符串
hive queue submit job.hive                                   # 提交 .hive 脚本
hive queue list                                              # 查看队列
hive queue logs 3 -f                                         # 实时跟踪日志
hive queue wait 3                                            # 阻塞等待完成，打印日志并以任务退出码退出
hive queue cancel 3                                          # 取消任务
hive queue daemon start|stop|status|logs
```

**`.hive` 脚本格式**（类比 SLURM 的 `#SBATCH` 指令）：

```bash
#!/bin/bash
#HIVE workdir=/path/to/project
#HIVE priority=5
#HIVE name=train-v1

python train.py --config exp/v1.yaml
```

**任务日志**位于 `~/.hive/logs/task-<id>.log`，包含完整的 stdout/stderr、
srun 错误信息以及执行 header/footer（节点、工作目录、退出码）。

**调度原理**：`hive-sched` 后台进程每 30 秒读取节点状态，通过 `srun --overlap`
将 pending 任务分配到 IDLE 节点；心跳文件（每 30s 更新）用于检测崩溃任务（5
分钟超时 → 标记 FAILED）。

### SLURM 队列面板

```bash
hive jobs           # 查看自己的作业
hive jobs -a        # 查看所有用户
hive jobs -r        # 仅显示运行中
hive jobs -p gpu    # 按分区过滤
```

## 文件说明

| 文件 | 位置 | 说明 |
|---|---|---|
| `pool_config.json` | `~/.hive/` | 占卡脚本 preset（本地，不进 git）|
| `node_monitor.json` | `~/.hive/` | 节点状态数据库（daemon 写入）|
| `queue.json` | `~/.hive/` | 任务队列数据库（hive-sched 写入）|
| `logs/task-<id>.log` | `~/.hive/` | 各任务的完整日志 |

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `HIVE_DIR` | `~/.hive/` | 配置/数据目录 |
| `HIVE_PYTHON` | 自动检测 | Python 解释器路径 |

## AI Agent 使用指南

请参阅 **[docs/agent_guide.md](docs/agent_guide.md)**，了解如何在 AI agent session 中安装、配置和使用 hive-cli。

安装时会自动安装 **Claude Code skill**。安装完成后，在 Claude Code 中直接使用：`/hive status`、`/hive submit "cmd"`、`/hive wait <id>` 等命令。

## 系统要求

- SLURM（支持 `srun --overlap`）
- NVIDIA GPU + `nvidia-smi`
- Python 3.6+

## License

MIT
