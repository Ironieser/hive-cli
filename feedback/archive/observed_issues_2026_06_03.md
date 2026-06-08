# Hive observed issues (2026-06-03)

Session: ma-egoqa LoHi full-set inference on Qwen3-VL-8B (1741 questions, 6 shards + prep + watcher). Tasks submitted via `hive queue submit` on 2026-06-02 evening; queue state captured the morning of 2026-06-03.

Concrete reference points:

- Hive tasks: `#3536-#3541` (6 shards `ma-ego-lf128_hf4-s{0..5}-q*_*`), `#3542` (`ma-ego-prep-q0-1741`), `#3543` (`ma-ego-watcher-lf128_hf4`).
- Hold-job pool: 9 × `highgpu_sleep` slots on `evc101 / evc102 / evc103 / evc104 / evc39 / evc49` (jobids 633001-633012, 633028-633029).
- Contending user: `socfac` running vLLM directly via SSH on the same H100s.
- Observed `hive queue list` footer at peak: `20 running  182 pending  (+1574 old done hidden — hive queue list --all)`.
- Workaround scripts created outside hive: `slurm/ma_egoqa_ssh_dispatcher.sh`, `slurm/ma_egoqa_node_dispatcher.sh`, `slurm/ma_egoqa_watcher.sh`, `slurm/run_ma_egoqa_full_shard.sh`.

---

## 1. Long FIFO queue with no priority, no ETA

**Symptom.** Tasks land at the tail of a single global FIFO. No knob for "this is a 30-min job, slot it in front of the 6-hour ones", no surfaced ETA.

**Concrete example.** Submitted `#3536-#3543` while `hive queue list` showed `20 running  182 pending`. My 8 tasks were positions ~183-190. They sat PENDING for 25+ minutes with zero progression because `socfac`'s in-flight tasks were long-running vLLM serves, not short ones.

**Impact.** Loss of the core hive value prop ("zero queue latency between iterations"). Once a co-tenant queues a backlog, every new submission inherits it. I could not tell whether to wait 5 min or 5 hours.

**Suggested fix.**
- Per-user fair-share: round-robin across submitters at dispatch time instead of strict FIFO, so my single task doesn't sit behind another user's 182.
- Optional `--priority N` on submit, capped per user, with audit in `queue.json`.
- `hive queue list` should show `pos=k/N  eta≈Xm` per row, computed from running-task elapsed × remaining slots.

---

## 2. Scheduler unaware of out-of-band GPU contention

**Symptom.** Hive's notion of "node has a slot" is "I hold an sbatch job on it". It does not look at live GPU memory. When another user is doing direct-SSH vLLM on the same H100, hive happily dispatches into a 75GB-occupied 80GB device and the task instantly CUDA-OOMs.

**Concrete example.** `socfac` ran vLLM directly on `evc101/102/103` (no SLURM job, just SSH into a node they had access to). Each H100 showed ~75GB used / 80GB total. Hive dispatched my shard tasks to those nodes anyway. `torch.cuda.OutOfMemoryError` within seconds.

**Impact.** Tasks burned dispatch credit, marked themselves failed, no retry. I lost hours before I realised hive could not see the contention. I had to abandon `hive queue` entirely for the heavy shards and write my own SSH-poll dispatcher (`slurm/ma_egoqa_ssh_dispatcher.sh`) that queries `nvidia-smi --query-gpu=memory.free` per node before dispatch.

**Suggested fix.**
- Pre-dispatch gate: before launching task T on node N, require `free_mb(N) ≥ T.need_mb` (declared in `.hive` or default 25 GB). Hive already gathers nvidia-smi state via `hive-poll`; just consult it.
- Add `#HIVE need_mb=25000` directive.
- If no node meets the gate, leave the task PENDING with reason `WAITING_FOR_MEM` instead of dispatching and OOM'ing.

---

## 3. No node affinity / "any free GPU" semantics

**Symptom.** Hive dispatches tasks to specific nodes by hold-job mapping. There is no "place this on whichever pool node currently has the most free VRAM" mode.

**Concrete example.** I ended up bypassing hive entirely with `slurm/ma_egoqa_ssh_dispatcher.sh` and `slurm/ma_egoqa_node_dispatcher.sh`, both of which loop over `NODES=(evc39 evc49 evc101 evc102 evc103 evc104)` and SSH into whichever one polls free first. This is exactly the scheduling decision hive should be making.

**Impact.** Custom shell dispatcher duplicates hive functionality and is harder for the agent to monitor (no `hive queue list` integration). State lives in a single shared jsonl deduped by `--resume`.

**Suggested fix.**
- Make placement decision live at dispatch time, not at submit time. Re-score every PENDING task against current node free-mem snapshot each tick.
- Surface a `policy: most_free_gpu | strict_node | round_robin` field on the task.

---

## 4. No OOM-aware retry

**Symptom.** When CUDA OOM (or any process kill) takes down a task mid-run, hive marks it `FAILED` and stops. There's no auto-resume even though the workload is plainly idempotent over `--resume`.

**Concrete example.** I wrapped each shard in `slurm/run_ma_egoqa_full_shard.sh` which loops on python exit and re-invokes with `--resume`, because hive cannot do this. Then the dispatcher script wraps that again with its own outer retry on a different node.

**Impact.** Two retry layers outside hive, each maintained by hand.

**Suggested fix.**
- `#HIVE retry_on_oom=3` directive: on detected OOM exit signature (rc=137 / "CUDA out of memory" in tail), requeue automatically with a node exclusion.
- Generalise to `retry_on=oom,timeout,node_lost` with per-task max attempts.

---

## 5. No live visibility into running tasks

**Symptom.** `hive queue list` reports only `PENDING / RUNNING / DONE`. To know what the task is actually doing — stuck loading the model? mid-inference? hung in dataloader? — I have to SSH into the node and `ps aux | grep python`.

**Concrete example.** For #3536-#3541 I repeatedly SSH'd to each pool node and tailed the underlying log on disk to figure out which shards had actually started writing to their `_dispatched.jsonl`.

**Impact.** Doubles the polling load and breaks the "one CLI to rule them all" abstraction. The agent skill cannot answer "are my tasks healthy?" from hive alone.

**Suggested fix.**
- `hive queue tail <id> [-f]` showing the task's captured stdout/stderr.
- `hive queue inspect <id>` showing dispatched-node, PID, elapsed, last-N log lines, and current GPU/MEM of the process (one `pidstat` + `nvidia-smi --query-compute-apps`).
- Store log path in `queue.json` so this is offline-readable too.

---

## 6. `hive queue list` truncates the CMD column

**Symptom.** Default view truncates the command at roughly 30 chars. Eight similarly-named shards become visually indistinguishable.

**Concrete example.** My `ma-ego-lf128_hf4-s0-q0_291` … `ma-ego-lf128_hf4-s5-q1455_1741` all rendered as `ma-ego-lf128_hf4-s0-q0_2…` in the default `hive queue list`. I had to pass `--all` (which then also dumps 1574 historical DONE rows) just to read the suffix.

**Impact.** Cannot tell apart sibling shards at a glance. The natural distinguishing token (the `q<start>_<end>` suffix) gets chopped first.

**Suggested fix.**
- Auto-fit CMD column to terminal width with right-truncation of the *middle*, preserving suffix: `ma-ego-lf128_hf4-s0…q0_291`.
- Or print the `name` field (from `#HIVE name=`) in a dedicated narrow column and let CMD wrap.
- `--full` flag for the un-truncated view that does *not* also pull in `--all`'s 1574 hidden rows.

---

## 7. No grouping / batch operations

**Symptom.** Submitting and managing N related tasks requires N invocations and N IDs to track by hand.

**Concrete example.** For #3536-#3543 I would have wanted `hive queue submit-batch shards.json` returning a group id, and `hive queue cancel --prefix ma-ego-` to nuke them all if I needed to restart.

**Impact.** Bookkeeping in the agent's scratchpad and Memory files. Easy to miss one when cancelling.

**Suggested fix.**
- `#HIVE group=ma-ego-lohi` directive; group becomes a first-class object.
- `hive queue submit-batch <jsonl>` with one task per line.
- `hive queue {list,cancel,wait,tail} --group ma-ego-lohi` and `--prefix ma-ego-`.

---

## 8. No `hive queue stats`

**Symptom.** The footer `20 running  182 pending  (+1574 old done hidden)` is the entire breakdown. Nothing per-user, per-group, no wait-time histogram.

**Concrete example.** At a glance I could not tell how many of the 182 pending were mine vs `socfac`'s. The only way to know was `hive queue list --all | grep socfac` and eyeball.

**Impact.** Cannot decide whether to wait or bail out and use a side channel.

**Suggested fix.**
- `hive queue stats` outputting:
  - per-user `running / pending / mean-wait / p95-wait`
  - per-prefix or per-group same
  - "your position in queue" for the calling user
- Optional `--json` for the skill.

---

## 9. Cancelled tasks linger in queue view

**Symptom.** Rows in `CANCELLED` state remain visible for a while in `hive queue list` (not in the `+old done hidden` count). Unclear when they get garbage-collected.

**Concrete example.** After cancelling earlier exploratory submissions, CANCELLED rows stayed at the top of `list` until I figured out they needed an explicit prune.

**Impact.** Clutters the default view and confuses "what is actually still active" — minor compared to #1-#5 but a paper cut.

**Suggested fix.**
- Document the GC rule in `docs/architecture.md`.
- Either fold CANCELLED into the hidden bucket after T seconds (say 60), or expose `hive queue prune --state cancelled`.

---

## 10. No dedupe — queued task duplicates work I'm already doing

**Symptom.** Hive does not know whether the work a queued task will do is already in progress elsewhere (another queued task, a login-node script, a prior run's leftover process).

**Concrete example.** `#3542 ma-ego-prep-q0-1741` (HF dataset prep, idempotent) sat PENDING while I was simultaneously running the same prep on the login node. When `#3542` eventually got dispatched it would re-download the same shards. Cache-idempotency saved correctness but not time / bandwidth.

**Impact.** Wasted dispatch slot when slots are scarce — and slots were extremely scarce this session (issue #1).

**Suggested fix.**
- `hive queue submit --skip-if-exists 'fingerprint'` where fingerprint is a user-supplied key (e.g. hash of command + workdir + key args). Hive refuses to enqueue if any RUNNING / PENDING task carries the same fingerprint.
- Post-launch dedupe: at dispatch time re-check the fingerprint and short-circuit to DONE if a sibling already finished.

---

## Prioritization

| Tier | Issues | Rationale |
|---|---|---|
| **P0 — blocking** | #2 (OOM-blind dispatch), #4 (no OOM retry) | These directly broke the workload. Without #2 hive cannot be used when any co-tenant runs out-of-band on pool nodes. #4 turns a transient kill into a manual recovery. |
| **P1 — high friction** | #1 (FIFO/no ETA), #3 (no most-free-GPU placement), #5 (no live visibility) | Each forced me to bypass hive entirely with the SSH-poll dispatchers. These are the features that would let `hive queue` replace the custom scripts. |
| **P2 — nice-to-have** | #6 (column truncation), #7 (batch ops), #8 (stats), #9 (CANCELLED GC), #10 (dedupe) | Quality-of-life and observability. Important for agent ergonomics but workable without. |

---

## Workarounds I used this run

All under `/lustre/fs1/home/si384883/project/lmms-eval/slurm/`:

- **`ma_egoqa_ssh_dispatcher.sh`** — outer loop. Polls `evc39 evc49 evc101 evc102 evc103 evc104` via `ssh ... nvidia-smi --query-gpu=memory.free`; when any node reports `≥NEED_MB` (default 25 GB) free, ssh's into it and runs the next window of questions. Implements #2, #3, and the outer half of #4. Has its own `WINDOW=120` chunking and a `MAX_HOURS` budget.
- **`ma_egoqa_node_dispatcher.sh`** — older variant of the above, pinned per-node.
- **`run_ma_egoqa_full_shard.sh`** — inner wrapper. Sets `LD_LIBRARY_PATH` / `HF_HOME` / `PYTHONPATH`, calls `tools/ma_egoqa_lohi.py run … --resume`, and loops on non-zero exit so a CUDA OOM mid-shard is retried in-place. Implements the inner half of #4.
- **`ma_egoqa_watcher.sh`** — single-shot version of the dispatcher used for the PoC. Same `nvidia-smi --query-gpu=memory.free` gate, single launch then exit.
- **Hive task `#3543 ma-ego-watcher-lf128_hf4`** — the watcher submitted *through* hive, as a placeholder, so it would appear in `hive queue list`. The real scheduling lives in the watcher script, not in hive.
- **Single shared output `logs/ma_egoqa_lohi_full/<TAG>_dispatched.jsonl`** — every shard writes here; idempotency comes from `--resume` reading existing `idx` values out of the jsonl. This is the "dedupe" I had to build to compensate for #10 across my own concurrent shards.

The shape of these workarounds is the spec for the next hive iteration: free-VRAM-aware placement, OOM-aware retry, group submit, and a live `tail` / `inspect`.
