---
id: 22
title: Correction to #21: not a deadlock, slow cold load off Lustre
severity: low
status: wontfix
tags: [scheduler, io]
submitter: si384883
hive_version: 0.4.0
task_ids: []
created: 2026-08-01T11:18:40
updated: 2026-09-25T06:53:37
source: cli
triage_note: Reporter withdrew #21 (slow cold load off Lustre, not a scheduler issue).
---

CORRECTION to my feedback #21. I reported that all 7 pool nodes deadlocked at 'Starting to load model'. That was WRONG and I withdraw it. What actually happened: I misread the wall clock and read a mid-buffer log tail. The jobs were loading slowly, not hung -- 7728/7731/7732 exited 0 within about 10 minutes, and 7727/7729/7733 were actively generating when I looked again. There was no deadlock and the scheduler did nothing wrong. Please disregard the suggestion in #21 that dispatch needs a stagger / max_concurrent_starts knob; I filed it on a false premise. The one real observation that survives: a 0.6B model took ~8 of those 10 minutes just to load off the shared Lustre HF cache with 7 concurrent loaders, which is the cost hive rule 5 already documents. Node-local staging fixes it and is a job-script concern, not a scheduler one.

## Auto-captured context

- hive_version: 0.4.0
- queue: cancelled:2049  done:4794  failed:787  pending:40  running:9
- nodes: updated=2026-08-01T15:16:56  cpu:1  idle:9  probe_failed:2
