---
id: 26
title: no_dispatchable_node when mem_free_mb is null: unknown treated as insufficient (supplements #25)
severity: high
status: done
tags: [scheduler, dispatch, node-monitor]
submitter: si384883
hive_version: 0.4.0
task_ids: [7937, 7938, 7934]
created: 2026-08-01T15:49:08
updated: 2026-09-25T06:53:36
source: cli
triage_note: Same root cause as #24 (not a null mem field — no such field exists). Fixed with #24.
---

Supplement to feedback #25 (same incident), attaching task logs.

DIAGNOSIS (cause, not symptom): idle entries in node_monitor.json carried
`mem_free_mb = null`. A task submitted with `--need-mb 60000` then never dispatched,
pending_reason `no_dispatchable_node`, while 13 nodes were status=idle and BOTH daemons
had heartbeats under 30s old.

The reason string is misleading twice over:
  1. it says `no_dispatchable_node` rather than `waiting_for_mem`, so the operator looks
     at the pool and the scheduler rather than at the memory probe;
  2. the pool was in fact fine -- identical `--need-mb 60000` tasks (#7915 #7929 #7931
     #7932 #7934) dispatched normally on the same pool in the preceding 90 minutes.

Suggested fix: treat `mem_free_mb = null` as UNKNOWN rather than as 0. Either dispatch and
let the task fail loudly, or keep it pending with reason `mem_probe_unavailable` so the
operator is pointed at the probe. Silently folding "unknown" into "insufficient" makes an
idle pool look undispatchable.

WORKAROUND USED: `srun --overlap --jobid=<a RUNNING hold>` -- ran to completion first try.
Resubmitting WITHOUT `--need-mb` did NOT help (#7938 also stuck), which is additional
evidence the null is being read at probe time rather than at need-check time.

## Auto-captured context

- hive_version: 0.4.0
- queue: cancelled:2092  done:4916  failed:828  pending:1
- nodes: updated=2026-08-01T19:48:05  busy:1  cpu:1  idle:13

<details><summary>task-7934.log (tail)</summary>

```
Loading weights:  66%|██████▌   | 782/1188 [00:07<00:03, 117.41it/s]
Loading weights:  67%|██████▋   | 796/1188 [00:07<00:03, 118.31it/s]
Loading weights:  68%|██████▊   | 809/1188 [00:07<00:03, 116.39it/s]
Loading weights:  69%|██████▉   | 824/1188 [00:07<00:03, 119.72it/s]
Loading weights:  80%|████████  | 954/1188 [00:07<00:00, 434.72it/s]
Loading weights: 100%|█████████▉| 1186/1188 [00:07<00:00, 952.97it/s]
Loading weights: 100%|██████████| 1188/1188 [00:07<00:00, 157.25it/s]
The following generation flags are not valid and may be ignored: ['top_p', 'top_k']. Set `TRANSFORMERS_VERBOSITY=info` for more details.
  hf batch 1/2
  hf batch 2/2
labels: {2: 14, 3: 9, 1: 7, 'X': 1}
-> easylogic_v5/training/data/mining/v7pilot/mag_arb_gemma4.json
=== V7PILOT magarb COMPLETE 2026-08-01T19:07:48Z rc=0 ===

=== hive task #7934 finished at 2026-08-01T15:07:48-04:00  exit_code=0 ===

```
</details>
