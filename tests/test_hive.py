#!/usr/bin/env python3
"""Deterministic OFFLINE unit + integration tests for hive-cli.

Invoked by tests/run.sh, which sets up mock SLURM binaries on PATH and a shadow
HIVE_DIR in a temp dir — this never touches ~/.hive, a real cluster, or the running
daemons. Usage:  python3 tests/test_hive.py [<repo_root>]
"""
import datetime
import json
import os
import re
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
# Nothing here may start a real daemon: `hive list` / `hive wait` start the scheduler
# when it looks dead, and real SLURM is next on PATH after the mocks. The fake heartbeat
# from run.sh goes stale after 90 s, so the starter itself is disarmed.
_daemon_calls = []
_real_cmd_daemon = hq.cmd_daemon        # only ever called with `stop` and os.kill patched
hq.cmd_daemon = lambda a: _daemon_calls.append(getattr(a, "subcmd", None))
hs.HEALTH_ASYNC = False   # health probes inline, so one cycle = one verdict; the
                          # background path has its own test

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
    hs._probe_backoff.clear()      # a new pool: nothing learned about the old one applies
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

print("== integration: multi-GPU node, one GPU dirty (GPU slots) ==")
# A hold job with N cards is N slots. GPU0 clean, GPU1 has 40GB resident: a 1-GPU task
# takes GPU0 (it used to be held, the hold job being one slot judged by its worst
# card); a 2-GPU task cannot be placed.
multi = node("multi", 72000)
multi["gpu"] = [{"index": 0, "util": 0,  "mem_used": 10,    "mem_total": 81920},
                {"index": 1, "util": 90, "mem_used": 40000, "mem_total": 81920}]
os.environ["MOCK_LIVE_GPU"] = "0, 10, 81920\\n0, 40000, 81920"   # live probe sees GPU1 dirty (memory resident, no util)
wdb({"700": multi})
wq({"31": dict(task(31, name="m2", est=300), gpus=2)})
hs.run_one_cycle()
chk("2-GPU task on a hold job with one dirty card is held (gpu_dirty)",
    rq()["31"]["state"] == "pending" and rq()["31"].get("pending_reason") == "gpu_dirty")
wdb({"700": multi})
wq({"30": dict(task(30, name="m", est=300), cmd="echo cvd=$CUDA_VISIBLE_DEVICES")})
os.environ["CUDA_VISIBLE_DEVICES"] = "GPU-aaa,GPU-bbb"
hs.run_one_cycle(); time.sleep(0.5)
os.environ.pop("CUDA_VISIBLE_DEVICES")
chk("1-GPU task takes the clean card", rq()["30"]["state"] in ("running", "done")
    and rq()["30"].get("gpu_slots") == [0] and "cvd=GPU-aaa\n" in open(rq()["30"]["log"]).read())
os.environ.pop("MOCK_LIVE_GPU", None)
hs.run_one_cycle()

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
os.environ["MOCK_LIVE_GPU"] = "0, 10, 81920\\n0, 10, 81920"      # the probe lists both cards
hs.run_one_cycle()
time.sleep(0.4)
os.environ.pop("MOCK_LIVE_GPU")
chk("gpus=2 task dispatches onto a 2-GPU hold job",
    rq()["51"]["state"] in ("running", "done") and rq()["51"].get("gpu_slots") == [0, 1])

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

print("== regression: head-of-queue task must not consume candidates (feedback #24-#26) ==")
# A high-priority task that can't be placed anywhere (gpus=2 on a 1-GPU pool) used to
# pop every candidate off the shared list, so the task behind it saw an empty list and
# reported no_dispatchable_node while the pool sat idle.
wdb({"700": node("a", 72000), "701": node("b", 72000)})
wq({"60": dict(task(60, name="head", priority=10), gpus=2),
    "61": task(61, name="behind")})
hs.run_one_cycle()
time.sleep(0.4)
chk("unplaceable head task stays pending with its own reason",
    rq()["60"]["state"] == "pending" and rq()["60"].get("pending_reason") == "insufficient_gpus")
chk("task behind it still dispatches (candidates were returned)",
    rq()["61"]["state"] in ("running", "done"))
wdb({"700": node("a", 1200), "701": node("b", 1200)})            # 20 min left on both
wq({"62": dict(task(62, name="long", priority=10), est_runtime_secs=36000),
    "63": task(63, name="short")})
hs.run_one_cycle()
time.sleep(0.4)
chk("walltime-blocked head task -> insufficient_walltime",
    rq()["62"].get("pending_reason") == "insufficient_walltime")
chk("estimate-less task behind it dispatches", rq()["63"]["state"] in ("running", "done"))
# Node-level rejections still consume: a dirty node is skipped for everyone.
dirty = node("d", 72000); dirty["gpu"][0]["mem_used"] = 20000
wdb({"700": dirty})
wq({"64": task(64, name="x"), "65": task(65, name="y")})
os.environ["MOCK_LIVE_GPU"] = "0, 20000, 81920"
hs.run_one_cycle()
os.environ.pop("MOCK_LIVE_GPU", None)
chk("dirty node rejected for every task (node-level)",
    rq()["64"].get("pending_reason") == "gpu_dirty" and rq()["65"].get("pending_reason") in ("gpu_dirty", "no_dispatchable_node"))

stale_dirty = node("sd", 72000); stale_dirty["gpu"][0]["mem_used"] = 30000   # DB says dirty
wdb({"700": stale_dirty}); wq({"66": task(66, name="sd")})
hs.run_one_cycle(); time.sleep(0.4)                                         # live probe: clean
chk("every dispatch is live-verified: a stale-dirty DB no longer blocks a now-clean card",
    rq()["66"]["state"] in ("running", "done"))

print("== regression: timestamps are tz-safe (feedback #31) ==")
# The scheduler, poller and each agent's shell can run under different TZs. A task's
# `*_at` strings written by different processes therefore can't be subtracted; the
# `*_ts` epoch twins can. Simulate a started_at string 12h off from the epoch.
_t = task(70, name="tz", state="running")
_t["started_ts"] = time.time() - 90
_t["started_at"] = (datetime.datetime.now() - datetime.timedelta(hours=12)).strftime('%Y-%m-%dT%H:%M:%S')
chk("running ELAPSED uses started_ts, not the (foreign-TZ) started_at string",
    hq.task_elapsed(_t) in ("1m30s", "1m31s"))
_t["submitted_ts"] = _t["started_ts"] - 45
chk("queued_secs from epoch fields", hs._queued_secs(_t) == 45)
_t["finished_ts"] = _t["started_ts"] + 600
chk("duration_secs from epoch fields", hs._duration_secs(_t) == 600)
chk("legacy task without *_ts still parses its *_at strings",
    hs._duration_secs({"started_at": "2026-06-07T10:00:00", "finished_at": "2026-06-07T10:10:00"}) == 600)
chk("submit writes submitted_ts", isinstance(
    (lambda: (hq.cmd_submit(__import__("argparse").Namespace(
        cmd_or_file="true", workdir=None, priority=None, name="ts", need_mb=None, gpus=None,
        est_runtime=None)), max(rq().values(), key=lambda t: t["id"]))[1].get("submitted_ts"))(), float))
_ev = list(ev.iter_events())[-1]
chk("events carry an epoch `t` field", isinstance(_ev.get("t"), float))

print("== regression: cancel of a running task is executed by the scheduler (cross-node) ==")
open(ev.EVENTS_FILE, "w").close()
wdb({"700": node("c", 72000)})
_c = task(80, name="cx", state="running", jid="700", node="c", st=loc(-30), dispatched=loc(-30))
_c["started_ts"] = _c["dispatched_ts"] = time.time() - 30
_c["cancel_requested"] = loc()
open(os.path.join(hs.HEARTBEAT_DIR, "80"), "w").write("x")     # step "alive"
wq({"80": _c})
hs.run_one_cycle()
t = rq()["80"]
chk("cancel_requested -> cancelled by the scheduler cycle",
    t["state"] == "cancelled" and "cancel_requested" not in t and isinstance(t.get("finished_ts"), float))
chk("cancel event recorded", any(e["event"] == "cancel" and e["task"] == 80 for e in ev.iter_events()))
os.remove(os.path.join(hs.HEARTBEAT_DIR, "80"))

print("== regression: dispatch wrapper is shell-safe (task #9128) ==")
_wd = os.path.join(hs.HIVE_DIR, "work dir")
os.makedirs(_wd, exist_ok=True)
_q = dict(task(90, name="quote"), cmd="echo 'a (b); c' && echo \"d $HOME\"", workdir=_wd)
hs.dispatch_task(_q, "700", "g1")
time.sleep(0.5)
_log = open(os.path.join(hs.LOG_DIR, "task-90.log")).read()
_xf = os.path.join(hs.HEARTBEAT_DIR, "90.exit")
chk("header records the cmd verbatim (quotes, parens, semicolon)",
    "=== cmd: echo 'a (b); c' && echo \"d $HOME\" ===" in _log)
chk("workdir with a space is cd'd into and logged", f"=== workdir: {_wd} ===" in _log and "cannot cd" not in _log)
chk("the command itself ran (exit 0)", os.path.exists(_xf) and open(_xf).read().strip() == "0")
chk("its output landed in the log", "a (b); c" in _log.split("=== cmd:")[1])

print("== regression: hold_job_alive confirms with squeue even when the node DB is stale ==")
chk("jid listed in a stale DB but gone from squeue -> not alive",
    hs.hold_job_alive("2001", {"2001": node("stale", 100)}) is False)
chk("jid squeue reports running -> alive", hs.hold_job_alive("700", {}) is True)

print("== node health: self-maintained quarantine list (feedback #13/#15/#16/#20/#28/#29/#30) ==")
hh = SourceFileLoader("hive_health", os.path.join(LIB, "hive_health.py")).load_module()
def reset_health():
    try: os.remove(hh.HEALTH_FILE)
    except FileNotFoundError: pass
reset_health()
chk("CUDA probe parser: ok / fail / unknown",
    hh.parse_cuda_probe(["0, 10, 81920", "CUDA_PROBE ok"]) == ("ok", "")
    and hh.parse_cuda_probe(["CUDA_PROBE fail cuCtxCreate=999"]) == ("fail", "cuCtxCreate=999")
    and hh.parse_cuda_probe(["garbage"])[0] == "unknown")
_lp = os.path.join(hs.LOG_DIR, "sig.log")
open(_lp, "w").write("x\n" * 50 + "torch.AcceleratorError: CUDA error: CUDA-capable device(s) is/are busy or unavailable\n")
chk("log tail classifier finds the CUDA-init signature", hh.classify_log_tail(_lp) is not None)
open(_lp, "w").write("Traceback\nKeyError: 'foo'\n")
chk("...and ignores an ordinary crash", hh.classify_log_tail(_lp) is None)

# (1) agent report -> quarantined -> scheduler never picks its hold jobs
data = hh.load(); hh.quarantine(data, "badnode", "agent says CUDA init dies", "agent", reporter="t"); hh.save(data)
wdb({"700": node("badnode", 72000), "701": node("goodnode", 72000)})
chk("quarantined node's hold job is not a candidate",
    [c[0] for c in hs.get_candidates({"700": node("badnode", 72000), "701": node("goodnode", 72000)},
                                     set(), set(), hh.quarantined_nodes())] == ["701"])
wq({"100": task(100, name="q")})
hs.run_one_cycle(); time.sleep(0.4)
chk("task dispatches to the healthy node instead", rq()["100"].get("node") == "goodnode")
wdb({"700": node("badnode", 72000)})
wq({"101": task(101, name="q2")})
hs.run_one_cycle()
chk("only quarantined nodes left -> pending_reason node_quarantined",
    rq()["101"].get("pending_reason") == "node_quarantined")

# (2) periodic check: after HEALTH_CHECK_SECS, HEALTH_OK_STREAK healthy probes release it
r0 = hh.load()["nodes"]["badnode"]
chk("a freshly reported node is probed in the very next cycle", r0.get("last_result") == "ok")
data = hh.load(); rec = data["nodes"]["badnode"]
rec["last_check"] = 0; rec["until"] = 0; rec["ok_streak"] = 0; hh.save(data)
hs.run_one_cycle()                                   # probe #1 (stubbed ok)
r1 = hh.load()["nodes"]["badnode"]
chk("first healthy probe recorded, still quarantined (needs a streak)",
    r1["ok_streak"] == 1 and r1["state"] == "quarantined")
r1["last_check"] = 0; d = hh.load(); d["nodes"]["badnode"] = r1; hh.save(d)
hs.run_one_cycle()                                   # probe #2
r2 = hh.load()["nodes"]["badnode"]
chk("second healthy probe releases the node automatically", r2["state"] == "ok")
chk("release event recorded", any(e["event"] == "release" and e.get("node") == "badnode" for e in ev.iter_events()))
time.sleep(0.4)
chk("released node receives the pending task", rq()["101"]["state"] in ("running", "done"))

# (3) a failed probe re-arms the hold instead of releasing
data = hh.load(); hh.quarantine(data, "badnode", "again", "agent"); data["nodes"]["badnode"]["last_check"] = 0; hh.save(data)
os.environ["HIVE_CUDA_PROBE_CMD"] = "echo 'CUDA_PROBE fail cuCtxCreate=999'"
wq({})
hs.run_one_cycle()
r3 = hh.load()["nodes"]["badnode"]
chk("failed periodic probe keeps quarantine and bumps strikes",
    r3["state"] == "quarantined" and r3["ok_streak"] == 0 and r3["strikes"] >= 1)

# (4) verify-before-dispatch: CUDA context fails on an IDLE-looking node -> quarantined at once
reset_health()
wdb({"700": node("evc43", 72000)})
wq({"110": task(110, name="v")})
hs.run_one_cycle()
chk("verify probe CUDA failure -> pending_reason cuda_unavailable_on_verify",
    rq()["110"].get("pending_reason") == "cuda_unavailable_on_verify")
chk("...and the node is quarantined immediately", "evc43" in hh.quarantined_nodes())
os.environ["HIVE_CUDA_PROBE_CMD"] = "echo 'CUDA_PROBE ok'"

# (5) two fast failures with a CUDA signature -> auto quarantine + the tripping task is requeued
reset_health()
wdb({"700": node("evc50", 72000)})
def fast_fail(tid):
    lp = os.path.join(hs.LOG_DIR, f"task-{tid}.log")
    open(lp, "w").write("loading...\nRuntimeError: CUDA unknown error - this may be due to an incorrectly set up environment\n")
    t = task(tid, name="ff", state="running", jid="700", node="evc50", st=loc(-20), dispatched=loc(-20))
    t["started_ts"] = t["dispatched_ts"] = time.time() - 20
    open(os.path.join(hs.HEARTBEAT_DIR, f"{tid}.exit"), "w").write("1")
    return t
wq({"120": fast_fail(120)})
hs.run_one_cycle()
h1 = hh.load()["nodes"].get("evc50", {})
chk("first fast CUDA-signature failure = 1 strike, not yet quarantined",
    rq()["120"]["state"] == "failed" and h1.get("strikes") == 1 and h1.get("state") != "quarantined")
wq({"121": fast_fail(121)})
hs.run_one_cycle()
h2 = hh.load()["nodes"]["evc50"]
chk("second strike quarantines the node", h2["state"] == "quarantined")
chk("the task that tripped it is requeued (pending, attempt 1, requeue event)",
    rq()["121"]["state"] == "pending" and rq()["121"].get("attempts") == 1
    and any(e["event"] == "requeue" and e.get("reason") == "node_quarantined" and e["task"] == 121
            for e in ev.iter_events()))
# a slow failure or a non-CUDA crash never counts
reset_health()
t = fast_fail(122); t["started_ts"] = t["dispatched_ts"] = time.time() - 900
wq({"122": t}); hs.run_one_cycle()
chk("a failure after FAST_FAIL_SECS is not a strike", not hh.load()["nodes"].get("evc50", {}).get("strikes"))
# success clears strikes
data = hh.load(); hh.strike(data, "evc50", "x"); hh.save(data)
t = task(123, name="okk", state="running", jid="700", node="evc50", st=loc(-20)); t["started_ts"] = time.time() - 20
open(os.path.join(hs.HEARTBEAT_DIR, "123.exit"), "w").write("0")
wq({"123": t}); hs.run_one_cycle()
chk("a successful task on the node clears its strikes", hh.load()["nodes"]["evc50"]["strikes"] == 0)
reset_health()

print("== unresponsive GPU: a wedged driver is a node fault, not a transient probe miss ==")
# 2026-09-26: three hold jobs sat 24 h with pending tasks because nvidia-smi never
# answered on their nodes and every probe read as "couldn't run" -> retried forever.
chk("gpu query parser: healthy",
    hh.parse_gpu_query(["GPU_PROBE granted=0", "0, 10, 81920", "GPU_PROBE rc=0"])
    == (["0, 10, 81920"], "0", None))
chk("gpu query parser: hung with a GPU granted -> gpu_unresponsive",
    hh.parse_gpu_query(["GPU_PROBE granted=0", "GPU_PROBE hung"])[2] == "gpu_unresponsive")
chk("gpu query parser: answered with no device -> no_gpu_devices",
    hh.parse_gpu_query(["GPU_PROBE granted=0", "No devices were found", "GPU_PROBE rc=6"])[2]
    == "no_gpu_devices")
chk("gpu query parser: CPU-only hold job (nothing granted) is never a fault",
    hh.parse_gpu_query(["GPU_PROBE granted=", "No devices were found", "GPU_PROBE rc=6"])[2] is None
    and hh.parse_gpu_query(["GPU_PROBE granted=", "GPU_PROBE hung"])[2] is None)
chk("gpu query parser: step never ran -> no verdict", hh.parse_gpu_query([]) == ([], None, None))

def reset_sched_state():
    getattr(hs, "_reserved", {}).clear()
    hs._probe_backoff.clear(); hs._reject_logged.clear()
    getattr(hs, "_freed_at", {}).clear(); getattr(hs, "_task_skip", {}).clear()
    hs._starve_streak = 0; hs._starve_threshold = hs.STARVATION_CYCLES
reset_health(); reset_sched_state()
os.environ.update({"MOCK_GPU_HANG": "4", "HIVE_GPU_QUERY_DEADLINE": "1", "CUDA_VISIBLE_DEVICES": "0"})
_t0 = time.time(); _p = hs.live_probe("700"); _dt = time.time() - _t0
chk("live_probe reports the fault", _p == {"ok": False, "fault": "gpu_unresponsive"})
chk("...at the deadline, without waiting for the stuck step", _dt < 3.5)
wdb({"700": node("wedged", 72000), "701": node("wedged", 72000)})
wq({"130": task(130, name="w1"), "131": task(131, name="w2", sub=loc(1))})
hs.run_one_cycle()
chk("both hold jobs of the node struck -> node quarantined",
    hh.load()["nodes"]["wedged"]["state"] == "quarantined")
chk("every pending task carries the real reason, not no_dispatchable_node",
    [rq()[k].get("pending_reason") for k in ("130", "131")] == ["gpu_unresponsive"] * 2)
chk("quarantine event recorded",
    any(e["event"] == "quarantine" and e.get("reason") == "gpu_unresponsive" for e in ev.iter_events()))
os.environ.pop("MOCK_GPU_HANG")
reset_health(); reset_sched_state()
os.environ["MOCK_GPU_NONE"] = "1"
chk("GPU granted but none listed -> no_gpu_devices", hs.live_probe("700").get("fault") == "no_gpu_devices")
os.environ.pop("MOCK_GPU_NONE"); os.environ.pop("CUDA_VISIBLE_DEVICES"); os.environ.pop("HIVE_GPU_QUERY_DEADLINE")
chk("healthy node still verifies clean", hs.live_probe("700").get("ok") is True)

_db = os.path.join(hs.HIVE_DIR, "cf_new.json"); _old = os.path.join(hs.HIVE_DIR, "cf_old.json")
def _carry(detail):
    json.dump({"jobs": {"700": node("n", 100)}}, open(_old, "w"))
    json.dump({"jobs": {"700": dict(node("n", 100, st="probe_failed"), gpu=[], probe_detail=detail)}},
              open(_db, "w"))
    sys.argv = ["hive-dbpost", _db, _old]; dbp.main()
    return json.load(open(_db))["jobs"]["700"]["status"]
_argv = sys.argv
chk("dbpost still carries a transient probe miss forward", _carry("srun_failed") == "idle")
chk("dbpost does NOT carry an unresponsive-GPU verdict forward", _carry("gpu_unresponsive") == "probe_failed")
sys.argv = _argv

# A node that works but crawls (evc45: nvidia-smi 24 s, CUDA context 184 s).
reset_health(); reset_sched_state()
_cmd = os.environ.pop("HIVE_CUDA_PROBE_CMD")
_bin = os.path.join(hs.HIVE_DIR, "slowpy"); open(_bin, "w").write("#!/bin/bash\nsleep 4\necho CUDA_PROBE ok\n"); os.chmod(_bin, 0o755)
_exe, sys.executable = sys.executable, _bin
os.environ["HIVE_CUDA_PROBE_DEADLINE"] = "1"
_t0 = time.time(); _p = hs.live_probe("700"); _dt = time.time() - _t0
chk("CUDA context not created by the deadline -> fail cuda_init_slow",
    _p.get("ok") is True and _p.get("cuda") == "fail" and _p.get("cuda_detail") == "cuda_init_slow")
chk("...reported at the deadline, not after the slow init finished", _dt < 3.5)
os.environ["HIVE_CUDA_PROBE_DEADLINE"] = "20"
chk("a context created in time still reads ok", hs.live_probe("700").get("cuda") == "ok")
sys.executable = _exe; os.environ.pop("HIVE_CUDA_PROBE_DEADLINE"); os.environ["HIVE_CUDA_PROBE_CMD"] = _cmd

print("== probe backoff / pending reasons / starvation backoff ==")
reset_health(); reset_sched_state()
_real_probe, _calls = hs.live_probe, []
hs.live_probe = lambda jid, **kw: (_calls.append(jid), {"ok": False})[1]   # srun cannot run
wdb({"700": node("silent", 72000)})
wq({"140": task(140, name="b1"), "141": task(141, name="b2", sub=loc(1))})
hs.run_one_cycle(); hs.run_one_cycle()
chk("a silent hold job is probed once, then backed off", _calls == ["700"])
chk("unanswered probes never strike the node", not hh.load()["nodes"].get("silent", {}).get("strikes"))
chk("tasks behind the first keep the node-level reason",
    [rq()[k].get("pending_reason") for k in ("140", "141")] == ["probe_unverifiable"] * 2)
hs._probe_backoff["700"] = (1, time.time() - 1, "probe_unverifiable", False)   # backoff elapsed
hs.run_one_cycle()
chk("...and probed again once the backoff elapsed", _calls == ["700", "700"])
chk("backoff grows with consecutive failures", hs._probe_backoff["700"][0] == 2
    and hs._probe_backoff["700"][1] - time.time() > hs.PROBE_BACKOFF_BASE)
hs.live_probe = _real_probe
reset_sched_state()
wdb({})
wq({"150": task(150, name="e")})
_polls = []
_real_repoll = hs.request_repoll
hs.request_repoll = lambda reason: _polls.append(hs._starve_streak)
for _ in range(3 + 6 + 10 + 10):
    hs.run_one_cycle()
chk("no hold job at all -> pending_reason pool_empty", rq()["150"].get("pending_reason") == "pool_empty")
chk("starvation re-poll backs off 3 -> 6 -> 10 -> 10 cycles", _polls == [3, 6, 10, 10])
wdb({"700": node("nodeX", 72000)})
hs.run_one_cycle(); time.sleep(0.4)
chk("a dispatch resets the starvation backoff",
    rq()["150"]["state"] in ("running", "done") and hs._starve_threshold == hs.STARVATION_CYCLES)
hs.request_repoll = _real_repoll
reset_health(); reset_sched_state()

print("== health monitor: recovery of a quarantined node that has no hold job ==")
# `pool add` keeps hold jobs off quarantined nodes, so the through-a-hold-job check
# never runs there again; without this the node stayed quarantined for good.
def mock(name, val=None):
    p = os.path.join(hs.HIVE_DIR, name)
    if val is None:
        try: os.remove(p)
        except FileNotFoundError: pass
    else:
        open(p, "w").write(val)
def quarantine_old(n, age=7200):
    reset_health(); reset_sched_state()
    d = hh.load(); hh.quarantine(d, n, "wedged", "verify")
    d["nodes"][n].update(since=time.time() - age, until=time.time() - age + 3600)
    hh.save(d)
def hrec(n):
    return hh.load()["nodes"][n]
for m in ("mock_boot", "mock_node_state", "mock_canary_state", "mock_sbatch.log", "mock_scancel.log"):
    mock(m)
os.environ["HIVE_CANARY_GAP"] = "0"
wdb({}); wq({})
chk("node_info parses scontrol", hh.node_info("evcX")["Partitions"] == ["gpu", "preemptable"]
    and hh.node_info("evcX")["State"] == "IDLE" and hh.node_info("evcX")["BootTime"] is not None)

# (1) reboot after the quarantine -> released, no job submitted
quarantine_old("evcR")
mock("mock_boot", time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - 600)))
hs.run_one_cycle()
chk("node rebooted after quarantine -> released", hrec("evcR")["state"] == "ok"
    and "rebooted" in hrec("evcR").get("release_reason", ""))
chk("...without submitting a canary", not os.path.exists(os.path.join(hs.HIVE_DIR, "mock_sbatch.log")))
chk("release event recorded", any(e["event"] == "release" and e.get("node") == "evcR" for e in ev.iter_events()))
mock("mock_boot")

# (2) healthy canary: two good probes in one job -> released
quarantine_old("evcC")
mock("mock_canary_state", "RUNNING")
hs.run_one_cycle()
r = hrec("evcC")
_sb = open(os.path.join(hs.HIVE_DIR, "mock_sbatch.log")).read()
chk("canary submitted, pinned to the node, in its partition",
    r.get("canary", {}).get("jobid") == "4242" and "--nodelist=evcC" in _sb
    and "--partition=gpu" in _sb and "--job-name=hive_canary" in _sb)
d = hh.load(); d["nodes"]["evcC"]["last_check"] = 0; hh.save(d)
hs.run_one_cycle()
chk("while the canary runs: still quarantined, no second canary",
    hrec("evcC")["state"] == "quarantined" and hrec("evcC")["last_result"] == "canary_running"
    and open(os.path.join(hs.HIVE_DIR, "mock_sbatch.log")).read().count("hive_canary") == 1)
mock("mock_canary_state"); mock("mock_canary_purged", "1")   # finished AND purged from squeue
d = hh.load(); d["nodes"]["evcC"]["last_check"] = 0; hh.save(d)
hs.run_one_cycle()
chk("canary came back healthy twice -> released (squeue: Invalid job id)", hrec("evcC")["state"] == "ok")
mock("mock_canary_purged")

# (3) canary on a node that is still broken -> quarantine re-armed, next one in 6 h
quarantine_old("evcB"); mock("mock_sbatch.log")
os.environ["HIVE_CUDA_PROBE_CMD"] = "echo 'CUDA_PROBE fail cuCtxCreate=999'"
hs.run_one_cycle()
os.environ["HIVE_CUDA_PROBE_CMD"] = "echo 'CUDA_PROBE ok'"
d = hh.load(); d["nodes"]["evcB"]["last_check"] = 0; hh.save(d)
hs.run_one_cycle()
r = hrec("evcB")
chk("failing canary keeps the node quarantined and extends the hold",
    r["state"] == "quarantined" and "canary" not in r and r["until"] > time.time() + 3000
    and r["last_result"].startswith("fail: canary"))
d = hh.load(); d["nodes"]["evcB"]["last_check"] = 0; hh.save(d)
hs.run_one_cycle()
chk("no new canary before CANARY_INTERVAL_SECS",
    open(os.path.join(hs.HIVE_DIR, "mock_sbatch.log")).read().count("hive_canary") == 1)
d = hh.load(); d["nodes"]["evcB"].update(last_check=0, last_canary=time.time() - hh.CANARY_INTERVAL_SECS - 1); hh.save(d)
hs.run_one_cycle()
chk("...and a new one after it", open(os.path.join(hs.HIVE_DIR, "mock_sbatch.log")).read().count("hive_canary") == 2)

# (4) canary stuck in the SLURM queue -> cancelled after CANARY_MAX_WAIT_SECS
mock("mock_canary_state", "PENDING")
d = hh.load(); d["nodes"]["evcB"]["last_check"] = 0
d["nodes"]["evcB"]["canary"]["submitted"] = time.time() - hh.CANARY_MAX_WAIT_SECS - 1; hh.save(d)
hs.run_one_cycle()
chk("canary that never started is cancelled", "canary" not in hrec("evcB")
    and "4242" in open(os.path.join(hs.HIVE_DIR, "mock_scancel.log")).read())
mock("mock_canary_state")

# (5) no canary onto a node SLURM has drained, or when switched off
quarantine_old("evcD"); mock("mock_sbatch.log"); mock("mock_node_state", "IDLE+DRAIN")
hs.run_one_cycle()
chk("drained node: no canary, state noted", not os.path.exists(os.path.join(hs.HIVE_DIR, "mock_sbatch.log"))
    and hrec("evcD")["last_result"] == "node_idle+drain")
mock("mock_node_state")
os.environ["HIVE_HEALTH_CANARY"] = "0"
d = hh.load(); d["nodes"]["evcD"]["last_check"] = 0; hh.save(d)
hs.run_one_cycle()
chk("HIVE_HEALTH_CANARY=0 -> no canary", not os.path.exists(os.path.join(hs.HIVE_DIR, "mock_sbatch.log")))
os.environ.pop("HIVE_HEALTH_CANARY"); os.environ.pop("HIVE_CANARY_GAP")
reset_health(); reset_sched_state()

print("== task timeout + notification hook ==")
reset_health(); reset_sched_state()
open(ev.EVENTS_FILE, "w").close()
_nf = os.path.join(hs.HIVE_DIR, "notified.txt")
_hook = f'echo "$HIVE_TASK_ID $HIVE_TASK_EVENT $HIVE_TASK_STATE $HIVE_TASK_EXIT_CODE $HIVE_TASK_FAIL_REASON $HIVE_TASK_NAME" >> {_nf}'
def notified():
    for _ in range(40):
        if os.path.exists(_nf):
            time.sleep(0.2); return open(_nf).read().splitlines()
        time.sleep(0.1)
    return []
def clear_notified():
    try: os.remove(_nf)
    except FileNotFoundError: pass
# (1) over the limit -> killed, failed/124, not retried, node not struck
_sl = _sp.Popen(["sleep", "60"], start_new_session=True)
t = dict(task(170, name="slow", state="running", jid="700", node="nodeX", st=loc(-120)),
         timeout_secs=60, notify=_hook, srun_pid=_sl.pid)
t["started_ts"] = t["dispatched_ts"] = time.time() - 120
open(os.path.join(hs.HEARTBEAT_DIR, "170"), "w").write("x")          # it is alive
open(t["log"], "w").write("running\n")
wdb({"700": node("nodeX", 72000)}); wq({"170": t})
hs.run_one_cycle()
t = rq()["170"]
chk("over --timeout -> failed, exit 124, fail_reason timeout",
    (t["state"], t["exit_code"], t.get("fail_reason")) == ("failed", 124, "timeout"))
time.sleep(0.3)
chk("the step was signalled", _sl.poll() is not None)
_sl.poll() is None and _sl.kill()
chk("log says why", "exceeded its --timeout" in open(t["log"]).read())
chk("finish event carries reason=timeout",
    any(e["event"] == "finish" and e.get("reason") == "timeout" for e in ev.iter_events()))
chk("a timeout is not a strike against the node", not hh.load()["nodes"].get("nodeX", {}).get("strikes"))
chk("hook ran with the task in its environment", notified() == ["170 finish failed 124 timeout slow"])
# (2) under the limit -> untouched
clear_notified()
t = dict(task(171, name="fine", state="running", jid="700", node="nodeX", st=loc(-10)), timeout_secs=60, notify=_hook)
t["started_ts"] = t["dispatched_ts"] = time.time() - 10
open(os.path.join(hs.HEARTBEAT_DIR, "171"), "w").write("x")
wq({"171": t}); hs.run_one_cycle()
chk("under the limit -> still running, no hook", rq()["171"]["state"] == "running" and not os.path.exists(_nf))
# (3) normal finish fires the hook; a task without one fires nothing
open(os.path.join(hs.HEARTBEAT_DIR, "171.exit"), "w").write("0")
hs.run_one_cycle()
chk("hook on a normal finish", notified() == ["171 finish done 0  fine"])
clear_notified()
t = task(172, name="quiet", state="running", jid="700", node="nodeX", st=loc(-10)); t["started_ts"] = time.time() - 10
open(os.path.join(hs.HEARTBEAT_DIR, "172.exit"), "w").write("3")
wq({"172": t}); hs.run_one_cycle(); time.sleep(0.5)
chk("no hook configured -> nothing runs", rq()["172"]["state"] == "failed" and not os.path.exists(_nf))
# (4) infra requeue -> event `requeue`, state pending
wdb({})
stale_hb(173); open(os.path.join(hs.LOG_DIR, "task-173.log"), "w").write("x")
t = dict(task(173, name="lost", state="running", jid="2001", node="gone", st=loc(-450), sub=loc(-500),
              dispatched=loc(-450)), notify=_hook)
wq({"173": t}); hs.run_one_cycle()
chk("hook on infra requeue says requeue/pending", notified() == ["173 requeue pending   lost"])
clear_notified()
# (5) a broken hook never affects the task
t = dict(task(174, name="badhook", state="running", jid="700", node="nodeX", st=loc(-10)), notify="exit 7; }{ syntax")
t["started_ts"] = time.time() - 10
open(os.path.join(hs.HEARTBEAT_DIR, "174.exit"), "w").write("0")
wdb({"700": node("nodeX", 72000)}); wq({"174": t}); hs.run_one_cycle()
chk("failing hook leaves the task done", rq()["174"]["state"] == "done")
# (6) submit side
os.environ["HIVE_NOTIFY"] = "echo env-hook"
wq({})
_ns = _ap_early = __import__("argparse").Namespace(cmd_or_file="true", workdir=None, priority=None, name="s", owner=None,
        need_mb=None, gpus=None, est_runtime=None, exclude=None, timeout="90m", notify=None)
import io as _io, contextlib as _cl
with _cl.redirect_stdout(_io.StringIO()):
    hq.cmd_submit(_ns)
t = max(rq().values(), key=lambda t: t["id"])
chk("--timeout parsed to seconds; $HIVE_NOTIFY is the default hook",
    t["timeout_secs"] == 5400 and t["notify"] == "echo env-hook")
os.environ.pop("HIVE_NOTIFY")
open(os.path.join(hs.HIVE_DIR, "t.hive"), "w").write("#HIVE timeout=2h\n#HIVE notify=echo hi\necho x\n")
_pf = hq.parse_hive_file(os.path.join(hs.HIVE_DIR, "t.hive"))
chk("#HIVE timeout= / notify= parsed", _pf.get("timeout") == "2h" and _pf.get("notify") == "echo hi")
reset_health(); reset_sched_state()

print("== health checks run in the background; finish time is the command's ==")
reset_health(); reset_sched_state()
hs.HEALTH_ASYNC = True
_real_hp = hs.health_probe
def _slow_probe(jid):
    time.sleep(1.5); return ("ok", "")
hs.health_probe = _slow_probe
d = hh.load(); hh.quarantine(d, "slowq", "wedged", "verify")
d["nodes"]["slowq"].update(since=time.time() - 7200, until=time.time() - 3600, last_check=0); hh.save(d)
wdb({"700": node("slowq", 72000), "701": node("fastnode", 72000)})
wq({"190": dict(task(190, name="prompt"), cmd="true")})
_t0 = time.time(); hs.run_one_cycle(); _dt = time.time() - _t0
chk("a slow health probe does not hold up the cycle", _dt < 1.2)
chk("...and the task is dispatched in that same cycle", rq()["190"].get("node") == "fastnode")
chk("probe is in flight, nothing recorded yet", "slowq" in hs._health_inflight
    and hh.load()["nodes"]["slowq"].get("ok_streak", 0) == 0)
hs.run_one_cycle()
chk("no second probe while one is in flight", len(hs._health_inflight) == 1)
time.sleep(1.8); hs.run_one_cycle()
chk("verdict applied on a later cycle", hh.load()["nodes"]["slowq"]["ok_streak"] == 1
    and "slowq" not in hs._health_inflight)
hs.health_probe = _real_hp; hs.HEALTH_ASYNC = False
t = task(191, name="late", state="running", jid="700", node="fastnode", st=loc(-100)); t["started_ts"] = time.time() - 100
_xf = os.path.join(hs.HEARTBEAT_DIR, "191.exit"); open(_xf, "w").write("0")
os.utime(_xf, (time.time() - 60, time.time() - 60))          # command ended a minute ago
wq({"191": t}); hs.run_one_cycle()
chk("duration ends when the command did, not when the cycle noticed",
    38 <= rq()["191"]["duration_secs"] <= 42)
reset_health(); reset_sched_state()

print("== GPU usage accounting: sampler -> task -> events -> stats / --need-mb auto ==")
reset_health(); reset_sched_state()
open(ev.EVENTS_FILE, "w").close()
wdb({"700": node("nodeX", 72000)})
wq({"180": dict(task(180, name="acct"), cmd="sleep 1.2", notify=f'echo "$HIVE_TASK_GPU_PEAK_MB" >> {_nf}')})
clear_notified()
hs.run_one_cycle()
for _ in range(40):
    if os.path.exists(os.path.join(hs.HEARTBEAT_DIR, "180.exit")): break
    time.sleep(0.1)
chk("wrapper wrote the usage file on the node",
    open(hs.usage_file(180)).read().split() == ["41234", "37", "37", "1"])
hs.run_one_cycle()
t = rq()["180"]
chk("peak / util recorded on the task",
    (t["state"], t.get("gpu_peak_mb"), t.get("gpu_avg_util"), t.get("gpu_max_util")) == ("done", 41234, 37, 37))
chk("usage file consumed", not os.path.exists(hs.usage_file(180)))
chk("finish event carries the usage",
    any(e["event"] == "finish" and e.get("gpu_peak_mb") == 41234 and e.get("gpu_avg_util") == 37
        for e in ev.iter_events()))
chk("hook sees HIVE_TASK_GPU_PEAK_MB", notified() == ["41234"])
clear_notified()
os.environ["MOCK_USAGE"] = "0, 10\\n90, 60000"          # 2-GPU hold job, task sees 1 card
wq({"181": dict(task(181, name="acct"), cmd="sleep 0.3")})
hs.run_one_cycle(); time.sleep(1.0); hs.run_one_cycle()
chk("only the cards the task can see are sampled (gpus=1 -> first line)",
    rq()["181"].get("gpu_peak_mb") == 10)
os.environ.pop("MOCK_USAGE")
t0 = dict(task(182, name="cpu"), cmd="sleep 0.3", gpus=0)
wq({"182": t0}); hs.run_one_cycle(); time.sleep(1.0); hs.run_one_cycle()
chk("gpus=0 task: nothing sampled, fields unset",
    rq()["182"]["state"] == "done" and "gpu_peak_mb" not in rq()["182"])
wq({})
for i, pk in enumerate((40000, 42000, 50000)):
    ev.record("finish", task=900 + i, name="big", state="done", run_secs=100, gpu_peak_mb=pk, gpu_avg_util=80)
chk("history_need_mb = P90 peak + 10 %, rounded up to 500 MiB",
    hq.history_need_mb("big") == (53500, 3) and hq.history_need_mb("nohistory") == (None, 0))
def _sub(**kw):
    ns = __import__("argparse").Namespace(cmd_or_file="true", workdir=None, priority=None, name=kw.get("name"),
            owner=None, need_mb=kw.get("need_mb"), gpus=None, est_runtime=None, exclude=None, timeout=None, notify=None)
    with _cl.redirect_stdout(_io.StringIO()):
        hq.cmd_submit(ns)
    return max(rq().values(), key=lambda t: t["id"])
chk("--need-mb auto resolves from history", _sub(name="big", need_mb="auto")["need_mb"] == 53500)
chk("--need-mb auto without history -> 0 (no constraint)", _sub(name="nohistory", need_mb="auto")["need_mb"] == 0)
chk("--need-mb 30000 still a plain number", _sub(name="x", need_mb="30000")["need_mb"] == 30000)
open(os.path.join(hs.HIVE_DIR, "n.hive"), "w").write("#HIVE name=big\n#HIVE need_mb=auto\necho x\n")
ns = __import__("argparse").Namespace(cmd_or_file=os.path.join(hs.HIVE_DIR, "n.hive"), workdir=None, priority=None,
        name=None, owner=None, need_mb=None, gpus=None, est_runtime=None, exclude=None, timeout=None, notify=None)
with _cl.redirect_stdout(_io.StringIO()):
    hq.cmd_submit(ns)
chk("#HIVE need_mb=auto", max(rq().values(), key=lambda t: t["id"])["need_mb"] == 53500)
_buf = _io.StringIO()
with _cl.redirect_stdout(_buf):
    hq.cmd_stats(__import__("argparse").Namespace(name="big"))
chk("hive stats shows GPU peak (P90) and mean util", "GPU-PEAK" in _buf.getvalue()
    and "47.3G" in _buf.getvalue() and "80%" in _buf.getvalue())
open(ev.EVENTS_FILE, "w").close(); wq({})
reset_health(); reset_sched_state()

print("== slow nodes: usable for long tasks, after every faster node ==")
reset_health(); reset_sched_state()
open(ev.EVENTS_FILE, "w").close()
chk("probe detail carries the init time", hh.cuda_secs("secs=146") == 146 and hh.cuda_secs("") is None)
chk("task_accepts_slow: explicit flag wins, else est >= 1h",
    hh.task_accepts_slow({"allow_slow": True}) and not hh.task_accepts_slow({"allow_slow": False, "est_runtime_secs": 99999})
    and hh.task_accepts_slow({"est_runtime_secs": 3600}) and not hh.task_accepts_slow({"est_runtime_secs": 3599})
    and not hh.task_accepts_slow({}))
# quarantined for cuda_init_slow; the periodic check finds the context IS created, slowly
d = hh.load(); hh.quarantine(d, "slown", "CUDA probe failed: cuda_init_slow", "verify")
d["nodes"]["slown"].update(since=time.time() - 7200, until=time.time() - 3600, last_check=0); hh.save(d)
_real_hp = hs.health_probe
hs.health_probe = lambda jid: ("ok", "secs=146")
wdb({"700": node("slown", 72000)}); wq({})
hs.run_one_cycle()
r = hh.load()["nodes"]["slown"]
chk("context created in 146s -> state slow, not quarantined",
    r["state"] == "slow" and r["slow_init_secs"] == 146 and hh.quarantined_nodes() == set())
chk("slow event recorded", any(e["event"] == "slow" and e.get("node") == "slown" for e in ev.iter_events()))
# placement
hs.health_probe = _real_hp
d = hh.load(); d["nodes"]["slown"]["last_check"] = time.time(); hh.save(d)
wdb({"700": node("slown", 72000)})
wq({"260": dict(task(260, name="quick"), cmd="true"),
    "261": dict(task(261, name="long", est=7200, sub=loc(1)), cmd="true")})
_probes = []
_real_lp = hs.live_probe
def _spy(jid, **kw):
    _probes.append(kw); return _real_lp(jid, **kw)
hs.live_probe = _spy
hs.run_one_cycle(); time.sleep(0.4)
q = rq()
chk("short task is held off the slow node (node_slow)",
    q["260"]["state"] == "pending" and q["260"].get("pending_reason") == "node_slow")
chk("long task (est 2h) takes it", q["261"].get("node") == "slown")
chk("verify on a slow node skips the CUDA probe", _probes and _probes[-1].get("skip_cuda") is True)
chk("its log warns about the slow start", "is a SLOW node" in open(q["261"]["log"]).read())
hs.live_probe = _real_lp
# --allow-slow / --no-slow
wdb({"700": node("slown", 72000)})
wq({"262": dict(task(262, name="optin"), cmd="true", allow_slow=True),
    "263": dict(task(263, name="optout", est=99999, sub=loc(1)), cmd="true", allow_slow=False)})
hs.run_one_cycle(); time.sleep(0.4)
chk("--allow-slow places a short task; --no-slow holds a long one",
    rq()["262"].get("node") == "slown" and rq()["263"]["state"] == "pending")
# fast node preferred even for a task that accepts slow
wdb({"700": node("slown", 72000), "701": node("fastn", 72000)})
wq({"264": dict(task(264, name="pick", est=7200), cmd="true")})
hs.run_one_cycle(); time.sleep(0.4)
chk("a fast node is used before a slow one", rq()["264"].get("node") == "fastn")
# partition preference
json.dump({"prefer_partitions": ["highgpu"]}, open(os.path.join(hs.HIVE_DIR, "pool_config.json"), "w"))
wdb({"700": dict(node("n_norm", 72000), partition="normal"), "701": dict(node("n_high", 72000), partition="highgpu")})
wq({"265": dict(task(265, name="pref"), cmd="true")})
hs.run_one_cycle(); time.sleep(0.4)
chk("prefer_partitions decides between equally good nodes", rq()["265"].get("node") == "n_high")
os.remove(os.path.join(hs.HIVE_DIR, "pool_config.json"))
# a slow node recovers, or gets worse
hs.health_probe = lambda jid: ("ok", "secs=3")
for _ in range(2):
    d = hh.load(); d["nodes"]["slown"]["last_check"] = 0; hh.save(d)
    wdb({"700": node("slown", 72000)}); wq({}); hs.run_one_cycle()
chk("two checks at normal speed -> released", hh.load()["nodes"]["slown"]["state"] == "ok")
d = hh.load(); hh.mark_slow(d, "slown", 150); d["nodes"]["slown"]["last_check"] = 0; hh.save(d)
hs.health_probe = lambda jid: ("fail", "gpu_unresponsive")
hs.run_one_cycle()
chk("a slow node that stops answering is quarantined", hh.load()["nodes"]["slown"]["state"] == "quarantined")
hs.health_probe = _real_hp
reset_health(); reset_sched_state(); open(ev.EVENTS_FILE, "w").close(); wq({})

print("== verify probes run outside queue.lock, in parallel ==")
reset_health(); reset_sched_state()
import fcntl as _fc
_lock_free, _real_lp2 = [], hs.live_probe
def _probe_checks_lock(jid, **kw):
    fd = open(hs.QUEUE_LOCK, "w")
    try:
        _fc.flock(fd, _fc.LOCK_EX | _fc.LOCK_NB); _lock_free.append(True); _fc.flock(fd, _fc.LOCK_UN)
    except OSError:
        _lock_free.append(False)
    fd.close(); time.sleep(1.0)
    return {"ok": True, "util": 0, "mem_used": 10, "mem_total": 81920, "cuda": "ok", "cuda_detail": ""}
hs.live_probe = _probe_checks_lock
wdb({str(700 + i): node(f"p{i}", 72000) for i in range(3)})
wq({str(280 + i): dict(task(280 + i, name=f"par{i}", sub=loc(i)), cmd="true") for i in range(3)})
_t0 = time.time(); hs.run_one_cycle(); _dt = time.time() - _t0
chk("queue.lock is free while nodes are probed", _lock_free == [True, True, True])
chk("three probes of 1s each take ~1s, not 3s", _dt < 2.2)
chk("all three tasks dispatched in that one cycle, one node each",
    sorted(t.get("node") for t in rq().values()) == ["p0", "p1", "p2"])
# a node rejected in pass 2 does not trigger more probes in the same cycle
_n = []
def _dirty_first(jid, **kw):
    _n.append(jid)
    used = 40000 if jid == "700" else 10
    return {"ok": True, "util": 0, "mem_used": used, "mem_total": 81920, "cuda": "ok", "cuda_detail": ""}
hs.live_probe = _dirty_first
time.sleep(0.4); hs.run_one_cycle()
wdb({"700": node("d0", 72000), "701": node("d1", 72000)})
wq({"285": dict(task(285, name="one"), cmd="true")})
hs.run_one_cycle()
chk("one task -> one probe; its node is dirty -> it waits for the next cycle",
    _n == ["700"] and rq()["285"]["state"] == "pending")
hs.run_one_cycle(); time.sleep(0.3)
chk("next cycle probes the other node and dispatches", rq()["285"].get("node") == "d1")
hs.live_probe = _real_lp2
reset_health(); reset_sched_state(); wq({})

print("== queue control: hold / unhold / priority ==")
reset_health(); reset_sched_state(); open(ev.EVENTS_FILE, "w").close()
import argparse as _ap2, io as _io2, contextlib as _cl2
def _run(fn, *a, **k):
    out = _io2.StringIO()
    try:
        with _cl2.redirect_stdout(out), _cl2.redirect_stderr(out):
            fn(*a, **k)
        return None, out.getvalue()
    except SystemExit as e:
        return e.code, out.getvalue()
wdb({"700": node("n1", 72000)})
wq({"270": dict(task(270, name="first"), cmd="sleep 1"),
    "271": dict(task(271, name="second", sub=loc(1)), cmd="true"),
    "272": dict(task(272, name="third", sub=loc(2)), cmd="true"),
    "273": dict(task(273, name="ran", state="done", sub=loc(3)), cmd="true")})
_run(hq.cmd_hold, _ap2.Namespace(id=[270, 273], array=None), True)
chk("hold marks a pending task, skips one that is not pending",
    rq()["270"].get("held") is True and not rq()["273"].get("held"))
hs.run_one_cycle(); time.sleep(0.4)
chk("a held task is passed over, the next one takes the node",
    rq()["270"]["state"] == "pending" and rq()["270"].get("pending_reason") == "held"
    and rq()["271"]["state"] in ("running", "done"))
_run(hq.cmd_priority, _ap2.Namespace(priority=50, id=[272], array=None))
_run(hq.cmd_hold, _ap2.Namespace(id=[270], array=None), False)
chk("unhold clears the flag; priority is stored", not rq()["270"].get("held") and rq()["272"]["priority"] == 50)
hs.run_one_cycle(); time.sleep(0.4)
chk("the reprioritised task now goes before the older one",
    rq()["272"]["state"] in ("running", "done") and rq()["270"]["state"] == "pending")
chk("hold on nothing pending exits 1", _run(hq.cmd_hold, _ap2.Namespace(id=[273], array=None), True)[0] == 1)
chk("events recorded", {"hold", "unhold", "priority"} <= {e["event"] for e in ev.iter_events()})
hs.run_one_cycle(); time.sleep(0.4); hs.run_one_cycle()
reset_health(); reset_sched_state(); open(ev.EVENTS_FILE, "w").close(); wq({})

print("== queued hold jobs learn about nodes quarantined after they were submitted ==")
reset_health(); reset_sched_state()
_upd = os.path.join(hs.HIVE_DIR, "mock_scontrol_update.log")
open(os.path.join(hs.HIVE_DIR, "mock_pending"), "w").write("1")
chk("only jobs logging to pool-logs/ count as hold jobs", hh.pending_hold_jobs() == {"5001": "evc[1-3]", "5002": ""})
def _no_excl_state():
    try: os.remove(hh.EXCL_FILE)
    except FileNotFoundError: pass
_no_excl_state()
chk("missing nodes are added, present ones are not repeated",
    hh.sync_pending_excludes({"evc2", "evc48"}) == {"5001": (["evc48"], []), "5002": (["evc2", "evc48"], [])})
_u = open(_upd).read()
chk("the job's own exclude list is kept", "JobId=5001 ExcNodeList=evc1,evc2,evc3,evc48" in _u
    and "JobId=5002 ExcNodeList=evc2,evc48" in _u and "5003" not in _u)
chk("what hive added is remembered per job",
    hh.hive_excludes() == {"5001": ["evc48"], "5002": ["evc2", "evc48"]})
chk("names that are not node names never reach SLURM",
    hh.sync_pending_excludes({"evc48", "evc50 --partition=debug", "ty po"}) is not None
    and "debug" not in open(_upd).read() and "ty po" not in open(_upd).read())
os.remove(_upd); _no_excl_state()
hs._excl_synced = None
d = hh.load(); hh.quarantine(d, "evc48", "wedged", "verify"); d["nodes"]["evc48"]["last_check"] = time.time(); hh.save(d)
wdb({}); wq({})
hs.run_one_cycle(); hs._excl_thread.join(5)
chk("the scheduler pushes the quarantine list in the background", "ExcNodeList=evc1,evc2,evc3,evc48" in open(_upd).read())
os.remove(_upd); hs.run_one_cycle()
(hs._excl_thread and hs._excl_thread.join(5))
chk("...once, not every cycle", not os.path.exists(_upd))
os.remove(os.path.join(hs.HIVE_DIR, "mock_pending"))
reset_health(); reset_sched_state()

print("== GPU slots: several tasks on one multi-GPU hold job ==")
reset_health(); reset_sched_state(); open(ev.EVENTS_FILE, "w").close()
def gpus(n, used=10, util=0):
    return [{"index": i, "util": util, "mem_used": used, "mem_total": 81920} for i in range(n)]
chk("pick_slots: first free clean cards", hs.pick_slots([(0, 10, 81920)] * 4, {1}, 2, 0) == ([0, 2], None, 3))
chk("pick_slots: a busy card is skipped, not fatal",
    hs.pick_slots([(90, 40000, 81920), (0, 10, 81920)], set(), 1, 0) == ([1], None, 1))
chk("pick_slots: nothing usable -> node-level reason, usable 0",
    hs.pick_slots([(90, 40000, 81920)], set(), 1, 0) == (None, "node_busy_on_verify", 0))
chk("pick_slots: clean but too little memory -> waiting_for_mem",
    hs.pick_slots([(0, 4000, 81920)], set(), 1, 80000) == (None, "waiting_for_mem", 1))
chk("pick_slots: gpus=0 needs no card", hs.pick_slots([(90, 40000, 81920)], set(), 0, 0)[0] == [])
four = dict(node("quad", 72000), gpu=gpus(4))
os.environ["MOCK_LIVE_GPU"] = "\\n".join(["0, 10, 81920"] * 4)
os.environ["CUDA_VISIBLE_DEVICES"] = "0,1,2,3"
wdb({"700": four})
wq({str(610 + i): dict(task(610 + i, name=f"s{i}", sub=loc(i)), cmd="echo cvd=$CUDA_VISIBLE_DEVICES; sleep 2")
    for i in range(5)})
_pr = []
_lp4 = hs.live_probe
def _count(jid, **kw):
    _pr.append(jid); return _lp4(jid, **kw)
hs.live_probe = _count
hs.run_one_cycle()
q = rq()
chk("four 1-GPU tasks run at once on ONE 4-GPU hold job, one card each",
    [q[str(610 + i)].get("gpu_slots") for i in range(4)] == [[0], [1], [2], [3]]
    and all(q[str(610 + i)]["state"] == "running" for i in range(4)))
chk("the fifth waits for a card", q["614"]["state"] == "pending"
    and q["614"].get("pending_reason") in ("no_dispatchable_node", "waiting_for_gpu"))
chk("one probe served all four placements", _pr == ["700"])
time.sleep(0.6)
chk("each task sees only its own card",
    ["cvd=%d\n" % i in open(q[str(610 + i)]["log"]).read() for i in range(4)] == [True] * 4)
# while they run the poller calls the hold job busy; one finishing frees ONE slot
busy4 = dict(node("quad", 72000, st="busy"), gpu=gpus(4, used=30000, util=80))
wdb({"700": busy4})
os.environ["MOCK_LIVE_GPU"] = "\\n".join(["80, 30000, 81920"] * 4)
hs.run_one_cycle()
chk("a full hold job is not a candidate", rq()["614"]["state"] == "pending")
open(os.path.join(hs.HEARTBEAT_DIR, "611.exit"), "w").write("0")      # task on slot 1 ends
os.environ["MOCK_LIVE_GPU"] = "80, 30000, 81920\\n0, 10, 81920\\n80, 30000, 81920\\n80, 30000, 81920"
hs.run_one_cycle()
q = rq()
chk("the freed slot goes to the waiting task; the others keep running",
    q["614"].get("gpu_slots") == [1] and q["614"]["state"] == "running"
    and all(q[k]["state"] == "running" for k in ("610", "612", "613")))
# a task from an older scheduler (no gpu_slots) holds the whole hold job
wdb({"700": four})
os.environ["MOCK_LIVE_GPU"] = "\\n".join(["0, 10, 81920"] * 4)
old_t = task(620, name="legacy", state="running", jid="700", node="quad", st=loc(-30)); old_t["started_ts"] = time.time() - 30
open(os.path.join(hs.HEARTBEAT_DIR, "620"), "w").write("x")
wq({"620": old_t, "621": dict(task(621, name="new"), cmd="true")})
hs.run_one_cycle()
chk("a running task without gpu_slots holds every slot", rq()["621"]["state"] == "pending")
# 2-GPU task next to a 1-GPU task
wdb({"700": four})
wq({"630": dict(task(630, name="a"), cmd="sleep 1"), "631": dict(task(631, name="b", sub=loc(1)), cmd="echo cvd=$CUDA_VISIBLE_DEVICES", gpus=2)})
try: os.remove(os.path.join(hs.HEARTBEAT_DIR, "620"))
except OSError: pass
hs.run_one_cycle(); time.sleep(0.5)
chk("a 2-GPU task gets the next two cards", rq()["630"].get("gpu_slots") == [0] and rq()["631"].get("gpu_slots") == [1, 2]
    and "cvd=1,2\n" in open(rq()["631"]["log"]).read())
# requeue drops the slots
t = dict(task(640, name="rq", state="running", jid="2001", node="gone", st=loc(-450), sub=loc(-500), dispatched=loc(-450)), gpu_slots=[2])
stale_hb(640); open(os.path.join(hs.LOG_DIR, "task-640.log"), "w").write("x")
wdb({}); wq({"640": t}); hs.run_one_cycle()
chk("a requeued task gives its slots back", rq()["640"]["state"] == "pending" and "gpu_slots" not in rq()["640"])
hs.live_probe = _lp4
os.environ.pop("MOCK_LIVE_GPU"); os.environ.pop("CUDA_VISIBLE_DEVICES")
hs.run_one_cycle(); time.sleep(0.3)
reset_health(); reset_sched_state(); open(ev.EVENTS_FILE, "w").close(); wq({}); wdb({})

print("== red team, round 1: regressions for what it found ==")
reset_health(); reset_sched_state(); open(ev.EVENTS_FILE, "w").close()
import subprocess as _sp2, argparse as _apR, io as _ioR, contextlib as _clR
def _rc(fn, *a, **k):
    out = _ioR.StringIO()
    try:
        with _clR.redirect_stdout(out), _clR.redirect_stderr(out):
            fn(*a, **k)
        return None, out.getvalue()
    except SystemExit as e:
        return e.code, out.getvalue()
def _nsR(**kw):
    base = dict(cmd_or_file="true", workdir=None, priority=None, name=None, owner=None, need_mb=None,
                gpus=None, est_runtime=None, exclude=None, timeout=None, notify=None, after=None,
                after_any=None, array=None, max_running=None, allow_slow=None, quiet=False,
                begin=None, cpus=None, mem=None, warn_before=None, nodes=None,
                preempt=None, preemptible=None)
    base.update(kw); return _apR.Namespace(**base)
def _new():
    return max(rq().values(), key=lambda t: t["id"])
def _bad(fn, *a):
    try: fn(*a); return False
    except ValueError: return True

# durations: the whole string has to parse
chk("1.5h is 1.5 hours (was read as 5h)", hq.parse_duration("1.5h") == 5400 and hq.parse_duration("0.5h") == 1800)
chk("2h30m / 90 / 1-12:00:00 still parse",
    hq.parse_duration("2h30m") == 9000 and hq.parse_duration("90") == 90 and hq.parse_duration("1-12:00:00") == 129600)
chk("garbage is refused, not half-read",
    all(hq.parse_duration(x) is None for x in ("100ms", "2h30", "-5m", "1h30", "h", "5x")))
wq({})
chk("--timeout that does not parse is an error", _rc(hq.cmd_submit, _nsR(timeout="100ms"))[0] == 2)

# --need-mb auto never exceeds the card
for i, pk in enumerate((76000, 77000, 78000)):
    ev.record("finish", task=800 + i, name="huge", state="done", run_secs=100, gpu_peak_mb=pk)
wdb({"700": node("n", 72000)})                                  # an 81920 MiB card
_n, _ = hq.history_need_mb("huge")
chk("--need-mb auto is capped below the largest card", 77000 <= _n <= 81920 - hq.NEED_MB_IDLE_USED)

# wait on several: ids that never existed, and removed tasks
wq({"290": dict(task(290, name="okk", state="done"), exit_code=0, duration_secs=5)})
ev.record("finish", task=291, name="gone_bad", state="failed", exit_code=7, run_secs=5)
_c, _o = _rc(hq.cmd_wait_many, _apR.Namespace(interval=0.2, pending_timeout=0), [290, 9998])
chk("an id that never existed: exit 2, said so", _c == 2 and "not found" in _o)
_c, _o = _rc(hq.cmd_wait_many, _apR.Namespace(interval=0.2, pending_timeout=0), [290, 291])
chk("a failed task that was removed still counts as failed", _c == 1 and "ended failed" in _o)
many = {str(i): dict(task(i, name="m", state="failed"), exit_code=1, duration_secs=1) for i in range(300, 500)}
wq(many)
_c, _o = _rc(hq.cmd_wait_many, _apR.Namespace(interval=0.2, pending_timeout=0), list(range(300, 500)))
chk("200 failed tasks: summary lists 10 ids, not 200", _o.splitlines()[-1].count("#") == 10 and "+190" in _o)
wq({"510": dict(task(510, name="stuck"), pending_reason="insufficient_walltime"),
    "511": dict(task(511, name="stuck"), pending_reason="insufficient_walltime")})
_c, _o = _rc(hq.cmd_wait_many, _apR.Namespace(interval=0.2, pending_timeout=0.6), [510, 511])
chk("a wait on tasks that cannot start says why", _c == 75 and "2 × insufficient_walltime" in _o)

# submit output / --quiet / --after aN / flag conflicts
json.dump({"version": 1, "next_id": 520, "tasks": {}}, open(hs.QUEUE_FILE, "w"))
_c, _o = _rc(hq.cmd_submit, _nsR(name="sw", array="0-2", owner="o"))
chk("array submit prints exactly one #id (the documented capture)", re.findall(r"#(\d+)", _o) == ["520"])
_c, _o = _rc(hq.cmd_submit, _nsR(name="rep", after="a520"))
t = _new()
chk("--after a520 waits for every member of the array", t["depends_on"] == [520, 521, 522]
    and re.findall(r"#(\d+)", _o) == [str(t["id"])])
_c, _o = _rc(hq.cmd_submit, _nsR(name="one", after="520"))
chk("--after <array id> alone says it is ONE task", "--after a520" in _o and _new()["depends_on"] == [520])
_b = _ioR.StringIO()
with _clR.redirect_stdout(_b), _clR.redirect_stderr(_ioR.StringIO()):
    hq.cmd_submit(_nsR(name="q", quiet=True, est_runtime="auto"))
chk("--quiet prints the id and nothing else", _b.getvalue().strip() == str(_new()["id"]))
chk("--after with --after-any is refused", _rc(hq.cmd_submit, _nsR(after="520", after_any="521"))[0] == 2)
os.environ["HIVE_NOTIFY"] = "echo default-hook"
_rc(hq.cmd_submit, _nsR(name="nohook", notify=""))
chk("--notify '' switches the default hook off", _new()["notify"] == "")
os.environ.pop("HIVE_NOTIFY")
open(os.path.join(hs.HIVE_DIR, "d.hive"), "w").write(
    "  #HIVE name=ind\n#HIVE timeout=90m  # 1.5 hours max\n#HIVE notify=echo a # b\n#HIVE allow_slow=maybe\necho x\n")
_pf = hq.parse_hive_file(os.path.join(hs.HIVE_DIR, "d.hive"))
chk("directive: indented is read, trailing remark is dropped, notify keeps its #",
    _pf.get("name") == "ind" and _pf.get("timeout") == "90m" and _pf.get("notify") == "echo a # b"
    and _pf["cmd"] == "echo x")
chk("#HIVE allow_slow=maybe is an error, not a silent no",
    _rc(hq.cmd_submit, _nsR(cmd_or_file=os.path.join(hs.HIVE_DIR, "d.hive")))[0] == 2)
chk("hive wait --array by a member id", [t["id"] for t in hq.resolve_array(json.load(open(hs.QUEUE_FILE)), 521)] == [520, 521, 522])

# limits are checked before expanding
_t0 = time.time()
chk("--array 0-999999999 is refused at once", _bad(hq.parse_array, "0-999999999") and time.time() - _t0 < 0.5)
_t0 = time.time()
chk("evc[1-999999999] is refused at once", _bad(hh.expand_nodes, "evc[1-999999999]") and time.time() - _t0 < 0.5)
chk("reversed range and non-names are errors", _bad(hh.expand_nodes, "evc[5-1]") and _bad(hh.expand_nodes, "evc1;rm"))
chk("spaces separate node names too", hh.expand_nodes("evc1 evc2") == ["evc1", "evc2"])

# the owner's cap is the owner's
json.dump({"version": 1, "next_id": 540, "tasks": {}}, open(hs.QUEUE_FILE, "w"))
_rc(hq.cmd_submit, _nsR(name="free1", owner="ag", cmd_or_file="sleep 1.5"))
_rc(hq.cmd_submit, _nsR(name="capped", owner="ag", max_running=1, cmd_or_file="sleep 1.5"))
_rc(hq.cmd_submit, _nsR(name="free2", owner="ag", cmd_or_file="sleep 1.5"))
wdb({str(700 + i): node(f"n{i}", 72000) for i in range(3)})
hs.run_one_cycle()
chk("one capped task caps the whole owner", [rq()[k]["state"] for k in ("540", "541", "542")] == ["running", "pending", "pending"])
for k in (540, 541, 542): _rc(hq.cmd_cancel, _apR.Namespace(id=k, array=None))
hs.run_one_cycle()

# cascade in one cycle although the dependants have the HIGHER priority
json.dump({"version": 1, "next_id": 550, "tasks": {}}, open(hs.QUEUE_FILE, "w"))
_rc(hq.cmd_submit, _nsR(name="A", cmd_or_file="exit 3"))
_rc(hq.cmd_submit, _nsR(name="B", after="550", priority=5))
_rc(hq.cmd_submit, _nsR(name="C", after="551", priority=10))
_rc(hq.cmd_submit, _nsR(name="D", after="552", priority=20))
wdb({"700": node("n0", 72000)})
hs.run_one_cycle(); time.sleep(0.6); hs.run_one_cycle()
chk("failure reaches the end of the chain in ONE cycle",
    [rq()[k].get("fail_reason") for k in ("551", "552", "553")] == ["dependency_failed"] * 3)

# cancel always goes through the scheduler when there is one; hooks fire
clear_notified()
wq({"560": dict(task(560, name="runs", state="running", jid="700", node="n0", st=loc(-30)), notify=_hook, srun_pid=None),
    "561": dict(task(561, name="waits"), notify=_hook)})
open(os.path.join(hs.HEARTBEAT_DIR, "560"), "w").write("x")
t = rq(); t["560"]["started_ts"] = time.time() - 30; wq(t)
_rc(hq.cmd_cancel, _apR.Namespace(id=560, array=None)); _rc(hq.cmd_cancel, _apR.Namespace(id=561, array=None))
chk("running: cancel is a request to the scheduler", rq()["560"]["state"] == "running" and rq()["560"].get("cancel_requested"))
hs.run_one_cycle()
_n = sorted(notified())
chk("both cancels fire the hook (running and pending)",
    len(_n) == 2 and _n[0].startswith("560 finish cancelled") and _n[1].startswith("561 finish cancelled"))
chk("the running one has its run time recorded", isinstance(rq()["560"].get("duration_secs"), int))
clear_notified()

# notify: bounded, scrubbed environment
os.environ["SECRET_OF_AGENT_A"] = "s3cret"; os.environ["HIVE_OWNER"] = "agentA"
_ef = os.path.join(hs.HIVE_DIR, "hookenv.txt")
hs.notify(dict(task(570, name="e", state="done"), owner="agentB",
               notify=f'echo "[$SECRET_OF_AGENT_A][$HIVE_OWNER][$HIVE_TASK_ID]" > {_ef}'), "finish")
for _ in range(30):
    if os.path.exists(_ef) and open(_ef).read().strip(): break
    time.sleep(0.1)
chk("the hook gets the task's owner, not the scheduler's environment", open(_ef).read().strip() == "[][agentB][570]")
os.environ.pop("SECRET_OF_AGENT_A"); os.environ.pop("HIVE_OWNER")
hs._notify_running.clear(); hs._notify_waiting.clear()
_many = [dict(task(600 + i, name="f", state="failed"), notify="sleep 2") for i in range(40)]
for t_ in _many:
    hs.notify(t_, "finish")
chk("at most NOTIFY_MAX_PARALLEL hooks run at once",
    len(hs._notify_running) == hs.NOTIFY_MAX_PARALLEL and not hs._notify_waiting)
chk("the rest is owed on the task itself, so a restart cannot lose it",
    sum(1 for t_ in _many if t_.get("notify_pending") == "finish") == 40 - hs.NOTIFY_MAX_PARALLEL)
for p_ in hs._notify_running: p_.kill()
hs._notify_running.clear(); hs._notify_waiting.clear()

# a command with a syntax error fails like any other command
wdb({"700": node("n0", 72000)})
wq({"580": dict(task(580, name="syn"), cmd="echo 'unbalanced quote"),
    "581": dict(task(581, name="par", sub=loc(1)), cmd="echo open (")})
wdb({"700": node("n0", 72000), "701": node("n1", 72000)})
hs.run_one_cycle(); time.sleep(0.8); hs.run_one_cycle()
chk("syntax error in the command -> failed with bash's exit code, footer written",
    all(rq()[k]["state"] == "failed" and rq()[k]["exit_code"] in (1, 2) for k in ("580", "581"))
    and "finished at" in open(rq()["580"]["log"]).read())

# probe that cannot write a temp file: no verdict, no strike
_pr = _sp2.run(["bash", "-c", "TMPDIR=/nonexistent-x; " + hh.full_probe_shell().replace("/tmp /dev/shm", "/nonexistent-y")],
               capture_output=True, text=True, env=dict(os.environ, CUDA_VISIBLE_DEVICES="0"))
chk("no writable temp dir -> unknown, not gpu_unresponsive",
    "GPU_PROBE nowrite" in _pr.stdout and hh.parse_full_probe(_pr.stdout)[0] == "unknown"
    and hh.parse_gpu_query(_pr.stdout.splitlines())[2] is None)
chk("no predictable temp name is left in the probe", "hive_gpuq.$$" not in hh.full_probe_shell()
    and "hive_cudap.$$" not in hh.full_probe_shell())

# quarantined for a real fault: one slow-but-ok probe is not a way out
reset_health(); reset_sched_state()
d = hh.load(); hh.quarantine(d, "realbad", "agent says CUDA init dies", "agent", reporter="t")
d["nodes"]["realbad"]["last_check"] = 0; hh.save(d)
_hp = hs.health_probe; hs.health_probe = lambda jid: ("ok", "secs=146")
wdb({"700": node("realbad", 72000)}); wq({}); hs.run_one_cycle()
chk("an agent-reported node stays quarantined after a slow-ok probe",
    hh.load()["nodes"]["realbad"]["state"] == "quarantined")
for _ in range(3):
    d = hh.load(); d["nodes"]["realbad"].update(last_check=0, until=0); hh.save(d); hs.run_one_cycle()
chk("...however many of them: it is the reporter's to release",
    hh.load()["nodes"]["realbad"]["state"] == "quarantined")
# a node hive quarantined for a fault, that then keeps answering slowly (evc48)
reset_health(); reset_sched_state()
d = hh.load(); hh.quarantine(d, "wasslow", "CUDA probe failed: gpu_unresponsive", "auto")
d["nodes"]["wasslow"]["last_check"] = 0; hh.save(d)
wdb({"700": node("wasslow", 72000)}); wq({}); hs.run_one_cycle()
r = hh.load()["nodes"]["wasslow"]
chk("one slow-ok probe: still quarantined, counted 1/2", r["state"] == "quarantined" and r.get("slow_streak") == 1)
d = hh.load(); d["nodes"]["wasslow"]["last_check"] = 0; hh.save(d); hs.run_one_cycle()
chk("two in a row but inside the minimum hold: still quarantined",
    hh.load()["nodes"]["wasslow"]["state"] == "quarantined")
d = hh.load(); d["nodes"]["wasslow"].update(last_check=0, until=time.time() - 1); hh.save(d); hs.run_one_cycle()
chk("two in a row after the hold: SLOW", hh.load()["nodes"]["wasslow"]["state"] == "slow")
d = hh.load(); hh.quarantine(d, "wasslow", "CUDA probe failed: gpu_unresponsive", "auto")
d["nodes"]["wasslow"].update(last_check=0, until=0, slow_streak=1); hh.save(d)
hs.health_probe = lambda jid: ("fail", "gpu_unresponsive"); hs.run_one_cycle()
chk("a failed probe in between resets the count", hh.load()["nodes"]["wasslow"].get("slow_streak") == 0)
hs.health_probe = _hp

# index bug: quarantining a node must not make the loop skip the next one
reset_health(); reset_sched_state()
two = node("X", 72000); two["gpu"] = two["gpu"] * 2
wdb({"701": node("X", 72000), "702": two, "703": dict(node("Y", 72000), gpu=two["gpu"])})
d = hh.load(); hh.verify_strike(d, "X", "earlier"); hh.save(d)         # X has one verify strike
_lp = hs.live_probe
hs.live_probe = lambda jid, **kw: ({"ok": False, "fault": "gpu_unresponsive"} if jid == "702"
    else {"ok": True, "util": 0, "mem_used": 10, "mem_total": 81920, "cuda": "ok", "cuda_detail": ""})
wq({"590": dict(task(590, name="two"), cmd="true", gpus=2)})
hs.run_one_cycle(); hs.run_one_cycle(); time.sleep(0.3)
chk("node X quarantined on its 2nd strike; the task lands on Y", hh.load()["nodes"]["X"]["state"] == "quarantined"
    and rq()["590"].get("node") == "Y")
chk("drop_node keeps the index on the right entry",
    hs.drop_node([("a", {"node": "X"}, 0), ("b", {"node": "Y"}, 0)], "X", 1) == 0)
# a clean verify forgives old strikes
reset_health(); reset_sched_state()
d = hh.load(); hh.verify_strike(d, "okn", "one slow answer"); hh.strike(d, "okn", "task died at CUDA init"); hh.save(d)
wdb({"700": node("okn", 72000)}); wq({"591": dict(task(591, name="c"), cmd="sleep 2")})
hs.run_one_cycle(); time.sleep(0.3)
chk("a clean verify probe clears earlier verify strikes", hh.load()["nodes"]["okn"].get("verify_strikes") == 0)
chk("...but NOT the strikes of tasks that died there", hh.load()["nodes"]["okn"]["strikes"] == 1)
hs.live_probe = _lp

# one scheduler per cluster
open(hs.SCHED_PID, "w").write(f"4242\notherhost\n"); open(hs.SCHED_HB, "w").write("x")
chk("a live scheduler on another host is seen", hs.another_scheduler() == (4242, "otherhost"))
_old = time.time() - 600; os.utime(hs.SCHED_HB, (_old, _old))
chk("...a stale one is not", hs.another_scheduler() is None)
open(hs.SCHED_PID, "w").write(f"{os.getpid()}\n{hs.socket.gethostname()}\n")
chk("our own pid file is not 'another scheduler'", hs.another_scheduler() is None)
open(hs.SCHED_PID, "w").write("999999\notherhost\n"); open(hs.SCHED_HB, "w").write("x")

# a stop request waits for the pass to end
hs._stop_requested = False; hs._in_cycle = True
_sd = []; _rs = hs.shutdown; hs.shutdown = lambda *a: _sd.append(a)
hs.handle_signal(15, None)
chk("SIGTERM inside a pass is deferred", hs._stop_requested is True and _sd == [])
hs._in_cycle = False; hs.handle_signal(15, None)
chk("SIGTERM between passes stops at once", len(_sd) == 1)
hs._stop_requested = False; hs.shutdown = _rs

# exclusions hive added are lifted when the node leaves quarantine
open(os.path.join(hs.HIVE_DIR, "mock_pending"), "w").write("1")
_no_excl = lambda: os.path.exists(hh.EXCL_FILE) and os.remove(hh.EXCL_FILE)
_no_excl()
hh.track_excludes("5001", ["evc2"])            # hive had added evc2 to job 5001 (evc[1-3])
_upd = os.path.join(hs.HIVE_DIR, "mock_scontrol_update.log")
os.path.exists(_upd) and os.remove(_upd)
_d = hh.sync_pending_excludes(set())                   # nothing quarantined any more
chk("a node that left quarantine comes off the queued job's list",
    _d.get("5001") == ([], ["evc2"]) and "JobId=5001 ExcNodeList=evc1,evc3" in open(_upd).read())
os.remove(os.path.join(hs.HIVE_DIR, "mock_pending")); os.remove(_upd); _no_excl()

# pool add: every form of the script's own exclude, and the config's
hp2 = SourceFileLoader("hp2", os.path.join(LIB, "hive-pool")).load_module()
_scr = os.path.join(hs.HIVE_DIR, "forms.slurm")
open(_scr, "w").write('#!/bin/bash\n#SBATCH -p gpu -x evc7\n#SBATCH --partition=gpu --exclude="evc8,evc9"\n'
                      '#SBATCH --exclude evc10   # remark\n##SBATCH --exclude=no1\n# #SBATCH -x no2\nsleep 1\n')
chk("script excludes: several options per line, quotes, remarks",
    hp2.script_excludes(_scr) == ["evc7", "evc8,evc9", "evc10"])
chk("an exclude given as a JSON list is accepted",
    hp2.build_exclude(_scr, None, [["evc50", "evc51"], None], False)[0] == "evc7,evc8,evc9,evc10,evc50,evc51")
reset_health(); reset_sched_state(); open(ev.EVENTS_FILE, "w").close(); wq({}); wdb({})

print("== red team, round 2 ==")
reset_health(); reset_sched_state(); open(ev.EVENTS_FILE, "w").close()
hs._freed_at.clear(); hs._task_skip.clear()
_ok = lambda **k: dict({"ok": True, "util": 0, "mem_used": 10, "mem_total": 81920,
                        "gpus": [(0, 10, 81920)], "cuda": "ok", "cuda_detail": ""}, **k)
_lpR = hs.live_probe

# F1: a node whose task just ended is reused at once, although the DB still says busy
wdb({"700": node("n0", 72000, st="busy")})
t = task(700, name="ends", state="running", jid="700", node="n0", st=loc(-30)); t["started_ts"] = time.time() - 30
t["gpu_slots"] = [0]
open(os.path.join(hs.HEARTBEAT_DIR, "700.exit"), "w").write("0")
wq({"700": t, "701": dict(task(701, name="next"), cmd="sleep 2")})
hs.live_probe = lambda jid, **kw: _ok()
hs.run_one_cycle()
chk("the freed hold job takes the next task in the same cycle", rq()["701"].get("node") == "n0")
# ...and still next cycle, if this one could not use it yet (memory not released)
reset_sched_state(); hs._freed_at.clear()
wdb({"700": node("n0", 72000, st="busy")})
t = task(702, name="ends", state="running", jid="700", node="n0", st=loc(-30)); t["started_ts"] = time.time() - 30
t["gpu_slots"] = [0]
open(os.path.join(hs.HEARTBEAT_DIR, "702.exit"), "w").write("0")
wq({"702": t, "703": dict(task(703, name="next"), cmd="sleep 2")})
hs.live_probe = lambda jid, **kw: _ok(mem_used=30000, gpus=[(0, 30000, 81920)])
hs.run_one_cycle()
chk("memory still resident -> not dispatched yet", rq()["703"]["state"] == "pending")
hs._probe_backoff.clear()
hs.live_probe = lambda jid, **kw: _ok()
hs.run_one_cycle()
chk("...a later cycle still offers the freed hold job (DB says busy until the next poll)",
    rq()["703"].get("node") == "n0")
chk("once the DB has a newer reading the node is no longer forced",
    (hs._freed_at.__setitem__("700", time.time() - 100), wdb({"700": node("n0", 72000, st="busy")}),
     hs.recently_freed(json.load(open(hs.NODE_DB))["jobs"]))[2] == set())

# F2: a node the task cannot use (walltime, card size) is not the one it asks to probe
reset_sched_state(); hs._freed_at.clear()
_seen = []
hs.live_probe = lambda jid, **kw: (_seen.append(jid), _ok())[1]
wdb({"701": node("short", 1800), "702": node("long", 36000)})
wq({"710": dict(task(710, name="two_h", est=7200), cmd="true")})
hs.run_one_cycle(); time.sleep(0.3)
chk("walltime is checked BEFORE the probe: only the usable node is probed",
    _seen == ["702"] and rq()["710"].get("node") == "long")
_seen.clear()
small = dict(node("small", 72000), gpu=[{"index": 0, "util": 0, "mem_used": 10, "mem_total": 24000}])
wdb({"701": small, "702": node("big", 72000)})
wq({"711": dict(task(711, name="big_mem", need_mb=60000), cmd="true")})
hs.run_one_cycle(); time.sleep(0.3)
chk("a card smaller than --need-mb is skipped without a probe", _seen == ["702"] and rq()["711"].get("node") == "big")
# memory short only right now: the task looks elsewhere next cycle
_seen.clear(); reset_sched_state()
hs.live_probe = lambda jid, **kw: (_seen.append(jid),
    _ok(mem_used=4000, gpus=[(0, 4000, 81920)]) if jid == "701" else _ok())[1]
wdb({"701": node("part", 72000), "702": node("free", 72000)})
wq({"712": dict(task(712, name="needs79", need_mb=79000), cmd="true")})
hs.run_one_cycle()
chk("first node has too little free memory for this task", rq()["712"]["state"] == "pending")
hs.run_one_cycle(); time.sleep(0.3)
chk("next cycle it probes the OTHER node and runs", _seen == ["701", "702"] and rq()["712"].get("node") == "free")

# F3: a node that passes every probe while every task dies at CUDA init IS quarantined
reset_health(); reset_sched_state(); hs._freed_at.clear(); hs._task_skip.clear()
hs.live_probe = lambda jid, **kw: _ok()
def _dies(i):
    t = task(i, name="dies", state="running", jid="700", node="sick", st=loc(-20)); t["started_ts"] = time.time() - 20
    t["gpu_slots"] = [0]
    open(t["log"], "w").write("RuntimeError: CUDA error: CUDA-capable device(s) is/are busy or unavailable\n")
    open(os.path.join(hs.HEARTBEAT_DIR, f"{i}.exit"), "w").write("1")
    return t
wdb({"700": node("sick", 72000)})
wq({"720": _dies(720), "721": dict(task(721, name="probe_ok"), cmd="sleep 2")})
hs.run_one_cycle()          # strike 1, then 721 is verified clean and dispatched there
chk("strike from the task survives a clean verify probe", hh.load()["nodes"]["sick"]["strikes"] == 1
    and rq()["721"].get("node") == "sick")
q_ = rq(); q_["722"] = _dies(722); q_["721"]["state"] = "cancelled"; wq(q_)
hs.run_one_cycle()
chk("second task dying at CUDA init quarantines the node", hh.load()["nodes"]["sick"]["state"] == "quarantined")

# F5: busy nodes back off for longer each time, so the node behind them is reached
reset_health(); reset_sched_state(); hs._freed_at.clear()
hs.note_probe_result("b1", False, "node_busy_on_verify", cap=hs.PROBE_BACKOFF_BUSY)
_w1 = hs._probe_backoff["b1"][1] - time.time()
hs.note_probe_result("b1", False, "node_busy_on_verify", cap=hs.PROBE_BACKOFF_BUSY)
_w2 = hs._probe_backoff["b1"][1] - time.time()
chk("busy backoff doubles (45s, 90s, …) instead of staying at 45s", 40 < _w1 < 50 and 85 < _w2 < 95)
for _ in range(6): hs.note_probe_result("b1", False, "node_busy_on_verify", cap=hs.PROBE_BACKOFF_BUSY)
chk("...up to a limit", hs._probe_backoff["b1"][1] - time.time() <= hs.PROBE_BACKOFF_BUSY_MAX + 1)
hs.note_probe_result("b1", False)
chk("one unanswered probe after many 'busy' starts at the base wait, not at ten minutes",
    hs._probe_backoff["b1"][1] - time.time() <= hs.PROBE_BACKOFF_BASE + 1)
_seen.clear(); reset_sched_state()
hs.live_probe = lambda jid, **kw: (_seen.append(jid),
    _ok() if jid == "703" else _ok(util=90, mem_used=30000, gpus=[(90, 30000, 81920)]))[1]
wdb({"701": node("busyA", 72000), "702": node("busyB", 72000), "703": node("freeC", 72000)})
wq({"730": dict(task(730, name="patient"), cmd="true")})
for _ in range(3):
    hs.run_one_cycle()
    for k, v in list(hs._probe_backoff.items()):          # 38 s pass between cycles
        hs._probe_backoff[k] = (v[0], v[1] - 38, v[2], v[3])
time.sleep(0.3)
chk("two busy nodes do not hide the free one behind them", rq()["730"].get("node") == "freeC")
hs.live_probe = _lpR

# F6: a stuck main loop is not reported as alive for ever
_ex = []; _oe = os._exit; os._exit = lambda c: (_ex.append(c), (_ for _ in ()).throw(SystemExit(c)))
_ts = hs.time.sleep; hs._last_pass = time.time() - hs.MAIN_STALL_SECS - 5
try:
    hs.heartbeat_forever()
except SystemExit:
    pass
os._exit = _oe; hs._last_pass = time.time()
chk("no pass for MAIN_STALL_SECS -> the scheduler gives up its place", _ex == [1])

# F4 / F12: stop request through the shared FS; only our own pid file is removed
open(hs.SCHED_STOP_REQ, "w").close()
chk("a stop request file is seen once", hs.stop_requested() is True and hs.stop_requested() is False)
open(hs.SCHED_PID, "w").write("4242\notherhost\n"); open(hs.SCHED_HB, "w").write("x")
hs.remove_own_files()
chk("another scheduler's pid file is left alone", os.path.exists(hs.SCHED_PID))
open(hs.SCHED_PID, "w").write(f"{os.getpid()}\n{hs.socket.gethostname()}\n")
hs.remove_own_files()
chk("our own is removed", not os.path.exists(hs.SCHED_PID))
open(hs.SCHED_PID, "w").write("999999\notherhost\n"); open(hs.SCHED_HB, "w").write("x")
_killed = []; _ok_kill = os.kill
os.kill = lambda pid, sig: _killed.append((pid, sig))
hq.STOP_WAIT_REMOTE_SECS = 1
_c, _o = _rc(_real_cmd_daemon, _apR.Namespace(subcmd="stop"))
os.kill = _ok_kill
chk("stop of a scheduler on ANOTHER host signals nothing here",
    _killed == [] and _c == 1 and "otherhost" in _o)
chk("...it leaves a request the scheduler picks up (withdrawn when not honoured)",
    not os.path.exists(hq.SCHED_STOP_REQ))
open(hs.SCHED_PID, "w").write("999999\notherhost\n"); open(hs.SCHED_HB, "w").write("x")

# F7: cancel --force
wq({"740": dict(task(740, name="stuck", state="running", jid="700", node="n0", st=loc(-30)), cancel_requested=loc(-20))})
_rc(hq.cmd_cancel, _apR.Namespace(id=740, array=None, force=False))
chk("plain cancel on a live scheduler only asks", rq()["740"]["state"] == "running")
_rc(hq.cmd_cancel, _apR.Namespace(id=740, array=None, force=True))
chk("cancel --force ends it now", rq()["740"]["state"] == "cancelled" and not rq()["740"].get("cancel_requested"))

# F10
for i, pk in enumerate((76000, 77000, 78000)):
    ev.record("finish", task=850 + i, name="huge2", state="done", run_secs=100, gpu_peak_mb=pk)
wdb({"700": node("n", 72000)})
chk("--need-mb auto leaves room for what an idle card already uses",
    hq.history_need_mb("huge2")[0] == 81920 - hq.NEED_MB_IDLE_USED)
os.remove(hs.NODE_DB)
chk("...and without pool data asks for the measured peak, not more", hq.history_need_mb("huge2")[0] == 78000)
wdb({})

# F11
json.dump({"version": 1, "next_id": 760, "tasks": {}}, open(hs.QUEUE_FILE, "w"))
_rc(hq.cmd_submit, _nsR(name="arr", array="0-1"))
ev.record("finish", task=760, name="arr", state="failed", exit_code=1, run_secs=3)
q_ = rq(); q_.pop("760"); wq(q_)                       # member 0 failed and was pruned
_rc(hq.cmd_submit, _nsR(name="dep", after="a760"))
chk("--after aN still includes a pruned member", _new()["depends_on"] == [760, 761])
wdb({"700": node("n0", 72000)})
hs.run_one_cycle()
chk("...so the dependant fails with it", _new().get("fail_reason") == "dependency_failed")

# F9: pool add recording a job while a sync runs is not overwritten
_no_excl_state()
open(os.path.join(hs.HIVE_DIR, "mock_pending"), "w").write("1")
_orig = hh.pending_hold_jobs
def _slow_list():
    r = _orig(); hh.track_excludes("5009", ["evc43"]); return r      # pool add, mid-sync
hh.pending_hold_jobs = _slow_list
hh.sync_pending_excludes({"evc43"})
hh.pending_hold_jobs = _orig
chk("an entry recorded during a sync survives it", hh.hive_excludes().get("5009") == ["evc43"])
hh.track_excludes("4000", ["evc1"])
d_ = hh.load_hive_excludes(); d_["4000"]["t"] = time.time() - hh.EXCL_KEEP_SECS - 5; hh.save_hive_excludes(d_)
hh.sync_pending_excludes({"evc43"})
chk("an old entry of a job that left the queue is dropped", "4000" not in hh.hive_excludes())
os.remove(os.path.join(hs.HIVE_DIR, "mock_pending")); _no_excl_state()
_u2 = os.path.join(hs.HIVE_DIR, "mock_scontrol_update.log"); os.path.exists(_u2) and os.remove(_u2)

# a command that starts with a dash
wdb({"700": node("n0", 72000)})
wq({"750": dict(task(750, name="dash"), cmd="-notacommand 2>/dev/null; echo after-dash")})
hs.run_one_cycle(); time.sleep(0.6); hs.run_one_cycle()
chk("a command starting with '-' runs as a command", "after-dash" in open(rq()["750"]["log"]).read())
hs._freed_at.clear(); hs._task_skip.clear()
reset_health(); reset_sched_state(); open(ev.EVENTS_FILE, "w").close(); wq({}); wdb({})

print("== checklist round 2: wake-up, prune, begin, step limits, warning, fair share ==")
reset_health(); reset_sched_state(); open(ev.EVENTS_FILE, "w").close(); wq({}); wdb({})
_cfg = os.path.join(hs.HIVE_DIR, "pool_config.json")
def cfg(**kw):
    json.dump(kw, open(_cfg, "w"))

# A1 — the scheduler's wait ends on a submit and on a task's exit
try: os.remove(hs.SCHED_WAKE)
except OSError: pass
hs._watch_exit.clear()
chk("nothing happened -> keep waiting", hs.woken() is False)
_rc(hq.cmd_submit, _nsR(name="wakes"))
chk("a submit leaves sched.wake", os.path.exists(hs.SCHED_WAKE))
chk("...which ends the wait, once", hs.woken() is True and hs.woken() is False)
hs._watch_exit.add(801); open(os.path.join(hs.HEARTBEAT_DIR, "801.exit"), "w").write("0")
chk("the exit file of a running task ends the wait", hs.woken() is True)
os.remove(os.path.join(hs.HEARTBEAT_DIR, "801.exit")); hs._watch_exit.clear()
_sl = []; _ts = hs.time.sleep; hs.time.sleep = lambda x: _sl.append(x)
open(hs.SCHED_WAKE, "w").close(); hs.idle_wait()
chk("but never sooner than MIN_CYCLE_GAP after the last cycle", len(_sl) == hs.MIN_CYCLE_GAP)
_sl.clear(); hs.idle_wait()
chk("without a wake-up the full interval is waited", len(_sl) == hs.POLL_INTERVAL)
hs.time.sleep = _ts

# A9 — auto-prune
old_t = dict(task(810, name="old", state="done"), finished_ts=time.time() - 20 * 86400, exit_code=0)
new_t = dict(task(811, name="new", state="done"), finished_ts=time.time() - 86400, exit_code=0)
owed = dict(task(812, name="owed", state="failed"), finished_ts=time.time() - 20 * 86400, notify_pending="finish")
q_ = {"version": 1, "next_id": 900, "tasks": {"810": old_t, "811": new_t, "812": owed,
                                              "813": task(813, name="pend")}}
open(os.path.join(hs.LOG_DIR, "task-810.log"), "w").write("x")
hs._last_auto_prune = 0
chk("tasks finished more than auto_prune_days ago leave the queue",
    hs.auto_prune(q_) == 1 and sorted(q_["tasks"]) == ["811", "812", "813"])
chk("their logs stay (log_keep_days is not set)", os.path.exists(os.path.join(hs.LOG_DIR, "task-810.log")))
q_["tasks"]["810"] = old_t
chk("not again before AUTO_PRUNE_EVERY", hs.auto_prune(q_) == 0)
cfg(auto_prune_days=0); hs._last_auto_prune = 0
chk("auto_prune_days: 0 switches it off", hs.auto_prune(q_) == 0 and "810" in q_["tasks"])
cfg(auto_prune_days=14, log_keep_days=10); hs._last_auto_prune = 0
_o = time.time() - 11 * 86400; os.utime(os.path.join(hs.LOG_DIR, "task-810.log"), (_o, _o))
hs.auto_prune(q_)
chk("log_keep_days removes old logs", not os.path.exists(os.path.join(hs.LOG_DIR, "task-810.log")))
os.remove(_cfg); hs._last_auto_prune = time.time()

# B3 — --begin
_n = time.time()
chk("parse_begin: delay, date-time, time of day",
    abs(hq.parse_begin("2h", _n) - (_n + 7200)) < 1
    and hq.parse_begin("2030-01-02T03:04") == datetime.datetime(2030, 1, 2, 3, 4).timestamp()
    and 0 < hq.parse_begin("00:00", _n) - _n <= 86400 and hq.parse_begin("25:00") is None
    and hq.parse_begin("soon") is None)
json.dump({"version": 1, "next_id": 820, "tasks": {}}, open(hs.QUEUE_FILE, "w"))
_rc(hq.cmd_submit, _nsR(name="later", begin="1h")); _rc(hq.cmd_submit, _nsR(name="now"))
wdb({"700": node("n0", 72000), "701": node("n1", 72000)})
hs.run_one_cycle(); time.sleep(0.3)
chk("--begin holds the task (waiting_for_begin); the one behind it runs",
    rq()["820"].get("pending_reason") == "waiting_for_begin" and rq()["821"]["state"] in ("running", "done"))
q_ = rq(); q_["820"]["begin_ts"] = time.time() - 1; wq(q_)
hs.run_one_cycle(); time.sleep(0.3)
chk("...and runs once its time has come", rq()["820"]["state"] in ("running", "done"))
chk("--begin that cannot be read is an error", _rc(hq.cmd_submit, _nsR(begin="soon"))[0] == 2)

# B4 — CPUs / memory of the step
_argv = []
_pop = hs.subprocess.Popen
class _Spy:
    def __init__(self, a, **k):
        _argv.append(list(a)); self._p = _pop(["true"]); self.pid = self._p.pid
hs.subprocess.Popen = _Spy
hs.dispatch_task(dict(task(830, name="lim"), cpus=2, mem_mb=16000), "700", "n0")
hs.dispatch_task(task(831, name="nolim"), "700", "n0")
hs.subprocess.Popen = _pop
chk("--cpus / --mem become limits of the srun step",
    "--cpus-per-task=2" in _argv[0] and "--mem=16000M" in _argv[0] and "--mem=0" not in _argv[0])
chk("without them the step is launched as before",
    "--mem=0" in _argv[1] and not any(a.startswith("--cpus") for a in _argv[1]))
chk("--cpus 0 is refused", _rc(hq.cmd_submit, _nsR(cpus=0))[0] == 2)
_rc(hq.cmd_submit, _nsR(name="l2", cpus=3, mem=8000, warn_before="10m"))
chk("stored on the task", (_new()["cpus"], _new()["mem_mb"], _new()["warn_before_secs"]) == (3, 8000, 600))

# B2 — warning before the node expires
_flag = os.path.join(hs.HIVE_DIR, "got_usr1")
_cmdw = f"trap 'echo caught > {_flag}; exit 0' USR1; for i in $(seq 100); do sleep 0.1; done; exit 5"
wdb({"700": node("expiring", 400)})                       # 400 s of walltime left
clear_notified()
wq({"840": dict(task(840, name="warned"), cmd=_cmdw, warn_before_secs=600, notify=_hook)})
hs.run_one_cycle()
chk("not placed on a node that expires before the warning could help",
    rq()["840"]["state"] == "pending" and rq()["840"].get("pending_reason") == "insufficient_walltime")
wdb({"700": node("expiring", 72000)})
hs.run_one_cycle()                                        # dispatched
chk("dispatched with the waiting wrapper", rq()["840"]["state"] == "running")
time.sleep(1.0)
wdb({"700": node("expiring", 400, st="busy")})            # …time passes: 400 s left
hs.run_one_cycle()
chk("a command that has only just started is not signalled yet", not rq()["840"].get("warned_ts"))
q_ = rq(); q_["840"]["started_ts"] = q_["840"]["dispatched_ts"] = time.time() - 90; wq(q_)
hs.run_one_cycle()                                        # walltime 400 s <= 600 s -> warn
t = rq()["840"]
chk("the task is warned once", t.get("warned_ts") and "will be reclaimed" in open(t["log"]).read())
for _ in range(40):
    if os.path.exists(_flag): break
    time.sleep(0.1)
chk("the command received SIGUSR1 and handled it", os.path.exists(_flag))
time.sleep(0.5); hs.run_one_cycle()
t = rq()["840"]
chk("the wrapper survived the signal: exit code and footer are there",
    (t["state"], t["exit_code"]) == ("done", 0) and "finished at" in open(t["log"]).read())
chk("hook called with event `expiring`", any(l.startswith("840 expiring") for l in notified()))
_w = open(t["log"]).read().count("will be reclaimed")
chk("...and only once", _w == 1)
clear_notified()
# a compound command: the signal must reach the real process, and the shell in between
# must survive it (it used to be killed: task failed 138, the process left running)
_flag2 = os.path.join(hs.HIVE_DIR, "got_usr1_py")
open(os.path.join(hs.HIVE_DIR, "w.py"), "w").write(
    "import signal, time, sys\n"
    f"signal.signal(signal.SIGUSR1, lambda *a: (open({_flag2!r}, 'w').write('x'), sys.exit(0)))\n"
    "time.sleep(20)\n")
_cmdc = f"{sys.executable} {hs.HIVE_DIR}/w.py; echo after-python"
wdb({"700": node("n0", 72000)})
wq({"842": dict(task(842, name="compound"), cmd=_cmdc, warn_before_secs=600)})
hs.run_one_cycle(); time.sleep(1.5)
q_ = rq(); q_["842"]["started_ts"] = q_["842"]["dispatched_ts"] = time.time() - 90; wq(q_)
wdb({"700": node("n0", 400, st="busy")})
hs.run_one_cycle()
for _ in range(50):
    if os.path.exists(_flag2): break
    time.sleep(0.1)
chk("compound command: the python process got the signal", os.path.exists(_flag2))
time.sleep(0.8); hs.run_one_cycle()
t = rq()["842"]
chk("...the shell around it lived on and finished the script (exit 0, not 138)",
    (t["state"], t["exit_code"]) == ("done", 0) and "after-python" in open(t["log"]).read())
# warned on one node, requeued, warned again on the next
t = dict(task(843, name="again", state="running", jid="2001", node="gone", st=loc(-450), sub=loc(-500),
              dispatched=loc(-450)), warn_before_secs=600, warned_ts=time.time() - 300, gpu_slots=[0])
stale_hb(843); open(os.path.join(hs.LOG_DIR, "task-843.log"), "w").write("x")
wdb({}); wq({"843": t}); hs.run_one_cycle()
chk("a requeue forgets the warning of the old node", rq()["843"]["state"] == "pending"
    and "warned_ts" not in rq()["843"])
wq({"841": dict(task(841, name="plenty"), cmd="sleep 1", warn_before_secs=600)})
wdb({"700": node("fresh", 72000)})
hs.run_one_cycle(); time.sleep(0.4); hs.run_one_cycle()
chk("no warning while the node has time left", not rq()["841"].get("warned_ts"))
time.sleep(1.0); hs.run_one_cycle()

# B7 — fair share
_now = time.time()
hist = {"850": dict(task(850, name="h", state="done"), owner="greedy", started_ts=_now - 7200,
                    finished_ts=_now - 600, duration_secs=6600),
        "851": dict(task(851, name="h2", state="done"), owner="modest", started_ts=_now - 700,
                    finished_ts=_now - 650, duration_secs=50),
        "852": dict(task(852, name="old", state="done"), owner="modest", started_ts=_now - 3 * 86400,
                    finished_ts=_now - 3 * 86400 + 90000, duration_secs=90000)}
u = hs.owner_usage(hist, _now)
chk("usage counts the last 24 h only, in coarse buckets", u.get("greedy", 0) > u.get("modest", 0) == 0)
pend = {"860": dict(task(860, name="g1", sub=loc(-30)), owner="greedy", cmd="sleep 1"),
        "861": dict(task(861, name="g2", sub=loc(-20)), owner="greedy", cmd="sleep 1"),
        "862": dict(task(862, name="m1", sub=loc(-10)), owner="modest", cmd="sleep 1"),
        "863": dict(task(863, name="urgent", sub=loc(-5), priority=5), owner="greedy", cmd="sleep 1")}
wdb({"700": node("n0", 72000), "701": node("n1", 72000)})
wq(dict(hist, **pend)); hs.run_one_cycle()
chk("without fair_share: first come first served (after priority)",
    sorted(k for k in pend if rq()[k]["state"] == "running") == ["860", "863"])
for k in pend: _rc(hq.cmd_cancel, _apR.Namespace(id=int(k), array=None, force=True))
cfg(fair_share=True)
wdb({"700": node("n0", 72000), "701": node("n1", 72000)})
wq(dict(hist, **pend)); hs.run_one_cycle()
chk("with fair_share: priority first, then the owner who used less",
    sorted(k for k in pend if rq()[k]["state"] == "running") == ["862", "863"])
for k in pend: _rc(hq.cmd_cancel, _apR.Namespace(id=int(k), array=None, force=True))
os.remove(_cfg)

# A7 — a timeout does not count the CUDA init of a slow node
t = dict(task(870, name="slowstart", state="running", jid="700", node="n0", st=loc(-200)),
         timeout_secs=120, node_slow_init_secs=180, gpu_slots=[0])
t["started_ts"] = time.time() - 200
open(os.path.join(hs.HEARTBEAT_DIR, "870"), "w").write("x")
wdb({"700": node("n0", 72000, st="busy")}); wq({"870": t}); hs.run_one_cycle()
chk("200 s into a 120 s timeout on a node with 180 s of init: not killed", rq()["870"]["state"] == "running")
q_ = rq(); q_["870"]["started_ts"] = time.time() - 320; wq(q_); hs.run_one_cycle()
chk("...killed once its own 120 s are over", rq()["870"].get("fail_reason") == "timeout")
try: os.remove(_flag)
except OSError: pass
hs.run_one_cycle(); time.sleep(0.3)
reset_health(); reset_sched_state(); open(ev.EVENTS_FILE, "w").close(); wq({}); wdb({})

print("== checklist round 2: multi-node tasks, preemption, autoscale ==")
reset_health(); reset_sched_state(); open(ev.EVENTS_FILE, "w").close(); wq({}); wdb({})
hauto = SourceFileLoader("hive_autoscale", os.path.join(LIB, "hive_autoscale.py")).load_module()

# B5 — multi-node task ("gang")
json.dump({"version": 1, "next_id": 900, "tasks": {}}, open(hs.QUEUE_FILE, "w"))
_c, _o = _rc(hq.cmd_submit, _nsR(name="ddp", nodes=3, owner="o",
             cmd_or_file='echo "rank=$HIVE_GANG_RANK/$HIVE_GANG_SIZE hosts=$HIVE_GANG_HOSTS"; sleep 1.5'))
q = rq()
chk("--nodes 3 creates three members that share an id",
    sorted(q) == ["900", "901", "902"] and {t["array_id"] for t in q.values()} == {900}
    and {t["gang_size"] for t in q.values()} == {3} and re.findall(r"#(\d+)", _o) == ["900"])
chk("--nodes with --array, or --nodes 1, is refused",
    _rc(hq.cmd_submit, _nsR(nodes=2, array="0-3"))[0] == 2 and _rc(hq.cmd_submit, _nsR(nodes=1))[0] == 2)
wdb({"700": node("a", 72000), "701": node("b", 72000)})               # only two nodes
hs.run_one_cycle()
chk("two nodes for three members: nobody starts (waiting_for_gang)",
    [rq()[k]["state"] for k in ("900", "901", "902")] == ["pending"] * 3
    and {rq()[k].get("pending_reason") for k in ("900", "901", "902")} == {"waiting_for_gang"})
wq(dict(rq(), **{"905": dict(task(905, name="single", sub=loc(5)), cmd="sleep 1.5")}))
hs.run_one_cycle()
chk("the cards a gang reserved and gave back are free for others", rq()["905"]["state"] == "running")
_rc(hq.cmd_cancel, _apR.Namespace(id=905, array=None, force=True))
two = dict(node("a", 72000), gpu=[{"index": i, "util": 0, "mem_used": 10, "mem_total": 81920} for i in range(2)])
os.environ["MOCK_LIVE_GPU"] = "0, 10, 81920\\n0, 10, 81920"
wdb({"700": two, "701": node("b", 72000)})
hs.run_one_cycle()
chk("two cards on ONE node do not count as two nodes",
    [rq()[k]["state"] for k in ("900", "901", "902")] == ["pending"] * 3)
os.environ.pop("MOCK_LIVE_GPU")
wdb({"700": node("a", 72000), "701": node("b", 72000), "702": node("c", 72000)})
hs.run_one_cycle(); time.sleep(0.5)
q = rq()
chk("three nodes: all members start in the same cycle, one node each",
    sorted(q[k]["node"] for k in ("900", "901", "902")) == ["a", "b", "c"]
    and len({q[k]["started_at"] for k in ("900", "901", "902")}) <= 2)
_hosts = q["900"]["gang_hosts"]
chk("every member knows its rank and all the hosts",
    all(f"rank={i}/3 hosts={_hosts}" in open(q[str(900 + i)]["log"]).read() for i in range(3))
    and sorted(_hosts.split(",")) == ["a", "b", "c"])
time.sleep(1.5); hs.run_one_cycle()
chk("all done", [rq()[k]["state"] for k in ("900", "901", "902")] == ["done"] * 3)
# one member fails -> the others are stopped
json.dump({"version": 1, "next_id": 910, "tasks": {}}, open(hs.QUEUE_FILE, "w"))
_rc(hq.cmd_submit, _nsR(name="ddp2", nodes=2,
    cmd_or_file='[ "$HIVE_GANG_RANK" = 0 ] && exit 4; sleep 30'))
wdb({"700": node("a", 72000), "701": node("b", 72000)})
hs.run_one_cycle(); time.sleep(0.6); hs.run_one_cycle()
q = rq()
chk("a failed member takes the running ones with it",
    q["910"]["state"] == "failed" and q["910"]["exit_code"] == 4
    and (q["911"]["state"] == "cancelled" or q["911"].get("cancel_requested")))
hs.run_one_cycle()
chk("...recorded as gang_member_failed", rq()["911"]["state"] == "cancelled"
    and rq()["911"].get("fail_reason") == "gang_member_failed")
# a member loses its node -> no partial restart
t0 = dict(task(920, name="g", state="running", jid="2001", node="gone", st=loc(-450), sub=loc(-500),
               dispatched=loc(-450)), array_id=920, array_index=0, gang_size=2, gpu_slots=[0])
t1 = dict(task(921, name="g", state="running", jid="700", node="a", st=loc(-450), sub=loc(-500),
               dispatched=loc(-450)), array_id=920, array_index=1, gang_size=2, gpu_slots=[0])
stale_hb(920); open(os.path.join(hs.HEARTBEAT_DIR, "921"), "w").write("x")
for i in (920, 921): open(os.path.join(hs.LOG_DIR, f"task-{i}.log"), "w").write("x")
wdb({"700": node("a", 72000, st="busy")}); wq({"920": t0, "921": t1})
hs.run_one_cycle()
q = rq()
chk("a member that lost its node is not re-run alone; the gang ends",
    q["920"]["state"] == "failed" and q["920"].get("fail_reason") == "gang_member_failed"
    and (q["921"].get("cancel_requested") or q["921"]["state"] == "cancelled"))
hs.run_one_cycle()

# red team round 3 — gangs
reset_health(); reset_sched_state()
_pr3 = []
hs.live_probe = lambda jid, **kw: (_pr3.append(jid), _ok())[1]
json.dump({"version": 1, "next_id": 960, "tasks": {}}, open(hs.QUEUE_FILE, "w"))
_rc(hq.cmd_submit, _nsR(name="g2", nodes=2, cmd_or_file="sleep 1"))
wdb(dict({str(700 + i): node("a", 72000) for i in range(4)}, **{"704": node("b", 72000)}))
hs.run_one_cycle(); time.sleep(0.3)
chk("four hold jobs on node a, one on b: the gang of 2 starts (a and b)",
    sorted(rq()[k].get("node") for k in ("960", "961")) == ["a", "b"])
chk("...having probed one hold job per node, not every one", len(_pr3) == 2)
time.sleep(1.0); hs.run_one_cycle()
_pr3.clear()
json.dump({"version": 1, "next_id": 962, "tasks": {}}, open(hs.QUEUE_FILE, "w"))
_rc(hq.cmd_submit, _nsR(name="g3", nodes=3, cmd_or_file="sleep 1"))
wdb({"700": node("a", 72000), "701": node("a", 72000), "702": node("b", 72000)})
for _ in range(3): hs.run_one_cycle()
chk("two nodes for a gang of three: it waits WITHOUT probing anything", _pr3 == []
    and {rq()[k].get("pending_reason") for k in ("962", "963", "964")} == {"waiting_for_gang"})
q_ = rq(); q_["963"]["held"] = True; wq(q_)
wdb({"700": node("a", 72000), "701": node("b", 72000), "702": node("c", 72000)})
hs.run_one_cycle()
chk("one held member holds the whole gang",
    [rq()[k]["state"] for k in ("962", "963", "964")] == ["pending"] * 3
    and {rq()[k].get("pending_reason") for k in ("962", "963", "964")} == {"held"})
q_ = rq(); q_["963"]["held"] = False
for k in ("962", "963", "964"): q_[k].update(owner="capped", max_running=2)
wq(q_); hs.run_one_cycle()
chk("a gang of 3 does not start under an owner cap of 2",
    {rq()[k].get("pending_reason") for k in ("962", "963", "964")} == {"owner_limit"})
# a finished member that was removed does not stop the ones still running
t0 = dict(task(966, name="g", state="running", jid="700", node="a", st=loc(-60)), array_id=965,
          array_index=1, gang_size=2, gpu_slots=[0])
t0["started_ts"] = time.time() - 60
open(os.path.join(hs.HEARTBEAT_DIR, "966"), "w").write("x")
wdb({"700": node("a", 72000, st="busy")}); wq({"966": t0}); hs.run_one_cycle()
chk("rank 0 ended and was removed from the queue: rank 1 keeps running",
    rq()["966"]["state"] == "running" and not rq()["966"].get("cancel_requested"))
old_m = dict(task(968, name="g", state="done"), array_id=967, array_index=0, gang_size=2,
             finished_ts=time.time() - 30 * 86400)
run_m = dict(t0, id=969, array_id=967)
qq = {"version": 1, "next_id": 1000, "tasks": {"968": old_m, "969": run_m}}
hs._last_auto_prune = 0; hs.auto_prune(qq)
chk("auto-prune leaves the members of a gang that is still running", "968" in qq["tasks"])
hs._last_auto_prune = time.time()
hs.live_probe = _lpR

# B6 — preemption
reset_health(); reset_sched_state()
_victims = []
def _running(i, prio, stubborn=False, **kw):
    _p = _sp.Popen(["bash", "-c", "trap '' TERM; sleep 300 & wait"] if stubborn else ["sleep", "300"],
                   start_new_session=True); _victims.append(_p)
    t = dict(task(i, name=f"r{i}", state="running", jid=kw.pop("jid", "700"), node=kw.pop("node", "a"),
                  st=loc(-60), priority=prio), gpu_slots=[0], srun_pid=_p.pid, **kw)
    t["started_ts"] = time.time() - kw.get("age", 60)
    open(os.path.join(hs.HEARTBEAT_DIR, str(i)), "w").write("x")
    open(t["log"], "w").write("x\n")
    return t
T = {"930": _running(930, 0, stubborn=True, preemptible=True), "931": _running(931, 0, jid="701", node="b"),
     "932": dict(task(932, name="urgent", priority=9), preempt=True, cmd="sleep 1.5")}
chk("victim: lower priority AND preemptible", hs.pick_victim(T["932"], T, set(), set())["id"] == 930)
chk("no victim among tasks that did not agree",
    hs.pick_victim(T["932"], {"931": T["931"], "932": T["932"]}, set(), set()) is None)
chk("no victim of equal or higher priority",
    hs.pick_victim(dict(T["932"], priority=0), T, set(), set()) is None)
chk("the latest started of the lowest priority is chosen",
    hs.pick_victim(T["932"], {"1": dict(T["930"], id=1, started_ts=time.time() - 5000),
                              "2": dict(T["930"], id=2, started_ts=time.time() - 50),
                              "3": dict(T["930"], id=3, priority=4, started_ts=time.time() - 5)},
                   set(), set())["id"] == 2)
busy = lambda n: node(n, 72000, st="busy")
wdb({"700": busy("a"), "701": busy("b")}); wq(T)
clear_notified()
hs.run_one_cycle()
q = rq()
chk("no free node: the preemptible task is asked to stop, the other is left alone",
    q["930"].get("cancel_requested") and q["930"].get("preempted_by") == 932
    and not q["931"].get("cancel_requested") and q["932"].get("pending_reason") == "preempting")
hs.run_one_cycle()
chk("first the step is asked to stop; the task is requeued only once it is gone",
    rq()["930"]["state"] == "running" and rq()["930"].get("preempt_signalled_ts"))
os.killpg(_victims[0].pid, 9); time.sleep(0.3); [_p.poll() for _p in _victims]
hs.run_one_cycle()
q = rq()
chk("the victim is REQUEUED, not cancelled", q["930"]["state"] == "pending"
    and q["930"].get("preempt_count") == 1 and q["930"].get("checkpoint_warning")
    and "PREEMPTED" in open(q["930"]["log"]).read())
hs.live_probe = lambda jid, **kw: _ok()
hs.run_one_cycle(); time.sleep(0.3)
chk("...and the urgent task has its node", rq()["932"].get("node") == "a")
chk("preempt / requeue events recorded",
    {"preempt"} <= {e["event"] for e in ev.iter_events()}
    and any(e["event"] == "requeue" and e.get("reason") == "preempted" for e in ev.iter_events()))
wq({"940": _running(940, 0, preemptible=True, preempt_count=hs.MAX_PREEMPTIONS),
    "941": dict(task(941, name="u2", priority=9), preempt=True)})
wdb({"700": busy("a")}); hs.run_one_cycle()
chk("a task preempted MAX_PREEMPTIONS times is left alone", not rq()["940"].get("cancel_requested"))
wq({"942": _running(942, 0, preemptible=True), "943": dict(task(943, name="plain", priority=9))})
wdb({"700": busy("a")}); hs.run_one_cycle()
chk("a task without --preempt never preempts", not rq()["942"].get("cancel_requested"))
# red team round 3
# a victim is never stopped for a task that could not use its node
reset_sched_state(); hs._reserved.clear()
wdb({"700": dict(busy("a"), time_left_secs=7200)})
wq({"944": _running(944, 0, preemptible=True),
    "945": dict(task(945, name="ten_h", priority=9, est=36000), preempt=True)})
for _ in range(3): hs.run_one_cycle()
chk("walltime too short for the preemptor -> nobody is preempted",
    not rq()["944"].get("cancel_requested") and rq()["944"]["state"] == "running")
small = dict(busy("a"), gpu=[{"index": 0, "util": 50, "mem_used": 30000, "mem_total": 40000}])
wdb({"700": small})
wq({"946": _running(946, 0, preemptible=True),
    "947": dict(task(947, name="big", priority=9, need_mb=60000), preempt=True)})
for _ in range(3): hs.run_one_cycle()
chk("card too small for the preemptor -> nobody is preempted", not rq()["946"].get("cancel_requested"))
# the freed node is kept for the task that asked for it
reset_sched_state(); hs._reserved.clear()
wdb({"700": busy("a")})
wq({"950": _running(950, 0, preemptible=True),
    "951": dict(task(951, name="ordinary", priority=5, sub=loc(-100)), cmd="sleep 2"),
    "952": dict(task(952, name="preemptor", priority=5, sub=loc(-10)), preempt=True, cmd="sleep 2")})
hs.run_one_cycle(); hs.run_one_cycle()
time.sleep(0.3); [_p.poll() for _p in _victims]
hs.run_one_cycle(); hs.run_one_cycle(); time.sleep(0.3)
q = rq()
chk("the node a preemption freed goes to the preemptor, not to whoever is first in line",
    q["952"].get("node") == "a" and q["951"]["state"] == "pending" and q["950"]["state"] == "pending")
# the user's cancel wins over a preemption under way
reset_sched_state(); hs._reserved.clear()
wdb({"700": busy("a")})
wq({"953": _running(953, 0, preemptible=True), "954": dict(task(954, name="p", priority=9), preempt=True)})
hs.run_one_cycle()
_rc(hq.cmd_cancel, _apR.Namespace(id=953, array=None, force=False))
hs.run_one_cycle(); time.sleep(0.3); [_p.poll() for _p in _victims]; hs.run_one_cycle()
chk("hive cancel of a task that is being preempted cancels it", rq()["953"]["state"] == "cancelled")
# the preemptor goes away before anything was signalled
reset_sched_state(); hs._reserved.clear()
wdb({"700": busy("a")})
v = _running(955, 0, preemptible=True); v.update(cancel_requested=loc(), preempted_by=956)
wq({"955": v, "956": dict(task(956, name="gone", priority=9, state="cancelled"), preempt=True)})
hs.run_one_cycle()
chk("preemptor cancelled before the victim was signalled: the victim keeps running",
    rq()["955"]["state"] == "running" and not rq()["955"].get("cancel_requested"))
# a victim that finishes by itself carries no stale flags
v = _running(957, 0, preemptible=True); v.update(cancel_requested=loc(), preempted_by=958)
open(os.path.join(hs.HEARTBEAT_DIR, "957.exit"), "w").write("0")
wq({"957": v, "958": dict(task(958, name="p", priority=9), preempt=True)})
hs.run_one_cycle()
chk("victim ended by itself: done, flags cleared", rq()["957"]["state"] == "done"
    and not rq()["957"].get("cancel_requested") and not rq()["957"].get("preempted_by"))
for _p in _victims:
    _p.poll() is None and _p.kill()
hs._reserved.clear()
hs.live_probe = _lpR
_rc(hq.cmd_submit, _nsR(name="pp", preempt=True, preemptible=True))
chk("--preempt / --preemptible stored", _new()["preempt"] is True and _new()["preemptible"] is True)

# B1 — autoscale
C = {"preset": "highgpu", "min_nodes": 4, "max_nodes": 6, "time": "7-00:00:00",
     "renew_before": 12 * 3600, "until": None}
def J(n, left=500000, nodes=None, **kw):
    return {str(700 + i): dict(node((nodes or [f"n{i}"] * n)[i] if nodes else f"n{i}", left), **kw)
            for i in range(n)}
chk("enough usable nodes -> nothing", hauto.plan(C, J(4), 0, 0, set(), [])[0] == 0)
chk("two short -> two submitted", hauto.plan(C, J(2), 0, 0, set(), [])[0] == 2)
chk("at most MAX_PER_RUN at a time", hauto.plan(C, {}, 0, 0, set(), [])[0] == hauto.MAX_PER_RUN)
chk("queued hold jobs count as on their way", hauto.plan(C, J(2), 0, 2, set(), [])[0] == 0)
chk("hold jobs about to expire are replaced before they do",
    hauto.plan(C, J(4, left=6 * 3600), 0, 0, set(), [])[0] == 2)
chk("...judged by the walltime left NOW, not when the DB was written",
    hauto.plan(C, J(4, left=13 * 3600), 2 * 3600, 0, set(), [])[0] == 2)
chk("hold jobs on quarantined or slow nodes do not count as usable",
    hauto.plan(C, J(4, nodes=["bad", "bad", "g1", "g2"]), 0, 0, {"bad"}, [])[0] == 2)
chk("...but they do count towards max_nodes",
    hauto.plan(C, J(6, nodes=["bad"] * 4 + ["g1", "g2"]), 0, 0, {"bad"}, [])[0] == 0)
chk("daily limit", hauto.plan(C, {}, 0, 0, set(), [time.time() - 60] * hauto.MAX_PER_DAY)[0] == 0)
chk("SLURM could not be asked -> nothing (never guess)", hauto.plan(C, {}, 0, None, set(), [])[0] == 0)
json.dump({"autoscale": {"enabled": False, "min_nodes": 4, "preset": "highgpu"}}, open(_cfg, "w"))
chk("off unless enabled", hauto.settings() is None)
json.dump({"autoscale": {"enabled": True, "min_nodes": 4, "preset": "highgpu", "until": "2020-01-01"}}, open(_cfg, "w"))
chk("off after `until`", hauto.settings() is None)
json.dump({"autoscale": {"enabled": True, "min_nodes": 4, "max_nodes": 2, "preset": "highgpu"}}, open(_cfg, "w"))
chk("off when max_nodes < min_nodes", hauto.settings() is None)
json.dump({"default": "normal", "presets": {"highgpu": {"script": os.path.join(hs.HIVE_DIR, "hold.slurm")}},
           "autoscale": {"enabled": True, "min_nodes": 3, "max_nodes": 5, "preset": "highgpu",
                         "time": "7-00:00:00", "renew_before": "12h", "until": "2099-01-01"}}, open(_cfg, "w"))
open(os.path.join(hs.HIVE_DIR, "hold.slurm"), "w").write("#!/bin/bash\n#SBATCH -p highgpu\nsleep 1\n")
for f in ("mock_sbatch.log", "autoscale_state.json"):
    try: os.remove(os.path.join(hs.HIVE_DIR, f))
    except OSError: pass
_n, _why = hauto.run(J(1), 0, set())
_sb = open(os.path.join(hs.HIVE_DIR, "mock_sbatch.log")).read().splitlines()
chk("run(): submits through hive pool add, with the configured time",
    _n == 2 and sum(1 for l in _sb if "--test-only" not in l) == 2 and all("--time 7-00:00:00" in l for l in _sb))
chk("...and not again before EVERY_SECS", hauto.run(J(1), 0, set()) == (0, "not due"))
chk("what it did is kept", len(hauto.load_state().get("submitted", [])) == 2)
os.remove(_cfg)
for f in ("mock_sbatch.log", "autoscale_state.json", "hold.slurm"):
    try: os.remove(os.path.join(hs.HIVE_DIR, f))
    except OSError: pass
reset_health(); reset_sched_state(); open(ev.EVENTS_FILE, "w").close(); wq({}); wdb({})

print("== a dead scheduler is restarted by list / wait (feedback #18/#19) ==")
_started = []
_real_daemon, _real_running = hq.cmd_daemon, hq.is_sched_running
hq.cmd_daemon = lambda a: _started.append(a.subcmd)
hq.is_sched_running = lambda: False
with _cl.redirect_stderr(_io.StringIO()):
    hq._sched_checked = 0
    chk("dead scheduler + nothing active -> left alone",
        hq.ensure_sched({"tasks": {"1": {"state": "done"}}}, every=0) is False and _started == [])
    chk("dead scheduler + a running task -> started",
        hq.ensure_sched({"tasks": {"1": {"state": "running"}}}, every=0) is True and _started == ["start"])
    chk("...checked at most once per interval",
        hq.ensure_sched({"tasks": {"1": {"state": "running"}}}, every=60) is False and _started == ["start"])
    hq.is_sched_running = lambda: True
    chk("live scheduler -> nothing to do",
        hq.ensure_sched({"tasks": {"1": {"state": "pending"}}}, every=0) is False and _started == ["start"])
hq.cmd_daemon, hq.is_sched_running = _real_daemon, _real_running   # (_real_daemon is the suite-wide stub)

print("== phase 3: dependencies, arrays, concurrency caps ==")
import argparse as _ap
def _capture(fn, *a):
    buf = _io.StringIO()
    with _cl.redirect_stdout(buf):
        fn(*a)
    return buf.getvalue()
reset_health(); reset_sched_state()
open(ev.EVENTS_FILE, "w").close()
chk("parse_array: range / list / step / cap",
    hq.parse_array("0-3") == ([0, 1, 2, 3], None) and hq.parse_array("1,3,5") == ([1, 3, 5], None)
    and hq.parse_array("0-20:5%2") == ([0, 5, 10, 15, 20], 2))
def _bad(fn, *a):
    try: fn(*a); return False
    except ValueError: return True
chk("parse_array rejects nonsense", _bad(hq.parse_array, "5-1") and _bad(hq.parse_array, "a-b")
    and _bad(hq.parse_array, "0-3%0") and _bad(hq.parse_array, "0-5000"))
chk("parse_id_list", hq.parse_id_list("12, 13 14,12") == [12, 13, 14] and _bad(hq.parse_id_list, "12,x"))

def _ns(**kw):
    base = dict(cmd_or_file="true", workdir=None, priority=None, name=None, owner=None, need_mb=None,
                gpus=None, est_runtime=None, exclude=None, timeout=None, notify=None, after=None,
                after_any=None, array=None, max_running=None, allow_slow=None, quiet=False)
    base.update(kw); return __import__("argparse").Namespace(**base)
def _submit3(**kw):
    buf = _io.StringIO()
    with _cl.redirect_stdout(buf):
        hq.cmd_submit(_ns(**kw))
    return buf.getvalue()
def _exits(fn, *a, **k):
    try:
        with _cl.redirect_stdout(_io.StringIO()), _cl.redirect_stderr(_io.StringIO()):
            fn(*a, **k)
    except SystemExit as e:
        return e.code
    return None

# ---- dependencies
json.dump({"version": 1, "next_id": 200, "tasks": {}}, open(hs.QUEUE_FILE, "w"))
_submit3(name="prep", cmd_or_file="true")                      # 200
_submit3(name="train", after="200")                             # 201
_submit3(name="eval", after="201")                              # 202
_submit3(name="cleanup", after_any="201")                       # 203
chk("--after stored", rq()["201"]["depends_on"] == [200] and rq()["203"]["depends_mode"] == "any")
chk("--after an unknown id is refused at submit", _exits(hq.cmd_submit, _ns(after="9999")) == 2)
wdb({"700": node("n1", 72000), "701": node("n2", 72000), "702": node("n3", 72000)})
hs.run_one_cycle(); time.sleep(0.5)
q = rq()
chk("only the head of the chain is dispatched", q["200"]["state"] in ("running", "done")
    and [q[k]["state"] for k in ("201", "202", "203")] == ["pending"] * 3)
chk("the rest say waiting_for_dependency",
    {q[k].get("pending_reason") for k in ("201", "202", "203")} == {"waiting_for_dependency"})
_polls = []; _rr = hs.request_repoll; hs.request_repoll = lambda r: _polls.append(r)
for _ in range(4): hs.run_one_cycle()
chk("tasks held by a dependency never trigger a starvation re-poll", _polls == [] or rq()["201"]["state"] != "pending")
hs.request_repoll = _rr
time.sleep(0.5); hs.run_one_cycle(); time.sleep(0.5); hs.run_one_cycle(); time.sleep(0.5); hs.run_one_cycle()
time.sleep(0.5); hs.run_one_cycle()
chk("chain runs to the end in order", all(rq()[k]["state"] == "done" for k in ("200", "201", "202", "203")))
# failure cascades; --after-any still runs
json.dump({"version": 1, "next_id": 210, "tasks": {}}, open(hs.QUEUE_FILE, "w"))
_submit3(name="boom", cmd_or_file="exit 3")                     # 210
_submit3(name="needs", after="210", notify=_hook)               # 211
_submit3(name="needs2", after="211")                            # 212
_submit3(name="always", after_any="210")                        # 213
clear_notified()
hs.run_one_cycle(); time.sleep(0.6); hs.run_one_cycle(); time.sleep(0.6); hs.run_one_cycle()
q = rq()
chk("dependency failed -> dependant fails without running (exit 125)",
    (q["211"]["state"], q["211"]["exit_code"], q["211"].get("fail_reason"), q["211"].get("node"))
    == ("failed", 125, "dependency_failed", None))
chk("...and the failure cascades down the chain", q["212"]["state"] == "failed"
    and q["212"].get("failed_dependency") == 211)
chk("--after-any runs although its dependency failed", q["213"]["state"] in ("running", "done"))
chk("hook fires for a task that never ran", notified() == ["211 finish failed 125 dependency_failed needs"])
chk("its log says why", "did not end `done`" in open(q["211"]["log"]).read())
# pruned dependency: state comes from the event log
json.dump({"version": 1, "next_id": 222, "tasks": {}}, open(hs.QUEUE_FILE, "w"))
ev.record("finish", task=220, name="old_ok", state="done", run_secs=5)
ev.record("finish", task=221, name="old_bad", state="failed", run_secs=5, exit_code=1)
_submit3(name="after_pruned_ok", after="220")                   # 222
_submit3(name="after_pruned_bad", after="221")                  # 223
hs.run_one_cycle(); time.sleep(0.5)
chk("pruned dependency: final state read from events.jsonl",
    rq()["222"]["state"] in ("running", "done") and rq()["223"].get("fail_reason") == "dependency_failed")

# ---- arrays
json.dump({"version": 1, "next_id": 230, "tasks": {}}, open(hs.QUEUE_FILE, "w"))
_out = _submit3(name="sweep", array="0-5%2", owner="ag",
                cmd_or_file=f'echo "idx=$HIVE_ARRAY_INDEX arr=$HIVE_ARRAY_ID id=$HIVE_TASK_ID"; sleep 1.5')
q = rq()
chk("array creates one task per index, sharing array_id",
    sorted(q) == [str(i) for i in range(230, 236)] and {t["array_id"] for t in q.values()} == {230}
    and [q[str(230 + i)]["array_index"] for i in range(6)] == list(range(6)) and "array #230" in _out)
wdb({str(700 + i): node(f"n{i}", 72000) for i in range(6)})     # six free nodes
hs.run_one_cycle()
q = rq()
chk("%2 -> two running although six nodes are free",
    sum(1 for t in q.values() if t["state"] == "running") == 2
    and sum(1 for t in q.values() if t.get("pending_reason") == "array_limit") == 4)
time.sleep(0.6)
chk("the command sees its index", "idx=0 arr=230 id=230" in open(q["230"]["log"]).read())
chk("hive list shows name[index]", "sweep[3]" in _capture(hq.cmd_list, _ap.Namespace(state=None, all=True, days=None, limit=None, owner="all")))
_exits(hq.cmd_cancel, _ap.Namespace(id=None, array=230))
hs.run_one_cycle()
chk("hive cancel --array cancels every member", {t["state"] for t in rq().values()} == {"cancelled"})

# ---- per-owner cap
json.dump({"version": 1, "next_id": 240, "tasks": {}}, open(hs.QUEUE_FILE, "w"))
for i in range(3): _submit3(name=f"a{i}", owner="greedy", max_running=1, cmd_or_file="sleep 1.5")
_submit3(name="b0", owner="other", cmd_or_file="sleep 1.5")
hs.run_one_cycle()
q = rq()
chk("--max-running 1: one task of that owner runs, the other owner is unaffected",
    [q[k]["state"] for k in ("240", "241", "242", "243")] == ["running", "pending", "pending", "running"]
    and q["241"].get("pending_reason") == "owner_limit")
chk("--max-running without an owner is refused", _exits(hq.cmd_submit, _ns(max_running=2)) == 2)
_submit3(name="s1", allow_slow=True); _a = max(rq().values(), key=lambda t: t["id"])["allow_slow"]
_submit3(name="s2"); _b = max(rq().values(), key=lambda t: t["id"])["allow_slow"]
chk("--allow-slow stored; unset stays None (decided from the estimate)", _a is True and _b is None)
for t in list(rq().values()):
    if t["name"] in ("s1", "s2"): _exits(hq.cmd_cancel, _ap.Namespace(id=t["id"], array=None))
for k in ("240", "243"): _exits(hq.cmd_cancel, _ap.Namespace(id=int(k), array=None))
hs.run_one_cycle()

# ---- wait on several
json.dump({"version": 1, "next_id": 250, "tasks": {}}, open(hs.QUEUE_FILE, "w"))
_submit3(name="w", array="0-1", cmd_or_file="true"); _submit3(name="wbad", cmd_or_file="exit 2")
hs.run_one_cycle(); time.sleep(0.6); hs.run_one_cycle(); time.sleep(0.6); hs.run_one_cycle()
_b = _io.StringIO()
try:
    with _cl.redirect_stdout(_b):
        hq.cmd_wait_many(_ap.Namespace(interval=0.2, pending_timeout=0), [250, 251, 252])
    _rc = None
except SystemExit as e:
    _rc = e.code
chk("hive wait ID ID ID: one line per task, exit 1 when one failed",
    _rc == 1 and "w[0]" in _b.getvalue() and "#252" in _b.getvalue() and "2 done, 1 not" in _b.getvalue())
_b = _io.StringIO()
try:
    with _cl.redirect_stdout(_b):
        hq.cmd_wait_many(_ap.Namespace(interval=0.2, pending_timeout=0), [250, 251])
    _rc = None
except SystemExit as e:
    _rc = e.code
chk("...exit 0 when all ended done, and no log is printed", _rc == 0 and "===" not in _b.getvalue())
open(ev.EVENTS_FILE, "w").close(); wq({}); clear_notified()
reset_health(); reset_sched_state()

print("== node exclusion: hive submit --exclude, hive pool add --exclude ==")
chk("expand_nodes: names, ranges, padding, duplicates",
    hh.expand_nodes("evc22,evc[1-3,07],gpu01,evc22") == ["evc22", "evc1", "evc2", "evc3", "evc07", "gpu01"])
try:
    hh.expand_nodes("evc[1-3"); _bad = False
except ValueError:
    _bad = True
chk("expand_nodes: unbalanced bracket is an error", _bad)
reset_health(); reset_sched_state()
wdb({"700": node("nodeA", 72000)})
wq({"160": dict(task(160, name="x1"), exclude_nodes=["nodeA"]), "161": task(161, name="x2", sub=loc(1))})
hs.run_one_cycle(); time.sleep(0.4)
_q = rq()
chk("excluded node is never used by that task", _q["160"]["state"] == "pending"
    and _q["160"].get("pending_reason") == "node_excluded")
chk("...and stays available to the task behind it", _q["161"].get("node") == "nodeA")
hp = SourceFileLoader("hp", os.path.join(LIB, "hive-pool")).load_module()
_scr = os.path.join(hs.HIVE_DIR, "hold.slurm")
open(_scr, "w").write("#!/bin/bash\n#SBATCH -p normal\n#SBATCH --exclude=evc[1-3],evc9\nsleep 1\n")
chk("script's own #SBATCH --exclude is read", hp.script_excludes(_scr) == ["evc[1-3],evc9"])
data = hh.load(); hh.quarantine(data, "evc43", "t", "agent"); hh.quarantine(data, "evc2", "t", "agent"); hh.save(data)
chk("merged = script + config + CLI + quarantined (no duplicate of evc2)",
    hp.build_exclude(_scr, "evc22", ["evc50", None], True) == ("evc[1-3],evc9,evc50,evc22,evc43", ["evc43"]))
chk("--no-auto-exclude leaves quarantined nodes out",
    hp.build_exclude(_scr, None, [], False) == ("evc[1-3],evc9", []))
reset_health(); reset_sched_state()

print("== context frugality: hive list cap, log tailing ==")
import io, contextlib, argparse as _ap
def _capture(fn, *a):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn(*a)
    return buf.getvalue()
many = {str(i): dict(task(i, name=f"d{i}", state="done"), finished_at=loc(-i), finished_ts=time.time() - i,
                     started_at=loc(-i - 5), started_ts=time.time() - i - 5, exit_code=0, duration_secs=5)
        for i in range(200, 230)}
many["300"] = task(300, name="active")            # pending: always shown
wq(many)
out = _capture(hq.cmd_list, _ap.Namespace(state=None, all=False, days=None, limit=None))
rows = [l for l in out.splitlines() if l.strip().startswith(tuple("0123456789"))]
chk("hive list shows all active + at most LIST_LIMIT_DEFAULT finished rows",
    len(rows) == 1 + hq.LIST_LIMIT_DEFAULT and "+20 more" in out)
out_all = _capture(hq.cmd_list, _ap.Namespace(state=None, all=True, days=None, limit=None))
chk("--all lifts the cap", sum(1 for l in out_all.splitlines() if l.strip().startswith(tuple("0123456789"))) == 31)
out_l = _capture(hq.cmd_list, _ap.Namespace(state="done", all=False, days=None, limit=3))
chk("--limit N applies to --state filters too",
    sum(1 for l in out_l.splitlines() if l.strip().startswith(tuple("0123456789"))) == 3)
_big = os.path.join(hs.LOG_DIR, "big.log")
open(_big, "w").write("=== hive task #1 started ===\n=== cmd: x ===\n" + "".join(f"line {i}\n" for i in range(1000)) + "=== finished exit_code=0 ===\n")
out = _capture(hq.print_log, _big, None, False, 1)
chk("long log is tailed (header kept + omission notice + last lines)",
    out.startswith("=== hive task #1") and "lines omitted" in out and out.rstrip().endswith("exit_code=0 ===")
    and len(out.splitlines()) <= hq.LOG_TAIL_DEFAULT + 4)
chk("short log printed whole", _capture(hq.print_log, os.path.join(hs.LOG_DIR, "task-90.log"), None, False, 90).count("lines omitted") == 0)
chk("--full prints everything", len(_capture(hq.print_log, _big, None, True, 1).splitlines()) == 1003)

print("== owner tag: --owner / #HIVE owner= / $HIVE_OWNER, hive list --owner ==")
os.environ.pop("HIVE_OWNER", None)
wq({})
def _submit(**kw):
    ns = _ap.Namespace(cmd_or_file="true", workdir=None, priority=None, name=kw.get("name"),
                       owner=kw.get("owner"), need_mb=None, gpus=None, est_runtime=None,
                       exclude=kw.get("exclude"))
    _capture(hq.cmd_submit, ns)
    return max(rq().values(), key=lambda t: t["id"])
chk("--owner stored on the task", _submit(owner="agentA", name="a1")["owner"] == "agentA")
chk("--exclude stored expanded on the task",
    _submit(owner="agentA", name="a0", exclude="evc[42-43]")["exclude_nodes"] == ["evc42", "evc43"])
os.environ["HIVE_OWNER"] = "projB"
chk("$HIVE_OWNER used when --owner absent", _submit(name="b1")["owner"] == "projB")
chk("--owner beats $HIVE_OWNER", _submit(owner="agentA", name="a2")["owner"] == "agentA")
open(os.path.join(hs.HIVE_DIR, "o.hive"), "w").write("#HIVE owner=projC\n#HIVE name=c1\necho hi\n")
chk("#HIVE owner= parsed", hq.parse_hive_file(os.path.join(hs.HIVE_DIR, "o.hive")).get("owner") == "projC")
def _rows(ns):
    return [l for l in _capture(hq.cmd_list, ns).splitlines() if l.strip().startswith(tuple("0123456789"))]
chk("hive list defaults to $HIVE_OWNER's tasks", len(_rows(_ap.Namespace(state=None, all=False, days=None, limit=None, owner=None))) == 1)
chk("--owner NAME filters explicitly", len(_rows(_ap.Namespace(state=None, all=False, days=None, limit=None, owner="agentA"))) == 3)
out_all = _capture(hq.cmd_list, _ap.Namespace(state=None, all=False, days=None, limit=None, owner="all"))
chk("--owner all shows everyone, with an OWNER column", "OWNER" in out_all and "agentA" in out_all and "projB" in out_all)
os.environ.pop("HIVE_OWNER", None)
chk("without $HIVE_OWNER the list is unfiltered", len(_rows(_ap.Namespace(state=None, all=False, days=None, limit=None, owner=None))) == 4)
chk("submit event carries owner", any(e["event"] == "submit" and e.get("owner") == "agentA" for e in ev.iter_events()))

print("== integration: history_estimate from event log (P90) ==")
open(ev.EVENTS_FILE, "w").close()
for d in (600, 900, 1200):
    ev.record("finish", task=hash(d) % 9999, name="bench2", state="done", run_secs=d)
est, n = hq.history_estimate("bench2")
chk("auto estimate = P90 of real history",
    est == hq._percentile([600, 900, 1200], 90) and n == 3)

print(f"\nPYTHON SUITE: {P['pass']} passed, {P['fail']} failed")
sys.exit(1 if P["fail"] else 0)
