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
if [[ "$fmt" == *"%j" ]]; then echo "700|nodeX|gpu|1:00:00|20:00:00|hold"
else echo "700|nodeX|gpu|1:00:00|20:00:00"; fi
EOF
cat > "$TMP/bin/srun" <<'EOF'
#!/usr/bin/env bash
a=("$@"); for i in "${!a[@]}"; do [[ "${a[$i]}" == "bash" ]] && exec "${a[@]:$i}"; done; exit 0
EOF
cat > "$TMP/bin/nvidia-smi" <<'EOF'
#!/usr/bin/env bash
[[ "$*" == *"--query-compute-apps"* ]] && exit 0
[[ "$*" == *"--query-gpu"* ]] && { echo "0, 10, 81920"; exit 0; }; exit 0
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
tl = d['jobs']['700']['time_left_secs']
assert tl == 72000, tl
print("  [OK] time_left_secs=72000 parsed from squeue %L (20:00:00)")
PY
smoke() { if "$@" >/dev/null 2>&1; then echo "  [OK] $label"; else echo "  [FAIL] $label"; fail=1; fi; }
label="submit --est-runtime"; smoke "$PY" "$REPO/libexec/hive-queue" submit "true" --name reg --est-runtime 30m
label="list";                 smoke "$PY" "$REPO/libexec/hive-queue" list
label="stats";                smoke "$PY" "$REPO/libexec/hive-queue" stats
label="prune --dry-run";      smoke "$PY" "$REPO/libexec/hive-queue" prune --dry-run
"$PY" "$REPO/libexec/hive-queue" wait 1 --no-log --interval 0.2 --pending-timeout 1 >/dev/null 2>&1; rc=$?
[[ $rc -eq 75 ]] && echo "  [OK] wait --pending-timeout -> exit 75" || { echo "  [FAIL] wait exit $rc (want 75)"; fail=1; }

echo
echo "=== python unit + integration ==="
"$PY" "$REPO/tests/test_hive.py" "$REPO" || fail=1

echo
if [[ $fail -eq 0 ]]; then echo "ALL TESTS PASSED"; else echo "SOME TESTS FAILED"; fi
exit $fail
