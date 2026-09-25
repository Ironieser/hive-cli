---
id: 13
title: Dispatch onto GPU held by an out-of-cgroup co-tenant: memory-based gpu_dirty check is blind to it
severity: high
status: triaged
tags: [scheduler, dispatch, probe]
submitter: si384883
hive_version: 0.4.0
task_ids: [7600, 7602]
created: 2026-07-30T15:58:15
updated: 2026-09-25T06:53:37
source: cli
triage_note: Node-health theme: co-tenant / broken node (evc43, evc50) accepts dispatch, CUDA init fails. Needs squeue -w foreign-job detection + auto-quarantine after N fast failures (ROADMAP C4). Not fixed yet.
---

## Symptom
hive dispatched tasks #7600 and #7602 (v143_smoke_q27) to hold job 715321 on evc43, which
hive reported as IDLE (GPU% 2, MEM 0G/80G). Both died within ~3 min at vLLM engine init:

    torch.AcceleratorError: CUDA error: CUDA-capable device(s) is/are busy or unavailable
    RuntimeError: Engine core initialization failed.

## Root cause
evc43 carries a FOREIGN GPU job from another user:

    $ squeue -w evc43 -o "%.10i %.12u %.20j %.8T %b"
        721771     ma958129    bash   RUNNING  gres/gpu:nvidia_h100_pcie:1
        715321     si384883    highgpu_sleep RUNNING gres/gpu:nvidia_h100_pcie:1

That job holds the card. From inside OUR cgroup, `nvidia-smi` shows the device with
**0 MiB used** (the co-tenant's memory/PIDs are not visible across cgroups), so the
poller sees "idle" and the `gpu_dirty` guard (>5 GB resident) never fires. The card is
nonetheless unusable: a plain `torch.zeros(8, device='cuda')` under
`srun --overlap --jobid=715321` fails with the same cudaErrorDevicesUnavailable.

So the memory-based dirty check is BLIND to out-of-cgroup co-tenants, and every task
dispatched to such a node fails at CUDA init after paying full model-rsync + load cost
(here ~4 min per attempt; for a big model it would be 20+ min per attempt, times the
3 auto-retries).

## Suggested fix (either or both)
1. Make the idle probe do a real CUDA-context probe (`torch.zeros(1, device='cuda')` or
   `cuInit` + `cuCtxCreate`) instead of / in addition to reading memory.used. That is the
   only check that detects a co-tenant.
2. In the probe, also run `squeue -w <node> --states=RUNNING -O JobID,UserName,TresPerNode`
   and mark the node dirty when a GPU job that is NOT one of our hold jobs is present.

## Second, smaller issue
Hold job 715320 (evc34) has been PFAIL for 19h: inside it `nvidia-smi` returns
"No devices were found" while `CUDA_VISIBLE_DEVICES=0`. A hold job with zero usable
devices stays in the pool forever as probe-fail noise; it would be better to detect
"allocation has no visible GPU" and report it distinctly (e.g. NOGPU) so the user knows
to release that specific hold job rather than assume a transient probe miss.

## Workaround used
`hive pool add` for fresh capacity; direct `srun --overlap --jobid=<good hold>` otherwise.

## Auto-captured context

- hive_version: 0.4.0
- queue: cancelled:2042  done:4696  failed:762  running:1
- nodes: updated=2026-07-30T19:52:42  idle:1  probe_failed:2

<details><summary>task-7600.log (tail)</summary>

```
    return func(*args, **kwargs)
           ^^^^^^^^^^^^^^^^^^^^^
  File "/home/si384883/.conda/envs/vlm/lib/python3.12/site-packages/vllm/v1/engine/core_client.py", line 734, in __init__
    super().__init__(
  File "/home/si384883/.conda/envs/vlm/lib/python3.12/site-packages/vllm/v1/engine/core_client.py", line 569, in __init__
    with launch_core_engines(
  File "/home/si384883/.conda/envs/vlm/lib/python3.12/contextlib.py", line 144, in __exit__
    next(self.gen)
  File "/home/si384883/.conda/envs/vlm/lib/python3.12/site-packages/vllm/v1/engine/utils.py", line 951, in launch_core_engines
    wait_for_engine_startup(
  File "/home/si384883/.conda/envs/vlm/lib/python3.12/site-packages/vllm/v1/engine/utils.py", line 1010, in wait_for_engine_startup
    raise RuntimeError(
RuntimeError: Engine core initialization failed. See root cause above. Failed core proc(s): {}

=== hive task #7600 finished at 2026-07-30T15:53:10-04:00  exit_code=1 ===

```
</details>

<details><summary>task-7602.log (tail)</summary>

```
    return func(*args, **kwargs)
           ^^^^^^^^^^^^^^^^^^^^^
  File "/home/si384883/.conda/envs/vlm/lib/python3.12/site-packages/vllm/v1/engine/core_client.py", line 734, in __init__
    super().__init__(
  File "/home/si384883/.conda/envs/vlm/lib/python3.12/site-packages/vllm/v1/engine/core_client.py", line 569, in __init__
    with launch_core_engines(
  File "/home/si384883/.conda/envs/vlm/lib/python3.12/contextlib.py", line 144, in __exit__
    next(self.gen)
  File "/home/si384883/.conda/envs/vlm/lib/python3.12/site-packages/vllm/v1/engine/utils.py", line 951, in launch_core_engines
    wait_for_engine_startup(
  File "/home/si384883/.conda/envs/vlm/lib/python3.12/site-packages/vllm/v1/engine/utils.py", line 1010, in wait_for_engine_startup
    raise RuntimeError(
RuntimeError: Engine core initialization failed. See root cause above. Failed core proc(s): {}

=== hive task #7602 finished at 2026-07-30T15:55:02-04:00  exit_code=1 ===

```
</details>
