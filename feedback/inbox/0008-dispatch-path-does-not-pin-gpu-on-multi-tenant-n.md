---
id: 8
title: Dispatch path does not pin GPU on multi-tenant nodes -> device_count>1 crash + GPU-0 collision
severity: high
status: done
tags: [scheduler, gpu, dispatch, cgroup, dataparallel]
submitter: si384883
hive_version: 0.3.2
task_ids: [6862, 6929]
created: 2026-07-20T08:54:17
updated: 2026-07-26T14:59:29
source: cli
triage_note: Root cause in the report was DISPROVED: srun --overlap DOES inherit the hold-job cgroup (verified on 7 hold jobs, 2 partitions, 3 multi-tenant nodes; SLURM_STEP_GPUS matched scontrol IDX). Real cause: hold job 689469 was submitted --gres=gpu:2 (sacct: gres/gpu=2, ~/2_gpu.slurm), so device_count=2 was correct and HF Trainer auto-DataParallel'd. Fixed by adding #HIVE gpus=N / --gpus (default 1): dispatch narrows CUDA_VISIBLE_DEVICES to the first N SLURM gave, placement gated on hold-job GPU count (insufficient_gpus), and every task log records its actual visibility. No --gres/--exact needed on the step.
---

# Dispatch path does not pin the GPU on multi-tenant nodes → device_count>1 crashes + GPU-0 collisions

## Summary
When multiple hold-jobs share one physical node, `dispatch_task()` launches the task
with `srun --jobid=<holdjob> --overlap` **without setting `CUDA_VISIBLE_DEVICES` and
without passing `--gres`/`--gpus`/`--gpu-bind`**. The overlap step does not inherit the
hold-job's per-GPU cgroup device binding, so the task sees the *whole node's* GPUs. Two
independent, reproducible failure modes result:

1. **DataParallel crash.** A framework that auto-parallelizes over visible GPUs (HF
   Trainer) sees `torch.cuda.device_count() > 1` and silently wraps the model in
   `nn.DataParallel`; PEFT/TRL's chunked-CE path then dies at step 0 with
   `Expected all tensors to be on the same device ... cuda:1 vs cuda:0`. This masqueraded
   as a "hang" for a long time — the job ran ~40 min (queue + shard load + tokenize) then
   crashed 39s into training, leaving an empty checkpoint dir.
2. **GPU-0 collision / OOM.** Every overlap task defaults to physical GPU 0, so N
   hold-jobs on one node all target GPU 0 and OOM each other, even though their jobids map
   to different GPUs.

## Evidence
- `libexec/hive-sched` `dispatch_task()` (currently ~386-472): the `srun` arg list is
  `["srun", "--jobid=<J>", "--overlap", "-n1", "--mem=0", "bash", "-c", wrapper]` and the
  wrapper exports no env. Grep of `libexec/` finds ZERO occurrences of
  `CUDA_VISIBLE_DEVICES`, `--gpus`, or `--gres`.
- Cluster is `ConstrainDevices=yes` + `TaskPlugin=task/cgroup`, so each *hold-job* gets a
  correct 1-GPU cgroup — but the `--overlap` *step* does not inherit it here.
- Read-only probe, SINGLE-tenant node (one hold-job on the box): overlap step correctly
  sees `nvidia-smi -L` = 1 GPU, `CUDA_VISIBLE_DEVICES=0`, `torch.cuda.device_count()==1`.
  → isolation works when only one hold-job is on the node.
- MULTI-tenant node (e.g. 3 hold-jobs 6862/6863/6864 co-located on evc102): the training
  job logged `device_count=2` and crashed at step 0. This is the reproducer.
- The poll side ALREADY worked around this asymmetry for *reads*: `libexec/hive-poll`
  hardcodes `nvidia-smi --id=0` and verifies process ownership via
  `/proc/<pid>/cgroup` containing `/job_<SLURM_JOB_ID>/`, with an explicit comment that
  "CUDA_VISIBLE_DEVICES is unreliable under concurrent srun --overlap steps." The **write
  (dispatch) path never received the equivalent fix.**

## Impact
Any HF-Trainer / multi-GPU-aware workload dispatched through hive onto a node that hosts
>1 hold-job crashes or collides. It is silent (no scheduler-level error) and easy to
misread as a hang or a user bug. Single-tenant nodes are unaffected, which makes it
intermittent and confusing.

## Suggested fix (matches the pre-existing proposal in
feedback/archive/SESSION_ISSUES.md Issue 2)
Pin the overlap step to the hold-job's own GPU. Two viable routes:

1. **Smallest correct change — SLURM-native, no bookkeeping.** Try
   `srun --jobid=<J> --overlap --exact --gres=gpu:1` in `dispatch_task()`. If this
   cluster honors `--exact` GRES on overlap steps, SLURM sets `CUDA_VISIBLE_DEVICES`
   correctly and both failure modes vanish with a one-line change. **Verify with a
   read-only probe before shipping** (print `$CUDA_VISIBLE_DEVICES`, `nvidia-smi -L | wc -l`,
   `torch.cuda.device_count()` with and without the flag on a multi-tenant node).
2. **UUID export (robust fallback).** Resolve each hold-job's assigned GPU UUID **from the
   hold-job's own sbatch body** (where task/cgroup binds correctly): `nvidia-smi
   --query-gpu=uuid --format=csv,noheader` returns exactly the one assigned GPU; cache it
   in `node_monitor.json`. Then in the dispatch wrapper `export
   CUDA_VISIBLE_DEVICES=GPU-<uuid>` (UUID, not index — index is raced under concurrent
   overlap steps, per the poll-side comment; do NOT resolve the UUID via an overlap probe,
   which rides the same broken path).

Avoid the whole-node `--exclusive gpu:8` pool-model change (SESSION_ISSUES Option 2)
unless you want the larger blast radius — it changes packing for every project and needs
new per-GPU occupancy state.

## Workaround in use meanwhile
Submit onto a node that currently hosts only one hold-job (single-tenant) — isolation then
works and `device_count==1`. Also keep application-side guards (pin
`CUDA_VISIBLE_DEVICES=0` before importing torch, assert `device_count==1`) as a belt, but
note `=0` forces the collision target on a multi-tenant node, so it is a stopgap, not a fix.

## Auto-captured context

- hive_version: 0.3.2
- queue: cancelled:1833  done:4292  failed:750
- nodes: updated=2026-07-20T12:39:14  busy:1  idle:1

<details><summary>task-6862.log (tail)</summary>

```
  File "/home/si384883/.conda/envs/socfac_train/lib/python3.12/site-packages/torch/nn/modules/module.py", line 1830, in inner
    result = forward_call(*args, **kwargs)
             ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/home/si384883/.conda/envs/socfac_train/lib/python3.12/site-packages/torch/nn/modules/sparse.py", line 191, in forward
    return F.embedding(
           ^^^^^^^^^^^^
  File "/home/si384883/.conda/envs/socfac_train/lib/python3.12/site-packages/torch/nn/functional.py", line 2567, in embedding
    return torch.embedding(weight, input, padding_idx, scale_grad_by_freq, sparse)
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
RuntimeError: Expected all tensors to be on the same device, but got index is on cuda:1, different from other tensors on cuda:0 (when checking argument in method wrapper_CUDA__index_select)


  0%|          | 0/147 [00:39<?, ?it/s]

=== hive task #6862 finished at 2026-07-15T07:07:34-04:00  exit_code=1 ===

```
</details>

<details><summary>task-6929.log (tail)</summary>

```
{'loss': 0.3021, 'grad_norm': 8.96676254272461, 'learning_rate': 1.6620592989192318e-06, 'entropy': 0.29139855518005786, 'num_tokens': 2414407.0, 'mean_token_accuracy': 0.83125, 'epoch': 0.74}
{'loss': 0.2923, 'grad_norm': 12.348800659179688, 'learning_rate': 1.3829563342637514e-06, 'entropy': 0.2741245202720165, 'num_tokens': 2493304.0, 'mean_token_accuracy': 0.83125, 'epoch': 0.77}
{'loss': 0.2707, 'grad_norm': 4.5988616943359375, 'learning_rate': 1.125714464485551e-06, 'entropy': 0.2729236592189409, 'num_tokens': 2567181.0, 'mean_token_accuracy': 0.84375, 'epoch': 0.79}
{'loss': 0.2535, 'grad_norm': 4.07861852645874, 'learning_rate': 8.918884368979969e-07, 'entropy': 0.26270041380776094, 'num_tokens': 2648133.0, 'mean_token_accuracy': 0.85, 'epoch': 0.82}
{'loss': 0.2962, 'grad_norm': 7.365673542022705, 'learning_rate': 6.828914755166826e-07, 'entropy': 0.281016356498003, 'num_tokens': 2724978.0, 'mean_token_accuracy': 0.8375, 'epoch': 0.84}
{'loss': 0.2866, 'grad_norm': 9.444068908691406, 'learning_rate': 4.999867396573499e-07, 'entropy': 0.27506883514579383, 'num_tokens': 2801874.0, 'mean_token_accuracy': 0.83125, 'epoch': 0.87}
{'loss': 0.2284, 'grad_norm': 1.8038944005966187, 'learning_rate': 3.4427968951170287e-07, 'entropy': 0.26169310780242083, 'num_tokens': 2879624.0, 'mean_token_accuracy': 0.91875, 'epoch': 0.89}
{'loss': 0.281, 'grad_norm': 2.9195988178253174, 'learning_rate': 2.1671140484290144e-07, 'entropy': 0.2676436848938465, 'num_tokens': 2955152.0, 'mean_token_accuracy': 0.8375, 'epoch': 0.91}
{'loss': 0.304, 'grad_norm': 4.555393695831299, 'learning_rate': 1.1805289718196499e-07, 'entropy': 0.2773726547602564, 'num_tokens': 3033035.0, 'mean_token_accuracy': 0.85, 'epoch': 0.94}
{'loss': 0.2999, 'grad_norm': 9.02629566192627, 'learning_rate': 4.8900449901653214e-08, 'entropy': 0.2800781705416739, 'num_tokens': 3104807.0, 'mean_token_accuracy': 0.825, 'epoch': 0.96}
{'loss': 0.2702, 'grad_norm': 2.7317347526550293, 'learning_rate': 9.672014332028357e-09, 'entropy': 0.27665542187169195, 'num_tokens': 3186307.0, 'mean_token_accuracy': 0.8625, 'epoch': 0.99}
{'train_runtime': 779.6917, 'train_samples_per_second': 4.27, 'train_steps_per_second': 0.535, 'train_loss': 0.5772740909521529, 'entropy': 0.27511404853846344, 'num_tokens': 3233969.0, 'mean_token_accuracy': 0.8163265306122449, 'epoch': 1.0}
[train_rft] DONE -> /lustre/fs1/home/si384883/project/socfac/training/ckpt/selfcal

=== hive task #6929 finished at 2026-07-20T06:04:52-04:00  exit_code=0 ===

```
</details>
