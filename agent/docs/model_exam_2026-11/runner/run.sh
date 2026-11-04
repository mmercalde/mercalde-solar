#!/bin/bash
# Part 3, unattended on KAMRUI. Every exit, of any kind, ends in cleanup():
# nothing on 8082, llama-server started, /health checked, sudoers rule removed.
D=${ABX_DIR:-/tmp/abexam}
BIN=${ABX_BIN:-$HOME/llama.cpp/build/bin/llama-server}
REPO=${ABX_REPO:-$HOME/mercalde-solar}
RULE=/etc/sudoers.d/llama-abtest
SWAP_LIMIT_KB=1953125
START_AT=${ABX_START:-$(date -d "2026-11-04 00:05" +%s)}
NO_NEW_MODEL_AFTER=${ABX_NO_NEW:-$(date -d "2026-11-04 02:30" +%s)}
TICK_TRIES=${ABX_TICK_TRIES:-120}          # 10 s apart: 20 minutes
PAUSE=${ABX_PAUSE:-30}
HEALTH_POLLS=${ABX_HEALTH_POLLS:-60}       # 3 s apart: 180 s

say() { echo "$(date '+%F %T') $*" >> "$D/events.log"; }
status() { { echo "state=$1"; echo "detail=$2"; echo "updated=$(date '+%F %T')"; } > "$D/status.tmp"; mv "$D/status.tmp" "$D/status"; }
gtt() { cat /sys/class/drm/card[0-9]*/device/mem_info_gtt_used 2>/dev/null | head -1; }
swap_kb() { awk '/SwapTotal/{t=$2}/SwapFree/{f=$2}END{print t-f}' /proc/meminfo; }
live_ok() { [ "$(curl -s -m 3 http://127.0.0.1:8080/health)" = '{"status":"ok"}' ]; }
in_blackout() { local t; t=${ABX_HHMM:-$(date +%H%M)}; [ "$((10#$t))" -ge 2030 ] && [ "$((10#$t))" -lt 2300 ]; }
# Both generators, read fresh. No answer counts as running: stopping live blind is the risk.
gens_clear() {
  local data; data=$(curl -s -m 8 http://192.168.3.10:8080/data)
  [ -n "$data" ] || return 1
  echo "$data" | grep -q '"kubotaAction":' || return 1
  echo "$data" | grep -q '"mep803aAction":' || return 1
  ! echo "$data" | grep -qE '"(kubotaAction|mep803aAction)":9[,}]'
}

kill_8082() {  # whatever listens on 8082, by port. 8080 and 8081 are not touched.
  local pids; pids=$(ss -ltnpH 'sport = :8082' 2>/dev/null | grep -oE 'pid=[0-9]+' | cut -d= -f2 | sort -u)
  [ -e "$D/cand.pid" ] && pids="$pids $(cat "$D/cand.pid")"
  for p in $pids; do kill "$p" 2>/dev/null; done
  for _ in $(seq 1 15); do
    local alive=0; for p in $pids; do kill -0 "$p" 2>/dev/null && alive=1; done
    [ $alive = 0 ] && break
    sleep 1
  done
  for p in $pids; do kill -9 "$p" 2>/dev/null; done
  rm -f "$D/cand.pid"
  [ -n "${pids// /}" ] && say "8082 cleared: $pids"
  return 0
}

start_live() {  # a start takes ~90 s: the unit waits on dev-dri-renderD128.device
  local try
  for try in 1 2; do
    live_ok && return 0
    say "live: start requested (try $try)"
    sudo -n systemctl start llama-server
    for _ in $(seq 1 "$HEALTH_POLLS"); do live_ok && return 0; sleep 3; done
  done
  return 1
}

gpu_check() {
  ( cd "$REPO/agent" && venv/bin/python llama_gpu_check.py >> "$D/gpu-check.log" 2>&1 )
  GPU_RC=$?
  say "live: llama_gpu_check rc=$GPU_RC gtt=$(gtt)"
  return $GPU_RC
}

restore() {
  kill_8082
  if start_live; then say "live: UP /health ok gtt=$(gtt)"; else say "live: DOWN after two starts"; return 1; fi
  gpu_check
}

wait_tick() {  # one real agent tick, finished after this moment
  local since line; since=$(date '+%F %T')
  for _ in $(seq 1 "$TICK_TRIES"); do
    line=$(journalctl -u solar-agent --since "$since" --no-pager | grep "tick done" | tail -1)
    if [ -n "$line" ]; then say "live: real tick completed: ${line##*__main__ }"; return 0; fi
    sleep 10
  done
  say "live: no tick completed in $((TICK_TRIES * 10)) s"
  return 1
}

CLEANED=0
cleanup() {  # the one way out. Runs at most once, from every trap.
  [ $CLEANED = 1 ] && return 0
  CLEANED=1
  trap '' INT TERM HUP
  trap - ERR EXIT
  local why=$1 live=up
  say "cleanup: $why"
  status cleaning "$why"
  rm -f "$D/guard.run"
  restore || true
  live_ok || live=DOWN
  if [ "$live" = up ]; then
    sudo -n /usr/bin/rm "$RULE" && say "sudoers rule removed" || say "sudoers rule NOT removed"
  else
    say "live down: leaving the rule for the 03:30 dead-man's last attempt, which then removes it"
  fi
  status done "$why; live=$live gpu_rc=${GPU_RC:-unset}"
  touch "$D/done"
  exit 0
}
trap 'cleanup "signal INT"' INT
trap 'cleanup "signal TERM"' TERM
trap 'cleanup "signal HUP"' HUP
trap 'cleanup "a command failed at line $LINENO"' ERR
trap 'cleanup "script ended"' EXIT

run_model() {  # name alias gguf [extra server flags...]. 0 ran, 3 skipped, else failed.
  local name=$1 alias=$2 gguf=$3 s ok=0 rc=0; shift 3
  echo "$name" > "$D/phase"
  [ -e "$D/KILLED" ] && { say "$name: skipped, an earlier limit kill"; return 3; }
  [ -e "$D/STOP" ] && { say "$name: skipped, dead-man stop"; return 3; }
  gens_clear || { say "$name: skipped, a generator is running or its state cannot be read"; return 3; }
  [ "$(date +%s)" -lt "$NO_NEW_MODEL_AFTER" ] || { say "$name: skipped, too late to start"; return 3; }
  ! in_blackout || { say "$name: skipped, inside the 20:30-23:00 window"; return 3; }
  status running "$name"
  say "$name: live: DOWN stop requested"
  sudo -n systemctl stop llama-server || { say "$name: stop failed"; return 4; }
  for _ in $(seq 1 60); do ss -ltnH 'sport = :8080' | grep -q . || break; sleep 3; done
  sleep "${ABX_SETTLE:-5}"
  s=$(swap_kb); say "$name: live stopped; idle gtt=$(gtt) swap_kb=$s"
  [ "$s" -le $SWAP_LIMIT_KB ] || { say "$name: not started, swap past 2 GB with live stopped"; return 5; }
  setsid nohup "$BIN" -m "$gguf" -ngl 99 --host 127.0.0.1 --port 8082 -c 16384 --jinja --cache-reuse 256 \
     "$@" --log-timestamps > "$D/server-$name.log" 2>&1 < /dev/null &
  echo $! > "$D/cand.pid"
  say "$name: server pid=$! extra flags: $*"
  for _ in $(seq 1 80); do
    [ "$(curl -s -m 3 http://127.0.0.1:8082/health)" = '{"status":"ok"}' ] && { ok=1; break; }
    kill -0 "$(cat "$D/cand.pid")" 2>/dev/null || break
    sleep 3
  done
  if [ $ok = 0 ] || grep -q "no usable GPU" "$D/server-$name.log"; then say "$name: did not come up on the GPU (ok=$ok)"; return 6; fi
  say "$name: up gtt=$(gtt); exam start"
  # Left to finish if the runner dies: after a kill it only has failures left
  # to record, and its JSON is written when it ends.
  ( cd "$REPO" && exec agent/venv/bin/python agent/model_eval.py --exam --repeat 3 --pause "$PAUSE" \
      --candidate "http://127.0.0.1:8082/v1/chat/completions=$alias" \
      --questions agent/docs/exam_questions_2026-11.txt --json \
      > "$D/result-$name.json" 2> "$D/result-$name.err" ) &
  wait $! || rc=$?
  say "$name: exam end rc=$rc gtt=$(gtt)"
  kill_8082
  return $rc
}

mkdir -p "$D"
say "runner armed for $(date -d "@$START_AT" '+%F %T')"
status waiting "until $(date -d "@$START_AT" '+%F %T')"
# The dates here are for one night. Armed later, they have all passed: do nothing.
[ "$(date +%s)" -lt "$((START_AT + 900))" ] || cleanup "armed after its start time; no stop made"
while [ "$(date +%s)" -lt "$START_AT" ]; do sleep 5 & wait $! || true; done

gens_clear || cleanup "a generator is running or unreadable at the start; no stop made"

touch "$D/guard.run"
setsid nohup "$(dirname "$0")/guard.sh" > "$D/guard.out" 2>&1 < /dev/null &
sleep 2

status snapshot "taking the snapshot"
( cd "$REPO" && agent/venv/bin/python -c '
import os, sys, time
sys.path.insert(0, "agent")
import config, history, model_eval
p = model_eval.make_snapshot(fresh=True)
c = history.readonly_connection(p)
print(time.strftime("%F %T"), "snapshot", p, os.path.getsize(p), "bytes; newest plan",
      history.stamp(history.latest_plan(c)["ts"], config.load()))
' >> "$D/events.log" 2>&1 ) || cleanup "the snapshot failed; no stop made"
[ -s "$D/history.sqlite" ] || cleanup "no snapshot file; no stop made"

# Gemma: the command from the tick A/B. Graded against the database, so its
# results stand on their own whatever happens to the incumbent.
rc=0; run_model gemma4 gemma4 "$HOME/models/candidates/gemma-4-E4B-it-Q4_K_M.gguf" \
    --temp 1.0 --top-k 64 --top-p 0.95 || rc=$?
restore || cleanup "live not verified after Gemma"
if [ -e "$D/KILLED" ] && grep -q "Tctl above" "$D/KILLED"; then
  # A thermal kill ends that model only. Anything else is a different problem.
  mv "$D/KILLED" "$D/KILLED-gemma4"
  say "gemma4: thermally killed; the incumbent still runs"
else
  [ $rc = 0 ] || cleanup "Gemma ended rc=$rc; incumbent not run"
  [ ! -e "$D/KILLED" ] || cleanup "non-thermal limit kill during Gemma; incumbent not run"
fi
[ ! -e "$D/GEN_FLAG" ] || cleanup "a generator started during Gemma; incumbent not run"
status tick "waiting for one real tick before the incumbent"
wait_tick || cleanup "no real tick after Gemma; incumbent not run"

# Incumbent: the unit's own ExecStart arguments, on 127.0.0.1:8082.
rc=0; run_model incumbent qwen3 "$HOME/models/Qwen3-8B-Q4_K_M.gguf" || rc=$?
restore || cleanup "live not verified after the incumbent"
status tick "waiting for one real tick after the incumbent"
wait_tick || say "no real tick after the incumbent; check journalctl"
cleanup "both models attempted (incumbent rc=$rc)"
