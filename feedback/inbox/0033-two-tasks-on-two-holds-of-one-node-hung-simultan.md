---
id: 33
title: Two tasks on two holds of one node hung simultaneously (unconfirmed; see #28/#30)
severity: medium
status: triaged
tags: [scheduler, hang]
submitter: si384883
hive_version: 0.4.0
task_ids: [9181, 9182, 9185]
created: 2026-09-25T15:36:38
updated: 2026-09-25T06:53:38
source: cli
triage_note: Node-health theme: co-tenant / broken node (evc43, evc50) accepts dispatch, CUDA init fails. Needs squeue -w foreign-job detection + auto-quarantine after N fast failures (ROADMAP C4). Not fixed yet.
---

Two tasks on two holds of the same node (evc104) both hung at the same moment (supplements #28/#30; cause unconfirmed)

- #9181 and #9182 were each dispatched to a different hold on evc104 (847877, 847885).
- Each started vLLM (Qwen3.5-27B, TP=1, gpu-mem 0.92); both became healthy and processed requests.
- At the same moment (vllm log 01:38 UTC) both engines stopped making progress: throughput 0 tok/s,
  with 32 and 18 requests shown as Running and none Waiting, for about 5 h until I cancelled them.
- No error in either vllm.log or client log.
- Later #9187 and #9188 ran on two holds of evc38 at the same time and finished normally, so this is
  not deterministic.
- #9185 (evc38) also ran ~20x slower than the same workload in #9180 for its first ~4 h without errors,
  then sped up.
I could not confirm the cause, so this is filed as an observation that may relate to #28/#30.

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
