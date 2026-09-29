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

TMP="$(mktemp -d)"
# The mock srun runs the dispatch wrapper locally, so its heartbeat and sampler loops
# are local processes. A wrapper that did not end normally (a task the test cancelled
# or timed out) leaves them looping forever on a deleted directory — 119 of them had
# piled up on the login node. Every one carries $TMP in its command line.
cleanup() { pkill -f "$TMP/" 2>/dev/null; sleep 0.3; pkill -9 -f "$TMP/" 2>/dev/null; rm -rf "$TMP"; }
trap cleanup EXIT
mkdir -p "$TMP/bin" "$TMP/hive/heartbeat" "$TMP/hive/logs"

# ── mock SLURM ────────────────────────────────────────────────────────────────
cat > "$TMP/bin/squeue" <<'EOF'
#!/usr/bin/env bash
# `-j <id>`: print id only if "alive" (700, 9001). Otherwise list one idle node with
# a 20h TimeLeft, in either the poll (%i|...|%L) or daemon (%i|...|%L|%j) format.
jid=""; prev=""
for a in "$@"; do [[ "$prev" == "-j" ]] && jid="$a"; prev="$a"; done
# every hold job of the user, running or queued (`-t R,PD`, autoscale): the lines of
# $HIVE_DIR/mock_hold_jobs, "jid|STATE|LEFT|NODE"; $HIVE_DIR/mock_squeue_fail = SLURM down
if [[ "$*" == *"-t R,PD"* ]]; then
  [[ -e "$HIVE_DIR/mock_squeue_fail" ]] && exit 1
  cat "$HIVE_DIR/mock_hold_jobs" 2>/dev/null; exit 0; fi
# pending jobs of the user (`-t PD`): two queued hold jobs and one foreign job
if [[ "$*" == *"-t PD"* ]]; then
  [[ -e "$HIVE_DIR/mock_pending" ]] && printf '5001\n5002\n5003\n'; exit 0; fi
if [[ -n "$jid" ]]; then
  # canary jobs (42xx): state comes from $HIVE_DIR/mock_canary_state, empty = finished
  if [[ "$jid" == 42* ]]; then
    # a purged job makes the real squeue fail like this
    [[ -e "$HIVE_DIR/mock_canary_purged" ]] && { echo "slurm_load_jobs error: Invalid job id specified" >&2; exit 1; }
    cat "$HIVE_DIR/mock_canary_state" 2>/dev/null; exit 0; fi
  case ",$jid," in *",700,"*|*",9001,"*) echo "$jid";; esac; exit 0; fi
# what SLURM allocated (`-O JobID,tres-alloc`, the pollers). $HIVE_DIR/mock_cpu_job adds
# hold job 777, which has no GPU; mock_tres_fail = this form of squeue does not work
if [[ "$*" == *"tres-alloc"* ]]; then
  [[ -e "$HIVE_DIR/mock_tres_fail" ]] && exit 1
  echo "700                 cpu=8,mem=128G,node=1,billing=8,gres/gpu=2"
  echo "801                 cpu=2,mem=8G,node=1,billing=2"
  [[ -e "$HIVE_DIR/mock_cpu_job" ]] && echo "777                 cpu=16,mem=1.50G,node=1,billing=16"
  exit 0; fi
fmt=""; for a in "$@"; do [[ "$a" == "%i|"* ]] && fmt="$a"; done
if [[ "$fmt" == *"%j" ]]; then
  echo "700|nodeX|gpu|1:00:00|20:00:00|hold"
  echo "801|login1|normal|1:00:00|15-00:00:00|cursor_ssh_proxy"   # must be filtered out
  [[ -e "$HIVE_DIR/mock_cpu_job" ]] && echo "777|nodeC|normal|1:00:00|20:00:00|odd|name"
else echo "700|nodeX|gpu|1:00:00|20:00:00"; fi
EOF
cat > "$TMP/bin/srun" <<'EOF'
#!/usr/bin/env bash
# $HIVE_DIR/mock_srun_dead lists hold jobs that are gone: no step can be started there
for x in "$@"; do [[ "$x" == --jobid=* ]] && grep -qx "${x#--jobid=}" "$HIVE_DIR/mock_srun_dead" 2>/dev/null \
  && { echo "srun: error: Invalid job id specified" >&2; exit 1; }; done
for x in "$@"; do [[ "$x" == --jobid=* ]] && export SLURM_JOB_ID="${x#--jobid=}"; done
a=("$@"); for i in "${!a[@]}"; do [[ "${a[$i]}" == "bash" ]] && exec "${a[@]:$i}"; done; exit 0
EOF
# scontrol show node: booted long ago unless $HIVE_DIR/mock_boot holds another BootTime.
cat > "$TMP/bin/scontrol" <<'EOF'
#!/usr/bin/env bash
if [[ "$1 $2" == "show job" ]]; then
  case "$3" in
    5001) exc="evc[1-3]"; out="$HIVE_DIR/pool-logs/slurm-5001.out";;
    5002) exc="(null)";   out="$HIVE_DIR/pool-logs/slurm-5002.out";;
    69*)  exc="(null)";   out="$HIVE_DIR/pool-logs/slurm-$3.out"; tres="cpu=8,mem=16G,node=1,billing=8";;   # …one without a GPU
    68*)  exc="(null)";   out="$HIVE_DIR/pool-logs/slurm-$3.out"; tres="cpu=4,mem=64G,node=1,billing=4,gres/gpu=1";;
    67*)  exc="(null)";   out="$HIVE_DIR/pool-logs/slurm-$3.out"; tres="cpu=4,mem=64G,node=1,billing=4"; per="gres/gpu:1";;   # GPUs not in this cluster's TRES
    6*)   exc="(null)";   out="$HIVE_DIR/pool-logs/slurm-$3.out";;        # hold jobs of the autoscale tests
    *)    exc="(null)";   out="/somewhere/else/slurm-$3.out";;        # not a hold job
  esac
  printf 'JobId=%s JobName=hold\n   ExcNodeList=%s\n   StdOut=%s\n' "$3" "$exc" "$out"
  [[ -n "${tres:-}" ]] && printf '   ReqTRES=%s\n   AllocTRES=%s\n' "$tres" "$tres"
  [[ -n "${per:-}" ]] && printf '   TresPerNode=%s\n' "$per"
  exit 0
fi
if [[ "$1" == "update" ]]; then echo "$*" >> "$HIVE_DIR/mock_scontrol_update.log"; exit 0; fi
node="${@: -1}"
boot="$(cat "$HIVE_DIR/mock_boot" 2>/dev/null || echo 2020-01-01T00:00:00)"
state="$(cat "$HIVE_DIR/mock_node_state" 2>/dev/null || echo IDLE)"
printf 'NodeName=%s Arch=x86_64\n   Gres=gpu:h100:2(S:0-1)\n   State=%s ThreadsPerCore=2\n   Partitions=gpu,preemptable \n   BootTime=%s SlurmdStartTime=2026-01-01T00:00:00\n' "$node" "$state" "$boot"
EOF
# sbatch: never submits. Records its argv, and for a canary runs the script (stdin)
# locally into the --output file, as job 4242.
cat > "$TMP/bin/sbatch" <<'EOF'
#!/usr/bin/env bash
echo "$*" >> "$HIVE_DIR/mock_sbatch.log"
out=""; for a in "$@"; do [[ "$a" == --output=* ]] && out="${a#--output=}"; done
[[ "$*" == *"--test-only"* ]] && exit 0
[[ -e "$HIVE_DIR/mock_sbatch_slow" ]] && sleep "$(cat "$HIVE_DIR/mock_sbatch_slow")"
[[ -e "$HIVE_DIR/mock_sbatch_fail" ]] && { echo "sbatch: error: Batch job submission failed" >&2; exit 1; }
if [[ "$*" == *"hive_canary"* ]]; then
  SLURM_JOB_ID=4242 bash -s > "${out//%j/4242}" 2>&1 </dev/stdin
  echo 4242; exit 0
fi
# a submitted hold job shows up in squeue as queued, like the real thing
n=$(( $(cat "$HIVE_DIR/mock_sbatch_n" 2>/dev/null || echo 6100) + 1 )); echo "$n" > "$HIVE_DIR/mock_sbatch_n"
echo "$n|PENDING|7-00:00:00|" >> "$HIVE_DIR/mock_hold_jobs"
echo "Submitted batch job $n"
EOF
cat > "$TMP/bin/scancel" <<'EOF'
#!/usr/bin/env bash
echo "$*" >> "$HIVE_DIR/mock_scancel.log"
EOF
cat > "$TMP/bin/nvidia-smi" <<'EOF'
#!/usr/bin/env bash
# hold job 777 has no GPU
[[ "${SLURM_JOB_ID:-}" == 777 ]] && { echo "No devices were found"; exit 6; }
# compute-apps: no GPU processes in the mock cluster
[[ "$*" == *"--query-compute-apps"* ]] && exit 0
# A wedged driver: nvidia-smi never answers within the probe's deadline.
[[ -n "${MOCK_GPU_HANG:-}" ]] && { sleep "${MOCK_GPU_HANG}"; echo "No devices were found"; exit 6; }
# A broken node that does answer, with nothing.
[[ -n "${MOCK_GPU_NONE:-}" ]] && { echo "No devices were found"; exit 6; }
# query-gpu: the daemon/poll probe asks for 'index,...' (4 fields, ALL the job's GPUs) —
# emit a 2-GPU node (GPU0 idle, GPU1 busy) so the poller proves it now sees BOTH cards
# (the old --id=0 probe would have missed GPU1). The sched live_probe asks without
# 'index' (util,used,total) — keep that single + idle/clean so dispatch tests still place.
if [[ "$*" == *"--query-gpu"* ]]; then
  # the dispatch wrapper's usage sampler asks for util + memory.used only
  if [[ "$*" != *"memory.total"* ]]; then printf '%b\n' "${MOCK_USAGE:-37, 41234}"; exit 0; fi
  if [[ "$*" == *"index"* ]]; then
    printf '0, 0, 10, 81920\n1, 85, 40000, 81920\n'
  elif [[ -n "${MOCK_LIVE_GPU:-}" ]]; then
    printf '%b\n' "$MOCK_LIVE_GPU"        # tests set this to simulate a dirty/busy live reading
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
# The mock cluster has no GPU: stub the CUDA-context probe as healthy (tests flip it).
export HIVE_CUDA_PROBE_CMD="echo 'CUDA_PROBE ok'"
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
assert j.get('cpus') == 8 and j.get('mem_mb') == 131072 and j.get('cpu_only') is False, j
print("  [OK] cpus=8, mem_mb=131072, cpu_only=false recorded from the SLURM allocation")
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
echo "[poll] a hold job without a GPU, and SLURM not saying what was allocated"
touch "$HIVE_DIR/mock_cpu_job"
"$REPO/libexec/hive-poll" >/dev/null 2>&1 || true
"$PY" - <<'PY' || fail=1
import json, os
d = json.load(open(os.environ['HIVE_DIR'] + '/node_monitor.json'))['jobs']
c = d['777']
assert (c['cpus'], c['mem_mb'], c['cpu_only']) == (16, 1536, True), c
print("  [OK] hold job without a GPU: cpus=16, mem_mb=1536 (1.50G), cpu_only=true")
assert c['node'] == 'nodeC' and c['time_left_secs'] == 72000, c
print("  [OK] a job name with a | in it does not shift the columns")
assert d['700']['cpu_only'] is False and '801' not in d
print("  [OK] the GPU hold job is still one; the ssh proxy is still left out")
PY
touch "$HIVE_DIR/mock_tres_fail"
"$REPO/libexec/hive-poll" >/dev/null 2>&1 || true
"$PY" - <<'PY' || fail=1
import json, os
d = json.load(open(os.environ['HIVE_DIR'] + '/node_monitor.json'))['jobs']
assert (d['777']['cpus'], d['777']['mem_mb'], d['777']['cpu_only']) == (16, 1536, True), d['777']
print("  [OK] a poll that cannot read the allocation keeps what the last one knew")
PY
rm -f "$HIVE_DIR/node_monitor.json"
"$REPO/libexec/hive-poll" >/dev/null 2>&1 || true
"$PY" - <<'PY' || fail=1
import json, os
d = json.load(open(os.environ['HIVE_DIR'] + '/node_monitor.json'))['jobs']
assert (d['777']['cpus'], d['777']['mem_mb'], d['777']['cpu_only']) == (None, None, None), d['777']
assert (d['700']['cpus'], d['700']['cpu_only']) == (None, False), d['700']
print("  [OK] never known -> null (never a limit, never 'no GPU'); a GPU the probe saw is a GPU")
PY
rm -f "$HIVE_DIR/mock_cpu_job" "$HIVE_DIR/mock_tres_fail"
"$REPO/libexec/hive-poll" >/dev/null 2>&1 || true
printf '700|nodeX|gpu|1:00:00|20:00:00|hold\n4242|nodeX|gpu|0:01|9:00|hive_canary\n' \
  | grep -vE "$(grep -o "grep -vE '[^']*'" "$REPO/libexec/hive-daemon" | sed "s/grep -vE '//;s/'$//")" | grep -q hive_canary \
  && { echo "  [FAIL] poller filter lets hive_canary into the pool"; fail=1; } \
  || echo "  [OK] pollers keep hive_canary jobs out of the pool"
echo "[poll] wedged GPU driver is a probe failure with a reason, never cpu/idle"
# (the previous poll left this job `busy`: a node fault must not be carried forward)
# HANG goes last: under the mock srun the stuck query is a local child that inherits the
# poller's node-DB flock for as long as it sleeps, so a poll right after it would skip.
for mode in "MOCK_GPU_NONE=1:no_gpu_devices" "MOCK_GPU_HANG=4:gpu_unresponsive"; do
  env "${mode%%:*}" HIVE_GPU_QUERY_TICKS=2 CUDA_VISIBLE_DEVICES=0 "$REPO/libexec/hive-poll" >/dev/null 2>&1 || true
  WANT="${mode##*:}" "$PY" - <<'PY' || fail=1
import json, os
j = json.load(open(os.environ['HIVE_DIR'] + '/node_monitor.json'))['jobs']['700']
want = os.environ['WANT']
assert j['status'] == 'probe_failed' and j.get('probe_detail') == want, (j['status'], j.get('probe_detail'))
print(f"  [OK] {want}: status=probe_failed, probe_detail={want}")
PY
done
rm -f "$HIVE_DIR/node_monitor.json"
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
# Two tasks sharing a 2-GPU hold job (GPU slots), one of them an array member.
"$PY" - <<'PY'
import json, os, time
H = os.environ['HIVE_DIR']
now = time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime())
g = {"util": 0, "mem_used": 0, "mem_total": 81920}
json.dump({"updated": now, "jobs": {"700": {"node": "nodeX", "partition": "gpu", "job_elapsed": "1h",
    "gpu": [dict(g, index=0), dict(g, index=1)], "processes": [], "status": "idle",
    "gpu_idle_since": None, "time_left_secs": 72000, "polled_at": now}}}, open(H + "/node_monitor.json", "w"))
t = lambda i, **k: dict({"id": i, "name": "sw", "cmd": "python train.py", "state": "running",
    "slurm_jobid": "700", "node": "nodeX", "submitted_at": now, "started_at": now,
    "log": H + f"/logs/task-{i}.log"}, **k)
json.dump({"version": 1, "next_id": 3, "tasks": {"1": t(1, gpu_slots=[0], array_id=1, array_index=0),
    "2": t(2, gpu_slots=[1], array_id=1, array_index=1)}}, open(H + "/queue.json", "w"))
PY
_nodes=$("$REPO/libexec/hive-nodes" 2>&1 | sed 's/\x1b\[[0-9;]*m//g')
if grep -q "(+1 more, 2/2 GPUs) #1 sw\[0\]: python train.py" <<<"$_nodes"; then
  echo "  [OK] a shared hold job shows how many tasks and cards, and the array index"
else echo "  [FAIL] shared hold job display"; echo "$_nodes" | tail -8; fail=1; fi
# A hold job without a GPU: CPUs taken / all, its tasks shown, never CLAIM or QUAR.
"$PY" - <<'PY'
import json, os, time
H = os.environ['HIVE_DIR']
now = time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime())
json.dump({"updated": now, "jobs": {"900": {"node": "nodeC", "partition": "normal", "job_elapsed": "1h",
    "gpu": [], "processes": [], "status": "cpu", "cpus": 8, "mem_mb": 16384, "cpu_only": True,
    "gpu_idle_since": None, "time_left_secs": 72000, "polled_at": now}}}, open(H + "/node_monitor.json", "w"))
t = lambda i, **k: dict({"id": i, "name": "prep", "cmd": "python prep.py", "state": "running",
    "slurm_jobid": "900", "node": "nodeC", "submitted_at": now, "started_at": now, "gpus": 0,
    "gpu_slots": [], "log": H + f"/logs/task-{i}.log"}, **k)
json.dump({"version": 1, "next_id": 3, "tasks": {"1": t(1, cpus=4, cpu_slots=[0, 1, 2, 3]),
    "2": t(2, cpu_slots=[4])}}, open(H + "/queue.json", "w"))
json.dump({"nodes": {"nodeC": {"state": "quarantined", "reason": "x", "since": time.time()}}},
          open(H + "/node_health.json", "w"))
PY
_nodes=$("$REPO/libexec/hive-nodes" 2>&1 | sed 's/\x1b\[[0-9;]*m//g')
if grep -q "CPU .*5/8 CPU" <<<"$_nodes" && grep -q "(+1 more) #1 prep: python prep.py" <<<"$_nodes" \
   && ! grep -q "CLAIM\|QUAR " <<<"$_nodes"; then
  echo "  [OK] a hold job without a GPU shows CPUs taken/all and its tasks, also on a quarantined node"
else echo "  [FAIL] CPU hold job display"; echo "$_nodes" | tail -8; fail=1; fi
rm -f "$HIVE_DIR/node_health.json"
# GPU% / MEM over all cards; SLOW; how old a stale row is (feedback #27)
"$PY" - <<'PY'
import json, os, time
H = os.environ['HIVE_DIR']
now = time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime())
old = time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime(time.time() - 25 * 60))
job = lambda node, gpu, polled=now, st="idle": {"node": node, "partition": "gpu", "job_elapsed": "1h",
    "gpu": gpu, "processes": [], "status": st, "gpu_idle_since": None, "time_left_secs": 72000, "polled_at": polled}
json.dump({"updated": now, "jobs": {
    "700": job("two", [{"index": 0, "util": 0, "mem_used": 0, "mem_total": 81920},
                       {"index": 1, "util": 90, "mem_used": 40960, "mem_total": 81920}], st="busy"),
    "701": job("slown", [{"index": 0, "util": 0, "mem_used": 0, "mem_total": 81920}]),
    "702": job("oldrow", [{"index": 0, "util": 0, "mem_used": 0, "mem_total": 81920}], polled=old, st="warning")}},
    open(H + "/node_monitor.json", "w"))
json.dump({"version": 1, "next_id": 1, "tasks": {}}, open(H + "/queue.json", "w"))
json.dump({"nodes": {"slown": {"state": "slow", "slow_init_secs": 184, "strikes": 0, "history": []}}},
          open(H + "/node_health.json", "w"))
PY
_nodes=$("$REPO/libexec/hive-nodes" 2>&1 | sed 's/\x1b\[[0-9;]*m//g')
if grep -E "^ +700 +two.* BUSY +90% +40G/160G x2 +(20h0m|19h59m) " <<<"$_nodes" >/dev/null; then
  echo "  [OK] GPU% is the busiest card, MEM the sum over all cards"
else echo "  [FAIL] multi-card columns"; echo "$_nodes" | tail -8; fail=1; fi
if grep -E "^ +701 +slown.* SLOW " <<<"$_nodes" >/dev/null && grep -q "slow: 1" <<<"$_nodes"; then
  echo "  [OK] a slow node shows SLOW"
else echo "  [FAIL] SLOW display"; echo "$_nodes" | tail -8; fail=1; fi
if grep -E "^ +702 +oldrow.*\[read 2[45]m ago\]" <<<"$_nodes" >/dev/null; then
  echo "  [OK] a stale row says how old its reading is"
else echo "  [FAIL] stale age"; echo "$_nodes" | tail -8; fail=1; fi
rm -f "$HIVE_DIR/node_health.json"
# A record the table cannot read costs that one row, not the rest of the table.
"$PY" - <<'PY'
import json, os, time
H = os.environ['HIVE_DIR']
now = time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime())
ok = lambda node: {"node": node, "partition": "gpu", "job_elapsed": "1h", "processes": [],
    "gpu": [{"index": 0, "util": 0, "mem_used": 0, "mem_total": 81920}], "status": "idle",
    "gpu_idle_since": None, "time_left_secs": 72000, "polled_at": now}
json.dump({"updated": now, "jobs": {
    "700": ok("first"),
    "701": dict(ok("nullgpu"), gpu=None),
    "702": dict(ok("badutil"), gpu=[{"index": 0, "util": "N/A", "mem_used": None, "mem_total": "81920"}]),
    "703": dict(ok("nocmd"), partition="", job_elapsed=None),
    "704": "not a record",
    "705": ok("last")}}, open(H + "/node_monitor.json", "w"))
json.dump({"version": 1, "next_id": 9, "tasks": {
    "1": {"id": 1, "name": "multi\nline\tname", "cmd": None, "state": "running", "slurm_jobid": "703",
          "node": "nocmd", "submitted_at": now, "started_at": now, "log": H + "/logs/task-1.log"},
    "2": {"id": 2, "name": "old", "cmd": "python x.py", "state": "running", "slurm_jobid": "705",
          "node": "last", "submitted_at": now, "started_at": now, "log": H + "/logs/task-2.log"}}},
    open(H + "/queue.json", "w"))
PY
_nodes=$("$REPO/libexec/hive-nodes" 2>&1 | sed 's/\x1b\[[0-9;]*m//g')
_rows=$(grep -cE "^ +70[0-5] " <<<"$_nodes")
if [[ "$_rows" == 6 ]] && grep -q "total: 6" <<<"$_nodes" && grep -qE "^ +705 +last" <<<"$_nodes"; then
  echo "  [OK] null / non-numeric / empty fields: all six rows are shown"
else echo "  [FAIL] robust table ($_rows rows)"; echo "$_nodes" | tail -12; fail=1; fi
if grep -E "^ +703 +nocmd +\? +\? +CLAIM" <<<"$_nodes" >/dev/null && grep -q "#1 multi line name" <<<"$_nodes"; then
  echo "  [OK] empty cells and a name with tab / newline do not shift the columns"
else echo "  [FAIL] column shift"; echo "$_nodes" | tail -12; fail=1; fi
"$PY" -m py_compile "$REPO/libexec/hive-top" && echo "  [OK] hive top compiles" || { echo "  [FAIL] hive top"; fail=1; }

echo
if [[ $fail -eq 0 ]]; then echo "ALL TESTS PASSED"; else echo "SOME TESTS FAILED"; fi
exit $fail
