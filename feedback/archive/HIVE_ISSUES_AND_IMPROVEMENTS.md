# hive 使用中遇到的问题与优化建议

> 背景：本文档基于一次真实的大模型实验（**EgoLongQA × LoHi**，Qwen3.5-4B/9B/27B/35B，部分配置需要 2–8 卡张量并行 + 高 token，如 512F、K=64/128、768F@0.5）落地。期间 hive 暴露出若干结构性限制，最终**只能完全弃用 hive、改回原生 `sbatch --gres=gpu:N`**。下面逐条记录「问题现象 → 根因（指到具体文件/代码）→ 优化建议」，并在文末给出按优先级排序的改进 roadmap。
>
> 代码版本：`~/hive-cli`（CHANGELOG 截至 2026-04-23）。核心调度逻辑在 `libexec/hive-sched`，提交在 `libexec/hive-queue`，节点轮询在 `libexec/hive-daemon`，池子管理在 `libexec/hive-pool`。

---

## 0. 先讲优点（hive 现有设计中值得保留的部分）

在批评之前，必须承认 hive 的几个设计是扎实且好用的，改进时**不应破坏**这些：

1. **flock 保护的并发安全提交**：`hive-queue` 与 `hive-sched` 都通过 `QueueLock`（`fcntl.flock(LOCK_EX)` + `~/.hive/queue.lock`）串行化对 `queue.json` 的读写，并用 `tmp + os.replace()` 做原子落盘（`hive-queue` 的 `save_queue()`，`hive-sched` 同名函数）。多个终端同时 `hive submit` 不会破坏队列文件，这点很可靠。
2. **daemon 轮询 + 跨节点心跳**：`hive-daemon` 每 900s 用 `squeue` 拉自己的 running 作业并 `srun --overlap` 进去探测 GPU/进程，写入 `node_monitor.json`；`hive-sched` 每 30s 调度。心跳文件 `sched.heartbeat` 放在共享 FS 上，`is_sched_running()` 用文件 mtime（`HEARTBEAT_DAEMON_TIMEOUT=90`）判断存活，因此 daemon 即使跑在别的登录节点也能被正确识别。
3. **per-task 独立日志**：每个 task 落到 `~/.hive/logs/task-<id>.log`，且 `dispatch_task()` 的 wrapper 把命令的 stdout/stderr **和 srun 自身的错误**一起重定向进去（注释明确写了要捕获 "srun: error: job ... is no longer running"）。`hive logs <id> -f` / `hive wait <id>` 体验顺手，尤其 `wait` 会以 task 的真实 exit code 退出，适合 agent 串联。
4. **queue 状态文件 + 友好列表**：`queue.json` 结构清晰（`next_id` / `tasks` / 每个 task 的完整生命周期字段）；`hive queue list` 把 running/pending 与终结态分区显示、自动隐藏 24h 前的 done 任务，信息密度合适。
5. **基于退出文件 + 心跳的任务存活判定**：`dispatch_task()` 在 wrapper 里写 `<id>.exit`，`check_task_status()` 先看 exit 文件、再看心跳 mtime（`HEARTBEAT_TIMEOUT=300`）、最后 `os.kill(pid,0)`，能在不依赖 SLURM 记账的情况下回收僵死任务。
6. **拒绝嵌套 srun 的护栏**：`cmd_submit()` 里已经用正则拦截命令里含 `srun` 的提交（见下文问题 7），方向是对的。

这些机制说明 hive 的"轻量任务队列"内核是健康的；下面的问题主要集中在**GPU 资源模型**和**调度公平性/健壮性**上。

---

## 问题清单（现象 → 根因 → 建议）

### 问题 1 ★最大痛点：不支持多卡任务，大模型张量并行只能弃用 hive

**现象**
27B/35B/122B + 高 token 配置（512F、K=64/128、768F@0.5）需要 2–8 卡张量并行（HF `device_map=auto` 或 vLLM `--tensor-parallel-size N`）。hive 每个 task 实际只能用到 1 张卡，大模型直接 OOM 起不来。结果只能放弃 hive，改用原生：

```bash
sbatch --gres=gpu:4 ...   # 27B TP=4
sbatch --gres=gpu:8 ...   # 122B TP=8
```

**根因（落到代码）**
- 没有"每任务 GPU 数"这个概念。`hive-queue` 的 `cmd_submit()` 与 `parse_hive_file()` 支持的字段只有 `workdir / priority / name`，**没有 `gpus`**；task 结构体里也没有该字段。
- 调度时 `hive-sched` 的 `run_one_cycle()` 用 `zip(idle_nodes, pending)` 一对一派发，**一个 idle"节点"= 一个任务**，且 `dispatch_task()` 的 srun 命令写死：
  ```python
  ["srun", f"--jobid={slurm_jobid}", "--overlap", "-n1", "--mem=0", "bash", "-c", wrapper]
  ```
  没有 `--gres`、没有 `--gpus`、没有设置 `CUDA_VISIBLE_DEVICES`，进程默认只看到/使用 GPU0。
- 池子结构本来是浪费的：`node_monitor.json` 里 evc101 / evc102 各挂着 **8 个独立的 1-GPU hold 作业**（633001–633008…），即一台 8 卡 highgpu 节点被拆成 8 个互不相关的单卡坑。本可以让一个 8 卡节点接一个 8 卡任务，却被 1-GPU 粒度的池子设计锁死。

**优化建议**
- `hive submit` / `.hive` 增加 `--gpus N`（默认 1）。`hive-queue`：`cmd_submit()` 写入 `task["gpus"]`，`parse_hive_file()` 解析 `#HIVE gpus=N`，`build_parser()` 加 `--gpus/-g`。
- `hive-sched` 的 `dispatch_task()` 注入 GPU 选择：在 wrapper 头部 `export CUDA_VISIBLE_DEVICES=<分配到的物理卡列表>`，并把 srun 改成 `-n1 --gpus={gpus}`（或 `--gres=gpu:{gpus}`）。
- 调度模型从"按节点"升级为"按 GPU 槽位"：把池子的"一节点多卡"建模成可计数的 GPU 池（见问题 4），`run_one_cycle()` 派发时检查 `node.free_gpus >= task.gpus`，多卡任务整段绑定同一节点的 N 张物理卡。
- 池子脚本侧：把 `~/1_gpu.slurm` 的 `--gres=gpu:1` 改成按整节点申请（`--gres=gpu:8 --exclusive`），让 hive 自己在节点内做 GPU 切分，而不是靠 SLURM 切成 8 个单卡坑。

---

### 问题 2：FIFO/无公平调度，小任务被早到的长队列饿死

**现象**
临时提交的小冒烟任务（如 task #2813，`smoke_4b_lohi`）排在 100+ 个更早的 pending 后面迟迟不跑；没有插队/抢占/公平份额机制。实测队列里 pending 任务的 `priority` 全是 0，等于纯 FIFO。

**根因（落到代码）**
`hive-sched` 的 `run_one_cycle()`：
```python
pending = sorted(
    [t for t in tasks.values() if t["state"] == "pending"],
    key=lambda t: (-t.get("priority", 0), t["submitted_at"]),
)
```
排序键是 `(-priority, submitted_at)`。`priority` 字段虽然存在（`hive submit -p N`），但：
- 没有任何"交互式/高优队列"概念，所有任务挤在同一条线；
- 没有 fair-share（不按提交者/项目分组轮转，单用户场景下就是"谁先提交谁先跑"）；
- 没有抢占，长任务一旦占住槽位，高优小任务也得等它自然结束。

**优化建议**
- 文档/默认值层面先教育：明确 `-p` 的语义并在 README 推荐"冒烟任务用高 priority"。
- 在 `run_one_cycle()` 引入**简单 fair-share / 轮转**：例如按 `name` 前缀或显式 `--group` 分桶，每轮调度在桶间 round-robin，避免单一批量提交独占所有空闲槽。
- 增加**交互式高优 lane**：保留少量 GPU 槽只服务 `priority >= HIGH` 的任务（类似 SLURM 的 reservation）。
- 可选的**抢占**：对标 `--preemptible` 任务，高优任务到达且无空闲槽时，向最低优 running 任务发 SIGTERM 让位（hive 已有按 srun_pid kill 的能力，见 `cmd_cancel()`，可复用）。

---

### 问题 3：池子饱和 + 绑死 highgpu，旁边一堆空闲 H100 不用

**现象**
池子节点 evc101–104 被自己的其它作业（socfac 等）占满，新任务全部饿死；与此同时 `normal` 分区当时有 ~17 个空闲 H100 节点完全没被 hive 利用。等于"自家池子堵死，公共资源闲置"。

**根因（落到代码）**
- 池子是**静态预申请**模型：`hive-pool` 的 `cmd_add()` 一次性 `sbatch` 若干 hold 作业占住节点，`hive-sched` 只把任务派进**已 hold 住的池子节点**。池子满了，调度器不会主动去 `normal` 分区借节点。
- `get_idle_nodes()` 只从 `node_monitor.json` 里挑 `status == "idle"` 的**现有池子作业**，没有"队列积压 → 自动 `pool add` 扩容"的反馈回路。
- 现状里也确实看到混合：`node_monitor.json` 既有 highgpu（20）也有 normal（7），但扩容是**人工**用 `hive pool add normal` 触发的，不是自动的。

**优化建议**
- 在 `hive-sched` 加一个**自动扩容（burst）回路**：当 `len(pending) > len(idle_nodes)` 且持续 N 个周期，自动调用 `hive pool add <burst_preset> --count K`（`pool_config.json` 里已经有 `normal` preset 指向 `~/1_normal_gpu.slurm`，直接复用即可），把积压突发到 `normal` 分区的空闲 H100。
- 增加**上限与回收**：burst 出来的 hold 作业打标记，队列清空后用 `hive pool release --idle`（`cmd_release(--idle)` 已实现）自动回收，避免长期占用公共分区。
- 把"可突发分区列表"做成 `pool_config.json` 的配置项（如 `"burst": ["normal"]`），让策略可调。

---

### 问题 4：GPU 隔离不严 → 同节点多任务共抢 GPU0 → 隐蔽且致命的 OOM

**现象**
token 配置恒定、单独跑能过的任务，在 hive 上**间歇性** CUDA OOM。日志里出现典型的"看到别人显存"信息（来自本次实验 `task-3155.log` / `task-3125.log`，两个不同 task 的进程）：

```
torch.OutOfMemoryError: CUDA out of memory. ... GPU 0 has a total capacity of 79.18 GiB
of which 560.69 MiB is free. Process 1788223 has 52.00 GiB memory in use.
```
两个本应隔离的任务**都报 "GPU 0"**、且都看到一个 52 GiB 的"陌生进程"——它们其实落在了同一张物理 GPU0 上互相踩踏。

**根因（落到代码）**
- 池子把一台 8 卡节点拆成 8 个独立的单卡 hold 作业（evc101 上 633001–633008），但 `hive-sched` 的 `dispatch_task()` 用 `srun --jobid=<某个 hold 作业> --overlap` 进去时**没设 `CUDA_VISIBLE_DEVICES`，也没 `--gres` 绑定**，进程默认全都用物理 GPU0。多个任务即使挂在不同的 hold 作业 jobid 上，落到同一节点就**共享 GPU0**。
- 监控侧把问题掩盖了：`hive-daemon` 的 `probe_job()` 探测时写死 `nvidia-smi --id=0`（只查 GPU0），所以 `node_monitor.json` 里每个 hold 作业都只报 1 张卡、且报的是**同一张 GPU0 的显存**。`status`（idle/busy）判定也只看 GPU0，导致调度器对"这张卡其实被别的任务占了"完全无感，照样往上派。
- `srun` 用了 `--mem=0`（不限内存）+ 没有 `--exclusive`，进一步放任共享。

**优化建议**
- **强制 per-task GPU 绑定**：调度器维护"节点 → 物理 GPU 占用表"，派发时挑空闲物理卡，在 wrapper 里 `export CUDA_VISIBLE_DEVICES=<分到的卡号>`；多卡任务分到一组连续/可用卡号。这是修复 OOM 的最小充分改动，落点同样在 `hive-sched` 的 `dispatch_task()`。
- **池子整节点化**：池子脚本改 `--gres=gpu:8 --exclusive`（改 `~/1_gpu.slurm`），一个 hold 作业 = 一整台 8 卡机，hive 在节点内自己切卡，从根上消除"多个 hold 作业共享物理卡"。
- **监控修正**：`hive-daemon` 的 `probe_job()` 去掉 `--id=0`，改为枚举全部 GPU（`nvidia-smi --query-gpu=index,...`，不加 `--id`），让 `node_monitor.json` 反映每张卡的真实占用；`status` 改成 per-GPU。
- 兜底：在文档明确告警"hive 当前不保证 GPU 独占"，并在 `dispatch_task()` 默认加 `--exclusive` 或显式 GPU 绑定二选一。

---

### 问题 5：把任务调度到 down/drain 节点 → munge 认证失败，整批失败

**现象**
任务被派到状态为 down 的 evc36，srun 进去触发 munge 认证错（`munge.socket.2: No such file or directory` 之类），整批任务直接失败。

**根因（落到代码）**
- `hive-sched` 的 `get_idle_nodes()` 只过滤 `status == "idle"`，**完全不看节点是否 down/drain/健康**。`node_monitor.json` 的 `status` 只有 cpu/busy/idle/unknown，没有节点级健康位。
- daemon 侧 `probe_job()` 如果 srun 探测失败，会写一条 `status: "unknown"` 的 fallback 记录——但 `get_idle_nodes()` 只排除非 idle，对 unknown 不会派发（这点算半个护栏），可真正的坑是**池子作业本身被 SLURM 放到了 down 节点上**时，`squeue` 仍把它当 R，daemon 标 idle，调度器照派。
- 现状里能看到这是个已知痛点的**人肉绕过**：`~/1_normal_gpu.slurm` 里硬编码了一长串 `--exclude=evc[1-10],evc[12-20],evc31,evc33,...`，本质就是手动躲坏节点，治标不治本。

**优化建议**
- **派发前健康检查**：`hive-sched` 在 `dispatch_task()` 之前对目标节点跑一次轻量校验——`sinfo -n <node> -h -o "%t"` 排除 `down*/drain*/fail*`，或对该节点做一次极短 `timeout 5 srun ... true` 探针，失败则跳过并把该 hold 作业临时标记不可用。
- **daemon 记录节点健康**：`hive-daemon` 在 `do_poll()` 里顺便 `sinfo` 一遍，把节点的 SLURM 状态写进 `node_monitor.json`，调度器据此自动排除。
- 把 `--exclude` 黑名单从 sbatch 脚本迁到 `pool_config.json` 的可配置字段，并让 hive 用动态健康检查替代静态黑名单。

---

### 问题 6：没有 per-task ETA / 队列位置可见性

**现象**
`hive queue list` 对 pending 任务只显示 `wait:<时长>`，看不到"我排第几""大概什么时候轮到我"。提交一堆任务后无法判断进度。

**根因（落到代码）**
`hive-queue` 的 `cmd_list()` + `task_elapsed()`：pending 分支只算 `now - submitted_at` 显示成 `wait:Xm`。没有计算队列序号，也没有基于历史时长的 ETA。

**优化建议**
- **队列位置**：`cmd_list()` 里对 pending 复用调度器的同一排序键 `(-priority, submitted_at)`，给出 `#k/总数`，让用户知道排第几。
- **ETA 估计**：用历史 done 任务的运行时长（`queue.json` 里 `started_at`/`finished_at` 现成）算平均/分位，结合"当前空闲槽数 + 前面 pending 数"给一个粗略 ETA 列。
- 在 `hive top`（TUI）里同样展示位置与 ETA。

---

### 问题 7：在已有 SLURM 作业里直接 `srun` 会失败

**现象**
从当前会话所在的 SLURM 作业内部直接 `srun python ...`，会因 munge / 内存等报错；正确做法是用 `sbatch` 提交一个独立作业来跑。

**根因（落到代码）**
嵌套 srun（job step 嵌套）会落进同一 job cgroup，叠加 munge 认证/内存约束导致失败。hive 作者其实已经意识到这一点：`hive-queue` 的 `cmd_submit()` 里有正则拦截：
```python
if re.match(r'\bsrun\b', cmd_first_line) or re.search(r';\s*srun\b', spec["cmd"]):
    print("Error: cmd must not contain 'srun'...")
```
但这个护栏只拦"用户命令里写了 srun"，**没有覆盖"用户本身就在一个 SLURM 作业里运行 hive"**这个场景，也没有文档说明。

**优化建议**
- **文档明确**：README / skill 文档加一节"不要在 interactive SLURM 作业内直接 srun，请用 hive submit（或裸 sbatch）提交独立作业"。
- **submit 自检**：`cmd_submit()` 检测 `SLURM_JOB_ID` 是否已存在于环境，若是则提示用户当前处于 SLURM 作业内，建议用独立 sbatch 隔离（或在未来版本里让 submit 自动改走 sbatch 提交而非复用当前 step）。
- 把现有 srun 拦截的正则收紧并补测试（当前 `\bsrun\b` 对 `xsrun`/`srun-wrapper` 这类会误判，可顺手修）。

---

## 改进 Roadmap（按优先级）

### P0 — 多卡支持（不做这个，大模型实验就用不了 hive）
- [ ] `hive submit --gpus N` + `.hive` 的 `#HIVE gpus=N`，task 结构体加 `gpus` 字段（`hive-queue`：`cmd_submit` / `parse_hive_file` / `build_parser`）。
- [ ] `hive-sched.dispatch_task()`：srun 加 `--gpus`/`--gres`，wrapper 头部 `export CUDA_VISIBLE_DEVICES`。
- [ ] 调度从"按节点一对一 `zip`"改为"按 GPU 槽位"，多卡任务整段绑定同节点 N 张卡（`run_one_cycle()`）。
- [ ] 池子脚本整节点化：`~/1_gpu.slurm` 改 `--gres=gpu:8 --exclusive`。

### P1 — 调度健壮性与隔离（修掉"间歇 OOM + 整批失败"这类隐蔽坑）
- [ ] **GPU 隔离**（问题 4）：per-task 物理卡绑定 / `--exclusive`，根治共享 GPU0 的 OOM。
- [ ] **down 节点排除**（问题 5）：`hive-sched` 派发前 `sinfo` 健康检查；daemon 把节点 SLURM 状态写进 DB；黑名单迁入 `pool_config.json`。
- [ ] **监控修正**（问题 4 配套）：`hive-daemon.probe_job()` 去掉 `--id=0`，按物理卡逐卡报告占用与 status。
- [ ] **公平/优先调度**（问题 2）：fair-share 轮转 + 交互式高优 lane（可选抢占）。
- [ ] **自动 burst 扩容**（问题 3）：积压时自动 `pool add normal`，空闲时 `pool release --idle` 回收。

### P2 — 可见性与易用性
- [ ] **队列位置 + ETA**（问题 6）：`cmd_list()` 显示 `#k/N` 与基于历史时长的 ETA，TUI 同步。
- [ ] **嵌套 SLURM 防护 + 文档**（问题 7）：submit 检测 `SLURM_JOB_ID`，文档补"勿在作业内 srun"，收紧 srun 拦截正则并补测试。

---

## 附：本次实验最终采用的绕过方案（供回归测试参考）

由于 P0 缺失，本次 EgoLongQA × LoHi 大模型实验**全程未用 hive**，直接：

```bash
# 27B，张量并行 4 卡
sbatch --partition=normal --gres=gpu:4 --exclusive ... run_egolongqa.sh Qwen/Qwen3.5-27B ...
# 35B / 122B，张量并行 8 卡
sbatch --partition=normal --gres=gpu:8 --exclusive ... run_egolongqa.sh ... TP=8
```

要让 hive 重新成为这类实验的首选，**P0 多卡 + P1 GPU 隔离**是硬门槛；二者补齐后，hive 的 flock 队列 / per-task 日志 / `wait` 退出码语义反而比裸 sbatch 更适合 agent 化批量跑实验。
