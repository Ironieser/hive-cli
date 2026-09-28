"""hive_autoscale — keep the pool at a wanted size (checklist B1).

Hold jobs expire; nothing replaced them, so a week after `hive pool add` the pool was
empty again, tasks sat at `pool_empty`, and the agents went back to sbatch. With

    "autoscale": {"enabled": true, "preset": "highgpu", "min_nodes": 6, "max_nodes": 8,
                  "time": "7-00:00:00", "renew_before": "12h", "until": "2026-10-12"}

in pool_config.json the scheduler submits hold jobs (through `hive pool add`, so with
its exclusions and log paths) whenever fewer than `min_nodes` are usable.

  usable = running hold jobs that are not on a quarantined or slow node and have more
           than `renew_before` of walltime left, plus the ones waiting in the SLURM queue

It spends allocation on its own, so it is bounded four ways: `max_nodes` hold jobs in
total (running + queued, usable or not), MAX_PER_RUN per decision, MAX_PER_DAY per 24 h,
and `until`, after which it stops by itself. `plan()` is pure; `run()` acts on it.
"""

import json
import os
import subprocess
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import hive_health as hh

HIVE_DIR   = os.environ.get("HIVE_DIR", os.path.expanduser("~/.hive"))
STATE_FILE = os.path.join(HIVE_DIR, "autoscale_state.json")
POOL_BIN   = os.path.join(os.path.dirname(os.path.realpath(__file__)), "hive-pool")

EVERY_SECS   = 600
MAX_PER_RUN  = 2
MAX_PER_DAY  = 12
SUBMIT_TIMEOUT = 120


def _secs(text, default):
    """'12h' / '90m' / '2d' / '3600' → seconds."""
    try:
        text = str(text).strip().lower()
        if text[-1:] in "smhd":
            return float(text[:-1]) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[text[-1]]
        return float(text)
    except (ValueError, IndexError, TypeError):
        return default


def settings():
    """The "autoscale" block of pool_config.json with defaults, or None if it is off,
    incomplete, or past its `until`."""
    try:
        with open(os.path.join(HIVE_DIR, "pool_config.json")) as f:
            cfg = json.load(f)
        a = cfg.get("autoscale") or {}
    except (OSError, json.JSONDecodeError, AttributeError):
        return None
    if not a.get("enabled"):
        return None
    try:
        out = {"preset": str(a.get("preset") or cfg.get("default") or ""),
               "min_nodes": int(a["min_nodes"]),
               "max_nodes": int(a.get("max_nodes", a["min_nodes"])),
               "time": str(a.get("time") or ""),
               "renew_before": _secs(a.get("renew_before", "12h"), 12 * 3600),
               "until": a.get("until")}
    except (KeyError, TypeError, ValueError):
        return None
    if not out["preset"] or out["min_nodes"] < 1 or out["max_nodes"] < out["min_nodes"]:
        return None
    if out["until"]:
        try:
            end = datetime.fromisoformat(str(out["until"]))
            if len(str(out["until"])) <= 10:
                end = end.replace(hour=23, minute=59, second=59)
            if time.time() > end.timestamp():
                return None
        except ValueError:
            return None                      # unreadable limit: do nothing rather than for ever
    return out


def load_state():
    try:
        with open(STATE_FILE) as f:
            s = json.load(f)
        return s if isinstance(s, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state):
    tmp = STATE_FILE + f".{os.getpid()}.tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, STATE_FILE)


def plan(cfg, jobs_db, db_age, pending_jobs, unusable_nodes, submitted, now=None):
    """How many hold jobs to submit now, and why: (n, text).

    jobs_db        the node DB's jobs (every running hold job)
    db_age         seconds since the DB was written (walltime left is as of then)
    pending_jobs   number of hold jobs waiting in the SLURM queue, or None if unknown
    unusable_nodes nodes that are quarantined or slow
    submitted      epochs of autoscale's own earlier submissions
    """
    now = now if now is not None else time.time()
    if pending_jobs is None:
        return 0, "SLURM could not be asked for queued hold jobs"
    usable = 0
    for info in jobs_db.values():
        if info.get("node") in unusable_nodes or info.get("status") == "cpu":
            continue
        left = info.get("time_left_secs")
        if left is None or int(left) < 0 or int(left) - db_age > cfg["renew_before"]:
            usable += 1
    usable += pending_jobs
    total = len(jobs_db) + pending_jobs
    lately = [t for t in submitted if now - t < 86400]
    want = cfg["min_nodes"] - usable
    if want <= 0:
        return 0, f"{usable} usable of {cfg['min_nodes']} wanted"
    room = cfg["max_nodes"] - total
    if room <= 0:
        return 0, (f"{usable} usable of {cfg['min_nodes']} wanted, but {total} hold jobs exist "
                   f"(max_nodes {cfg['max_nodes']}): release the unusable ones")
    if len(lately) >= MAX_PER_DAY:
        return 0, f"{len(lately)} hold jobs submitted in the last 24 h (limit {MAX_PER_DAY})"
    n = min(want, room, MAX_PER_RUN, MAX_PER_DAY - len(lately))
    return n, f"{usable} usable of {cfg['min_nodes']} wanted, {total} in total"


def run(jobs_db, db_age, unusable_nodes, log=None, now=None):
    """Decide and act. Returns (submitted, text). Does its own SLURM calls: run it in
    a thread, not under queue.lock."""
    now = now if now is not None else time.time()
    cfg = settings()
    if cfg is None:
        return 0, "off"
    state = load_state()
    if now - float(state.get("last_run") or 0) < EVERY_SECS:
        return 0, "not due"
    state["last_run"] = now
    pending = hh.pending_hold_jobs()
    n, why = plan(cfg, jobs_db, db_age, None if pending is None else len(pending),
                  unusable_nodes, state.get("submitted") or [], now)
    state["last_decision"] = f"{n}: {why}"
    done = 0
    if n > 0:
        argv = [sys.executable or "python3", POOL_BIN, "add", cfg["preset"], "--count", str(n)]
        if cfg["time"]:
            argv += ["--time", cfg["time"]]
        try:
            res = subprocess.run(argv, capture_output=True, text=True, timeout=SUBMIT_TIMEOUT)
            done = (res.stdout or "").count("Submitted batch job")
            if res.returncode != 0 or done == 0:
                why += " — hive pool add failed: " + ((res.stderr or res.stdout or "").strip()[-200:])
        except (subprocess.TimeoutExpired, OSError) as e:
            why += f" — hive pool add failed: {e}"
        state["submitted"] = [t for t in (state.get("submitted") or []) if now - t < 86400] \
            + [now] * done
    try:
        save_state(state)
    except OSError:
        pass
    if log and (done or n):
        log("Autoscale: submitted %d hold job(s) [%s] — %s", done, cfg["preset"], why)
    return done, why
