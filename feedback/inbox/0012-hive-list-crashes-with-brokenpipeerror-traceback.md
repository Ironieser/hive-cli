---
id: 12
title: hive list crashes with BrokenPipeError traceback when its stdout is closed early
severity: medium
status: done
tags: []
submitter: si384883
hive_version: 0.4.0
task_ids: []
created: 2026-07-30T15:52:05
updated: 2026-09-25T06:53:36
source: cli
triage_note: Fixed: SIGPIPE restored to default in hive-queue (hive list | head exits quietly).
---

hive list crashes with BrokenPipeError traceback when its stdout is closed early by a pager/head (e.g. `hive list | head -12`). Expected: exit quietly on SIGPIPE. Repro: `hive list | head -12` -> prints rows then a Python traceback from hive-queue print_row (libexec/hive-queue:649). Cosmetic but it pollutes agent logs and makes a successful command look failed. Fix: signal(SIGPIPE, SIG_DFL) at startup, or wrap the print loop in try/except BrokenPipeError.

## Auto-captured context

- hive_version: 0.4.0
- queue: cancelled:2042  done:4696  failed:760  running:2
- nodes: updated=2026-07-30T19:36:00  idle:1  probe_failed:2
