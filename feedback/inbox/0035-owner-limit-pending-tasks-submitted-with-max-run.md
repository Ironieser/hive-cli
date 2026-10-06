---
id: 35
title: owner_limit: pending tasks submitted with --max-running 14 stay held at 9 runnin
severity: medium
status: open
tags: []
submitter: si384883
hive_version: 0.4.0
task_ids: []
created: 2026-10-04T00:38:21
source: cli
---

owner_limit: pending tasks submitted with --max-running 14 stay held at 9 running while older running tasks of the same owner carry max_running=9; 3 GPUs idle

## Auto-captured context

- hive_version: 0.4.0
- queue: cancelled:217  done:1098  failed:120  pending:10  running:9
- nodes: updated=2026-10-04T04:32:51  busy:9  idle:3
