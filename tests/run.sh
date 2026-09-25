#!/usr/bin/env bash
# tests/run.sh — deterministic OFFLINE test suite for hive-cli.
#
# Spins up MOCK SLURM binaries (squeue/srun/nvidia-smi) on PATH and a shadow HIVE_DIR
# in a temp dir, then exercises the pollers, scheduler, queue CLI and event log. It
# NEVER touches ~/.hive, a real cluster, or the running daemons. Run from anywhere:
#
#     bash tests/run.sh
#
# Exit code 0 = all passed, non-zero = something failed.
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${HIVE_PYTHON:-}"
[[ -z "$PY" && -x "$HOME/.conda/envs/vlm/bin/python3" ]] && PY="$HOME/.conda/envs/vlm/bin/python3"
[[ -z "$PY" ]] && PY="$(command -v python3 || true)"
[[ -z "$PY" ]] && { echo "python3 not found (set HIVE_PYTHON)"; exit 2; }

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/bin" "$TMP/hive/heartbeat" "$TMP/hive/logs"

# ── mock SLURM ────────────────────────────────────────────────────────────────
cat > "$TMP/bin/squeue" <<'EOF'
#!/usr/bin/env bash
# `-j <id>`: print id only if "alive" (700, 9001). Otherwise list one idle node with
# a 20h TimeLeft, in either the poll (%i|...|%L) or daemon (%i|...|%L|%j) format.
jid=""; prev=""
for a in "$@"; do [[ "$prev" == "-j" ]] && jid="$a"; prev="$a"; done
if [[ -n "$jid" ]]; then case ",$jid," in *",700,"*|*",9001,"*) echo "$jid";; esac; exit 0; fi
fmt=""; for a in "$@"; do [[ "$a" == "%i|"* ]] && fmt="$a"; done
if [[ "$fmt" == *"%j" ]]; then
  echo "700|nodeX|gpu|1:00:00|20:00:00|hold"
  echo "801|login1|normal|1:00:00|15-00:00:00|cursor_ssh_proxy"   # must be filtered out
else echo "700|nodeX|gpu|1:00:00|20:00:00"; fi
EOF
cat > "$TMP/bin/srun" <<'EOF'
#!/usr/bin/env bash
a=("$@"); for i in "${!a[@]}"; do [[ "${a[$i]}" == "bash" ]] && exec "${a[@]:$i}"; done; exit 0
EOF
cat > "$TMP/bin/nvidia-smi" <<'EOF'
#!/usr/bin/env bash
# compute-apps: no GPU processes in the mock cluster
[[ "$*" == *"--query-compute-apps"* ]] && exit 0
# query-gpu: the daemon/poll probe asks for 'index,...' (4 fields, ALL the job's GPUs) —
# emit a 2-GPU node (GPU0 idle, GPU1 busy) so the poller proves it now sees BOTH cards
# (the old --id=0 probe would have missed GPU1). The sched live_probe asks without
# 'index' (util,used,total) — keep that single + idle/clean so dispatch tests still place.
if [[ "$*" == *"--query-gpu"* ]]; then
  if [[ "$*" == *"index"* ]]; then
    printf '0, 0, 10, 81920\n1, 85, 40000, 81920\n'
  else
    echo "0, 10, 81920"
  fi
  exit 0
fi
exit 0
EOF
chmod +x "$TMP/bin/"*

export PATH="$TMP/bin:$PATH"
export HIVE_DIR="$TMP/hive"
export USER="${USER:-tester}"
# pretend a scheduler runs elsewhere so `submit` doesn't autostart a real one
date -Iseconds > "$TMP/hive/sched.heartbeat"
printf '%s\n%s\n' 999999 otherhost > "$TMP/hive/sched.pid"

fail=0
echo "=== CLI / bash smoke (real scripts, mock SLURM) ==="
echo "[poll] bash poller records remaining walltime"
"$REPO/libexec/hive-poll" >/dev/null 2>&1 || true
"$PY" - <<'PY' || fail=1
import json, os
d = json.load(open(os.environ['HIVE_DIR'] + '/node_monitor.json'))
j = d['jobs']['700']
assert j['time_left_secs'] == 72000, j['time_left_secs']
print("  [OK] time_left_secs=72000 parsed from squeue %L (20:00:00)")
# Phase-1 multi-GPU: the poller enumerates ALL the job's GPUs (not just --id=0) and the
# job status is busy if ANY GPU is busy. The mock node has GPU0 idle + GPU1 busy.
gpu = j.get('gpu', [])
assert len(gpu) == 2, gpu
assert {g['index'] for g in gpu} == {0, 1}, gpu
assert j['status'] == 'busy', j['status']
print("  [OK] poller sees BOTH GPUs (idx 0,1); node busy because GPU1 is busy")
assert '801' not in d['jobs'], sorted(d['jobs'])
print("  [OK] hive-poll filters cursor_ssh_proxy like the daemon (was: no %j column, grep never matched)")
PY
smoke() { if "$@" >/dev/null 2>&1; then echo "  [OK] $label"; else echo "  [FAIL] $label"; fail=1; fi; }
label="submit --est-runtime"; smoke "$PY" "$REPO/libexec/hive-queue" submit "true" --name reg --est-runtime 30m
label="list";                 smoke "$PY" "$REPO/libexec/hive-queue" list
label="stats";                smoke "$PY" "$REPO/libexec/hive-queue" stats
label="prune --dry-run";      smoke "$PY" "$REPO/libexec/hive-queue" prune --dry-run
"$PY" "$REPO/libexec/hive-queue" wait 1 --no-log --interval 0.2 --pending-timeout 1 >/dev/null 2>&1; rc=$?
[[ $rc -eq 75 ]] && echo "  [OK] wait --pending-timeout -> exit 75" || { echo "  [FAIL] wait exit $rc (want 75)"; fail=1; }

"$PY" "$REPO/libexec/hive-queue" list 2>"$TMP/bp.err" | head -1 >/dev/null
if grep -q "Traceback" "$TMP/bp.err"; then echo "  [FAIL] list | head -> BrokenPipe traceback"; fail=1
else echo "  [OK] list | head exits quietly (no BrokenPipe traceback)"; fi

echo
echo "=== python unit + integration ==="
"$PY" "$REPO/tests/test_hive.py" "$REPO" || fail=1

echo
echo "=== hive nodes: CLAIMED display ==="
# A hold job whose GPU reads idle but that has a RUNNING hive task in queue.json must
# show CLAIM (not IDLE) with the task in the TASK column (feedback #4/#7/#17/#32).
"$PY" - <<'PY'
import json, os, time
H = os.environ['HIVE_DIR']
now = time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime())
json.dump({"updated": now, "jobs": {"700": {"node": "nodeX", "partition": "gpu", "job_elapsed": "1h",
    "gpu": [{"index": 0, "util": 0, "mem_used": 0, "mem_total": 81920}], "processes": [],
    "status": "idle", "gpu_idle_since": None, "time_left_secs": 72000, "polled_at": now}}},
    open(H + "/node_monitor.json", "w"))
json.dump({"version": 1, "next_id": 2, "tasks": {"1": {"id": 1, "name": "warm", "cmd": "python train.py",
    "state": "running", "slurm_jobid": "700", "node": "nodeX", "submitted_at": now, "started_at": now,
    "log": H + "/logs/task-1.log"}}}, open(H + "/queue.json", "w"))
PY
printf '%s\n%s\n' "$$" "$(hostname)" > "$HIVE_DIR/node_monitor.pid"   # "poller alive" -> no autostart
_nodes=$("$REPO/libexec/hive-nodes" 2>&1 | sed 's/\x1b\[[0-9;]*m//g')   # strip ANSI colors
if grep -q "CLAIM" <<<"$_nodes" && grep -q "#1 warm: python train.py" <<<"$_nodes" && grep -q "claimed: 1" <<<"$_nodes"; then
  echo "  [OK] running task on a GPU-idle hold job shows CLAIM + task name + claimed count"
else echo "  [FAIL] CLAIM display"; echo "$_nodes" | tail -8; fail=1; fi

echo
if [[ $fail -eq 0 ]]; then echo "ALL TESTS PASSED"; else echo "SOME TESTS FAILED"; fi
exit $fail
