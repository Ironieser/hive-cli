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

print("== integration: history_estimate from event log (P90) ==")
open(ev.EVENTS_FILE, "w").close()
for d in (600, 900, 1200):
    ev.record("finish", task=hash(d) % 9999, name="bench2", state="done", run_secs=d)
est, n = hq.history_estimate("bench2")
chk("auto estimate = P90 of real history",
    est == hq._percentile([600, 900, 1200], 90) and n == 3)

print(f"\nPYTHON SUITE: {P['pass']} passed, {P['fail']} failed")
sys.exit(1 if P["fail"] else 0)
