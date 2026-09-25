---
id: 16
title: evc43 (hold 715321) reports IDLE and accepts dispatch, but every vLLM task place
severity: medium
status: done
tags: []
submitter: si384883
hive_version: 0.4.0
task_ids: []
created: 2026-07-31T11:04:34
updated: 2026-09-25T07:29:18
source: cli
triage_note: Fixed: hive health — verify-before-dispatch creates a real CUDA context (ctypes/libcuda) and quarantines the node on failure (evc43 verified: cuCtxCreate=999); fast task failures with a CUDA-init log signature strike the node (2 → quarantine + requeue); periodic probes release it. hive health report/check/clear.
---

evc43 (hold 715321) reports IDLE and accepts dispatch, but every vLLM task placed on it dies at torch.cuda.set_device with cudaErrorDevicesUnavailable ('CUDA-capable device(s) is/are busy or unavailable'). Same signature as hive issue #15 on 07-31 (evc43 IDLE misreport killed 9 P4 tasks); it has now cost tasks 7675/7679/7683/7666 (P4 panel) and 7702 (v14.4 W1 v6.1 panel, glm4_32). The node monitor shows GPU% 2 and MEM 0G/80G for it, so the dirty-GPU guard (>5GB resident) does not catch this failure mode. Suggestion: treat a repeated set_device failure on a node as a dispatch-blocking signal, or probe with an actual CUDA context rather than nvidia-smi memory.

## Auto-captured context

- hive_version: 0.4.0
- queue: cancelled:2048  done:4771  failed:785  pending:1  running:6
- nodes: updated=2026-07-31T14:54:16  idle:5  probe_failed:2  warning:3
