#!/bin/bash
# Failure paths of run.sh / guard.sh / deadman.sh on SER8, with everything that
# could reach KAMRUI or a real service replaced by stubs on PATH. Fast timers.
HERE=$(cd "$(dirname "$0")" && pwd)
W=$(mktemp -d)
trap 'pkill -f "$W" 2>/dev/null; rm -rf "$W"' EXIT
mkdir -p "$W/bin" "$W/repo/agent/venv/bin" "$W/repo/agent/docs"
STATE=$W/state; mkdir -p "$STATE"
export STATE

cat > "$W/bin/sudo" <<'EOF'
#!/bin/bash
[ "$1" = "-n" ] && shift
echo "sudo $*" >> "$STATE/sudo.log"
# Exactly what the sudoers rule allows, and nothing else gets through.
case "$*" in
  "systemctl stop llama-server"|"systemctl start llama-server") exec "$@" ;;
  "/usr/bin/rm /etc/sudoers.d/llama-abtest") echo removed >> "$STATE/rule.log"; exit 0 ;;
  *) echo "sudo: a password is required" >&2; exit 1 ;;
esac
EOF
cat > "$W/bin/systemctl" <<'EOF'
#!/bin/bash
case "$1" in
  stop) rm -f "$STATE/live"; echo stop >> "$STATE/systemctl.log" ;;
  start) echo start >> "$STATE/systemctl.log"; [ -e "$STATE/start_fails" ] || touch "$STATE/live" ;;
  is-active) [ -e "$STATE/live" ] ;;
esac
EOF
cat > "$W/bin/curl" <<'EOF'
#!/bin/bash
case "$*" in
  *:8080/health*) [ -e "$STATE/live" ] && echo '{"status":"ok"}' ;;
  *:8082/health*) [ -e "$STATE/cand_up" ] && echo '{"status":"ok"}' ;;
  */data*) [ -e "$STATE/gen_unreadable" ] && exit 7
     a=10; [ -e "$STATE/gen_on" ] && a=9
     echo "{\"kubotaAction\":10,\"mep803aAction\":$a,\"kubotaState\":\"auto_off\",\"mepState\":\"auto_off\"}" ;;
esac
EOF
cat > "$W/bin/ss" <<'EOF'
#!/bin/bash
case "$*" in
  *8080*) [ -e "$STATE/live" ] && echo "LISTEN 8080" ;;
  *8082*) [ -e "$STATE/cand_pid" ] && kill -0 "$(cat "$STATE/cand_pid")" 2>/dev/null && echo "LISTEN users:((\"llama-server\",pid=$(cat "$STATE/cand_pid"),fd=5))" ;;
esac
exit 0
EOF
cat > "$W/bin/journalctl" <<'EOF'
#!/bin/bash
[ -e "$STATE/no_tick" ] || echo "x __main__ tick done in 19.9s; no change"
EOF
cat > "$W/bin/fakeserver" <<'EOF'
#!/bin/bash
echo $$ > "$STATE/cand_pid"; touch "$STATE/cand_up"; echo "$*" >> "$STATE/server-args.log"
trap 'rm -f "$STATE/cand_up"; exit 0' TERM
while :; do sleep 0.2; done
EOF
# The exam: optionally slow, optionally failing. Records what ran.
cat > "$W/repo/agent/venv/bin/python" <<'EOF'
#!/bin/bash
case "$*" in
  *llama_gpu_check*) [ -e "$STATE/gpu_fails" ] && exit 1; exit 0 ;;
  *make_snapshot*) [ -e "$STATE/snapshot_fails" ] && exit 1; echo snap > "$ABX_DIR/history.sqlite"; echo "snapshot taken"; exit 0 ;;
  *model_eval*) echo "$*" >> "$STATE/exam.log"
     [ -e "$STATE/exam_slow" ] && sleep 30.7731
     case "$*" in *=qwen3*) [ -e "$STATE/second_slow" ] && sleep 30.7731 ;; esac
     [ -e "$STATE/exam_fails" ] && exit 2
     echo '[]'; exit 0 ;;
esac
EOF
chmod +x "$W"/bin/* "$W/repo/agent/venv/bin/python"
touch "$W/repo/agent/llama_gpu_check.py"
export PATH="$W/bin:$PATH" HOME="$W/home" ABX_BIN="$W/bin/fakeserver" ABX_REPO="$W/repo" ABX_SETTLE=0 ABX_PAUSE=0 ABX_HHMM=0005
mkdir -p "$HOME"

# A line in the real flight recorder's format. Its swapfree is deliberately
# nonsense: the guard must not read it.
fl() { echo "$(date +%T) up=1816216.68 tctl=$1 edge=50000 ppt= memavail=9015140 swapfree=0 gtt=9352536064 vram=464814080 load=0.22"; }
meminfo() { printf 'SwapTotal:      10485752 kB\nSwapFree:       %s kB\n' "$((10485752 - $1))" > "$W/meminfo"; }

PASS=0; FAIL=0
check() { if eval "$2"; then PASS=$((PASS+1)); echo "  ok   $1"; else FAIL=$((FAIL+1)); echo "  FAIL $1"; fi; }

scenario() {  # name, then the setup is done by the caller before calling go
  echo "--- $1"
  export ABX_DIR="$W/run-$RANDOM"; mkdir -p "$ABX_DIR"
  /bin/rm -f "$STATE"/* ; touch "$STATE/live"
  export ABX_START=$(date +%s) ABX_NO_NEW=$(( $(date +%s) + 3600 )) ABX_TICK_TRIES=2 ABX_CUT=$(( $(date +%s) + 3600 )) ABX_DEADMAN=$(( $(date +%s) + 3600 ))
  : > "$W/flight"; export ABX_FLIGHT="$W/flight"
  fl 50000 > "$W/flight"
  meminfo 859392; export ABX_MEMINFO="$W/meminfo"
}
waitdone() { for _ in $(seq 1 "${1:-150}"); do [ -e "$ABX_DIR/done" ] && return 0; sleep 0.2; done; return 1; }
restored() { [ -e "$STATE/live" ] && ! { [ -e "$STATE/cand_pid" ] && kill -0 "$(cat "$STATE/cand_pid")" 2>/dev/null; }; }

scenario "happy path: both models run, in order, live back, rule gone"
"$HERE/run.sh" > /dev/null 2>&1 &
waitdone
check "finished" '[ -e "$ABX_DIR/done" ]'
check "two exams" '[ "$(grep -c model_eval "$STATE/exam.log")" = 2 ]'
check "gemma first" 'head -1 "$STATE/exam.log" | grep -q "=gemma4"'
check "incumbent second" 'tail -1 "$STATE/exam.log" | grep -q "=qwen3"'
check "30 s pause flag and repeat 3 passed" 'grep -q -- "--repeat 3" "$STATE/exam.log" && grep -q -- "--pause" "$STATE/exam.log"'
check "gemma sampling flags" 'head -1 "$STATE/server-args.log" | grep -q -- "--temp 1.0 --top-k 64 --top-p 0.95"'
check "incumbent has no sampling flags" '! tail -1 "$STATE/server-args.log" | grep -q -- "--temp"'
check "both on 127.0.0.1:8082" '[ "$(grep -c -- "--host 127.0.0.1 --port 8082" "$STATE/server-args.log")" = 2 ]'
check "stopped twice, started twice" '[ "$(grep -c stop "$STATE/systemctl.log")" = 2 ] && [ "$(grep -c start "$STATE/systemctl.log")" = 2 ]'
check "live restored" restored
check "rule removed" '[ -s "$STATE/rule.log" ]'
check "status says done" 'grep -q state=done "$ABX_DIR/status" && grep -q live=up "$ABX_DIR/status"'
check "guard stopped" '[ ! -e "$ABX_DIR/guard.run" ]'

scenario "generator on at the start: no stop at all, rule still removed"
touch "$STATE/gen_on"
"$HERE/run.sh" > /dev/null 2>&1 &
waitdone
check "never stopped live" '! grep -q stop "$STATE/systemctl.log" 2>/dev/null'
check "no exam" '[ ! -e "$STATE/exam.log" ]'
check "live up" restored
check "rule removed" '[ -s "$STATE/rule.log" ]'

scenario "generator state unreadable at the start: same"
touch "$STATE/gen_unreadable"
"$HERE/run.sh" > /dev/null 2>&1 &
waitdone
check "never stopped live" '! grep -q stop "$STATE/systemctl.log" 2>/dev/null'
check "rule removed" '[ -s "$STATE/rule.log" ]'

scenario "generator starts during Gemma: it finishes, live back, no incumbent"
touch "$STATE/exam_slow"
"$HERE/run.sh" > /dev/null 2>&1 &
for _ in $(seq 1 100); do [ -s "$STATE/exam.log" ] && break; sleep 0.1; done
touch "$STATE/gen_on"; echo "$(date '+%F %T') generator running: test" > "$ABX_DIR/GEN_FLAG"
/bin/rm -f "$STATE/exam_slow"; pkill -xf "sleep 30.7731" ;
waitdone
check "one exam only" '[ "$(grep -c model_eval "$STATE/exam.log")" = 1 ]'
check "live restored" restored
check "reason recorded" 'grep -q "generator started" "$ABX_DIR/status"'
check "rule removed" '[ -s "$STATE/rule.log" ]'

scenario "SIGTERM mid-exam: candidate killed, live back, rule gone"
touch "$STATE/exam_slow"
"$HERE/run.sh" > /dev/null 2>&1 &
R=$!
for _ in $(seq 1 100); do [ -s "$STATE/exam.log" ] && break; sleep 0.1; done
check "candidate was up, live was down" '[ -e "$STATE/cand_up" ] && [ ! -e "$STATE/live" ]'
kill -TERM $R
waitdone
check "live restored" restored
check "rule removed" '[ -s "$STATE/rule.log" ]'
check "reason recorded" 'grep -q "signal TERM" "$ABX_DIR/status"'

scenario "SIGINT mid-exam, launched the way arm.sh does: same"
touch "$STATE/exam_slow"
setsid nohup python3 "$HERE/launch.py" "$HERE/run.sh" > /dev/null 2>&1 < /dev/null &
R=$!
for _ in $(seq 1 100); do [ -s "$STATE/exam.log" ] && break; sleep 0.1; done
check "the launcher kept the pid arm.sh hands the dead-man" 'grep -q "run.sh" /proc/$R/cmdline'
kill -INT $R
waitdone 50
check "it reacted within 10 s, not at the end of the exam" '[ -e "$ABX_DIR/done" ]'
check "live restored" restored
check "rule removed" '[ -s "$STATE/rule.log" ]'
check "reason recorded" 'grep -q "signal INT" "$ABX_DIR/status"'
pkill -xf "sleep 30.7731"

scenario "SIGHUP mid-exam, launched the way arm.sh does: same"
touch "$STATE/exam_slow"
setsid nohup python3 "$HERE/launch.py" "$HERE/run.sh" > /dev/null 2>&1 < /dev/null &
R=$!
for _ in $(seq 1 100); do [ -s "$STATE/exam.log" ] && break; sleep 0.1; done
kill -HUP $R
waitdone 50
check "live restored" restored
check "rule removed" '[ -s "$STATE/rule.log" ]'
check "reason recorded" 'grep -q "signal HUP" "$ABX_DIR/status"'
pkill -xf "sleep 30.7731"

scenario "exam crashes: live back, incumbent not run"
touch "$STATE/exam_fails"
"$HERE/run.sh" > /dev/null 2>&1 &
waitdone
check "one exam only" '[ "$(grep -c model_eval "$STATE/exam.log")" = 1 ]'
check "live restored" restored
check "rule removed" '[ -s "$STATE/rule.log" ]'

scenario "snapshot fails: no stop made"
touch "$STATE/snapshot_fails"
"$HERE/run.sh" > /dev/null 2>&1 &
waitdone
check "never stopped live" '! grep -q stop "$STATE/systemctl.log" 2>/dev/null'
check "rule removed" '[ -s "$STATE/rule.log" ]'

scenario "no real ticks at all: the incumbent still runs, and the miss after it is logged"
touch "$STATE/no_tick"; export ABX_TICK_TRIES=1
"$HERE/run.sh" > /dev/null 2>&1 &
waitdone 200
check "both models ran" '[ "$(grep -c model_eval "$STATE/exam.log")" = 2 ]'
check "only the wait after the incumbent happened" '[ "$(grep -c "no tick completed" "$ABX_DIR/events.log")" = 1 ] && grep -q "no real tick after the incumbent" "$ABX_DIR/events.log"'
check "live restored" restored
check "rule removed" '[ -s "$STATE/rule.log" ]'

scenario "llama_gpu_check fails after Gemma: no incumbent"
touch "$STATE/gpu_fails"
"$HERE/run.sh" > /dev/null 2>&1 &
waitdone 200
check "one exam only" '[ "$(grep -c model_eval "$STATE/exam.log")" = 1 ]'
check "reason recorded" 'grep -q "live not verified after Gemma" "$ABX_DIR/status"'
check "gpu rc reported" 'grep -q "gpu_rc=1" "$ABX_DIR/status"'
check "rule removed" '[ -s "$STATE/rule.log" ]'

scenario "inside the 20:30-23:00 window: no stop made"
ABX_HHMM=2100 "$HERE/run.sh" > /dev/null 2>&1 &
waitdone
check "never stopped live" '! grep -q stop "$STATE/systemctl.log" 2>/dev/null'
check "no exam" '[ ! -e "$STATE/exam.log" ]'
check "rule removed" '[ -s "$STATE/rule.log" ]'

scenario "armed after its dates have passed: no stop made"
ABX_START=1 "$HERE/run.sh" > /dev/null 2>&1 &
waitdone
check "never stopped live" '! grep -q stop "$STATE/systemctl.log" 2>/dev/null'
check "says why" 'grep -q "armed after its start time" "$ABX_DIR/status"'

scenario "too late to start: no stop made"
export ABX_NO_NEW=1
"$HERE/run.sh" > /dev/null 2>&1 &
waitdone
check "never stopped live" '! grep -q stop "$STATE/systemctl.log" 2>/dev/null'
check "rule removed" '[ -s "$STATE/rule.log" ]'

scenario "Gemma thermally killed: live back at once, the incumbent still runs"
touch "$STATE/exam_slow"
"$HERE/run.sh" > /dev/null 2>&1 &
for _ in $(seq 1 100); do [ -s "$STATE/exam.log" ] && break; sleep 0.1; done
( for _ in $(seq 1 14); do fl 86000 >> "$W/flight"; sleep 1; done; fl 50000 >> "$W/flight" ) &
HOT=$!
for _ in $(seq 1 200); do [ -e "$ABX_DIR/KILLED" ] && break; sleep 0.1; done
check "killed for temperature" 'grep -q "Tctl above 85" "$ABX_DIR/KILLED"'
for _ in $(seq 1 30); do [ -e "$STATE/live" ] && break; sleep 0.1; done
check "live back while the exam is still recording" '[ -e "$STATE/live" ]'
wait $HOT
/bin/rm -f "$STATE/exam_slow"; pkill -xf "sleep 30.7731"; waitdone
check "both models ran" '[ "$(grep -c model_eval "$STATE/exam.log")" = 2 ]'
check "incumbent ran after it" 'tail -1 "$STATE/exam.log" | grep -q "=qwen3"'
check "the kill is kept against Gemma" 'grep -q "Tctl above 85" "$ABX_DIR/KILLED-gemma4"'
check "live restored" restored
check "rule removed" '[ -s "$STATE/rule.log" ]'

scenario "Gemma thermally killed and its exam then crashes: the incumbent still runs"
touch "$STATE/exam_slow" "$STATE/exam_fails"
"$HERE/run.sh" > /dev/null 2>&1 &
for _ in $(seq 1 100); do [ -s "$STATE/exam.log" ] && break; sleep 0.1; done
( for _ in $(seq 1 14); do fl 86000 >> "$W/flight"; sleep 1; done; fl 50000 >> "$W/flight" ) &
HOT=$!
for _ in $(seq 1 200); do [ -e "$ABX_DIR/KILLED" ] && break; sleep 0.1; done
wait $HOT
/bin/rm -f "$STATE/exam_slow"; pkill -xf "sleep 30.7731"; waitdone
check "both models ran" '[ "$(grep -c model_eval "$STATE/exam.log")" = 2 ]'
check "live restored" restored

scenario "incumbent thermally killed: Gemma's results stand, live back, rule gone"
touch "$STATE/second_slow"
"$HERE/run.sh" > /dev/null 2>&1 &
for _ in $(seq 1 200); do [ "$(grep -c model_eval "$STATE/exam.log" 2>/dev/null)" = 2 ] && break; sleep 0.1; done
( for _ in $(seq 1 14); do fl 86000 >> "$W/flight"; sleep 1; done; fl 50000 >> "$W/flight" ) &
HOT=$!
for _ in $(seq 1 200); do [ -e "$ABX_DIR/KILLED" ] && break; sleep 0.1; done
check "killed for temperature" 'grep -q "Tctl above 85" "$ABX_DIR/KILLED"'
wait $HOT
/bin/rm -f "$STATE/second_slow"; pkill -xf "sleep 30.7731"; waitdone
check "both models were attempted" '[ "$(grep -c model_eval "$STATE/exam.log")" = 2 ]'
check "Gemma has its result file" '[ -s "$ABX_DIR/result-gemma4.json" ]'
check "live restored" restored
check "rule removed" '[ -s "$STATE/rule.log" ]'

scenario "Tctl over for under 10 s: not killed"
touch "$STATE/exam_slow"
"$HERE/run.sh" > /dev/null 2>&1 &
R=$!
for _ in $(seq 1 100); do [ -s "$STATE/exam.log" ] && break; sleep 0.1; done
for _ in $(seq 1 5); do fl 86000 >> "$W/flight"; sleep 1; done
fl 60000 >> "$W/flight"; sleep 1.5
for _ in $(seq 1 5); do fl 86000 >> "$W/flight"; sleep 1; done
check "no kill" '[ ! -e "$ABX_DIR/KILLED" ]'
kill -TERM $R; pkill -xf "sleep 30.7731"; waitdone

scenario "swap under 2 GB in /proc/meminfo, swapfree=0 in the flight log: not killed"
touch "$STATE/exam_slow"
"$HERE/run.sh" > /dev/null 2>&1 &
R=$!
for _ in $(seq 1 100); do [ -s "$STATE/exam.log" ] && break; sleep 0.1; done
meminfo 1900000
for _ in 1 2 3; do fl 50000 >> "$W/flight"; sleep 1; done
check "no kill" '[ ! -e "$ABX_DIR/KILLED" ]'
check "the guard logged the /proc/meminfo figure" 'tail -1 "$ABX_DIR/guard.log" | grep -q "swap_kb=1900000"'
check "the guard parsed tctl from a real-format line" 'tail -1 "$ABX_DIR/guard.log" | grep -q "tctl=50000"'
kill -TERM $R; pkill -xf "sleep 30.7731"; waitdone

scenario "swap past 2 GB in /proc/meminfo: candidate killed"
touch "$STATE/exam_slow"
"$HERE/run.sh" > /dev/null 2>&1 &
for _ in $(seq 1 100); do [ -s "$STATE/exam.log" ] && break; sleep 0.1; done
meminfo 2000000
for _ in $(seq 1 40); do [ -e "$ABX_DIR/KILLED" ] && break; sleep 0.1; done
check "killed for swap" 'grep -q "swap" "$ABX_DIR/KILLED"'
pkill -xf "sleep 30.7731"; waitdone
check "not a thermal kill, so the incumbent does not run" '[ "$(grep -c model_eval "$STATE/exam.log")" = 1 ]'
check "reason recorded" 'grep -q "non-thermal" "$ABX_DIR/status"'
check "live restored" restored

scenario "kill -9 the runner mid-exam: the dead-man restores live and removes the rule"
touch "$STATE/exam_slow"
"$HERE/run.sh" > /dev/null 2>&1 &
R=$!
export ABX_DEADMAN=$(( $(date +%s) + 3600 )) ABX_DM_POLL=1
"$HERE/deadman.sh" "$R" > /dev/null 2>&1 &
DM=$!
for _ in $(seq 1 100); do [ -s "$STATE/exam.log" ] && break; sleep 0.1; done
kill -9 $R
check "live is down after the kill" '[ ! -e "$STATE/live" ]'
wait $DM
check "live restored" '[ -e "$STATE/live" ]'
check "8082 cleared" '! kill -0 "$(cat "$STATE/cand_pid")" 2>/dev/null'
check "rule removed" '[ -s "$STATE/rule.log" ]'
check "guard told to stop" '[ ! -e "$ABX_DIR/guard.run" ]'
check "status says done" 'grep -q state=done "$ABX_DIR/status"'
pkill -xf "sleep 30.7731"

scenario "dead-man stands down when the runner finished cleanly"
export ABX_DEADMAN=$(( $(date +%s) + 3600 ))
"$HERE/run.sh" > /dev/null 2>&1 &
R=$!
"$HERE/deadman.sh" "$R" > /dev/null 2>&1 &
DM=$!
waitdone
for _ in $(seq 1 150); do kill -0 $DM 2>/dev/null || break; sleep 0.2; done
kill -0 $DM 2>/dev/null && { kill $DM; check "stood down" false; } || check "stood down" true

scenario "live will not start: runner leaves the rule, dead-man tries once more, then removes it"
touch "$STATE/start_fails" "$STATE/exam_fails"
export ABX_HEALTH_POLLS=1 ABX_DEADMAN=$(( $(date +%s) + 25 ))
"$HERE/run.sh" > /dev/null 2>&1 &
R=$!
"$HERE/deadman.sh" "$R" > /dev/null 2>&1 &
DM=$!
waitdone 300
check "runner left the rule for the dead-man" '[ ! -s "$STATE/rule.log" ] && grep -q "live=DOWN" "$ABX_DIR/status"'
wait $DM
check "dead-man tried to start it" '[ "$(grep -c start "$STATE/systemctl.log")" -ge 3 ]'
check "dead-man removed the rule anyway" '[ -s "$STATE/rule.log" ]'
check "status admits live is down" 'grep -q "live=DOWN" "$ABX_DIR/status"'
unset ABX_HEALTH_POLLS


scenario "live unhealthy at the deadline with the runner alive and stuck: dead-man stops it"
touch "$STATE/exam_slow"
"$HERE/run.sh" > /dev/null 2>&1 &
R=$!
for _ in $(seq 1 100); do [ -s "$STATE/exam.log" ] && break; sleep 0.1; done
export ABX_DEADMAN=$(( $(date +%s) + 2 ))
"$HERE/deadman.sh" "$R" > /dev/null 2>&1 &
DM=$!
wait $DM
check "live restored" '[ -e "$STATE/live" ]'
check "stop flag set" '[ -e "$ABX_DIR/STOP" ]'
check "rule removed" '[ -s "$STATE/rule.log" ]'
pkill -xf "sleep 30.7731"
echo; echo "passed=$PASS failed=$FAIL"
[ "$FAIL" = 0 ]
