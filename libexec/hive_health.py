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
    r"Engine core initialization failed",
]
_CUDA_FAULT_RE = re.compile("|".join(f"(?:{p})" for p in CUDA_FAULT_PATTERNS))

# ── The probe: create a real CUDA context via libcuda (stdlib ctypes only) ───
# Runs INSIDE `srun --overlap` on the node, under the hold job's cgroup, so it sees
# exactly the GPU a task would get. Prints one line: `CUDA_PROBE ok`,
# `CUDA_PROBE fail <where>=<code>` or `CUDA_PROBE unknown <why>` (can't tell — never
# treated as a fault). Verified live: healthy node → ok; evc43 → cuCtxCreate=999.
CUDA_PROBE_PY = r'''
import ctypes, sys
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
print("CUDA_PROBE ok" if r == 0 else f"CUDA_PROBE fail cuMemAlloc={r}")
'''.strip("\n")

CUDA_MARKER = "---CUDA---"

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
GPU_QUERY_DEADLINE = 20     # seconds; a healthy node answers in well under 10. Measured on
                            # the wedged node: +4 s step start, +15 s step teardown after
                            # the verdict, so callers need ~deadline + 30 s of srun timeout
GPU_MARKER = "GPU_PROBE"
GPU_QUERY_FIELDS = "utilization.gpu,memory.used,memory.total"


def gpu_query_shell(fields=GPU_QUERY_FIELDS, deadline=None):
    """Shell snippet printing, in order: `GPU_PROBE granted=<CUDA_VISIBLE_DEVICES>`
    (empty = SLURM gave this job no GPU), nvidia-smi's csv lines, then
    `GPU_PROBE rc=<n>` or `GPU_PROBE hung`. Sets $_HIVE_GPU_HUNG when it gave up."""
    if deadline is None:
        deadline = float(os.environ.get("HIVE_GPU_QUERY_DEADLINE", GPU_QUERY_DEADLINE))
    ticks = max(1, int(deadline * 2))
    return (
        f'echo "{GPU_MARKER} granted=${{CUDA_VISIBLE_DEVICES:-}}"; '
        '_o=$(mktemp 2>/dev/null || echo "/tmp/hive_gpuq.$$"); '
        f'( nvidia-smi --query-gpu={fields} --format=csv,noheader,nounits >"$_o" 2>&1; '
        'echo $? >"$_o.rc" ) </dev/null >/dev/null 2>&1 & '
        f'_i=0; while [ ! -s "$_o.rc" ] && [ "$_i" -lt {ticks} ]; do sleep 0.5; _i=$((_i+1)); done; '
        '_HIVE_GPU_HUNG=; '
        f'if [ -s "$_o.rc" ]; then cat "$_o"; echo "{GPU_MARKER} rc=$(cat "$_o.rc")"; '
        f'else _HIVE_GPU_HUNG=1; echo "{GPU_MARKER} hung"; fi; '
        'rm -f "$_o" "$_o.rc"; '
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


def cuda_probe_shell():
    """Shell snippet that runs CUDA_PROBE_PY on the node. Uses the interpreter this
    process runs (a shared-FS conda env is visible on compute nodes) and falls back to
    the node's python3; a missing interpreter yields `unknown`, never `fail`.

    HIVE_CUDA_PROBE_CMD overrides the whole snippet (offline tests: the mock cluster
    has no GPU, so the real probe would report cuInit=999 and quarantine everything)."""
    override = os.environ.get("HIVE_CUDA_PROBE_CMD")
    if override:
        return override
    py = shlex.quote(sys.executable or "python3")
    return (f"PY={py}; [ -x \"$PY\" ] || PY=python3; "
            f"\"$PY\" - <<'HIVE_CUDA_PROBE_EOF' 2>/dev/null || echo 'CUDA_PROBE unknown python_failed'\n"
            f"{CUDA_PROBE_PY}\nHIVE_CUDA_PROBE_EOF")


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


def cuda_probe(slurm_jobid, timeout=CUDA_PROBE_TIMEOUT):
    """Standalone CUDA-context probe through hold job `slurm_jobid`.
    Returns (verdict, detail) — see parse_cuda_probe; srun failure → ('unknown', ...)."""
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


# ── Node lists (`hive pool add --exclude`, `hive submit --exclude`) ──────────
def expand_nodes(spec):
    """Node names from a SLURM-style list: 'evc22,evc[1-3,07],gpu01' →
    ['evc22', 'evc1', 'evc2', 'evc3', 'evc07', 'gpu01']. Order kept, duplicates
    dropped, zero padding preserved. Raises ValueError on unbalanced brackets."""
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
                for n in range(int(lo), int(hi) + 1):
                    names.append(f"{pre}{str(n).zfill(len(lo))}{post}")
        for n in names:
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
                "until": now + QUARANTINE_MIN_SECS, "ok_streak": 0})
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
    _hist(rec, "check", result=rec["last_result"])
    if rec.get("state") != "quarantined":
        return None
    if verdict == "ok":
        rec["ok_streak"] = int(rec.get("ok_streak", 0)) + 1
        if rec["ok_streak"] >= HEALTH_OK_STREAK and now >= float(rec.get("until") or 0):
            release(data, node, f"{HEALTH_OK_STREAK} consecutive healthy CUDA probes", "auto")
            return "released"
    elif verdict == "fail":
        rec["ok_streak"] = 0
        rec["strikes"] = int(rec.get("strikes", 0)) + 1
        rec["until"] = now + QUARANTINE_MIN_SECS
        rec["reason"] = f"CUDA probe failed: {detail}"
        return "extended"
    return None


def due_for_check(rec, now=None):
    now = now if now is not None else time.time()
    return rec.get("state") == "quarantined" and \
        (now - float(rec.get("last_check") or 0)) >= HEALTH_CHECK_SECS
