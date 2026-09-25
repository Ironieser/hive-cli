---
id: 32
title: hive nodes shows stale GPU state after cancel/dispatch
severity: low
status: done
tags: [nodes, poll]
submitter: si384883
hive_version: 0.4.0
task_ids: [9181, 9182, 9185]
created: 2026-09-25T15:36:38
updated: 2026-09-25T06:53:37
source: cli
triage_note: Display half fixed: hive nodes/top show CLAIM + task for a hold job with a running task; BUSY-after-cancel half is poll staleness (row shows ! when >10min).
---

hive nodes shows stale GPU state after tasks are cancelled or started

Observed on 2026-09-25:
- After cancelling #9181/#9182 (both on evc104), "hive nodes" still listed holds 847877 and 847885
  as BUSY 100% 75-76G/80G "VLLM::EngineCore". ssh evc104 showed no processes of mine and
  nvidia-smi 0 MiB; cancel had cleaned up correctly. Only the table was stale.
- While #9185 was running vLLM on evc38, the evc38 holds (847900, 848135) were listed IDLE 0G/80G.
The header shows the global "last polled" time, but not how old each row is, so the table reads as
current. Suggestion: trigger a poll after cancel/dispatch, or show per-row age / mark rows older than the
last cancel/dispatch on that hold.

## Auto-captured context

- hive_version: 0.4.0
- queue: cancelled:2285  done:5758  failed:1044  running:1
- nodes: updated=2026-09-25T07:21:43  busy:2  idle:2  probe_failed:2  warning:2

<details><summary>task-9181.log (tail)</summary>

```
=== hive task #9181 started at 2026-09-25T01:19:13 ===
=== node: evc104  slurm_jobid: 847885 ===
=== node walltime remaining at dispatch: 10h24m (then this node is reclaimed) ===
=== estimated runtime: 2h00m (source: user) ===
=== workdir: /lustre/fs1/home/si384883/project/MotionJudge_front ===
=== cmd: JOB=A REF=mx_real PORT=8001 bash run_mj_job.sh ===

=== gpus: requested 1, visible=[0] (hold job provided [0]) ===
[2026-09-25_01:19:14] start on evc104 gpus=0 model=/lustre/fs1/home/si384883/.cache/huggingface/hub/models--Qwen--Qwen3.5-27B/snapshots/fc05daec18b0a78c049392ed2e771dde82bdf654/
[2026-09-25_01:26:14] vLLM healthy
[2026-09-25_01:30:26] c4 4572/4572
srun: forcing job termination
srun: Job step aborted: Waiting up to 32 seconds for job step to finish.
[2026-09-25T02:29:47.645] error: *** STEP 847885.6 ON evc104 CANCELLED AT 2026-09-25T02:29:47 DUE to SIGNAL Killed ***
srun: error: evc104: task 0: Killed

```
</details>

<details><summary>task-9182.log (tail)</summary>

```
=== hive task #9182 started at 2026-09-25T01:19:13 ===
=== node: evc104  slurm_jobid: 847877 ===
=== node walltime remaining at dispatch: 22h23m (then this node is reclaimed) ===
=== estimated runtime: 2h00m (source: user) ===
=== workdir: /lustre/fs1/home/si384883/project/MotionJudge_front ===
=== cmd: JOB=B REF=mx_zero PORT=8002 bash run_mj_job.sh ===

=== gpus: requested 1, visible=[0] (hold job provided [0]) ===
[2026-09-25_01:19:14] start on evc104 gpus=0 model=/lustre/fs1/home/si384883/.cache/huggingface/hub/models--Qwen--Qwen3.5-27B/snapshots/fc05daec18b0a78c049392ed2e771dde82bdf654/
[2026-09-25_01:26:14] vLLM healthy
[2026-09-25_01:30:29] c4 4572/4572
srun: forcing job termination
srun: Job step aborted: Waiting up to 32 seconds for job step to finish.
[2026-09-25T02:29:47.945] error: *** STEP 847877.6 ON evc104 CANCELLED AT 2026-09-25T02:29:47 DUE to SIGNAL Killed ***
srun: error: evc104: task 0: Killed

```
</details>

<details><summary>task-9185.log (tail)</summary>

```
=== hive task #9185 started at 2026-09-25T02:30:07 ===
=== node: evc38  slurm_jobid: 847900 ===
=== node walltime remaining at dispatch: 6h53m (then this node is reclaimed) ===
=== estimated runtime: 3h00m (source: user) ===
=== workdir: /lustre/fs1/home/si384883/project/MotionJudge_front ===
=== cmd: JOB=AB REF=mx_real PORT=8005 bash run_mj_job.sh ===

=== gpus: requested 1, visible=[0] (hold job provided [0]) ===
[2026-09-25_02:30:08] start on evc38 gpus=0 model=/lustre/fs1/home/si384883/.cache/huggingface/hub/models--Qwen--Qwen3.5-27B/snapshots/fc05daec18b0a78c049392ed2e771dde82bdf654/
[2026-09-25_02:35:49] vLLM healthy
srun: forcing job termination
srun: Job step aborted: Waiting up to 32 seconds for job step to finish.
[2026-09-25T02:41:45.649] error: *** STEP 847900.25 ON evc38 CANCELLED AT 2026-09-25T02:41:45 DUE to SIGNAL Killed ***
srun: error: evc38: task 0: Killed

```
</details>
