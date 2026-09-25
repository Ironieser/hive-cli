---
id: 31
title: hive list: ELAPSED of RUNNING tasks inflated by ~12h
severity: low
status: done
tags: [list, ui]
submitter: si384883
hive_version: 0.4.0
task_ids: [9176, 9181, 9186, 9187, 9188, 9189]
created: 2026-09-25T15:36:38
updated: 2026-09-25T06:53:36
source: cli
triage_note: Root cause: naive-local *_at strings written by processes under different TZ (sched EDT, poller Asia/Shanghai, shell CST). Fixed: epoch *_ts twins on every timestamp; list/wait/stats use them.
---

hive list: ELAPSED for RUNNING tasks is inflated by ~12h

Observed on 2026-09-25 (login node clock CST):
- Task #9176 showed "RUNNING  evc101  12h00m" about 20 s after submission.
- Tasks #9186-#9189 showed 12h03m-12h11m within minutes of starting.
- Task #9181 showed 13h10m after running for roughly 1h10m.
- Once tasks finish, ELAPSED is correct (e.g. #9180 DONE 1h14m, #9186 DONE 25m08s).
So the offset appears only for RUNNING rows and is about 12h (not the 8h CST/UTC gap), which suggests
a 12-hour-clock or start-time parsing issue. It makes it hard to tell a hung task from a normal one.

## Auto-captured context

- hive_version: 0.4.0
- queue: cancelled:2285  done:5758  failed:1044  running:1
- nodes: updated=2026-09-25T07:21:43  busy:2  idle:2  probe_failed:2  warning:2

<details><summary>task-9176.log (tail)</summary>

```
(APIServer pid=2866565)            ^^^^^^^^^^^^^^^^^^^^^
(APIServer pid=2866565)   File "/home/si384883/envs/vllm_gemma4/lib/python3.12/site-packages/vllm/v1/engine/core_client.py", line 963, in __init__
(APIServer pid=2866565)     super().__init__(
(APIServer pid=2866565)   File "/home/si384883/envs/vllm_gemma4/lib/python3.12/site-packages/vllm/v1/engine/core_client.py", line 573, in __init__
(APIServer pid=2866565)     with launch_core_engines(
(APIServer pid=2866565)          ^^^^^^^^^^^^^^^^^^^^
(APIServer pid=2866565)   File "/home/si384883/envs/vllm_gemma4/lib/python3.12/contextlib.py", line 144, in __exit__
(APIServer pid=2866565)     next(self.gen)
(APIServer pid=2866565)   File "/home/si384883/envs/vllm_gemma4/lib/python3.12/site-packages/vllm/v1/engine/utils.py", line 1213, in launch_core_engines
(APIServer pid=2866565)     wait_for_engine_startup(
(APIServer pid=2866565)   File "/home/si384883/envs/vllm_gemma4/lib/python3.12/site-packages/vllm/v1/engine/utils.py", line 1272, in wait_for_engine_startup
(APIServer pid=2866565)     raise RuntimeError(
(APIServer pid=2866565) RuntimeError: Engine core initialization failed. See root cause above. Failed core proc(s): {}

=== hive task #9176 finished at 2026-09-24T23:23:24-04:00  exit_code=1 ===

```
</details>

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

<details><summary>task-9186.log (tail)</summary>

```
=== node walltime remaining at dispatch: 19h13m (then this node is reclaimed) ===
=== estimated runtime: 1h30m (source: user) ===
=== workdir: /lustre/fs1/home/si384883/project/MotionJudge_front ===
=== cmd: JOB=Kreal REF=mx_real PORT=8011 bash run_mj_job.sh ===

=== gpus: requested 1, visible=[0] (hold job provided [0]) ===
[2026-09-25_02:42:09] start on evc101 gpus=0 model=/lustre/fs1/home/si384883/.cache/huggingface/hub/models--Qwen--Qwen3.5-27B/snapshots/fc05daec18b0a78c049392ed2e771dde82bdf654/
[2026-09-25_02:44:49] vLLM healthy
[2026-09-25_02:46:56] c4 2286/2286
[2026-09-25_02:56:50] c6 rows=7918
[2026-09-25_03:02:09] c8 mx_real/audit mean 1.7493 n 2264 pf 18
[2026-09-25_03:07:05] c8 mx_real/fact mean 1.7321 n 2264 pf 2
[2026-09-25_03:07:09] ALL DONE

=== hive task #9186 finished at 2026-09-25T03:07:09-04:00  exit_code=0 ===

```
</details>

<details><summary>task-9187.log (tail)</summary>

```
=== node walltime remaining at dispatch: 6h38m (then this node is reclaimed) ===
=== estimated runtime: 1h30m (source: user) ===
=== workdir: /lustre/fs1/home/si384883/project/MotionJudge_front ===
=== cmd: JOB=Kabsent REF=mx_absent PORT=8012 bash run_mj_job.sh ===

=== gpus: requested 1, visible=[0] (hold job provided [0]) ===
[2026-09-25_02:45:09] start on evc38 gpus=0 model=/lustre/fs1/home/si384883/.cache/huggingface/hub/models--Qwen--Qwen3.5-27B/snapshots/fc05daec18b0a78c049392ed2e771dde82bdf654/
[2026-09-25_02:47:29] vLLM healthy
[2026-09-25_02:50:49] c4 2286/2286
[2026-09-25_03:06:48] c6 rows=7921
[2026-09-25_03:15:12] c8 mx_absent/audit mean 1.5754 n 2265 pf 18
[2026-09-25_03:22:53] c8 mx_absent/fact mean 1.6207 n 2265 pf 3
[2026-09-25_03:22:59] ALL DONE

=== hive task #9187 finished at 2026-09-25T03:22:59-04:00  exit_code=0 ===

```
</details>

<details><summary>task-9188.log (tail)</summary>

```
=== node walltime remaining at dispatch: 6h48m (then this node is reclaimed) ===
=== estimated runtime: 1h30m (source: user) ===
=== workdir: /lustre/fs1/home/si384883/project/MotionJudge_front ===
=== cmd: JOB=Kzero REF=mx_zero PORT=8013 bash run_mj_job.sh ===

=== gpus: requested 1, visible=[0] (hold job provided [0]) ===
[2026-09-25_02:48:10] start on evc38 gpus=0 model=/lustre/fs1/home/si384883/.cache/huggingface/hub/models--Qwen--Qwen3.5-27B/snapshots/fc05daec18b0a78c049392ed2e771dde82bdf654/
[2026-09-25_02:50:20] vLLM healthy
[2026-09-25_02:53:46] c4 2286/2286
[2026-09-25_03:12:17] c6 rows=7918
[2026-09-25_03:21:13] c8 mx_zero/audit mean 1.5757 n 2264 pf 13
[2026-09-25_03:29:23] c8 mx_zero/fact mean 1.5883 n 2264 pf 5
[2026-09-25_03:29:29] ALL DONE

=== hive task #9188 finished at 2026-09-25T03:29:29-04:00  exit_code=0 ===

```
</details>

<details><summary>task-9189.log (tail)</summary>

```
=== node walltime remaining at dispatch: 8h53m (then this node is reclaimed) ===
=== estimated runtime: 1h30m (source: user) ===
=== workdir: /lustre/fs1/home/si384883/project/MotionJudge_front ===
=== cmd: JOB=Kshuffle REF=mx_shuffle PORT=8014 bash run_mj_job.sh ===

=== gpus: requested 1, visible=[0] (hold job provided [0]) ===
[2026-09-25_02:50:44] start on evc104 gpus=0 model=/lustre/fs1/home/si384883/.cache/huggingface/hub/models--Qwen--Qwen3.5-27B/snapshots/fc05daec18b0a78c049392ed2e771dde82bdf654/
[2026-09-25_02:53:34] vLLM healthy
[2026-09-25_02:55:41] c4 2286/2286
[2026-09-25_03:05:15] c6 rows=7921
[2026-09-25_03:10:28] c8 mx_shuffle/audit mean 1.4851 n 2265 pf 12
[2026-09-25_03:15:19] c8 mx_shuffle/fact mean 1.5503 n 2265 pf 8
[2026-09-25_03:15:24] ALL DONE

=== hive task #9189 finished at 2026-09-25T03:15:24-04:00  exit_code=0 ===

```
</details>
