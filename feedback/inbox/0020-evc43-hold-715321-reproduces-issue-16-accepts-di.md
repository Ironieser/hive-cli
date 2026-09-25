---
id: 20
title: evc43 (hold 715321) reproduces issue #16: accepts dispatch, dies at CUDA set_device
severity: high
status: done
tags: [scheduler, node-health]
submitter: si384883
hive_version: 0.4.0
task_ids: [7730]
created: 2026-08-01T11:17:22
updated: 2026-09-25T07:29:18
source: cli
triage_note: Fixed: hive health — verify-before-dispatch creates a real CUDA context (ctypes/libcuda) and quarantines the node on failure (evc43 verified: cuCtxCreate=999); fast task failures with a CUDA-init log signature strike the node (2 → quarantine + requeue); periodic probes release it. hive health report/check/clear.
---

evc43 (hold 715321) reproduces issue #16: accepts dispatch, dies at CUDA set_device

## Auto-captured context

- hive_version: 0.4.0
- queue: cancelled:2049  done:4791  failed:786  pending:24  running:7
- nodes: updated=2026-08-01T15:16:56  cpu:1  idle:9  probe_failed:2

<details><summary>task-7730.log (tail)</summary>

```
           ^^^^^^^^^^^^^^^^^^^^^
  File "/home/si384883/.conda/envs/vlm/lib/python3.12/site-packages/vllm/v1/worker/gpu_worker.py", line 257, in init_device
    current_platform.set_device(self.device)
  File "/home/si384883/.conda/envs/vlm/lib/python3.12/site-packages/vllm/platforms/cuda.py", line 141, in set_device
    torch.cuda.set_device(device)
  File "/home/si384883/.conda/envs/vlm/lib/python3.12/site-packages/torch/cuda/__init__.py", line 584, in set_device
    torch._C._cuda_setDevice(device)
torch.AcceleratorError: CUDA error: CUDA-capable device(s) is/are busy or unavailable
Search for `cudaErrorDevicesUnavailable' in https://docs.nvidia.com/cuda/cuda-runtime-api/group__CUDART__TYPES.html for more information.
CUDA kernel errors might be asynchronously reported at some other API call, so the stacktrace below might be incorrect.
For debugging consider passing CUDA_LAUNCH_BLOCKING=1
Compile with `TORCH_USE_CUDA_DSA` to enable device-side assertions.


=== hive task #7730 finished at 2026-08-01T11:16:29-04:00  exit_code=1 ===

```
</details>
