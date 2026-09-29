"""hive_autoscale — keep the pool at a wanted size (checklist B1).

Hold jobs expire; nothing replaced them, so a week after `hive pool add` the pool was
empty again, tasks sat at `pool_empty`, and the agents went back to sbatch. With

    "autoscale": {"enabled": true, "preset": "highgpu", "min_nodes": 6, "max_nodes": 8,
                  "time": "7-00:00:00", "renew_before": "12h", "until": "2026-10-12"}

in pool_config.json the scheduler submits hold jobs (through `hive pool add`, so with
its exclusions and log paths) whenever fewer than `min_nodes` are usable.

  usable = hold jobs that run on a node that is neither quarantined nor slow and have
           more than `renew_before` of walltime left, plus the ones still queued

It spends allocation with nobody watching, so it is built to do NOTHING whenever it is
not sure:

  * It counts hold jobs by asking SLURM (`observe`), never from the node DB — the DB
    is empty when the poller is down and minutes behind when it is not, and both made
    it submit past `max_nodes`. A hold job is a job of the user whose stdout is under
    pool-logs/; the user's other jobs are not counted and not touched.
  * If SLURM cannot be asked, if its own state file cannot be read or WRITTEN, or if
    the configuration is not exactly valid, it submits nothing.
  * What it is about to submit is written to the state file BEFORE it submits, and
    also kept in memory, so no failure afterwards can make it forget.
  * Bounds: `max_nodes` hold jobs in total (usable or not), MAX_PER_RUN per decision,
    MAX_PER_DAY per 24 h, one decision per EVERY_SECS, and `until`, which is required.
  * `"active_within": "48h"` ties it to use: hold jobs are only submitted while somebody
    has submitted a task in that time. A pool nobody uses runs out by itself, and the
    first task submitted afterwards brings it back at the next decision.
"""

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import hive_health as hh

HIVE_DIR   = os.environ.get("HIVE_DIR", os.path.expanduser("~/.hive"))
STATE_FILE = os.path.join(HIVE_DIR, "autoscale_state.json")
POOL_BIN   = os.path.join(os.path.dirname(os.path.realpath(__file__)), "hive-pool")

EVERY_SECS     = 600
MAX_PER_RUN    = 2
MAX_PER_DAY    = 12
MAX_NODES_CAP  = 64          # no configuration may ask for more hold jobs than this
SUBMIT_TIMEOUT = 120

# What this process did, whatever happens to the state file.
_mem = {"last_run": 0.0, "submitted": []}


def _secs(text):
    """'12h' / '90m' / '2d' / '3600' / '7-00:00:00' → seconds, or None."""
    text = str(text).strip().lower()
    m = re.fullmatch(r"(?:(\d+)-)?(\d+):(\d{2}):(\d{2})", text)
    if m:
        d, h, mi, s = (int(x or 0) for x in m.groups())
        return d * 86400 + h * 3600 + mi * 60 + s
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([smhd]?)", text)
    if not m:
        return None
    return float(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]


def check_settings():
    """(settings or None, why it is off). Anything that is not exactly valid is off:
    `"enabled": "false"` used to switch it ON."""
    try:
        with open(os.path.join(HIVE_DIR, "pool_config.json")) as f:
            cfg = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None, "pool_config.json cannot be read"
    a = cfg.get("autoscale") if isinstance(cfg, dict) else None
    if not isinstance(a, dict):
        return None, 'no "autoscale" block in pool_config.json'
    if a.get("enabled") is not True:
        return None, '"enabled" is not true (it must be the JSON value true)'
    preset = a.get("preset") or cfg.get("default")
    if not isinstance(preset, str) or preset not in (cfg.get("presets") or {}):
        return None, f'preset "{preset}" is not defined under "presets"'
    lo, hi = a.get("min_nodes"), a.get("max_nodes")
    if isinstance(lo, bool) or not isinstance(lo, int) or lo < 1:
        return None, '"min_nodes" must be a whole number >= 1'
    if hi is None:
        hi = lo + 2          # room to replace the ones that are about to expire
    if isinstance(hi, bool) or not isinstance(hi, int) or hi < lo or hi > MAX_NODES_CAP:
        return None, f'"max_nodes" must be a whole number from min_nodes to {MAX_NODES_CAP}'
    wall = _secs(a.get("time")) if a.get("time") else None
    if a.get("time") and not wall:
        return None, f'"time" "{a.get("time")}" cannot be read'
    renew = _secs(a.get("renew_before", "12h"))
    if renew is None or renew < 0:
        return None, '"renew_before" cannot be read'
    if wall and renew >= wall:
        return None, ('"renew_before" is not shorter than "time": every new hold job would '
                      'count as about to expire')
    until = a.get("until")
    if not until or not isinstance(until, str):
        return None, '"until" (a date) is required: autoscale never runs without an end'
    try:
        end = datetime.fromisoformat(until)
        if len(until) <= 10:
            end = end.replace(hour=23, minute=59, second=59)
    except ValueError:
        return None, f'"until" "{until}" is not a date'
    if time.time() > end.timestamp():
        return None, f'"until" {until} has passed'
    active = None
    if a.get("active_within") not in (None, "", 0):
        active = _secs(a.get("active_within"))
        if not active or active <= 0:
            return None, f'"active_within" "{a.get("active_within")}" cannot be read'
    return {"preset": preset, "min_nodes": lo, "max_nodes": hi, "time": str(a.get("time") or ""),
            "renew_before": renew, "until": until, "active_within": active}, None


def settings():
    return check_settings()[0]


def load_state():
    """The state, {} if there is none yet, None if the file exists and cannot be
    trusted — then nothing is submitted, for a lost state is a lost daily limit."""
    if not os.path.exists(STATE_FILE):
        return {"last_run": 0.0, "submitted": []}
    try:
        with open(STATE_FILE) as f:
            s = json.load(f)
        if not isinstance(s, dict):
            return None
        s["last_run"] = float(s.get("last_run") or 0)
        s["submitted"] = [float(t) for t in (s.get("submitted") or [])]
        return s
    except (OSError, ValueError, TypeError):
        return None


def save_state(state):
    tmp = STATE_FILE + f".{os.getpid()}.tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, STATE_FILE)


def _left_secs(text):
    """squeue %L → seconds; -1 unlimited; None unknown."""
    text = (text or "").strip()
    if text.upper() in ("UNLIMITED", "INFINITE"):
        return -1
    m = re.fullmatch(r"(?:(\d+)-)?(?:(\d+):)?(\d+):(\d{2})", text)
    if not m:
        return None
    d, h, mi, s = (int(x or 0) for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + s


def observe():
    """The hold jobs as SLURM sees them now: [{jid, state, node, left}], or None if
    SLURM could not be asked. Only jobs whose stdout is under pool-logs/."""
    out = hh._slurm(["squeue", "-h", "-u", os.environ.get("USER", ""), "-t", "R,PD",
                     "-o", "%i|%T|%L|%N|%b"])
    if out is None:
        return None
    jobs = []
    for line in out.splitlines():
        parts = line.strip().split("|")
        if len(parts) < 4 or not parts[0].isdigit():
            continue
        if len(parts) > 4 and parts[4].strip() and "gpu" not in parts[4]:
            continue                             # a hold job without a GPU: not what
                                                 # min_nodes / max_nodes count
        info = hh._slurm(["scontrol", "show", "job", parts[0]])
        if info is None:
            return None                          # cannot tell what it is: do not guess
        m = re.search(r"^\s*StdOut=(\S+)", info, flags=re.M)
        if not m or not m.group(1).startswith(hh.POOL_LOG_DIR + os.sep):
            continue
        jobs.append({"jid": parts[0], "state": parts[1].upper(), "node": parts[3],
                     "left": _left_secs(parts[2])})
    return jobs


EVENTS_FILE   = os.path.join(HIVE_DIR, "events.jsonl")
QUEUE_FILE    = os.path.join(HIVE_DIR, "queue.json")
IGNORE_OWNERS = ("hive-selftest",)     # hive's own checks are not somebody using it
TAIL_BYTES    = 512 * 1024


def last_submit():
    """Epoch of the latest task submission, or None if there is none on record (or the
    record cannot be read: then nothing is assumed to have happened). Pending and
    running tasks count as of now — they are use, however long ago they were queued."""
    latest = None
    try:
        with open(QUEUE_FILE) as f:
            tasks = json.load(f).get("tasks", {})
        for t in tasks.values():
            if t.get("owner") in IGNORE_OWNERS:
                continue
            if t.get("state") in ("pending", "running"):
                return time.time()
            ts = t.get("submitted_ts")
            if isinstance(ts, (int, float)) and (latest is None or ts > latest):
                latest = float(ts)
    except (OSError, ValueError, AttributeError):
        pass
    try:
        with open(EVENTS_FILE, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - TAIL_BYTES))
            lines = f.read().decode("utf-8", errors="replace").splitlines()[1:]
        for line in reversed(lines):
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get("event") == "submit" and e.get("owner") not in IGNORE_OWNERS \
                    and isinstance(e.get("t"), (int, float)):
                if latest is None or e["t"] > latest:
                    latest = float(e["t"])
                break
    except OSError:
        pass
    return latest


def plan(cfg, jobs, unusable_nodes, submitted, now=None):
    """How many hold jobs to submit now, and why: (n, text). `jobs` is observe()'s
    answer (None = SLURM could not be asked); `submitted` the epochs of autoscale's own
    earlier submissions."""
    now = now if now is not None else time.time()
    if jobs is None:
        return 0, "SLURM could not be asked for the hold jobs"
    usable = 0
    for j in jobs:
        if j["state"] == "PENDING":
            usable += 1
        elif j["state"] == "RUNNING" and j["node"] not in unusable_nodes \
                and (j["left"] is None or j["left"] < 0 or j["left"] > cfg["renew_before"]):
            usable += 1
    total = len(jobs)
    lately = [t for t in submitted if now - t < 86400]
    want = cfg["min_nodes"] - usable
    if want <= 0:
        return 0, f"{usable} usable of {cfg['min_nodes']} wanted"
    room = cfg["max_nodes"] - total
    if room <= 0:
        return 0, (f"{usable} usable of {cfg['min_nodes']} wanted, but {total} hold jobs exist "
                   f"(max_nodes {cfg['max_nodes']}): the unusable ones take the room — "
                   f"`hive nodes` shows them, `hive pool release` frees them")
    if len(lately) >= MAX_PER_DAY:
        return 0, f"{len(lately)} hold jobs submitted in the last 24 h (limit {MAX_PER_DAY})"
    n = min(want, room, MAX_PER_RUN, MAX_PER_DAY - len(lately))
    return n, f"{usable} usable of {cfg['min_nodes']} wanted, {total} in total"


def decide(unusable_nodes, now=None):
    """What a run would do, without doing it: (n, text, cfg, state, jobs)."""
    now = now if now is not None else time.time()
    cfg, off = check_settings()
    if cfg is None:
        return 0, f"off: {off}", None, None, None
    state = load_state()
    if state is None:
        return 0, f"{STATE_FILE} cannot be read — not submitting until it is repaired or removed", \
            cfg, None, None
    last = max(state["last_run"], _mem["last_run"])
    if now - last < EVERY_SECS:
        return 0, f"not due (next decision in {int(EVERY_SECS - (now - last))}s)", cfg, state, None
    if cfg.get("active_within"):
        seen = last_submit()
        if seen is None or now - seen > cfg["active_within"]:
            ago = "never" if seen is None else f"{(now - seen) / 3600:.0f}h ago"
            return 0, (f"idle: the last task was submitted {ago}, and the pool is only kept "
                       f"while hive was used in the last {cfg['active_within'] / 3600:g}h"), \
                cfg, state, []
    jobs = observe()
    submitted = sorted(set(state["submitted"]) | set(_mem["submitted"]))
    n, why = plan(cfg, jobs, unusable_nodes, submitted, now)
    return n, why, cfg, state, jobs


def run(unusable_nodes, log=None, now=None):
    """Decide and act. Returns (submitted, text). Asks SLURM and may run
    `hive pool add`: call it from a thread, not under queue.lock."""
    now = now if now is not None else time.time()
    n, why, cfg, state, jobs = decide(unusable_nodes, now)
    if cfg is None or state is None or why.startswith("not due"):
        return 0, why
    submitted = sorted(t for t in set(state["submitted"]) | set(_mem["submitted"]) if now - t < 86400)
    _mem["last_run"] = now
    state.update(last_run=now, last_decision=f"{n}: {why}", submitted=submitted + [now] * n)
    _mem["submitted"] = list(state["submitted"])
    try:
        save_state(state)              # BEFORE submitting: what cannot be recorded is not done
    except OSError as e:
        return 0, f"state cannot be written ({e}) — not submitting"
    if n <= 0:
        return 0, why
    before = len(jobs or [])
    argv = [sys.executable or "python3", POOL_BIN, "add", cfg["preset"], "--count", str(n)]
    if cfg["time"]:
        argv += ["--time", cfg["time"]]
    said, trouble = 0, ""
    try:
        res = subprocess.run(argv, capture_output=True, text=True, timeout=SUBMIT_TIMEOUT)
        said = (res.stdout or "").count("Submitted batch job")
        if res.returncode != 0 or said == 0:
            trouble = " — hive pool add: " + ((res.stderr or res.stdout or "").strip()[-200:] or "failed")
    except (subprocess.TimeoutExpired, OSError) as e:
        trouble = f" — hive pool add: {e}"
    # How many there really are now, whatever `hive pool add` said or did not get to say.
    after = observe()
    seen = max(0, len(after) - before) if after is not None else n
    done = max(said, seen) if (said or after is not None) else n
    # The n recorded above stay recorded if we cannot tell; otherwise exactly `done`.
    state["submitted"] = submitted + [now] * (done if after is not None else max(done, n))
    _mem["submitted"] = list(state["submitted"])
    try:
        save_state(state)
    except OSError:
        pass                           # the larger figure written before stands
    if log:
        log("Autoscale: submitted %d hold job(s) [%s] — %s%s", done, cfg["preset"], why, trouble)
    return done, why + trouble
