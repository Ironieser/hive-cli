"""hive_events — shared append-only event log for the hive task queue.

One JSON object per line in `$HIVE_DIR/events.jsonl`. This is the **durable history**
(timing feedback + runtime samples) that survives `queue.json` growth/pruning:

    {"ts":"2026-06-08T10:01:05","event":"submit","task":5,"name":"train", ...}
    {"ts":"2026-06-08T10:04:17","event":"dispatch","task":5,"node":"evc23", ...}
    {"ts":"2026-06-08T11:16:29","event":"finish","task":5,"state":"done","run_secs":4332,"queued_secs":192, ...}

Why an append-only log rather than rewriting a JSON blob: appends are O(1), a single
line is well under PIPE_BUF so concurrent appends interleave cleanly, and the file is a
natural audit trail. In practice every caller (submit / scheduler cycle / cancel) is
already holding `queue.lock` when it records, so writes are serialized anyway.

Keep the field names stable — `hive-sched` and `hive-queue` write these events and
`hive stats` / `--est-runtime auto` read them back; drift would silently break history.
Imported by both scripts via `sys.path.insert(0, <libexec>)`.
"""

import json
import os
import time
from datetime import datetime

HIVE_DIR    = os.environ.get("HIVE_DIR", os.path.expanduser("~/.hive"))
EVENTS_FILE = os.path.join(HIVE_DIR, "events.jsonl")

# Keep the log bounded so it never grows without limit and reads stay cheap (same idea
# as hive-sched's roll_log). When the file exceeds MAX_BYTES we trim to the last
# KEEP_LINES — recent history is what estimates need; older samples are dropped. The size
# check is a cheap os.stat on every append; the (O(n)) trim only runs when over the cap.
EVENTS_MAX_BYTES  = 4 * 1024 * 1024   # ~4 MB
EVENTS_KEEP_LINES = 20000             # ≈ several thousand recent tasks


def _now_iso():
    return datetime.now().strftime('%Y-%m-%dT%H:%M:%S')


def _maybe_compact():
    """Trim the log to the last EVENTS_KEEP_LINES if it grew past EVENTS_MAX_BYTES.
    Callers hold queue.lock, so this is serialized; os.replace makes it atomic for
    concurrent readers (they keep reading the old inode)."""
    try:
        if os.path.getsize(EVENTS_FILE) <= EVENTS_MAX_BYTES:
            return
        with open(EVENTS_FILE) as f:
            lines = f.readlines()
        if len(lines) <= EVENTS_KEEP_LINES:
            return
        tmp = EVENTS_FILE + f".compact.{os.getpid()}.tmp"
        with open(tmp, "w") as f:
            f.writelines(lines[-EVENTS_KEEP_LINES:])
        os.replace(tmp, EVENTS_FILE)
    except OSError:
        pass


def record(event, **fields):
    """Append one event line. Best-effort: never raises into the caller's hot path.
    None-valued fields are dropped to keep lines compact (0 / "" are kept).
    Must be called while holding queue.lock (every caller already does)."""
    rec = {"ts": _now_iso(), "t": round(time.time(), 3), "event": event}
    rec.update({k: v for k, v in fields.items() if v is not None})
    try:
        os.makedirs(HIVE_DIR, exist_ok=True)
        with open(EVENTS_FILE, "a") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        _maybe_compact()
    except OSError:
        pass


def iter_events():
    """Yield every well-formed event dict; tolerate a partially-written tail line."""
    if not os.path.exists(EVENTS_FILE):
        return
    try:
        with open(EVENTS_FILE) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
    except OSError:
        return


def done_runs():
    """{task_id: (name, run_secs)} from the last `done` finish event per task.

    Keyed by task so a task that was requeued + finished once counts a single sample."""
    by_task = {}
    for e in iter_events():
        if e.get("event") == "finish" and e.get("state") == "done":
            rs = e.get("run_secs")
            if isinstance(rs, int) and rs > 0:
                by_task[e.get("task")] = (e.get("name") or "", rs)
    return by_task
