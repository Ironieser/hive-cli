#!/usr/bin/env python3
"""Deterministic OFFLINE unit + integration tests for hive-cli.

Invoked by tests/run.sh, which sets up mock SLURM binaries on PATH and a shadow
HIVE_DIR in a temp dir — this never touches ~/.hive, a real cluster, or the running
daemons. Usage:  python3 tests/test_hive.py [<repo_root>]
"""
import datetime
import json
import os
import sys
import time
from importlib.machinery import SourceFileLoader

REPO = sys.argv[1] if len(sys.argv) > 1 else \
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIB = os.path.join(REPO, "libexec")
sys.path.insert(0, LIB)

ev = SourceFileLoader("hive_events", os.path.join(LIB, "hive_events.py")).load_module()
hs = SourceFileLoader("hs", os.path.join(LIB, "hive-sched")).load_module()
hq = SourceFileLoader("hq", os.path.join(LIB, "hive-queue")).load_module()
dbp = SourceFileLoader("dbp", os.path.join(LIB, "hive-dbpost")).load_module()

for d in (hs.HIVE_DIR, hs.HEARTBEAT_DIR, hs.LOG_DIR):
    os.makedirs(d, exist_ok=True)

P = {"pass": 0, "fail": 0}


def chk(desc, cond):
    ok = bool(cond)
    print(f"  [{'OK ' if ok else 'FAIL'}] {desc}")
    P["pass" if ok else "fail"] += 1


def utc(o=0):
    return (datetime.datetime.now(datetime.timezone.utc)
            + datetime.timedelta(seconds=o)).strftime('%Y-%m-%dT%H:%M:%S')


def loc(o=0):
    return (datetime.datetime.now() + datetime.timedelta(seconds=o)).strftime('%Y-%m-%dT%H:%M:%S')


def wdb(jobs):
    json.dump({"updated": utc(), "jobs": jobs}, open(hs.NODE_DB, "w"))


def wq(tasks):
    json.dump({"version": 1, "next_id": 999, "tasks": tasks}, open(hs.QUEUE_FILE, "w"))


def rq():
    return json.load(open(hs.QUEUE_FILE))["tasks"]


def node(name, left, st="idle"):
    return {"node": name, "partition": "gpu", "status": st,
            "gpu": [{"index": 0, "util": 0, "mem_used": 10, "mem_total": 81920}],
            "processes": [], "gpu_idle_since": None, "time_left_secs": left,
            "polled_at": utc()}


def task(i, **k):
    t = {"id": i, "cmd": "true", "workdir": ".", "name": k.get("name", "t"),
         "state": k.get("state", "pending"), "priority": k.get("priority", 0),
         "submitted_at": k.get("sub", loc()), "started_at": k.get("st"),
         "finished_at": None, "slurm_jobid": k.get("jid"), "node": k.get("node"),
         "srun_pid": None, "exit_code": None, "need_mb": k.get("need_mb", 0),
         "est_runtime_secs": k.get("est"), "est_source": None, "requeue_count": 0,
         "checkpoint_warning": False, "node_time_left_secs": None,
         "duration_secs": None, "queued_secs": None,
         "log": os.path.join(hs.LOG_DIR, f"task-{i}.log")}
    if k.get("dispatched"):
        t["dispatched_at"] = k["dispatched"]
    return t


def stale_hb(tid, age=400):
    p = os.path.join(hs.HEARTBEAT_DIR, str(tid))
    open(p, "w").write("x")
    o = time.time() - age
    os.utime(p, (o, o))


print("== unit: duration parsing / percentile ==")
chk("parse 2h", hq.parse_duration("2h") == 7200)
chk("parse 1-12:00:00", hq.parse_duration("1-12:00:00") == 129600)
chk("parse 90m", hq.parse_duration("90m") == 5400)
chk("parse bad -> None", hq.parse_duration("xyz") is None)
chk("p90 of 100/200/300", hq._percentile([100, 200, 300], 90) == 280)

print("== unit: scheduler time helpers ==")
chk("db_time_left unlimited", hs.db_time_left({"time_left_secs": -1}, utc()) == -1)
chk("db_time_left missing -> None", hs.db_time_left({}, utc()) is None)
chk("db_time_left fresh ~3600", 3590 <= hs.db_time_left({"time_left_secs": 3600}, utc()) <= 3600)
chk("duration_secs", hs._duration_secs(
    {"started_at": "2026-06-07T10:00:00", "finished_at": "2026-06-07T11:30:00"}) == 5400)
chk("fmt_dur", hs._fmt_dur(5400) == "1h30m" and hs._fmt_dur(-1) == "unlimited")

print("== unit: multi-GPU aggregation (Phase 1) ==")
chk("db_gpu_mem takes max across GPUs",
    hs.db_gpu_mem({"gpu": [{"mem_used": 10, "mem_total": 81920},
                           {"mem_used": 40000, "mem_total": 81920}]}) == (40000, 81920))
chk("_max_gpu picks the busiest GPU (util,used,total)",
    hs._max_gpu(["0, 10, 81920", "85, 40000, 81920"]) == (85, 40000, 81920))
chk("_max_gpu single GPU unchanged", hs._max_gpu(["0, 10, 81920"]) == (0, 10, 81920))
chk("_max_gpu no parseable line -> None", hs._max_gpu(["", "garbage"]) is None)

print("== unit: _pid_alive (zombie-aware liveness) ==")
import subprocess as _sp
chk("live self is alive", hs._pid_alive(os.getpid()) is True)
chk("nonexistent pid is dead", hs._pid_alive(2**31 - 1) is False)
chk("None is dead", hs._pid_alive(None) is False)
_z = _sp.Popen(["true"]); time.sleep(0.3)   # exited but unreaped → <defunct> zombie
chk("zombie child treated as DEAD (not alive)", hs._pid_alive(_z.pid) is False)
_z.wait()

print("== event log: record / done_runs / compaction ==")
open(ev.EVENTS_FILE, "w").close()
ev.record("finish", task=1, name="bench", state="done", run_secs=120, queued_secs=2)
ev.record("finish", task=2, name="bench", state="failed", run_secs=9)  # not a 'done'
chk("done_runs counts only done", ev.done_runs() == {1: ("bench", 120)})
_omax, _okeep = ev.EVENTS_MAX_BYTES, ev.EVENTS_KEEP_LINES
ev.EVENTS_MAX_BYTES, ev.EVENTS_KEEP_LINES = 1500, 8
for i in range(300):
    ev.record("finish", task=1000 + i, name="z", state="done", run_secs=i + 1)
nlines = len(open(ev.EVENTS_FILE).read().splitlines())
chk("compaction keeps log bounded", nlines < 80)
chk("compaction retains newest", json.loads(open(ev.EVENTS_FILE).read().splitlines()[-1])["task"] == 1299)
ev.EVENTS_MAX_BYTES, ev.EVENTS_KEEP_LINES = _omax, _okeep
open(ev.EVENTS_FILE, "w").close()

print("== integration: dispatch -> done + events ==")
wdb({"700": node("nodeX", 72000)})
wq({"1": task(1, name="run", est=300)})
hs.run_one_cycle()
time.sleep(0.6)
hs.run_one_cycle()
t = rq()["1"]
chk("dispatched then done", t["state"] == "done")
chk("duration recorded", isinstance(t.get("duration_secs"), int))
evs = list(ev.iter_events())
chk("dispatch+finish events emitted",
    any(e["event"] == "dispatch" for e in evs) and any(e["event"] == "finish" for e in evs))

print("== integration: walltime gate (reject vs pass) ==")
wdb({"700": node("nodeX", 1200)})            # 20 min left
wq({"2": task(2, name="long", est=3600)})    # needs 1h
hs.run_one_cycle()
chk("est > walltime -> insufficient_walltime",
    rq()["2"].get("pending_reason") == "insufficient_walltime")
wdb({"700": node("nodeX", 72000)})           # 20h left
wq({"3": task(3, name="short", est=600)})    # needs 10m
hs.run_one_cycle()
time.sleep(0.4)
chk("est < walltime -> dispatches", rq()["3"]["state"] in ("running", "done"))

print("== integration: infra reclaim vs self-crash ==")
open(ev.EVENTS_FILE, "w").close()
wdb({})                                       # no live node; job 2001 gone
open(os.path.join(hs.LOG_DIR, "task-10.log"), "w").write("x")
stale_hb(10)
wq({"10": task(10, name="r", state="running", jid="2001", node="dead",
               st=loc(-450), sub=loc(-500), dispatched=loc(-450))})
hs.run_one_cycle()
t = rq()["10"]
chk("hold-job gone -> requeue + checkpoint_warning",
    t["state"] == "pending" and t["requeue_count"] == 1 and t["checkpoint_warning"])
chk("infra_failure requeue event",
    any(e["event"] == "requeue" and e.get("reason") == "infra_failure" for e in ev.iter_events()))
wdb({"700": node("nodeX", 72000)})
open(os.path.join(hs.LOG_DIR, "task-11.log"), "w").write("x")
stale_hb(11)
wq({"11": task(11, name="c", state="running", jid="9001", node="live",
               st=loc(-450), sub=loc(-500), dispatched=loc(-450))})
hs.run_one_cycle()
chk("hold-job alive (self-crash) -> failed, no retry", rq()["11"]["state"] == "failed")

print("== integration: backward-compat (old schema = walltime-blind) ==")
wdb({"5001": {"node": "old", "partition": "gpu", "status": "idle",
              "gpu": [{"index": 0, "util": 0, "mem_used": 10, "mem_total": 81920}],
              "processes": [], "gpu_idle_since": None, "polled_at": utc()}})  # no time_left_secs
wq({"20": {"id": 20, "cmd": "true", "workdir": ".", "name": "old", "state": "pending",
           "priority": 0, "submitted_at": loc(), "started_at": None, "finished_at": None,
           "slurm_jobid": None, "node": None, "srun_pid": None, "exit_code": None,
           "log": os.path.join(hs.LOG_DIR, "task-20.log")}})  # task w/o any new fields
hs.run_one_cycle()
time.sleep(0.4)
chk("old task + old node DB dispatch cleanly", rq()["20"]["state"] in ("running", "done"))

print("== integration: multi-GPU node, one GPU dirty -> held (Phase 1) ==")
# Phase 1 treats a multi-GPU hold-job as ONE slot: it must NOT dispatch while ANY of its
# GPUs is dirty (db_gpu_mem takes the max). GPU0 clean, GPU1 has 40GB resident.
multi = node("multi", 72000)
multi["gpu"] = [{"index": 0, "util": 0,  "mem_used": 10,    "mem_total": 81920},
                {"index": 1, "util": 90, "mem_used": 40000, "mem_total": 81920}]
wdb({"700": multi})
wq({"30": task(30, name="m", est=300)})
hs.run_one_cycle()
chk("2-GPU node with one dirty GPU held as gpu_dirty (not dispatched)",
    rq()["30"]["state"] == "pending" and rq()["30"].get("pending_reason") == "gpu_dirty")

print("== regression #10: `warning` must be transient, never a terminal state ==")
# A hold job that finishes a task and goes quiet used to be pinned at `warning`
# forever (gpu_idle_since was carried over but never cleared), and get_candidates()
# skipped `warning` — so one completed task permanently evicted the node from the
# dispatch pool, surviving daemon restarts via node_monitor.json.
dbp_old, dbp_new = os.path.join(hs.HIVE_DIR, "dbp_old.json"), os.path.join(hs.HIVE_DIR, "dbp_new.json")


def run_dbpost(old_jobs, new_jobs):
    """Run hive-dbpost over (new, old) and return the post-processed jobs."""
    json.dump({"updated": utc(), "jobs": old_jobs}, open(dbp_old, "w"))
    json.dump({"updated": utc(), "jobs": new_jobs}, open(dbp_new, "w"))
    _sp.run([sys.executable, os.path.join(LIB, "hive-dbpost"), dbp_new, dbp_old], check=True)
    return json.load(open(dbp_new))["jobs"]


# Step 1: busy (40GB resident) → a fresh probe says idle. Grace timer arms.
was_busy = node("g1", 72000, st="busy")
was_busy["gpu"] = [{"index": 0, "util": 90, "mem_used": 40000, "mem_total": 81920}]
armed = run_dbpost({"700": was_busy}, {"700": node("g1", 72000)})["700"]
chk("busy->idle arms the grace timer (status=warning, gpu_idle_since set)",
    armed["status"] == "warning" and armed["gpu_idle_since"] is not None)

# Step 2: still idle, but the grace window has now expired → must become a TRUE idle
# with the timer cleared, so the next busy→idle transition can re-arm it.
armed["gpu_idle_since"] = time.time() - dbp.WARN_SECS - 1
expired = run_dbpost({"700": armed}, {"700": node("g1", 72000)})["700"]
chk("grace expiry -> status back to idle (was: pinned at warning forever)",
    expired["status"] == "idle")
chk("grace expiry -> gpu_idle_since cleared (timer can re-arm)",
    expired["gpu_idle_since"] is None)

# Step 3: a still-quiet node must not silently re-arm the timer on the next poll.
steady = run_dbpost({"700": expired}, {"700": node("g1", 72000)})["700"]
chk("idle node stays idle across polls (no re-arm loop)",
    steady["status"] == "idle" and steady["gpu_idle_since"] is None)

# Step 4: a node_monitor.json written by an OLDER hive (stuck at warning) must
# self-heal — the scheduler treats `warning` as uncertain, not busy, so it becomes a
# candidate with needs_verify and live_probe() decides.
stuck = node("g1", 72000, st="warning")
stuck["gpu_idle_since"] = time.time() - 40000
cands = hs.get_candidates({"700": stuck}, set(), set())
chk("legacy stuck `warning` node is a dispatch candidate again",
    [c[0] for c in cands] == ["700"])
chk("...and it carries needs_verify (live-probed before dispatch)",
    bool(cands) and cands[0][2] is True)
chk("a genuinely busy node is still excluded",
    hs.get_candidates({"700": node("g1", 72000, st="busy")}, set(), set()) == [])

# End to end: a warning node with a free GPU must actually place a pending task.
wdb({"700": stuck})
wq({"40": task(40, name="w", est=300)})
hs.run_one_cycle()
time.sleep(0.4)
chk("pending task dispatches onto a `warning` node with a free GPU (was: deadlock)",
    rq()["40"]["state"] in ("running", "done"))

print("== regression #8: per-task GPU visibility (default 1, multi-GPU opt-in) ==")
# A hold job may own several GPUs. Handing all of them to a task that never asked
# made HF Trainer auto-DataParallel and crash at step 0 (task 6862 landed on a
# gpu:2 hold job and saw device_count=2). Default is now 1; more is opt-in.
chk("#HIVE gpus=2 parsed from a .hive file", (lambda: (
    open(os.path.join(hs.HIVE_DIR, "g.hive"), "w").write(
        "#HIVE name=g\n#HIVE gpus=2\necho hi\n"),
    hq.parse_hive_file(os.path.join(hs.HIVE_DIR, "g.hive")).get("gpus"))[1])() == 2)
chk("a .hive file without gpus= leaves it unset (default applied at submit)", (lambda: (
    open(os.path.join(hs.HIVE_DIR, "n.hive"), "w").write("#HIVE name=n\necho hi\n"),
    "gpus" in hq.parse_hive_file(os.path.join(hs.HIVE_DIR, "n.hive")))[1])() is False)
chk("scheduler default matches the queue default (legacy tasks read the same)",
    hs.DEFAULT_TASK_GPUS == hq.DEFAULT_TASK_GPUS == 1)

# Placement gate: a 2-GPU task must not land on a 1-GPU hold job.
one_gpu = node("g1", 72000)                      # node() builds a single-GPU record
wdb({"700": one_gpu})
wq({"50": dict(task(50, name="two", est=300), gpus=2)})
hs.run_one_cycle()
chk("gpus=2 task held on a 1-GPU hold job (insufficient_gpus)",
    rq()["50"]["state"] == "pending" and rq()["50"].get("pending_reason") == "insufficient_gpus")

# Same task, a hold job that really owns 2 clean GPUs -> places.
two_gpu = node("g2", 72000)
two_gpu["gpu"] = [{"index": 0, "util": 0, "mem_used": 10, "mem_total": 81920},
                  {"index": 1, "util": 0, "mem_used": 10, "mem_total": 81920}]
wdb({"700": two_gpu})
wq({"51": dict(task(51, name="two", est=300), gpus=2)})
hs.run_one_cycle()
time.sleep(0.4)
chk("gpus=2 task dispatches onto a 2-GPU hold job",
    rq()["51"]["state"] in ("running", "done"))

# A task queued by an OLDER hive has no "gpus" key -> must still dispatch (default 1).
wdb({"700": node("g3", 72000)})
wq({"52": task(52, name="legacy", est=300)})     # task() emits no gpus key
hs.run_one_cycle()
time.sleep(0.4)
chk("legacy task without a gpus key still dispatches (default 1)",
    rq()["52"]["state"] in ("running", "done"))

# The wrapper must NARROW CUDA_VISIBLE_DEVICES, never widen or invent devices.
_src_task = dict(task(53, name="w"), gpus=1)
_src_task["log"] = os.path.join(hs.LOG_DIR, "task-53.log")
hs.dispatch_task(_src_task, "700", "g1")
time.sleep(0.3)
_log = open(os.path.join(hs.LOG_DIR, "task-53.log")).read()
chk("dispatch logs the task's actual GPU visibility",
    "=== gpus: requested 1" in _log and "hold job provided" in _log)

print("== integration: history_estimate from event log (P90) ==")
open(ev.EVENTS_FILE, "w").close()
for d in (600, 900, 1200):
    ev.record("finish", task=hash(d) % 9999, name="bench2", state="done", run_secs=d)
est, n = hq.history_estimate("bench2")
chk("auto estimate = P90 of real history",
    est == hq._percentile([600, 900, 1200], 90) and n == 3)

print(f"\nPYTHON SUITE: {P['pass']} passed, {P['fail']} failed")
sys.exit(1 if P["fail"] else 0)
