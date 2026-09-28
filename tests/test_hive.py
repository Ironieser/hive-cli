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
os.environ["MOCK_LIVE_GPU"] = "0, 10, 81920\\n0, 40000, 81920"   # live probe sees GPU1 dirty (memory resident, no util)
hs.run_one_cycle()
os.environ.pop("MOCK_LIVE_GPU", None)
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
    hs._probe_backoff.clear(); hs._reject_logged.clear()
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
hs._probe_backoff["700"] = (1, time.time() - 1, "probe_unverifiable")   # backoff elapsed
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
chk("missing nodes are added, present ones are not repeated",
    hh.sync_pending_excludes({"evc2", "evc48"}) == {"5001": ["evc48"], "5002": ["evc2", "evc48"]})
_u = open(_upd).read()
chk("the job's own exclude list is kept", "JobId=5001 ExcNodeList=evc[1-3],evc48" in _u
    and "JobId=5002 ExcNodeList=evc2,evc48" in _u and "5003" not in _u)
os.remove(_upd)
hs._excl_synced = None
d = hh.load(); hh.quarantine(d, "evc48", "wedged", "verify"); d["nodes"]["evc48"]["last_check"] = time.time(); hh.save(d)
wdb({}); wq({})
hs.run_one_cycle(); hs._excl_thread.join(5)
chk("the scheduler pushes the quarantine list in the background", "ExcNodeList=evc[1-3],evc48" in open(_upd).read())
os.remove(_upd); hs.run_one_cycle()
(hs._excl_thread and hs._excl_thread.join(5))
chk("...once, not every cycle", not os.path.exists(_upd))
os.remove(os.path.join(hs.HIVE_DIR, "mock_pending"))
reset_health(); reset_sched_state()

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
hq.cmd_daemon, hq.is_sched_running = _real_daemon, _real_running

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
                after_any=None, array=None, max_running=None, allow_slow=None)
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
