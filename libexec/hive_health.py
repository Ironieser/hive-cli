"""hive_health — self-maintained bad-node list (quarantine) shared by hive-sched and
`hive health`.

Why: several nodes (evc43, evc50 in feedback #13/#15/#16/#20/#23/#28/#29/#30) read
IDLE to nvidia-smi — free memory, no processes — yet every task placed there died at
CUDA init within a minute (`CUDA-capable device(s) is/are busy or unavailable`, `CUDA
unknown error`). The memory-based dirty check can't see that; only creating a real CUDA
context can. So hive keeps a per-PHYSICAL-NODE health list that:

  * an agent can seed  (`hive health report evc43 --reason ...`)  → quarantined at once;
  * the scheduler feeds automatically — a task that fails fast with a CUDA-init
    signature in its log is a *strike* (2 strikes → quarantined); a verify-before-
    dispatch probe whose CUDA context creation fails quarantines immediately;
  * the scheduler re-checks periodically (`HEALTH_CHECK_SECS`) by creating a CUDA
    context through one of the node's hold jobs, and releases the node after
    `HEALTH_OK_STREAK` consecutive healthy checks — so the list heals itself.

File: $HIVE_DIR/node_health.json
{
  "nodes": {
    "evc43": {
      "state": "quarantined" | "ok",
      "reason": "...", "source": "agent" | "auto" | "verify" | "manual",
      "reporter": "user",
      "since": <epoch>, "until": <epoch>,      # `until` is a MINIMUM hold, not an expiry:
      "strikes": 2, "ok_streak": 0,            #   release needs HEALTH_OK_STREAK OK checks
      "last_check": <epoch>, "last_result": "ok" | "fail: ..." | "no_hold_job",
      "history": [ {"t": <epoch>, "event": "...", ...} ]   # last HISTORY_KEEP entries
    }
  }
}

Writers (scheduler cycle, `hive health` CLI) hold queue.lock — the same convention as
events.jsonl — so there is no separate lock. Readers (`hive nodes`, `hive top`) just
read. Keyed by node name because hold jobs come and go while the fault is the node's.
"""

import ctypes  # noqa: F401  (documents that the probe below needs only the stdlib)
import json
import os
import re
import shlex
import subprocess
import sys
import time

HIVE_DIR    = os.environ.get("HIVE_DIR", os.path.expanduser("~/.hive"))
HEALTH_FILE = os.path.join(HIVE_DIR, "node_health.json")

# ── Tuning ────────────────────────────────────────────────────────────────────
FAST_FAIL_SECS        = 180    # a task failing within this after dispatch is "fast"
STRIKES_TO_QUARANTINE = 2      # fast CUDA-signature failures before auto-quarantine
QUARANTINE_MIN_SECS   = 3600   # minimum hold before a healthy check can release
HEALTH_CHECK_SECS     = 600    # how often the scheduler re-probes a quarantined node
HEALTH_OK_STREAK      = 2      # consecutive OK probes needed to release
CUDA_PROBE_TIMEOUT    = 45     # srun + python startup on a cold conda env can be slow
HISTORY_KEEP          = 20
LOG_TAIL_BYTES        = 16384

# Log signatures of "the node's GPU is unusable", as opposed to a bug in the command.
# Matched against the task log tail after a fast failure. Keep these specific: a wrong
# device index or a real OOM must NOT count as a node fault.
CUDA_FAULT_PATTERNS = [
    r"CUDA-capable device\(s\) is/are busy or unavailable",
    r"cudaErrorDevicesUnavailable",
    r"CUDA unknown error",
    r"CUDA_ERROR_UNKNOWN",
    r"CUDA_ERROR_DEVICE_UNAVAILABLE",
    r"CUDA driver initialization failed",
    r"Setting the available devices to be zero",
    r"no CUDA-capable device is detected",
    # NOT "Engine core initialization failed": vLLM prints that for ANY failure while
    # the engine starts — an AssertionError in its CUDA-graph capture, a model that does
    # not fit, a bad argument. On 2026-09-28 seven tasks of one campaign failed that
    # way on healthy nodes, each a strike; two in a row would have quarantined evc102
    # with both its hold jobs. When the node really is at fault the log carries one of
    # the CUDA errors above as well, and verify-before-dispatch creates a CUDA context
    # on the node before any task gets there.
]
_CUDA_FAULT_RE = re.compile("|".join(f"(?:{p})" for p in CUDA_FAULT_PATTERNS))

# ── The probe: create a real CUDA context via libcuda (stdlib ctypes only) ───
# Runs INSIDE `srun --overlap` on the node, under the hold job's cgroup, so it sees
# exactly the GPU a task would get. Prints one line: `CUDA_PROBE ok`,
# `CUDA_PROBE fail <where>=<code>` or `CUDA_PROBE unknown <why>` (can't tell — never
# treated as a fault). Verified live: healthy node → ok; evc43 → cuCtxCreate=999.
CUDA_PROBE_PY = r'''
import ctypes, sys, time
_t0 = time.time()
try:
    lib = ctypes.CDLL("libcuda.so.1")
except OSError:
    print("CUDA_PROBE unknown no_libcuda"); sys.exit(0)
r = lib.cuInit(0)
if r: print(f"CUDA_PROBE fail cuInit={r}"); sys.exit(0)
n = ctypes.c_int(); r = lib.cuDeviceGetCount(ctypes.byref(n))
if r or n.value == 0: print(f"CUDA_PROBE fail cuDeviceGetCount={r} n={n.value}"); sys.exit(0)
dev = ctypes.c_int(); r = lib.cuDeviceGet(ctypes.byref(dev), 0)
if r: print(f"CUDA_PROBE fail cuDeviceGet={r}"); sys.exit(0)
ctx = ctypes.c_void_p(); r = lib.cuCtxCreate_v2(ctypes.byref(ctx), 0, dev)
if r: print(f"CUDA_PROBE fail cuCtxCreate={r}"); sys.exit(0)
p = ctypes.c_void_p(); r = lib.cuMemAlloc_v2(ctypes.byref(p), ctypes.c_size_t(1 << 20))
lib.cuCtxDestroy_v2(ctx)
print(f"CUDA_PROBE ok secs={time.time() - _t0:.0f}" if r == 0 else f"CUDA_PROBE fail cuMemAlloc={r}")
'''.strip("\n")

CUDA_MARKER = "---CUDA---"
CUDA_INIT_DEADLINE = 60     # seconds to create a CUDA context; a healthy node needs 1-3
CUDA_SLOW_DETAIL = "cuda_init_slow"

# ── The GPU query: nvidia-smi under a deadline that survives a hung driver ───
# A node whose NVIDIA driver is wedged (observed on evc22, 2026-09-28: kernel thread
# `nv_open_q` in D state, `nvidia-smi` answering "No devices were found" after 154 s,
# root's own exporter stuck the same way) still runs `srun ... true` in 0.5 s. SLURM
# keeps handing such a GPU out — nobody holds on to it — so hold jobs gravitate to it.
# The old probe was a bare `nvidia-smi` under the caller's srun timeout, so this read
# as "couldn't run the probe" (transient, retry forever): three hold jobs sat 24 h with
# pending tasks while every one of ~870 probe steps per job timed out.
#
# `timeout nvidia-smi` is not enough: a process blocked in the driver is in
# uninterruptible sleep and ignores SIGKILL until the call returns, and `timeout`
# waits for it. So the query runs in the background with its output in a file (it must
# not hold the step's stdout open) and the step stops waiting at the deadline.
GPU_QUERY_DEADLINE = 60     # seconds. Measured 2026-09-28 on normal-partition nodes: a
                            # HEALTHY node can take 24 s to answer (evc45, valid reading),
                            # wedged ones took 116-154 s (evc22/evc29/evc34). 20 s
                            # quarantined evc45 by mistake. Callers need about
                            # deadline + 30 s of srun timeout (step start + teardown).
GPU_MARKER = "GPU_PROBE"
GPU_QUERY_FIELDS = "utilization.gpu,memory.used,memory.total"

# $var = a fresh temp file that is proven writable (its .rc twin too), or empty.
TMPFILE_SH = ('{var}=; for _d in "${{TMPDIR:-}}" /tmp /dev/shm; do '
              '[ -n "$_d" ] && [ -d "$_d" ] || continue; '
              '_t=$(mktemp -p "$_d" hive_probe.XXXXXXXX 2>/dev/null) || continue; '
              'if echo x >"$_t" 2>/dev/null && echo x >"$_t.rc" 2>/dev/null; then '
              ': >"$_t"; rm -f "$_t.rc"; {var}=$_t; break; fi; rm -f "$_t" "$_t.rc"; done; ')


def gpu_query_shell(fields=GPU_QUERY_FIELDS, deadline=None):
    """Shell snippet printing, in order: `GPU_PROBE granted=<CUDA_VISIBLE_DEVICES>`
    (empty = SLURM gave this job no GPU), nvidia-smi's csv lines, then
    `GPU_PROBE rc=<n>` or `GPU_PROBE hung`. Sets $_HIVE_GPU_HUNG when it gave up."""
    if deadline is None:
        deadline = float(os.environ.get("HIVE_GPU_QUERY_DEADLINE", GPU_QUERY_DEADLINE))
    ticks = max(1, int(deadline * 2))
    return (
        f'echo "{GPU_MARKER} granted=${{CUDA_VISIBLE_DEVICES:-}}"; '
        # No predictable fallback name: it would be written through a symlink another
        # user planted. And "cannot write a temp file" (/tmp full, TMPDIR exported from
        # another host) must not read as a GPU that does not answer.
        + TMPFILE_SH.format(var="_o") +
        f'if [ -z "$_o" ]; then echo "{GPU_MARKER} nowrite"; else '
        f'( nvidia-smi --query-gpu={fields} --format=csv,noheader,nounits >"$_o" 2>&1; '
        'echo $? >"$_o.rc" ) </dev/null >/dev/null 2>&1 & '
        f'_i=0; while [ ! -s "$_o.rc" ] && [ "$_i" -lt {ticks} ]; do sleep 0.5; _i=$((_i+1)); done; '
        '_HIVE_GPU_HUNG=; '
        f'if [ -s "$_o.rc" ]; then cat "$_o"; echo "{GPU_MARKER} rc=$(cat "$_o.rc")"; '
        f'else _HIVE_GPU_HUNG=1; echo "{GPU_MARKER} hung"; fi; '
        'rm -f "$_o" "$_o.rc"; fi; '
    )


def parse_gpu_query(lines):
    """(csv_lines, granted, fault) from gpu_query_shell output.

    `fault` names a NODE fault and is only ever set when SLURM granted the job a GPU
    (a CPU-only hold job legitimately sees no device): `gpu_unresponsive` — nvidia-smi
    gave no answer by the deadline; `no_gpu_devices` — it answered but listed none.
    `granted` is None when the probe printed no GPU_PROBE line at all (step never ran)."""
    csv, granted, hung, answered = [], None, False, False
    for ln in lines:
        ln = ln.strip()
        if not ln:
            continue
        if ln.startswith(GPU_MARKER + " "):
            rest = ln[len(GPU_MARKER) + 1:]
            if rest.startswith("granted="):
                granted = rest[len("granted="):].strip()
            elif rest == "hung":
                hung = True
            elif rest == "nowrite":
                return [], granted, None          # could not run: no verdict at all
            elif rest.startswith("rc="):
                answered = True
            continue
        csv.append(ln)
    fault = None
    if granted:
        if hung:
            fault = "gpu_unresponsive"
        elif answered and not any(len(l.split(",")) >= 3 for l in csv):
            fault = "no_gpu_devices"
    return csv, granted, fault


def cuda_probe_shell(deadline=None):
    """Shell snippet that runs CUDA_PROBE_PY on the node. Uses the interpreter this
    process runs (a shared-FS conda env is visible on compute nodes) and falls back to
    the node's python3; a missing interpreter yields `unknown`, never `fail`.

    HIVE_CUDA_PROBE_CMD overrides the whole snippet (offline tests: the mock cluster
    has no GPU, so the real probe would report cuInit=999 and quarantine everything)."""
    override = os.environ.get("HIVE_CUDA_PROBE_CMD")
    if override:
        return override
    # Same background + deadline pattern as the GPU query, for the same reason. It
    # also catches a node that works but crawls: evc45 (2026-09-28) answered nvidia-smi
    # in 24 s and then took 184 s to create a CUDA context. Every CUDA init of every
    # task would pay that, so it is reported as a failure (`cuda_init_slow`), and the
    # 3-minute wait is not spent holding queue.lock.
    py = shlex.quote(sys.executable or "python3")
    if deadline is None:
        deadline = float(os.environ.get("HIVE_CUDA_PROBE_DEADLINE", CUDA_INIT_DEADLINE))
    ticks = max(1, int(float(deadline) * 2))
    return (f"PY={py}; [ -x \"$PY\" ] || PY=python3; "
            + TMPFILE_SH.format(var="_c") +
            'if [ -z "$_c" ]; then echo "CUDA_PROBE unknown no_tmp"; else\n'
            "( \"$PY\" - >\"$_c\" 2>/dev/null <<'HIVE_CUDA_PROBE_EOF'\n"
            f"{CUDA_PROBE_PY}\nHIVE_CUDA_PROBE_EOF\n"
            'echo $? >"$_c.rc" ) >/dev/null 2>&1 &\n'
            f'_i=0; while [ ! -s "$_c.rc" ] && [ "$_i" -lt {ticks} ]; do sleep 0.5; _i=$((_i+1)); done; '
            'if [ ! -s "$_c.rc" ]; then echo "CUDA_PROBE fail '
            f'{CUDA_SLOW_DETAIL}"; '
            'elif [ -s "$_c" ]; then cat "$_c"; else echo "CUDA_PROBE unknown python_failed"; fi; '
            'rm -f "$_c" "$_c.rc"\nfi')


def parse_cuda_probe(lines):
    """('ok'|'fail'|'unknown', detail) from probe output lines."""
    for ln in lines:
        ln = ln.strip()
        if ln.startswith("CUDA_PROBE "):
            parts = ln.split(None, 2)
            verdict = parts[1] if len(parts) > 1 else "unknown"
            detail = parts[2] if len(parts) > 2 else ""
            if verdict not in ("ok", "fail", "unknown"):
                verdict = "unknown"
            return verdict, detail
    return "unknown", "no_probe_output"


def cuda_probe(slurm_jobid, timeout=None):
    """Standalone CUDA-context probe through hold job `slurm_jobid`.
    Returns (verdict, detail) — see parse_cuda_probe; srun failure → ('unknown', ...)."""
    if timeout is None:     # the in-step deadline plus step start and teardown
        timeout = float(os.environ.get("HIVE_CUDA_PROBE_DEADLINE", CUDA_INIT_DEADLINE)) + 45
    try:
        out = subprocess.run(
            ["srun", f"--jobid={slurm_jobid}", "--overlap", "-n1", "--mem=0",
             "bash", "-c", cuda_probe_shell()],
            capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return "unknown", "srun_timeout"
    except OSError as e:
        return "unknown", f"srun_error:{e}"
    if out.returncode != 0 and "CUDA_PROBE" not in (out.stdout or ""):
        return "unknown", f"srun_rc={out.returncode}"
    return parse_cuda_probe((out.stdout or "").splitlines())


def full_probe_shell(cuda_deadline=None, skip_cuda=False):
    """The whole verify probe — guarded GPU query, then the CUDA-context probe (skipped
    when the query hung: it would block in the same driver call). One definition for
    verify-before-dispatch, the periodic check and the canary job."""
    return (gpu_query_shell()
            + f"echo {CUDA_MARKER}; "
            + 'if [ -n "$_HIVE_GPU_HUNG" ]; then echo "CUDA_PROBE unknown gpu_hung"; else\n'
            + ("echo 'CUDA_PROBE unknown skipped'" if skip_cuda else cuda_probe_shell(cuda_deadline))
            + "\nfi")


def cuda_secs(detail):
    """Seconds the CUDA context took, from an `ok secs=N` detail; None if not stated."""
    m = re.search(r"secs=(\d+)", detail or "")
    return int(m.group(1)) if m else None


def parse_full_probe(text):
    """('ok'|'fail'|'unknown', detail) for one full_probe_shell() output."""
    gpu_part, _, cuda_part = (text or "").partition(CUDA_MARKER)
    csv, granted, fault = parse_gpu_query(gpu_part.splitlines())
    if fault:
        return "fail", fault
    if granted is None or not csv:
        return "unknown", "no_gpu_output"
    return parse_cuda_probe(cuda_part.splitlines())


# ── Recovery without a hold job: the health monitor ─────────────────────────
# `hive pool add` keeps new hold jobs off quarantined nodes, so the periodic check —
# which probes THROUGH a hold job — never runs there again and the node would stay
# quarantined for good. Two other ways back:
#
#   1. Reboot: a wedged driver is cured by a reboot. If SLURM reports the node booted
#      after it was quarantined, release it; the first verify probe on it decides.
#   2. Canary: every CANARY_INTERVAL_SECS submit a small batch job pinned to the node
#      that runs the probe twice. Two healthy answers release the node, a failure
#      re-arms the quarantine. At most one canary per node is outstanding.
#
# `"health_canary": false` in pool_config.json (or HIVE_HEALTH_CANARY=0) turns the
# canary off; reboot detection costs one `scontrol` and stays on.
PROBE_DIR            = os.path.join(HIVE_DIR, "health-probes")
CANARY_NAME          = "hive_canary"   # the pollers filter this name out of the pool
CANARY_INTERVAL_SECS = 6 * 3600
CANARY_MAX_WAIT_SECS = 24 * 3600       # still queued after this → cancel, try later
CANARY_TIME          = "00:15:00"   # two probes of up to SLOW_CUDA_DEADLINE + the gap
CANARY_GAP_SECS      = 60              # between the two probes of one canary
CANARY_SEP           = "---HIVE-CANARY-PROBE---"
SLURM_CMD_TIMEOUT    = 15
NODE_DOWN_STATES     = ("DOWN", "DRAIN", "MAINT", "FAIL", "NOT_RESPONDING", "REBOOT")


def canary_enabled():
    if os.environ.get("HIVE_HEALTH_CANARY", "").strip().lower() in ("0", "false", "no", "off"):
        return False
    try:
        with open(os.path.join(HIVE_DIR, "pool_config.json")) as f:
            return json.load(f).get("health_canary", True) is not False
    except (OSError, json.JSONDecodeError, AttributeError):
        return True


def _slurm(argv):
    """stdout of a bounded SLURM command, or None when it could not be run / failed."""
    try:
        out = subprocess.run(argv, capture_output=True, text=True, timeout=SLURM_CMD_TIMEOUT)
    except (subprocess.TimeoutExpired, OSError):
        return None
    return out.stdout if out.returncode == 0 else None


def job_state(jobid):
    """SLURM state of a job still in the queue, "" once it has left it, None when
    squeue could not be asked. A finished job is purged after a few minutes and
    `squeue -j` then FAILS with "Invalid job id specified" — that is "left the
    queue", not "could not ask" (the first canary sat at canary_unknown over this)."""
    try:
        out = subprocess.run(["squeue", "-h", "-j", str(jobid), "-o", "%T"],
                             capture_output=True, text=True, timeout=SLURM_CMD_TIMEOUT)
    except (subprocess.TimeoutExpired, OSError):
        return None
    if out.returncode != 0:
        return "" if "invalid job id" in (out.stderr or "").lower() else None
    return (out.stdout or "").strip().split("\n")[0].strip()


def node_info(node):
    """{State, BootTime (epoch | None), Partitions [..]} from `scontrol show node`."""
    out = _slurm(["scontrol", "show", "node", node])
    if not out:
        return None
    kv = dict(re.findall(r"(\w+)=(\S+)", out))
    if kv.get("NodeName") != node:
        return None
    boot = None
    try:
        # scontrol prints local time in this process's zone, and mktime reads it back
        # in the same one.
        boot = time.mktime(time.strptime(kv.get("BootTime", ""), "%Y-%m-%dT%H:%M:%S"))
    except (ValueError, OverflowError):
        pass
    return {"State": kv.get("State", ""), "BootTime": boot,
            "Partitions": [p for p in kv.get("Partitions", "").split(",") if p]}


def submit_canary(node, partition=None):
    """sbatch the canary on `node`; returns {"jobid", "out"} or None."""
    os.makedirs(PROBE_DIR, exist_ok=True)
    out_pat = os.path.join(PROBE_DIR, f"{node}-%j.out")
    gap = os.environ.get("HIVE_CANARY_GAP", str(CANARY_GAP_SECS))
    # Same patience as the periodic check: a slow node must be able to show that it
    # works, or the canary re-quarantines it every six hours for ever.
    probe = full_probe_shell(cuda_deadline=SLOW_CUDA_DEADLINE)
    script = ("#!/bin/bash\n"
              f'echo "HIVE_CANARY start node=$(hostname -s) job=$SLURM_JOB_ID"\n'
              f"echo {CANARY_SEP}\n{probe}\n"
              f"sleep {shlex.quote(gap)}\n"
              f"echo {CANARY_SEP}\n{probe}\n"
              'echo "HIVE_CANARY done"\n')
    argv = ["sbatch", "--parsable", f"--job-name={CANARY_NAME}", f"--nodelist={node}",
            "--nodes=1", "--ntasks=1", "--cpus-per-task=1", "--mem=4G", "--gres=gpu:1",
            f"--time={CANARY_TIME}", f"--output={out_pat}", f"--error={out_pat}"]
    if partition:
        argv.append(f"--partition={partition}")
    try:
        res = subprocess.run(argv, input=script, capture_output=True, text=True,
                             timeout=SLURM_CMD_TIMEOUT)
    except (subprocess.TimeoutExpired, OSError):
        return None
    jobid = (res.stdout or "").strip().split(";")[0].strip()
    if res.returncode != 0 or not jobid.isdigit():
        return None
    return {"jobid": jobid, "out": out_pat.replace("%j", jobid)}


def read_canary(path):
    """Verdicts [(verdict, detail), ...] of the probes a canary managed to run."""
    try:
        with open(path, errors="replace") as f:
            text = f.read()
    except OSError:
        return []
    return [parse_full_probe(sec) for sec in text.split(CANARY_SEP)[1:]]


def check_without_hold_job(data, node, partition=None, now=None):
    """Periodic check of a quarantined node that has no hold job to probe through.
    Returns 'released' | 'extended' | None, like record_check."""
    now = now if now is not None else time.time()
    rec = _rec(data, node)
    can = rec.get("canary")
    if can:
        state = job_state(can["jobid"])
        if state is None:
            return record_check(data, node, None, "canary_unknown")   # squeue failed: wait
        if state:                                   # still queued / running
            if now - float(can.get("submitted") or now) > CANARY_MAX_WAIT_SECS:
                _slurm(["scancel", str(can["jobid"])])
                rec.pop("canary", None)
                return record_check(data, node, None, "canary_never_started")
            return record_check(data, node, None, f"canary_{state.lower()}")
        rec.pop("canary", None)
        verdicts = read_canary(can.get("out", ""))
        if not verdicts:
            return record_check(data, node, "unknown", "canary: no output")
        outcome = None
        slowest = max((cuda_secs(d) or 0 for v, d in verdicts if v == "ok"), default=0)
        if slowest > CUDA_INIT_DEADLINE and all(v == "ok" for v, _ in verdicts):
            if may_become_slow(rec):
                return "slow" if mark_slow(data, node, slowest) else None
            # Out for a fault and now merely slow: counted towards `slow`, like the
            # check through a hold job does (one canary = one probe of the streak).
            return "slow" if slow_probe(data, node, slowest, now) else None
        for verdict, detail in verdicts:
            outcome = record_check(data, node, verdict, f"canary: {detail}".rstrip(": ")) or outcome
            if outcome == "released":
                break
        return outcome

    info = node_info(node)
    if info is None:
        return record_check(data, node, None, "no_hold_job")
    boot, since = info["BootTime"], float(rec.get("since") or 0)
    if boot and since and boot > since:
        release(data, node, "node rebooted after it was quarantined", "auto")
        rec["last_check"] = now
        return "released"
    if any(s in info["State"].upper() for s in NODE_DOWN_STATES) or info["State"].endswith("*"):
        return record_check(data, node, None, f"node_{info['State'].lower()}")
    if canary_enabled() and now - float(rec.get("last_canary") or 0) >= CANARY_INTERVAL_SECS:
        part = partition if partition in info["Partitions"] else \
            (info["Partitions"][0] if info["Partitions"] else None)
        job = submit_canary(node, part)
        rec["last_canary"] = now            # also on failure: don't retry every 10 min
        if job:
            rec["canary"] = dict(job, submitted=now)
            return record_check(data, node, None, f"canary_submitted {job['jobid']}")
        return record_check(data, node, None, "canary_submit_failed")
    return record_check(data, node, None, "no_hold_job")


# ── Hold jobs that are still queued when a node is quarantined ──────────────
# `pool add` excludes the nodes quarantined at submit time. A hold job that waits in
# the SLURM queue for hours can still be started on a node quarantined in the meantime
# (861821 → evc48, 2026-09-28) and then sits there unused for its whole walltime. So
# the exclusion is pushed onto the queued hold jobs whenever the list grows.
POOL_LOG_DIR = os.path.join(HIVE_DIR, "pool-logs")


def pending_hold_jobs():
    """{jobid: ExcNodeList} of this user's PENDING hold jobs. A hold job is recognised
    by its stdout being under pool-logs/ (`pool add` sets that), so jobs the user
    submitted some other way are never touched. None if SLURM could not be asked."""
    out = _slurm(["squeue", "-h", "-u", os.environ.get("USER", ""), "-t", "PD", "-o", "%i"])
    if out is None:
        return None
    jobs = {}
    for jid in out.split():
        info = _slurm(["scontrol", "show", "job", jid])
        if not info:
            continue
        # Field by field, anchored at the start of a token: a job NAME containing
        # "StdOut=…" must not make a foreign job look like a hold job.
        out_m = re.search(r"^\s*StdOut=(\S+)", info, flags=re.M)
        exc_m = re.search(r"(?:^|\s)ExcNodeList=(\S+)", info, flags=re.M)
        if not out_m or not out_m.group(1).startswith(POOL_LOG_DIR + os.sep):
            continue
        exc = exc_m.group(1) if exc_m else ""
        jobs[jid] = "" if exc in ("(null)", "") else exc
    return jobs


EXCL_FILE = os.path.join(HIVE_DIR, "pool_excludes.json")   # {jobid: [nodes hive added]}
NODE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")


def valid_node_name(name):
    return bool(NODE_NAME_RE.match(str(name or "")))


class ExcludesLock:
    """Serialises read-modify-write of pool_excludes.json between `hive pool add` and
    the scheduler's sync thread."""
    def __enter__(self):
        import fcntl
        os.makedirs(HIVE_DIR, exist_ok=True)
        self._fd = open(EXCL_FILE + ".lock", "w")
        fcntl.flock(self._fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *_):
        import fcntl
        fcntl.flock(self._fd, fcntl.LOCK_UN)
        self._fd.close()


EXCL_KEEP_SECS = 900     # an entry this young is kept even if its job is not (yet)
                         # in a squeue listing: the listing may predate the job


def track_excludes(jobid, nodes):
    """Record that hive put `nodes` on job `jobid`'s exclude list."""
    with ExcludesLock():
        tracked = load_hive_excludes()
        tracked[str(jobid)] = {"nodes": sorted(nodes), "t": time.time()}
        save_hive_excludes(tracked)


def hive_excludes():
    """{jobid: [nodes hive added]} — the file without its timestamps."""
    return {j: list(v["nodes"]) for j, v in load_hive_excludes().items()}


def load_hive_excludes():
    try:
        with open(EXCL_FILE) as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        out = {}
        for k, v in data.items():
            if isinstance(v, dict):
                out[str(k)] = {"nodes": list(v.get("nodes") or []), "t": float(v.get("t") or 0)}
            else:
                out[str(k)] = {"nodes": list(v or []), "t": 0.0}
        return out
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return {}


def save_hive_excludes(data):
    tmp = EXCL_FILE + f".{os.getpid()}.tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, EXCL_FILE)


def sync_pending_excludes(nodes):
    """Make every queued hold job exclude exactly: what its script and the user asked
    for, plus `nodes` (the quarantine list now). What hive added earlier is tracked in
    pool_excludes.json, so a node that left quarantine — or turned out to be slow, not
    broken — comes OFF the list again; add-only lost such nodes for every hold job
    that was waiting in the SLURM queue at the time.

    Returns {jobid: (added, removed)} plus "_failed": True if SLURM refused an update;
    None if SLURM could not be asked."""
    jobs = pending_hold_jobs()
    if jobs is None:
        return None
    want = {n for n in nodes if valid_node_name(n)}
    tracked = load_hive_excludes()
    known_at_start = set(tracked)
    updates = {}
    done = {}
    for jid, exc in jobs.items():
        try:
            have = expand_nodes(exc)
        except ValueError:
            done["_failed"] = True           # a list we cannot read: do not touch it
            continue
        ours = set(tracked.get(jid, {}).get("nodes", []))
        base = [n for n in have if n not in ours]          # the job's own exclusions
        target = base + sorted(want - set(base))
        if set(target) == set(have):
            updates[jid] = sorted(want - set(base))
            continue
        if _slurm(["scontrol", "update", f"JobId={jid}",
                   f"ExcNodeList={','.join(target)}"]) is None:
            done["_failed"] = True
            continue
        updates[jid] = sorted(want - set(base))
        done[jid] = (sorted(set(target) - set(have)), sorted(set(have) - set(target)))
    # Written back on top of the file as it is NOW, not as it was read: `hive pool add`
    # may have recorded a new job meanwhile, and only entries this run knew about and
    # no longer finds in the queue are dropped.
    try:
        with ExcludesLock():
            fresh = load_hive_excludes()
            for jid, nodes in updates.items():
                fresh[jid] = {"nodes": nodes, "t": fresh.get(jid, {}).get("t") or time.time()}
            for jid in known_at_start:
                if jid not in jobs and time.time() - fresh.get(jid, {}).get("t", 0) > EXCL_KEEP_SECS:
                    fresh.pop(jid, None)             # started or gone
            save_hive_excludes(fresh)
    except OSError:
        pass
    return done


# ── Node lists (`hive pool add --exclude`, `hive submit --exclude`) ──────────
EXPAND_NODES_MAX = 4096     # names one list may expand to


def expand_nodes(spec):
    """Node names from a SLURM-style list: 'evc22,evc[1-3,07],gpu01' →
    ['evc22', 'evc1', 'evc2', 'evc3', 'evc07', 'gpu01']. Order kept, duplicates
    dropped, zero padding preserved. Entries may also be separated by spaces. Raises
    ValueError on anything it cannot read, on a reversed range, and on a list of more
    than EXPAND_NODES_MAX names (checked before expanding: `evc[1-999999999]`)."""
    spec = re.sub(r"\s+", ",", str(spec or "").strip())
    out, seen = [], set()
    parts, depth, cur = [], 0, ""
    for ch in (spec or ""):
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth < 0:
                raise ValueError(f"unbalanced ']' in node list: {spec}")
        if ch == "," and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    if depth:
        raise ValueError(f"unbalanced '[' in node list: {spec}")
    parts.append(cur)
    for part in parts:
        part = part.strip()
        if not part:
            continue
        m = re.fullmatch(r"([^\[\]]*)\[([^\[\]]+)\]([^\[\]]*)", part)
        names = []
        if not m:
            if "[" in part or "]" in part:
                raise ValueError(f"cannot parse node list entry: {part}")
            names = [part]
        else:
            pre, body, post = m.groups()
            for rng in body.split(","):
                rng = rng.strip()
                lo, _, hi = rng.partition("-")
                if not lo.isdigit() or (hi and not hi.isdigit()):
                    raise ValueError(f"cannot parse node range: {part}")
                hi = hi or lo
                if int(hi) < int(lo):
                    raise ValueError(f"reversed node range: {part}")
                if len(out) + len(names) + int(hi) - int(lo) + 1 > EXPAND_NODES_MAX:
                    raise ValueError(f"node list too long (over {EXPAND_NODES_MAX} names): {spec[:40]}")
                for n in range(int(lo), int(hi) + 1):
                    names.append(f"{pre}{str(n).zfill(len(lo))}{post}")
        for n in names:
            if not valid_node_name(n):
                raise ValueError(f"not a node name: '{n}'")
            if n not in seen:
                seen.add(n)
                out.append(n)
    return out


# ── Log classification ───────────────────────────────────────────────────────
def classify_log_tail(log_path, tail_bytes=LOG_TAIL_BYTES):
    """The CUDA-fault signature found in the tail of a task log, or None."""
    try:
        with open(log_path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - tail_bytes))
            tail = f.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    m = _CUDA_FAULT_RE.search(tail)
    return m.group(0) if m else None


# ── State file ───────────────────────────────────────────────────────────────
def load():
    try:
        with open(HEALTH_FILE) as f:
            data = json.load(f)
        if not isinstance(data.get("nodes"), dict):
            data["nodes"] = {}
        return data
    except (OSError, json.JSONDecodeError):
        return {"nodes": {}}


def save(data):
    os.makedirs(HIVE_DIR, exist_ok=True)
    tmp = HEALTH_FILE + f".{os.getpid()}.tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, HEALTH_FILE)


# ── Slow nodes ───────────────────────────────────────────────────────────────
# Some nodes are not broken, only slow to start: evc48 (2026-09-28) needed 146 s from
# `torch.cuda.init()` to the first tensor — evc104 needs 4 — and then ran at a steady
# rate. Useless for a two-minute debug run, fine for a six-hour training. Such a node
# is `slow`, not `quarantined`: it takes tasks that said they can live with it
# (`--allow-slow`, or a runtime estimate of SLOW_OK_MIN_EST_SECS or more) and only
# after every faster node was considered.
SLOW_CUDA_DEADLINE   = 300     # the periodic check waits this long for a context
SLOW_OK_MIN_EST_SECS = 3600    # est. runtime from which a slow start stops mattering


def slow_nodes(data=None):
    data = data if data is not None else load()
    return {n for n, r in data.get("nodes", {}).items() if r.get("state") == "slow"}


def may_become_slow(rec):
    """Only a node that hive itself put aside for being slow may be reclassified from
    one probe. Anything else — an agent's report, CUDA errors, a GPU that did not
    answer — is a real fault: it keeps its minimum hold and needs HEALTH_OK_STREAK
    healthy probes, however long the context took to appear on one lucky try."""
    if rec.get("state") == "slow":
        return True
    return rec.get("state") == "quarantined" and rec.get("source") in ("verify", "auto") \
        and CUDA_SLOW_DETAIL in str(rec.get("reason") or "")


def slow_probe(data, node, secs, now=None):
    """A node that is out for a FAULT created a CUDA context, slowly. Counted towards
    `slow` the way healthy probes are counted towards a release: HEALTH_OK_STREAK of
    them in a row, and not before the minimum hold is over. Without this a slow node
    that failed one probe (evc48: slow → one unanswered probe → quarantined) could
    never be slow again, only fast or out. A node an agent or a person put away is
    theirs to release. Returns True when the node just became `slow`."""
    now = now if now is not None else time.time()
    rec = _rec(data, node)
    rec["last_check"] = now
    # Not for a node an agent or a person put away, and not for one that is out
    # because TASKS died on it at CUDA init: such a node passes probes while it fails
    # work, and coming back as `slow` would cost a failed task an hour, for ever.
    if rec.get("state") != "quarantined" or rec.get("source") not in ("verify", "auto") \
            or "CUDA-init failures" in str(rec.get("reason") or ""):
        rec["last_result"] = f"unknown: slow init {int(secs)}s on a node that is out for a fault"
        return False
    rec["slow_streak"] = int(rec.get("slow_streak", 0)) + 1
    rec["ok_streak"] = 0
    if rec["slow_streak"] >= HEALTH_OK_STREAK and now >= float(rec.get("until") or 0):
        rec["slow_streak"] = 0
        return mark_slow(data, node, secs)
    rec["last_result"] = (f"slow init {int(secs)}s ({rec['slow_streak']}/{HEALTH_OK_STREAK} "
                          f"towards SLOW)")
    _hist(rec, "check", result=rec["last_result"])
    return False


def mark_slow(data, node, secs):
    """`node` works but needs `secs` to create a CUDA context. Returns True if this
    changed its state."""
    rec = _rec(data, node)
    was = rec.get("state")
    rec.update({"state": "slow", "slow_init_secs": int(secs), "ok_streak": 0, "strikes": 0,
                "reason": f"slow CUDA init: {int(secs)}s (a healthy node needs <10s)"})
    rec["last_check"] = time.time()
    rec["last_result"] = f"slow: init {int(secs)}s"
    if was != "slow":
        rec["since"] = time.time()
        _hist(rec, "slow", secs=int(secs))
    return was != "slow"


def task_accepts_slow(task):
    """Whether a task may be placed on a slow node: an explicit `allow_slow` wins,
    otherwise a runtime estimate of at least SLOW_OK_MIN_EST_SECS."""
    allow = task.get("allow_slow")
    if allow is not None:
        return bool(allow)
    try:
        return int(task.get("est_runtime_secs") or 0) >= SLOW_OK_MIN_EST_SECS
    except (TypeError, ValueError):
        return False


def quarantined_nodes(data=None):
    """Set of node names currently quarantined."""
    data = data if data is not None else load()
    return {n for n, r in data.get("nodes", {}).items() if r.get("state") == "quarantined"}


def _rec(data, node):
    return data.setdefault("nodes", {}).setdefault(node, {
        "state": "ok", "strikes": 0, "ok_streak": 0, "history": []})


def _hist(rec, event, **fields):
    rec.setdefault("history", []).append({"t": round(time.time(), 1), "event": event, **fields})
    del rec["history"][:-HISTORY_KEEP]


def quarantine(data, node, reason, source, reporter=None):
    """Put `node` in quarantine (idempotent: re-reporting refreshes reason/until)."""
    rec = _rec(data, node)
    now = time.time()
    fresh = rec.get("state") != "quarantined"
    rec.update({"state": "quarantined", "reason": reason, "source": source,
                "until": now + QUARANTINE_MIN_SECS, "ok_streak": 0, "slow_streak": 0})
    if reporter:
        rec["reporter"] = reporter
    if fresh:
        rec["since"] = now
    _hist(rec, "quarantine", reason=reason, source=source)
    return fresh


def release(data, node, reason, source):
    rec = _rec(data, node)
    was = rec.get("state") == "quarantined"
    rec.update({"state": "ok", "strikes": 0, "ok_streak": 0, "released_at": time.time(),
                "release_reason": reason})
    _hist(rec, "release", reason=reason, source=source)
    return was


def strike(data, node, reason, source="auto"):
    """Count one fast CUDA-signature failure on `node`. Returns True if this strike
    tipped the node into quarantine."""
    rec = _rec(data, node)
    rec["strikes"] = int(rec.get("strikes", 0)) + 1
    rec["last_strike"] = time.time()
    _hist(rec, "strike", reason=reason, strikes=rec["strikes"])
    if rec.get("state") != "quarantined" and rec["strikes"] >= STRIKES_TO_QUARANTINE:
        quarantine(data, node, f"{rec['strikes']} fast CUDA-init failures; last: {reason}", source)
        return True
    return False


def verify_strike(data, node, reason):
    """Count one verify probe the GPU did not answer. Its own counter: task-failure
    strikes (`strikes`) are cleared by a task that SUCCEEDS on the node, and must not
    be cleared by a probe that merely answers — such a node passes every probe while
    every task dies at CUDA init. Returns True if it tipped the node into quarantine."""
    rec = _rec(data, node)
    rec["verify_strikes"] = int(rec.get("verify_strikes", 0)) + 1
    rec["last_strike"] = time.time()
    _hist(rec, "verify_strike", reason=reason, strikes=rec["verify_strikes"])
    if rec.get("state") != "quarantined" and rec["verify_strikes"] >= STRIKES_TO_QUARANTINE:
        quarantine(data, node, f"{rec['verify_strikes']} verify probes unanswered; last: {reason}",
                   "verify")
        rec["verify_strikes"] = 0
        return True
    return False


def clear_verify_strikes(data, node):
    """A verify probe answered cleanly: two misses have to be in a row to count."""
    rec = data.get("nodes", {}).get(node)
    if rec and rec.get("verify_strikes"):
        rec["verify_strikes"] = 0
        return True
    return False


def clear_strikes(data, node):
    """A task ran successfully on `node` → forget accumulated strikes."""
    rec = data.get("nodes", {}).get(node)
    if rec and rec.get("strikes"):
        rec["strikes"] = 0
        return True
    return False


def record_check(data, node, verdict, detail=""):
    """Store a periodic probe result. verdict: 'ok' | 'fail' | 'unknown' | None
    (None = could not run, e.g. no hold job on the node). Returns 'released' when the
    node just came out of quarantine, 'extended' when a failure re-armed it, else None."""
    rec = _rec(data, node)
    now = time.time()
    rec["last_check"] = now
    if verdict is None:
        rec["last_result"] = detail or "not_checked"
        return None
    rec["last_result"] = verdict if verdict == "ok" else f"{verdict}: {detail}"
    rec["slow_streak"] = 0          # "in a row": any other answer in between starts over
    _hist(rec, "check", result=rec["last_result"])
    if rec.get("state") == "slow":
        # ok here means a context in normal time (the caller sends slow ones to
        # mark_slow), so the node got better; a failure means it got worse.
        if verdict == "ok":
            rec["ok_streak"] = int(rec.get("ok_streak", 0)) + 1
            if rec["ok_streak"] >= HEALTH_OK_STREAK:
                release(data, node, f"{HEALTH_OK_STREAK} consecutive healthy CUDA probes", "auto")
                return "released"
        elif verdict == "fail":
            quarantine(data, node, f"CUDA probe failed: {detail}", "auto")
            return "quarantined"
        return None
    if rec.get("state") != "quarantined":
        return None
    if verdict == "ok":
        rec["ok_streak"] = int(rec.get("ok_streak", 0)) + 1
        if rec["ok_streak"] >= HEALTH_OK_STREAK and now >= float(rec.get("until") or 0):
            release(data, node, f"{HEALTH_OK_STREAK} consecutive healthy CUDA probes", "auto")
            return "released"
    elif verdict == "fail":
        rec["ok_streak"] = 0
        rec["slow_streak"] = 0
        rec["strikes"] = int(rec.get("strikes", 0)) + 1
        rec["until"] = now + QUARANTINE_MIN_SECS
        rec["reason"] = f"CUDA probe failed: {detail}"
        return "extended"
    return None


def due_for_check(rec, now=None):
    now = now if now is not None else time.time()
    return rec.get("state") in ("quarantined", "slow") and \
        (now - float(rec.get("last_check") or 0)) >= HEALTH_CHECK_SECS
